import os
import sys
import json
import time
import base64
import struct
import string
import shutil
import atexit
import signal
import zipfile
import mimetypes
import subprocess
import xml.etree.ElementTree as ET
import datetime
from datetime import timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from flask import Flask, request, jsonify, render_template_string, send_file
from dotenv import load_dotenv, set_key

# ==============================================================================
# CONFIGURACIÓN INICIAL Y SEGURIDAD (.env)
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "sync_config.json"
RECORDS_DIR = BASE_DIR / "sync_records"
TEMP_CACHE_DIR = BASE_DIR / "temp_cache"
RECORDS_DIR.mkdir(exist_ok=True)
TEMP_CACHE_DIR.mkdir(exist_ok=True)

def cleanup_temp_cache():
    """Elimina automáticamente la carpeta temporal de descargas al cerrar el programa."""
    try:
        if TEMP_CACHE_DIR.exists():
            shutil.rmtree(TEMP_CACHE_DIR, ignore_errors=True)
            print(f"[CLEANUP] Caché temporal eliminada: {TEMP_CACHE_DIR}")
    except Exception:
        pass

atexit.register(cleanup_temp_cache)

def _signal_handler(signum, frame):
    cleanup_temp_cache()
    sys.exit(0)

signal.signal(signal.SIGINT, _signal_handler)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _signal_handler)

# Crear .env por defecto si no existe
if not ENV_FILE.exists():
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write('FILEJUMP_API_KEY=""\n')
        f.write('FILEJUMP_BASE_URL="https://filejump.com/api"\n')
        f.write('FILEJUMP_UPLOAD_URL="https://uploads.filejump.com/api/upload"\n')

load_dotenv(ENV_FILE)

app = Flask(__name__)

# Estado global en memoria para logs en tiempo real hacia la UI
activity_logs = []

def log_event(level, message):
    """Registra eventos con marca de tiempo para mostrarlos en la interfaz web."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "level": level, "message": message}
    activity_logs.append(entry)
    if len(activity_logs) > 200:
        activity_logs.pop(0)
    print(f"[{timestamp}] [{level.upper()}] {message}")


# ==============================================================================
# GESTIÓN DE CONFIGURACIÓN Y REGISTROS JSON
# ==============================================================================
def load_config():
    """Carga la configuración de tareas y parámetros globales desde sync_config.json."""
    default_config = {
        "max_workers": 4,
        "min_impact_percentage": 40,
        "use_high_speed_upload": True,
        "tasks": []
    }
    if not CONFIG_FILE.exists():
        save_config(default_config)
        return default_config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in default_config.items():
                data.setdefault(k, v)
            return data
    except Exception as e:
        log_event("error", f"Error leyendo {CONFIG_FILE.name}: {e}")
        return default_config

def save_config(config_data):
    """Guarda la configuración en sync_config.json."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4, ensure_ascii=False)

def get_task_record_path(task_name):
    """Devuelve la ruta del JSON de historial para una tarea específica."""
    safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in task_name)
    return RECORDS_DIR / f"{safe_name}_record.json"

def load_task_record(task_name):
    """Carga el registro JSON local de archivos/carpetas sincronizados."""
    path = get_task_record_path(task_name)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("files", {})
                data.setdefault("folders", {})
                return data
        except Exception:
            pass
    return {"files": {}, "folders": {}, "last_sync": None}

def save_task_record(task_name, record_data):
    """Guarda el registro JSON local de archivos/carpetas sincronizados."""
    record_data["last_sync"] = datetime.datetime.now(timezone.utc).isoformat()
    path = get_task_record_path(task_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record_data, f, indent=4, ensure_ascii=False)


# ==============================================================================
# CLIENTE API REST DE FILEJUMP (NUEVA VERSIÓN)
# ==============================================================================
class FileJumpClient:
    def __init__(self):
        load_dotenv(ENV_FILE, override=True)
        self.api_key = os.getenv("FILEJUMP_API_KEY", "").strip()
        self.base_url = os.getenv("FILEJUMP_BASE_URL", "https://filejump.com/api").rstrip("/")
        self.upload_url = os.getenv("FILEJUMP_UPLOAD_URL", "https://uploads.filejump.com/api/upload").rstrip("/")

    def _headers(self, is_json=True):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json"
        }
        if is_json:
            headers["Content-Type"] = "application/json"
        return headers

    def check_connection(self):
        """Verifica si el token Bearer es válido consultando el endpoint de archivos o carpetas."""
        if not self.api_key:
            return False, "No se ha configurado FILEJUMP_API_KEY en el archivo .env"
        try:
            resp = requests.get(f"{self.base_url}/files", headers=self._headers(), params={"per_page": 1}, timeout=10)
            if resp.status_code == 200:
                return True, "Conexión exitosa con FileJump REST API"
            elif resp.status_code == 401:
                return False, "Token inválido o expirado (401 Unauthorized)"
            else:
                return False, f"Respuesta inesperada de la API: HTTP {resp.status_code}"
        except Exception as e:
            return False, f"Error de red al conectar con FileJump: {str(e)}"

    def list_folders(self, parent_id=None):
        """Lista carpetas remotas usando GET /folders."""
        params = {}
        if parent_id is not None:
            params["parent_id"] = parent_id
            params["folder_id"] = parent_id
            params["parentId"] = parent_id
        try:
            resp = requests.get(f"{self.base_url}/folders", headers=self._headers(), params=params, timeout=15)
            if resp.status_code == 200:
                payload = resp.json()
                items = payload.get("data", payload if isinstance(payload, list) else [])
                # Si parent_id es None, filtramos las carpetas que están en la raíz (sin padre)
                if parent_id is None:
                    root_items = [
                        f for f in items
                        if f.get("parent_id") in (None, 0, "0", "")
                        and f.get("folder_id") in (None, 0, "0", "")
                        and f.get("parentId") in (None, 0, "0", "")
                    ]
                    return root_items if root_items else items
                return items
            return []
        except Exception as e:
            log_event("error", f"Error listando carpetas remotas: {e}")
            return []

    def list_files(self, folder_id=None):
        """Lista archivos remotos usando GET /files."""
        params = {}
        if folder_id is not None:
            params["folder_id"] = folder_id
            params["parent_id"] = folder_id
            params["parentId"] = folder_id
        all_files = []
        page = 1
        while True:
            params["page"] = page
            params["per_page"] = 100
            try:
                resp = requests.get(f"{self.base_url}/files", headers=self._headers(), params=params, timeout=15)
                if resp.status_code != 200:
                    log_event("warning", f"GET /files devolvió HTTP {resp.status_code}")
                    break
                payload = resp.json()
                items = payload.get("data", payload if isinstance(payload, list) else [])
                if not items:
                    break
                all_files.extend(items)
                # Verificar paginación estándar de Laravel/REST
                meta = payload.get("meta", payload) if isinstance(payload, dict) else {}
                last_page = meta.get("last_page", 1)
                if page >= last_page:
                    break
                page += 1
            except Exception as e:
                log_event("error", f"Error listando archivos remotos: {e}")
                break
        return all_files

    def create_folder(self, name, parent_id=None):
        """Crea una carpeta remota usando POST /folders."""
        payload = {"name": name}
        if parent_id is not None:
            payload["parent_id"] = parent_id
            payload["folder_id"] = parent_id
        try:
            resp = requests.post(f"{self.base_url}/folders", headers=self._headers(), json=payload, timeout=15)
            if resp.status_code in (200, 201):
                data = resp.json()
                folder_obj = data.get("data", data.get("folder", data))
                return folder_obj.get("id")
            else:
                log_event("error", f"No se pudo crear carpeta '{name}': HTTP {resp.status_code} - {resp.text}")
                return None
        except Exception as e:
            log_event("error", f"Excepción creando carpeta '{name}': {e}")
            return None

    def upload_file(self, local_path, folder_id=None, use_high_speed=True):
        """
        Sube un archivo utilizando el endpoint de alta velocidad
        (https://uploads.filejump.com/api/upload) o el estándar (/files).
        """
        target_url = self.upload_url if use_high_speed else f"{self.base_url}/files"
        file_name = os.path.basename(local_path)
        data = {}
        if folder_id is not None:
            data["folder_id"] = str(folder_id)

        try:
            with open(local_path, "rb") as f:
                files = {"file": (file_name, f, "application/octet-stream")}
                resp = requests.post(
                    target_url,
                    headers=self._headers(is_json=False),
                    files=files,
                    data=data,
                    timeout=300
                )
            if resp.status_code in (200, 201):
                res_json = resp.json()
                file_obj = res_json.get("data", res_json.get("fileEntry", res_json))
                return file_obj
            else:
                log_event("error", f"Error subiendo '{file_name}': HTTP {resp.status_code} - {resp.text}")
                return None
        except Exception as e:
            log_event("error", f"Excepción subiendo '{file_name}': {e}")
            return None

    def delete_file(self, file_id):
        """Elimina un archivo remoto usando DELETE /files/{id}."""
        try:
            resp = requests.delete(f"{self.base_url}/files/{file_id}", headers=self._headers(), timeout=15)
            return resp.status_code in (200, 204)
        except Exception as e:
            log_event("error", f"Error eliminando archivo remoto {file_id}: {e}")
            return False

    def download_file_to_cache(self, file_id, filename):
        """Descarga un archivo remoto a TEMP_CACHE_DIR y devuelve la ruta local en caché."""
        TEMP_CACHE_DIR.mkdir(exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in (".", "-", "_", " ") else "_" for c in filename).strip() or f"file_{file_id}"
        cached_path = TEMP_CACHE_DIR / f"{file_id}_{safe_name}"
        if cached_path.exists() and cached_path.stat().st_size > 0:
            return cached_path

        headers = {"Authorization": f"Bearer {self.api_key}"}
        candidate_urls = [
            f"{self.base_url}/files/{file_id}/download",
            f"{self.base_url}/drive/file-entries/{file_id}/download"
        ]
        for url in candidate_urls:
            try:
                with requests.get(url, headers=headers, stream=True, timeout=120) as resp:
                    if resp.status_code == 200:
                        with open(cached_path, "wb") as out_f:
                            for chunk in resp.iter_content(chunk_size=65536):
                                if chunk:
                                    out_f.write(chunk)
                        log_event("info", f"Archivo remoto #{file_id} descargado en caché temporal.")
                        return cached_path
            except Exception as e:
                log_event("warning", f"Intento de descarga fallido ({url}): {e}")
        return None


def extract_office_preview_html(file_path):
    """Extrae una vista previa HTML limpia de archivos de ofimática (.docx, .xlsx, .pptx, .odt, .ods)."""
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".docx":
            with zipfile.ZipFile(file_path, "r") as z:
                xml_content = z.read("word/document.xml")
            root = ET.fromstring(xml_content)
            paragraphs = []
            for p in root.iter():
                if p.tag.endswith("}p"):
                    texts = [t.text for t in p.iter() if t.tag.endswith("}t") and t.text]
                    if texts:
                        line = "".join(texts).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        paragraphs.append(f"<p class='mb-2 leading-relaxed'>{line}</p>")
            return "".join(paragraphs[:300]) or "<p class='text-slate-400'>Documento Word vacío.</p>"

        elif ext == ".xlsx":
            with zipfile.ZipFile(file_path, "r") as z:
                shared_strings = []
                if "xl/sharedStrings.xml" in z.namelist():
                    ss_root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                    for si in ss_root.iter():
                        if si.tag.endswith("}si"):
                            texts = [t.text for t in si.iter() if t.tag.endswith("}t") and t.text]
                            shared_strings.append("".join(texts))
                sheet_files = sorted([n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")])
                if not sheet_files:
                    return "<p class='text-slate-400'>No se encontraron hojas en el libro Excel.</p>"
                sheet_root = ET.fromstring(z.read(sheet_files[0]))
                rows_html = []
                for row in list(sheet_root.iter()):
                    if row.tag.endswith("}row"):
                        cells_html = []
                        for c in row:
                            if c.tag.endswith("}c"):
                                cell_type = c.attrib.get("t", "")
                                val_text = ""
                                for child in c:
                                    if child.tag.endswith("}v") and child.text:
                                        if cell_type == "s" and child.text.isdigit():
                                            idx = int(child.text)
                                            val_text = shared_strings[idx] if idx < len(shared_strings) else child.text
                                        else:
                                            val_text = child.text
                                    elif child.tag.endswith("}is"):
                                        val_text = "".join([t.text for t in child.iter() if t.tag.endswith("}t") and t.text])
                                safe_val = str(val_text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                                cells_html.append(f"<td class='border border-slate-700 px-2.5 py-1.5 text-xs'>{safe_val}</td>")
                        if cells_html:
                            rows_html.append(f"<tr class='hover:bg-slate-800/50'>{''.join(cells_html)}</tr>")
                        if len(rows_html) >= 120:
                            break
                return f"<div class='overflow-auto'><table class='border-collapse w-full text-left'>{''.join(rows_html)}</table></div>"

        elif ext == ".pptx":
            with zipfile.ZipFile(file_path, "r") as z:
                slides = sorted([n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")])
                slides_html = []
                for idx, s_name in enumerate(slides[:40], 1):
                    s_root = ET.fromstring(z.read(s_name))
                    texts = [t.text for t in s_root.iter() if t.tag.endswith("}t") and t.text]
                    body = "<br>".join(t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for t in texts)
                    slides_html.append(f"<div class='bg-slate-900 border border-slate-700 rounded-xl p-4 mb-3'><div class='text-[11px] font-semibold text-indigo-400 mb-1'>Diapositiva {idx}</div><div class='text-xs text-slate-200'>{body or '(Sin texto)'}</div></div>")
                return "".join(slides_html) or "<p class='text-slate-400'>Presentación vacía.</p>"

        elif ext in (".odt", ".ods"):
            with zipfile.ZipFile(file_path, "r") as z:
                xml_content = z.read("content.xml")
            root = ET.fromstring(xml_content)
            lines = []
            for p in root.iter():
                if p.tag.endswith("}p"):
                    txt = "".join(p.itertext()).strip()
                    if txt:
                        safe = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        lines.append(f"<p class='mb-2 text-xs'>{safe}</p>")
                if len(lines) >= 250:
                    break
            return "".join(lines) or "<p class='text-slate-400'>Archivo OpenDocument sin texto visible.</p>"

    except Exception as e:
        return f"<p class='text-amber-400 text-xs'>No se pudo renderizar la vista previa de ofimática ({ext}): {e}</p>"
    return f"<p class='text-slate-400 text-xs'>Vista previa no disponible para formato {ext}.</p>"


# ==============================================================================
# MOTOR DE ESCANEO Y COMPARACIÓN (LOCAL VS REMOTO)
# ==============================================================================
def parse_iso_timestamp(iso_str):
    """Convierte una fecha ISO 8601 de la API a timestamp numérico UTC."""
    if not iso_str:
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0

def scan_local_directory(base_path):
    """Escanea recursivamente una carpeta local y extrae metadatos (tamaño y mtime UTC)."""
    base = Path(base_path)
    items = []
    if not base.exists() or not base.is_dir():
        return items

    for root, dirs, files in os.walk(base):
        rel_root = Path(root).relative_to(base).as_posix()
        if rel_root == ".":
            rel_root = ""

        for d in sorted(dirs):
            rel_path = f"{rel_root}/{d}".lstrip("/")
            full_path = Path(root) / d
            stat = full_path.stat()
            items.append({
                "type": "folder",
                "name": d,
                "rel_path": rel_path,
                "full_path": str(full_path),
                "size": 0,
                "mtime": stat.st_mtime,
                "mtime_str": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })

        for f in sorted(files):
            rel_path = f"{rel_root}/{f}".lstrip("/")
            full_path = Path(root) / f
            stat = full_path.stat()
            items.append({
                "type": "file",
                "name": f,
                "rel_path": rel_path,
                "full_path": str(full_path),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_str": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })
    return items

def list_local_immediate_children(base_path, rel_prefix=""):
    """Lista únicamente los hijos inmediatos (1 nivel) de una carpeta local para carga perezosa."""
    base = Path(base_path)
    target = (base / rel_prefix).resolve() if rel_prefix else base.resolve()
    # Seguridad: evitar salir de base_path
    if not str(target).startswith(str(base.resolve())) or not target.exists() or not target.is_dir():
        return []

    children = []
    try:
        entries = sorted(target.iterdir(), key=lambda p: (0 if p.is_dir() else 1, p.name.lower()))
        for entry in entries:
            stat = entry.stat()
            rel_path = entry.relative_to(base.resolve()).as_posix()
            is_dir = entry.is_dir()
            has_subfolders = False
            if is_dir:
                try:
                    has_subfolders = any(sub.is_dir() for sub in entry.iterdir())
                except Exception:
                    has_subfolders = False
            children.append({
                "type": "folder" if is_dir else "file",
                "name": entry.name,
                "rel_path": rel_path,
                "full_path": str(entry),
                "has_children": has_subfolders,
                "size": 0 if is_dir else stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_str": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })
    except Exception as e:
        log_event("error", f"Error leyendo carpeta local '{target}': {e}")
    return children

def get_local_drives():
    """Detecta las unidades de almacenamiento locales disponibles (Windows/Linux/macOS)."""
    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive_path = f"{letter}:\\"
            if os.path.exists(drive_path):
                drives.append({"id": drive_path, "name": f"Disco Local ({letter}:)"})
    else:
        drives.append({"id": "/", "name": "Sistema de Archivos (/)"})
        for mount_base in ("/mnt", "/media", "/Volumes"):
            if os.path.isdir(mount_base):
                try:
                    for entry in sorted(os.listdir(mount_base)):
                        full_m = os.path.join(mount_base, entry)
                        if os.path.isdir(full_m):
                            drives.append({"id": full_m, "name": f"{entry} ({full_m})"})
                except Exception:
                    pass
    return drives

def list_local_path_immediate_children(root_drive_or_path, rel_prefix=""):
    """Lista hijos inmediatos (1 nivel) a partir de cualquier unidad o ruta local."""
    base = Path(root_drive_or_path).resolve()
    target = (base / rel_prefix).resolve() if rel_prefix else base
    if not str(target).startswith(str(base)) or not target.exists() or not target.is_dir():
        return []

    children = []
    try:
        entries = sorted(target.iterdir(), key=lambda p: (0 if p.is_dir() else 1, p.name.lower()))
        for entry in entries:
            try:
                stat = entry.stat()
            except (PermissionError, OSError):
                continue
            rel_path = entry.relative_to(base).as_posix()
            is_dir = entry.is_dir()
            has_subfolders = False
            if is_dir:
                try:
                    has_subfolders = any(sub.is_dir() for sub in entry.iterdir())
                except Exception:
                    has_subfolders = False
            children.append({
                "type": "folder" if is_dir else "file",
                "name": entry.name,
                "rel_path": rel_path,
                "full_path": str(entry),
                "has_children": has_subfolders,
                "size": 0 if is_dir else stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_str": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })
    except (PermissionError, OSError) as e:
        log_event("warning", f"Acceso denegado o error leyendo '{target}': {e}")
    return children

def list_remote_immediate_children(client, parent_id="ROOT", current_prefix=""):
    """Lista únicamente los hijos inmediatos (1 nivel) de una carpeta remota en FileJump."""
    is_account_root = (parent_id in (None, "ROOT", 0, "0", ""))
    query_parent = None if is_account_root else parent_id

    folders = client.list_folders(parent_id=query_parent)
    files = client.list_files(folder_id=query_parent)
    items = []

    for fld in sorted(folders, key=lambda x: (x.get("name") or x.get("file_name") or "").lower()):
        fname = fld.get("name") or fld.get("file_name") or ""
        fid = fld.get("id")
        parent_ref = (
            fld.get("parent_id")
            if fld.get("parent_id") is not None
            else (fld.get("folder_id") if fld.get("folder_id") is not None else fld.get("parentId"))
        )
        if is_account_root:
            if parent_ref not in (None, 0, "0", ""):
                continue
        else:
            if parent_ref is not None and str(parent_ref) != str(parent_id):
                continue

        rel_path = f"{current_prefix}/{fname}".lstrip("/")
        updated_str = fld.get("updated_at") or fld.get("created_at") or ""
        mtime = parse_iso_timestamp(updated_str)
        items.append({
            "type": "folder",
            "id": fid,
            "name": fname,
            "rel_path": rel_path,
            "parent_id": "ROOT" if is_account_root else parent_id,
            "has_children": True,
            "size": 0,
            "mtime": mtime,
            "mtime_str": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "-"
        })

    for fl in sorted(files, key=lambda x: (x.get("name") or x.get("file_name") or "").lower()):
        fname = fl.get("name") or fl.get("file_name") or ""
        fid = fl.get("id")
        folder_ref = (
            fl.get("folder_id")
            if fl.get("folder_id") is not None
            else (fl.get("parent_id") if fl.get("parent_id") is not None else fl.get("parentId"))
        )
        if is_account_root:
            if folder_ref not in (None, 0, "0", ""):
                continue
        else:
            if folder_ref is not None and str(folder_ref) != str(parent_id):
                continue

        rel_path = f"{current_prefix}/{fname}".lstrip("/")
        updated_str = fl.get("updated_at") or fl.get("created_at") or ""
        mtime = parse_iso_timestamp(updated_str)
        size = fl.get("size") or fl.get("file_size") or 0
        items.append({
            "type": "file",
            "id": fid,
            "name": fname,
            "rel_path": rel_path,
            "parent_id": "ROOT" if is_account_root else parent_id,
            "has_children": False,
            "size": int(size),
            "mtime": mtime,
            "mtime_str": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "-"
        })

    return items

def scan_remote_tree(client, root_folder_id, current_prefix="", depth=0, max_depth=8):
    """Construye el listado plano ordenado jerárquicamente de carpetas y archivos remotos."""
    if depth > max_depth:
        return []

    is_account_root = (root_folder_id in (None, "ROOT", 0, "0"))
    query_parent = None if is_account_root else root_folder_id

    remote_items = []
    folders = client.list_folders(parent_id=query_parent)
    files = client.list_files(folder_id=query_parent)

    # Ordenar carpetas alfabéticamente para mantener consistencia jerárquica
    sorted_folders = sorted(folders, key=lambda x: (x.get("name") or x.get("file_name") or "").lower())
    for fld in sorted_folders:
        fname = fld.get("name") or fld.get("file_name") or ""
        fid = fld.get("id")
        parent_ref = (
            fld.get("parent_id")
            if fld.get("parent_id") is not None
            else (fld.get("folder_id") if fld.get("folder_id") is not None else fld.get("parentId"))
        )

        if is_account_root:
            if parent_ref not in (None, 0, "0", ""):
                continue
        else:
            if parent_ref is not None and str(parent_ref) != str(root_folder_id):
                continue

        rel_path = f"{current_prefix}/{fname}".lstrip("/")
        updated_str = fld.get("updated_at") or fld.get("created_at") or ""
        mtime = parse_iso_timestamp(updated_str)
        remote_items.append({
            "type": "folder",
            "id": fid,
            "name": fname,
            "rel_path": rel_path,
            "depth": depth,
            "parent_id": None if is_account_root else root_folder_id,
            "size": 0,
            "mtime": mtime,
            "mtime_str": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "-"
        })
        # Recursión jerárquica dentro de la subcarpeta
        remote_items.extend(scan_remote_tree(client, fid, rel_path, depth + 1, max_depth))

    # Ordenar archivos alfabéticamente dentro del nivel actual
    sorted_files = sorted(files, key=lambda x: (x.get("name") or x.get("file_name") or "").lower())
    for fl in sorted_files:
        fname = fl.get("name") or fl.get("file_name") or ""
        fid = fl.get("id")
        folder_ref = (
            fl.get("folder_id")
            if fl.get("folder_id") is not None
            else (fl.get("parent_id") if fl.get("parent_id") is not None else fl.get("parentId"))
        )

        if is_account_root:
            if folder_ref not in (None, 0, "0", ""):
                continue
        else:
            if folder_ref is not None and str(folder_ref) != str(root_folder_id):
                continue

        rel_path = f"{current_prefix}/{fname}".lstrip("/")
        updated_str = fl.get("updated_at") or fl.get("created_at") or ""
        mtime = parse_iso_timestamp(updated_str)
        size = fl.get("size") or fl.get("file_size") or 0
        remote_items.append({
            "type": "file",
            "id": fid,
            "name": fname,
            "rel_path": rel_path,
            "depth": depth,
            "parent_id": None if is_account_root else root_folder_id,
            "size": int(size),
            "mtime": mtime,
            "mtime_str": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "-"
        })

    return remote_items

def compare_local_and_remote(local_items, remote_items, record):
    """
    Compara archivos locales y remotos basándose en ruta relativa, tamaño y fecha de modificación.
    Asigna un estado de sincronización a cada elemento.
    """
    remote_map = {item["rel_path"]: item for item in remote_items}
    local_map = {item["rel_path"]: item for item in local_items}
    recorded_files = record.get("files", {})

    compared_local = []
    for item in local_items:
        rel = item["rel_path"]
        rem = remote_map.get(rel)
        rec = recorded_files.get(rel)

        if item["type"] == "folder":
            status = "synced" if rem else "new"
        else:
            if not rem:
                status = "new"
            else:
                size_diff = item["size"] != rem["size"]
                # Comparamos contra el último mtime local registrado o contra el remoto con tolerancia de 2s
                ref_mtime = rec["local_mtime"] if rec else rem["mtime"]
                time_diff = abs(item["mtime"] - ref_mtime) > 2.0
                status = "modified" if (size_diff or time_diff) else "synced"

        compared_local.append({
            **item,
            "sync_status": status,
            "remote_id": rem["id"] if rem else None
        })

    compared_remote = []
    for item in remote_items:
        rel = item["rel_path"]
        loc = local_map.get(rel)
        if not loc:
            status = "remote_only"
        elif item["type"] == "folder":
            status = "synced"
        else:
            rec = recorded_files.get(rel)
            ref_mtime = rec["local_mtime"] if rec else item["mtime"]
            size_diff = loc["size"] != item["size"]
            time_diff = abs(loc["mtime"] - ref_mtime) > 2.0
            status = "modified" if (size_diff or time_diff) else "synced"

        compared_remote.append({
            **item,
            "sync_status": status
        })

    return compared_local, compared_remote


# ==============================================================================
# RUTAS DE LA API DEL SERVIDOR WEB (FLASK)
# ==============================================================================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/status", methods=["GET"])
def api_status():
    client = FileJumpClient()
    ok, msg = client.check_connection()
    masked_key = ""
    if client.api_key:
        masked_key = client.api_key[:6] + "•" * 12 + client.api_key[-4:] if len(client.api_key) > 10 else "••••••"
    return jsonify({
        "connected": ok,
        "message": msg,
        "base_url": client.base_url,
        "upload_url": client.upload_url,
        "has_key": bool(client.api_key),
        "masked_key": masked_key,
        "logs": activity_logs[-50:]
    })

@app.route("/api/remote/directories", methods=["GET"])
def api_remote_directories():
    """Obtiene todas las carpetas disponibles en la raíz de FileJump al conectarse."""
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"connected": False, "directories": []})

    folders = client.list_folders(parent_id=None)
    directories = [
        {
            "id": f.get("id"),
            "name": f.get("name", "Sin nombre"),
            "updated_at": f.get("updated_at") or f.get("created_at") or ""
        }
        for f in folders if f.get("id") is not None
    ]
    log_event("info", f"Directorios remotos actualizados: {len(directories)} carpeta(s) en raíz de FileJump.")
    return jsonify({"connected": True, "directories": directories})

@app.route("/api/local/drives", methods=["GET"])
def api_local_drives():
    """Devuelve todas las unidades de almacenamiento locales disponibles."""
    return jsonify({"drives": get_local_drives()})

@app.route("/api/local/browse", methods=["GET"])
def api_local_browse():
    """Permite explorar cualquier unidad o directorio local (1 nivel a la vez)."""
    root_path = request.args.get("root_path", "").strip()
    prefix = request.args.get("prefix", "").strip().strip("/")
    if not root_path:
        return jsonify({"error": "Falta root_path"}), 400
    items = list_local_path_immediate_children(root_path, prefix)
    for item in items:
        item["sync_status"] = "local_only"
    return jsonify({"root_path": root_path, "items": items})

@app.route("/api/remote/browse", methods=["GET"])
def api_remote_browse():
    """Permite explorar cualquier carpeta remota directamente (1 nivel a la vez)."""
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY en el archivo .env"}), 400

    remote_id = request.args.get("remote_id", "ROOT").strip() or "ROOT"
    prefix = request.args.get("prefix", "").strip().strip("/")
    target_id = "ROOT" if remote_id == "ROOT" else (int(remote_id) if remote_id.isdigit() else remote_id)

    remote_items = list_remote_immediate_children(client, target_id, current_prefix=prefix)
    for item in remote_items:
        item["sync_status"] = "remote_only"

    return jsonify({
        "remote_root_id": remote_id,
        "remote_items": remote_items
    })

@app.route("/api/local/children", methods=["GET"])
def api_local_children():
    """Carga bajo demanda los hijos inmediatos de una carpeta local (por tarea o por unidad)."""
    task_name = request.args.get("task", "").strip()
    root_path = request.args.get("root_path", "").strip()
    prefix = request.args.get("prefix", "").strip().strip("/")

    if root_path:
        items = list_local_path_immediate_children(root_path, prefix)
        for item in items:
            item["sync_status"] = "local_only"
        return jsonify({"items": items})

    config = load_config()
    task = next((t for t in config["tasks"] if t["name"] == task_name), None)
    if not task:
        return jsonify({"error": "Tarea no encontrada"}), 404

    record = load_task_record(task_name)
    recorded_files = record.get("files", {})
    recorded_folders = record.get("folders", {})
    items = list_local_immediate_children(task["path"], prefix)

    for item in items:
        rel = item["rel_path"]
        if item["type"] == "folder":
            item["sync_status"] = "synced" if rel in recorded_folders else "new"
        else:
            rec = recorded_files.get(rel)
            if not rec:
                item["sync_status"] = "new"
            else:
                changed = (item["size"] != rec.get("size")) or (abs(item["mtime"] - rec.get("local_mtime", 0)) > 2.0)
                item["sync_status"] = "modified" if changed else "synced"

    return jsonify({"items": items})

@app.route("/api/remote/children", methods=["GET"])
def api_remote_children():
    """Carga bajo demanda los hijos inmediatos de una carpeta remota y los compara si hay tarea."""
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY en .env"}), 400

    parent_id = request.args.get("parent_id", "ROOT").strip() or "ROOT"
    prefix = request.args.get("prefix", "").strip().strip("/")
    task_name = request.args.get("task", "").strip()

    target_id = "ROOT" if parent_id == "ROOT" else (int(parent_id) if parent_id.isdigit() else parent_id)
    items = list_remote_immediate_children(client, target_id, current_prefix=prefix)

    config = load_config()
    task = next((t for t in config["tasks"] if t["name"] == task_name), None) if task_name else None
    record = load_task_record(task_name) if task else {"files": {}}
    recorded_files = record.get("files", {})

    for item in items:
        rel = item["rel_path"]
        if not task:
            item["sync_status"] = "remote_only"
            continue
        local_candidate = Path(task["path"]) / rel
        if not local_candidate.exists():
            item["sync_status"] = "remote_only"
        elif item["type"] == "folder":
            item["sync_status"] = "synced"
        else:
            st = local_candidate.stat()
            rec = recorded_files.get(rel)
            ref_mtime = rec["local_mtime"] if rec else item["mtime"]
            changed = (st.st_size != item["size"]) or (abs(st.st_mtime - ref_mtime) > 2.0)
            item["sync_status"] = "modified" if changed else "synced"

    return jsonify({"items": items})

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """Lee o actualiza la configuración (.env y sync_config.json)."""
    if request.method == "GET":
        load_dotenv(ENV_FILE, override=True)
        return jsonify({
            "env": {
                "FILEJUMP_BASE_URL": os.getenv("FILEJUMP_BASE_URL", "https://filejump.com/api"),
                "FILEJUMP_UPLOAD_URL": os.getenv("FILEJUMP_UPLOAD_URL", "https://uploads.filejump.com/api/upload"),
                "HAS_API_KEY": bool(os.getenv("FILEJUMP_API_KEY", "").strip())
            },
            "config": load_config()
        })

    data = request.json or {}
    env_updates = data.get("env", {})
    if env_updates.get("FILEJUMP_API_KEY"):
        set_key(str(ENV_FILE), "FILEJUMP_API_KEY", env_updates["FILEJUMP_API_KEY"].strip())
        log_event("info", "Clave API actualizada de forma segura en .env")
    if env_updates.get("FILEJUMP_BASE_URL"):
        set_key(str(ENV_FILE), "FILEJUMP_BASE_URL", env_updates["FILEJUMP_BASE_URL"].strip())
    if env_updates.get("FILEJUMP_UPLOAD_URL"):
        set_key(str(ENV_FILE), "FILEJUMP_UPLOAD_URL", env_updates["FILEJUMP_UPLOAD_URL"].strip())

    if "config" in data:
        save_config(data["config"])
        log_event("info", "Parámetros de sincronización guardados en sync_config.json")

    return jsonify({"success": True})

@app.route("/api/inspect/<task_name>", methods=["GET"])
def api_inspect_task(task_name):
    """Carga únicamente el nivel raíz (1 nivel) de local y remoto para inicialización rápida."""
    config = load_config()
    task = next((t for t in config["tasks"] if t["name"] == task_name), None)
    if not task:
        return jsonify({"error": "Tarea no encontrada"}), 404

    local_path = task["path"]
    local_exists = os.path.exists(local_path)
    record = load_task_record(task_name)
    recorded_files = record.get("files", {})
    recorded_folders = record.get("folders", {})

    local_items = list_local_immediate_children(local_path, "") if local_exists else []
    for item in local_items:
        rel = item["rel_path"]
        if item["type"] == "folder":
            item["sync_status"] = "synced" if rel in recorded_folders else "new"
        else:
            rec = recorded_files.get(rel)
            if not rec:
                item["sync_status"] = "new"
            else:
                changed = (item["size"] != rec.get("size")) or (abs(item["mtime"] - rec.get("local_mtime", 0)) > 2.0)
                item["sync_status"] = "modified" if changed else "synced"

    client = FileJumpClient()
    remote_items = []
    override_remote_id = request.args.get("remote_id", "").strip()
    remote_root_id = override_remote_id if override_remote_id else record.get("task_remote_id")

    if client.api_key:
        if override_remote_id:
            if override_remote_id == "ROOT":
                remote_root_id = "ROOT"
            else:
                record["task_remote_id"] = int(override_remote_id) if override_remote_id.isdigit() else override_remote_id
                save_task_record(task_name, record)
                remote_root_id = record["task_remote_id"]

        if not remote_root_id:
            top_folders = client.list_folders(parent_id=None)
            for f in top_folders:
                if f.get("name", "").lower() == task_name.lower():
                    remote_root_id = f.get("id")
                    record["task_remote_id"] = remote_root_id
                    save_task_record(task_name, record)
                    break

        if remote_root_id:
            remote_items = list_remote_immediate_children(client, remote_root_id, current_prefix="")
            for item in remote_items:
                rel = item["rel_path"]
                local_candidate = Path(local_path) / rel
                if not local_candidate.exists():
                    item["sync_status"] = "remote_only"
                elif item["type"] == "folder":
                    item["sync_status"] = "synced"
                else:
                    st = local_candidate.stat()
                    rec = recorded_files.get(rel)
                    ref_mtime = rec["local_mtime"] if rec else item["mtime"]
                    changed = (st.st_size != item["size"]) or (abs(st.st_mtime - ref_mtime) > 2.0)
                    item["sync_status"] = "modified" if changed else "synced"

    return jsonify({
        "task": task,
        "local_exists": local_exists,
        "remote_root_id": remote_root_id,
        "last_sync": record.get("last_sync"),
        "local_items": local_items,
        "remote_items": remote_items
    })

@app.route("/api/sync/<task_name>", methods=["POST"])
def api_run_sync(task_name):
    """Ejecuta la sincronización de archivos nuevos y modificados hacia FileJump."""
    config = load_config()
    task = next((t for t in config["tasks"] if t["name"] == task_name), None)
    if not task:
        return jsonify({"error": "Tarea no encontrada"}), 404

    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY en el archivo .env"}), 400

    local_root = Path(task["path"])
    if not local_root.exists():
        return jsonify({"error": f"La ruta local no existe: {local_root}"}), 400

    log_event("info", f"Iniciando sincronización para tarea '{task_name}'...")
    record = load_task_record(task_name)

    # 1. Asegurar carpeta raíz remota de la tarea
    task_remote_id = record.get("task_remote_id")
    if not task_remote_id:
        for f in client.list_folders(parent_id=None):
            if f.get("name") == task_name:
                task_remote_id = f.get("id")
                break
        if not task_remote_id:
            task_remote_id = client.create_folder(task_name, parent_id=None)
        if not task_remote_id:
            return jsonify({"error": "No se pudo crear la carpeta raíz en FileJump"}), 500
        record["task_remote_id"] = task_remote_id

    # 2. Escanear estado local y remoto
    local_items = scan_local_directory(local_root)
    remote_items = scan_remote_tree(client, task_remote_id)
    comp_local, _ = compare_local_and_remote(local_items, remote_items, record)

    # Mapa de carpetas remotas por ruta relativa
    folder_id_map = {"": task_remote_id}
    for r_item in remote_items:
        if r_item["type"] == "folder":
            folder_id_map[r_item["rel_path"]] = r_item["id"]

    # 3. Crear carpetas locales faltantes en orden jerárquico
    for item in comp_local:
        if item["type"] == "folder" and item["rel_path"] not in folder_id_map:
            parent_rel = Path(item["rel_path"]).parent.as_posix()
            if parent_rel == ".":
                parent_rel = ""
            parent_remote_id = folder_id_map.get(parent_rel, task_remote_id)
            new_fid = client.create_folder(item["name"], parent_id=parent_remote_id)
            if new_fid:
                folder_id_map[item["rel_path"]] = new_fid
                record["folders"][item["rel_path"]] = {"remote_id": new_fid}
                log_event("success", f"Carpeta creada en nube: {item['rel_path']}")

    # 4. Filtrar archivos nuevos o modificados para subir
    to_upload = [i for i in comp_local if i["type"] == "file" and i["sync_status"] in ("new", "modified")]

    # Verificar umbral de seguridad
    total_tracked = max(len(record.get("files", {})), 1)
    impact_pct = (len(to_upload) / total_tracked) * 100
    force = (request.json or {}).get("force", False)
    threshold = task.get("min_impact_percentage", config.get("min_impact_percentage", 40))

    if len(record.get("files", {})) > 0 and impact_pct > threshold and not force:
        msg = f"Impacto del {impact_pct:.1f}% supera el umbral del {threshold}%. Confirma con 'Forzar Sincronización'."
        log_event("warning", msg)
        return jsonify({"requires_confirmation": True, "message": msg, "count": len(to_upload)}), 200

    use_hs = config.get("use_high_speed_upload", True)
    workers = config.get("max_workers", 4)
    uploaded_count = 0

    def upload_worker(file_item):
        # Si fue modificado y existe remotamente, eliminar versión anterior primero
        if file_item["sync_status"] == "modified" and file_item.get("remote_id"):
            client.delete_file(file_item["remote_id"])

        parent_rel = Path(file_item["rel_path"]).parent.as_posix()
        if parent_rel == ".":
            parent_rel = ""
        target_folder_id = folder_id_map.get(parent_rel, task_remote_id)
        res = client.upload_file(file_item["full_path"], folder_id=target_folder_id, use_high_speed=use_hs)
        return file_item, res, target_folder_id

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(upload_worker, item) for item in to_upload]
        for fut in as_completed(futures):
            f_item, api_res, folder_id = fut.result()
            if api_res and api_res.get("id"):
                uploaded_count += 1
                record["files"][f_item["rel_path"]] = {
                    "remote_id": api_res["id"],
                    "folder_id": folder_id,
                    "size": f_item["size"],
                    "local_mtime": f_item["mtime"],
                    "updated_at": datetime.datetime.now(timezone.utc).isoformat()
                }
                log_event("success", f"Sincronizado: {f_item['rel_path']} (ID: {api_res['id']})")

    save_task_record(task_name, record)
    log_event("info", f"Sincronización finalizada: {uploaded_count}/{len(to_upload)} archivos subidos.")
    return jsonify({"success": True, "uploaded": uploaded_count, "total": len(to_upload)})


def _resolve_target_file():
    """Resuelve un archivo local directamente desde disco (sin caché) o descarga remoto a temp_cache."""
    source = request.args.get("source", "local").strip()
    if source == "local":
        direct_path = request.args.get("full_path", "").strip()
        if direct_path:
            target = Path(direct_path).resolve()
            if not target.exists() or not target.is_file():
                return None, "Archivo local no encontrado"
            return target, None

        task_name = request.args.get("task", "").strip()
        rel_path = request.args.get("rel_path", "").strip().lstrip("/")
        config = load_config()
        task = next((t for t in config["tasks"] if t["name"] == task_name), None)
        if not task:
            return None, "Tarea no encontrada"
        base = Path(task["path"]).resolve()
        target = (base / rel_path).resolve()
        if not str(target).startswith(str(base)) or not target.exists() or not target.is_file():
            return None, "Archivo local no encontrado"
        return target, None
    else:
        remote_id = request.args.get("remote_id", "").strip()
        filename = request.args.get("name", f"archivo_{remote_id}").strip()
        if not remote_id:
            return None, "Falta remote_id"
        client = FileJumpClient()
        if not client.api_key:
            return None, "Falta FILEJUMP_API_KEY en .env"
        cached = client.download_file_to_cache(remote_id, filename)
        if not cached or not cached.exists():
            return None, "No se pudo descargar el archivo desde FileJump"
        return cached, None


@app.route("/api/file/stream", methods=["GET"])
def api_file_stream():
    """Sirve el archivo en línea (PDF en nueva ventana, audio, video o imágenes)."""
    file_path, err = _resolve_target_file()
    if err:
        return jsonify({"error": err}), 404
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return send_file(file_path, mimetype="application/pdf", as_attachment=False, download_name=file_path.name, conditional=True)

    # Si es un video no nativo (.avi, .mkv, etc.) y se solicita transcodificar con ffmpeg si está disponible
    if request.args.get("transcode") == "1" and ext in {".avi", ".mkv", ".mov", ".wmv", ".flv"}:
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            mp4_cached = TEMP_CACHE_DIR / f"{file_path.stem}_web.mp4"
            if not mp4_cached.exists() or mp4_cached.stat().st_size == 0:
                try:
                    subprocess.run(
                        [ffmpeg_bin, "-y", "-i", str(file_path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", str(mp4_cached)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
                    )
                except Exception:
                    pass
            if mp4_cached.exists() and mp4_cached.stat().st_size > 0:
                return send_file(mp4_cached, mimetype="video/mp4", as_attachment=False, conditional=True)

    mime_type, _ = mimetypes.guess_type(str(file_path))
    return send_file(
        file_path,
        mimetype=mime_type or "application/octet-stream",
        as_attachment=False,
        download_name=file_path.name,
        conditional=True
    )


@app.route("/api/file/preview", methods=["GET"])
def api_file_preview():
    """Prepara vista previa de texto o documentos de ofimática."""
    file_path, err = _resolve_target_file()
    if err:
        return jsonify({"error": err}), 404

    ext = file_path.suffix.lower()
    text_exts = {".txt", ".log", ".json", ".py", ".md", ".csv", ".xml", ".html", ".htm", ".css", ".js", ".env", ".ini", ".yaml", ".yml", ".sql"}
    office_exts = {".docx", ".xlsx", ".pptx", ".odt", ".ods"}

    if ext in text_exts:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(250_000) # Límite seguro de 250 KB para texto
            return jsonify({"kind": "text", "name": file_path.name, "content": content})
        except Exception as e:
            return jsonify({"error": f"Error leyendo archivo de texto: {e}"}), 500

    if ext in office_exts:
        html_preview = extract_office_preview_html(file_path)
        return jsonify({"kind": "office", "name": file_path.name, "html": html_preview})

    return jsonify({"kind": "unsupported", "name": file_path.name, "ext": ext})


def extract_id3_metadata(file_path):
    """Extrae título, artista, álbum y carátula incrustada (APIC) de un archivo MP3."""
    meta = {
        "title": Path(file_path).stem,
        "artist": "Artista desconocido",
        "album": "",
        "cover_data_url": None
    }
    try:
        with open(file_path, "rb") as f:
            header = f.read(10)
            if len(header) == 10 and header[:3] == b"ID3":
                ver_major = header[3]
                flags = header[5]
                tag_size = (
                    ((header[6] & 0x7F) << 21)
                    | ((header[7] & 0x7F) << 14)
                    | ((header[8] & 0x7F) << 7)
                    | (header[9] & 0x7F)
                )
                tag_data = f.read(min(tag_size, 8 * 1024 * 1024))
                pos = 0
                # Saltar cabecera extendida si existe
                if (flags & 0x40) and len(tag_data) >= 4:
                    ext_size = struct.unpack(">I", tag_data[:4])[0]
                    pos += ext_size

                def decode_id3_text(raw_bytes):
                    if not raw_bytes:
                        return ""
                    enc = raw_bytes[0]
                    payload = raw_bytes[1:]
                    try:
                        if enc == 0:
                            return payload.decode("latin-1", errors="ignore").strip("\x00 ").strip()
                        elif enc == 1:
                            return payload.decode("utf-16", errors="ignore").strip("\x00 ").strip()
                        elif enc == 2:
                            return payload.decode("utf-16-be", errors="ignore").strip("\x00 ").strip()
                        else:
                            return payload.decode("utf-8", errors="ignore").strip("\x00 ").strip()
                    except Exception:
                        return ""

                while pos + 10 <= len(tag_data):
                    frame_id = tag_data[pos:pos + 4]
                    if frame_id == b"\x00\x00\x00\x00" or not all(32 <= b <= 90 for b in frame_id):
                        break
                    if ver_major == 4:
                        frame_size = (
                            ((tag_data[pos + 4] & 0x7F) << 21)
                            | ((tag_data[pos + 5] & 0x7F) << 14)
                            | ((tag_data[pos + 6] & 0x7F) << 7)
                            | (tag_data[pos + 7] & 0x7F)
                        )
                    else:
                        frame_size = struct.unpack(">I", tag_data[pos + 4:pos + 8])[0]
                    pos += 10
                    if frame_size <= 0 or pos + frame_size > len(tag_data):
                        break
                    frame_body = tag_data[pos:pos + frame_size]
                    pos += frame_size

                    fid_str = frame_id.decode("latin-1", errors="ignore")
                    if fid_str == "TIT2":
                        val = decode_id3_text(frame_body)
                        if val:
                            meta["title"] = val
                    elif fid_str == "TPE1":
                        val = decode_id3_text(frame_body)
                        if val:
                            meta["artist"] = val
                    elif fid_str == "TALB":
                        val = decode_id3_text(frame_body)
                        if val:
                            meta["album"] = val
                    elif fid_str == "APIC" and len(frame_body) > 4 and not meta["cover_data_url"]:
                        enc = frame_body[0]
                        mime_end = frame_body.find(b"\x00", 1)
                        if mime_end != -1:
                            mime = frame_body[1:mime_end].decode("latin-1", errors="ignore").strip() or "image/jpeg"
                            if "/" not in mime:
                                mime = "image/jpeg"
                            cursor = mime_end + 2  # saltar byte de tipo de imagen
                            if enc in (1, 2):
                                desc_end = frame_body.find(b"\x00\x00", cursor)
                                img_start = (desc_end + 2) if desc_end != -1 else cursor
                            else:
                                desc_end = frame_body.find(b"\x00", cursor)
                                img_start = (desc_end + 1) if desc_end != -1 else cursor
                            img_bytes = frame_body[img_start:]
                            if len(img_bytes) > 32:
                                b64 = base64.b64encode(img_bytes).decode("ascii")
                                meta["cover_data_url"] = f"data:{mime};base64,{b64}"

            # Fallback ID3v1 al final del archivo si no hubo título en ID3v2
            if meta["title"] == Path(file_path).stem:
                f.seek(0, os.SEEK_END)
                if f.tell() >= 128:
                    f.seek(-128, os.SEEK_END)
                    v1 = f.read(128)
                    if v1[:3] == b"TAG":
                        t1 = v1[3:33].decode("latin-1", errors="ignore").strip("\x00 ").strip()
                        a1 = v1[33:63].decode("latin-1", errors="ignore").strip("\x00 ").strip()
                        al1 = v1[63:93].decode("latin-1", errors="ignore").strip("\x00 ").strip()
                        if t1:
                            meta["title"] = t1
                        if a1:
                            meta["artist"] = a1
                        if al1:
                            meta["album"] = al1
    except Exception:
        pass
    return meta


@app.route("/api/file/audio_meta", methods=["GET"])
def api_file_audio_meta():
    """Devuelve las etiquetas ID3 y la imagen incrustada en Base64 para archivos de audio."""
    file_path, err = _resolve_target_file()
    if err:
        return jsonify({"error": err}), 404
    meta = extract_id3_metadata(file_path)
    return jsonify(meta)


@app.route("/api/file/prefetch", methods=["GET"])
def api_file_prefetch():
    """Descarga en segundo plano a temp_cache la siguiente pista remota para reproducción instantánea."""
    source = request.args.get("source", "local").strip()
    if source != "remote":
        return jsonify({"prefetched": False, "reason": "local_direct"})
    remote_id = request.args.get("remote_id", "").strip()
    filename = request.args.get("name", f"audio_{remote_id}").strip()
    if not remote_id:
        return jsonify({"prefetched": False, "error": "Falta remote_id"}), 400
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"prefetched": False, "error": "Sin API key"}), 400
    cached = client.download_file_to_cache(remote_id, filename)
    return jsonify({"prefetched": bool(cached and cached.exists()), "remote_id": remote_id})


# ==============================================================================
# INTERFAZ WEB (HTML + TAILWIND CSS + VANILLA JS)
# ==============================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FileJump Sync Studio</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #0f172a; }
        ::-webkit-scrollbar-thumb { background: #334155; border-radius: 9999px; }
        ::-webkit-scrollbar-thumb:hover { background: #475569; }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col">

    <!-- Barra Superior -->
    <header class="bg-slate-900/90 backdrop-blur border-b border-slate-800 px-4 sm:px-6 py-3.5 flex flex-wrap items-center justify-between gap-4 sticky top-0 z-30">
        <div class="flex items-center gap-3">
            <div class="w-9 h-9 rounded-xl bg-indigo-600 flex items-center justify-center shadow-lg shadow-indigo-500/20">
                <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/>
                </svg>
            </div>
            <div>
                <h1 class="text-base sm:text-lg font-bold tracking-tight text-white">FileJump Sync Studio</h1>
                <p class="text-xs text-slate-400">Comparador & Sincronizador Web Dual</p>
            </div>
        </div>

        <div class="flex items-center gap-3">
            <!-- Estado de Conexión API -->
            <div id="apiStatusBadge" class="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium bg-slate-800 border border-slate-700 text-slate-300">
                <span id="apiStatusDot" class="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
                <span id="apiStatusText">Verificando API...</span>
            </div>

            <button onclick="openSettingsModal()" class="px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs sm:text-sm font-medium flex items-center gap-2 transition">
                <svg class="w-4 h-4 text-slate-300" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/>
                    <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
                </svg>
                <span>Configuración & .env</span>
            </button>
        </div>
    </header>

    <!-- Barra de Control de Tareas -->
    <section class="max-w-7xl w-full mx-auto px-4 sm:px-6 pt-5 pb-3">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4 flex flex-col md:flex-row md:items-center justify-between gap-4 shadow-sm">
            <div class="flex flex-col sm:flex-row sm:items-center gap-3 flex-1">
                <label class="text-xs font-semibold uppercase tracking-wider text-slate-400">Tarea Activa:</label>
                <select id="taskSelector" onchange="inspectSelectedTask()" class="bg-slate-950 border border-slate-700 rounded-xl px-3.5 py-2 text-sm text-white focus:outline-none focus:border-indigo-500 min-w-[220px]">
                    <option value="">-- Selecciona o crea una tarea --</option>
                </select>
                <span id="lastSyncLabel" class="text-xs text-slate-400"></span>
            </div>

            <div class="flex flex-wrap items-center gap-2.5">
                <button onclick="inspectSelectedTask()" class="px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs sm:text-sm font-medium text-slate-200 transition flex items-center gap-1.5">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                    Comparar Ahora
                </button>
                <button id="syncBtn" onclick="triggerSync(false)" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs sm:text-sm font-semibold text-white shadow-lg shadow-indigo-600/20 transition flex items-center gap-1.5">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>
                    Sincronizar a FileJump
                </button>
            </div>
        </div>

        <!-- Leyenda de Estados -->
        <div class="flex flex-wrap items-center gap-4 mt-3 px-1 text-xs text-slate-400">
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-emerald-500"></span> Sincronizado</span>
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-sky-500"></span> Nuevo (Pendiente subir)</span>
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-amber-500"></span> Modificado (Tamaño/Fecha distinta)</span>
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-purple-500"></span> Solo en FileJump</span>
        </div>
    </section>

    <!-- DOBLE VENTANA DE COMPARACIÓN (LOCAL VS REMOTO) -->
    <main class="max-w-7xl w-full mx-auto px-4 sm:px-6 py-2 flex-1 grid grid-cols-1 lg:grid-cols-2 gap-5">
        
        <!-- VENTANA 1: EXPLORADOR LOCAL ESTILO WINDOWS -->
        <div class="bg-slate-900 border border-slate-800 rounded-2xl flex flex-col overflow-hidden shadow-md h-[510px]">
            <div class="px-4 py-3 bg-slate-900 border-b border-slate-800 flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0 flex-1">
                    <span class="p-1.5 rounded-lg bg-slate-800 text-sky-400 shrink-0">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>
                    </span>
                    <div class="min-w-0 flex-1">
                        <div class="flex items-center gap-2">
                            <h2 class="text-sm font-semibold text-white shrink-0">Equipo Local</h2>
                            <select id="localDriveSelector" onchange="onLocalDriveChange()" class="bg-slate-950 border border-slate-700 rounded-lg px-2 py-0.5 text-[11px] text-sky-300 focus:outline-none focus:border-sky-500 max-w-[180px] truncate">
                                <option value="">Auto (Carpeta de Tarea)</option>
                            </select>
                            <button onclick="fetchLocalDrives(true)" class="p-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition shrink-0" title="Actualizar unidades locales">
                                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                            </button>
                        </div>
                        <p id="localPathSubtitle" class="text-[11px] text-slate-400 truncate max-w-[280px]">Sin ruta seleccionada</p>
                    </div>
                </div>
                <span id="localCountBadge" class="text-xs px-2.5 py-1 rounded-full bg-slate-800 text-slate-300 font-medium shrink-0">0 elementos</span>
            </div>

            <!-- Barra de Navegación Rápida Local (Breadcrumbs) -->
            <div id="localBreadcrumbBar" class="px-3 py-1.5 bg-slate-950/90 border-b border-slate-800/80 flex items-center gap-1.5 text-[11px] text-slate-400 overflow-x-auto shrink-0">
                <button onclick="selectLocalFolder('')" class="hover:text-sky-400 font-medium flex items-center gap-1 shrink-0">
                    <span>🏠 Raíz</span>
                </button>
            </div>

            <!-- Cuerpo Dividido estilo Explorador de Windows: Árbol de Carpetas (Izq) + Contenido (Der) -->
            <div class="flex flex-1 min-h-0 overflow-hidden">
                <!-- Panel Izquierdo: Árbol Jerárquico de Carpetas (Lazy Load con +) -->
                <div class="w-44 sm:w-52 bg-slate-950/60 border-r border-slate-800/80 overflow-y-auto p-1.5 select-none shrink-0">
                    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500 px-2 py-1">Carpetas</div>
                    <div id="localFolderTree" class="space-y-0.5 text-xs">
                        <div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>
                    </div>
                </div>

                <!-- Panel Derecho: Archivos y Subcarpetas de la Carpeta Seleccionada -->
                <div class="flex-1 overflow-auto min-w-0">
                    <table class="w-full text-left border-collapse text-xs select-none">
                        <thead class="bg-slate-950/70 text-slate-400 sticky top-0 z-10">
                            <tr>
                                <th class="py-2 px-2.5 font-medium">Nombre</th>
                                <th class="py-2 px-2.5 font-medium">Tamaño</th>
                                <th class="py-2 px-2.5 font-medium">Modificado</th>
                                <th class="py-2 px-2.5 font-medium text-right">Estado</th>
                            </tr>
                        </thead>
                        <tbody id="localTableBody" class="divide-y divide-slate-800/60">
                            <tr><td colspan="4" class="py-12 text-center text-slate-500">Selecciona una tarea para inspeccionar archivos locales</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- VENTANA 2: EXPLORADOR REMOTO (FILEJUMP) ESTILO WINDOWS -->
        <div class="bg-slate-900 border border-slate-800 rounded-2xl flex flex-col overflow-hidden shadow-md h-[510px]">
            <div class="px-4 py-3 bg-slate-900 border-b border-slate-800 flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0 flex-1">
                    <span class="p-1.5 rounded-lg bg-indigo-950 text-indigo-400 shrink-0">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.096A4.001 4.001 0 003 15z"/></svg>
                    </span>
                    <div class="min-w-0 flex-1">
                        <div class="flex items-center gap-2">
                            <h2 class="text-sm font-semibold text-white shrink-0">Nube FileJump</h2>
                            <select id="remoteDirSelector" onchange="onRemoteDirectoryChange()" class="bg-slate-950 border border-slate-700 rounded-lg px-2 py-0.5 text-[11px] text-indigo-300 focus:outline-none focus:border-indigo-500 max-w-[180px] truncate">
                                <option value="">Auto (Carpeta de Tarea)</option>
                                <option value="ROOT">/ (Raíz de FileJump)</option>
                            </select>
                            <button onclick="fetchRemoteDirectories(true)" class="p-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition shrink-0" title="Actualizar directorios remotos">
                                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                            </button>
                        </div>
                        <p id="remotePathSubtitle" class="text-[11px] text-slate-400 truncate">ID Carpeta Raíz: -</p>
                    </div>
                </div>
                <span id="remoteCountBadge" class="text-xs px-2.5 py-1 rounded-full bg-slate-800 text-slate-300 font-medium shrink-0">0 elementos</span>
            </div>

            <!-- Barra de Navegación Rápida Remota (Breadcrumbs) -->
            <div id="remoteBreadcrumbBar" class="px-3 py-1.5 bg-slate-950/90 border-b border-slate-800/80 flex items-center gap-1.5 text-[11px] text-slate-400 overflow-x-auto shrink-0">
                <button onclick="selectRemoteNode('ROOT')" class="hover:text-indigo-400 font-medium flex items-center gap-1 shrink-0">
                    <span>☁️ Raíz</span>
                </button>
            </div>

            <!-- Cuerpo Dividido estilo Explorador de Windows: Árbol de Carpetas (Izq) + Contenido (Der) -->
            <div class="flex flex-1 min-h-0 overflow-hidden">
                <!-- Panel Izquierdo: Árbol Jerárquico Remoto (Lazy Load con +) -->
                <div class="w-44 sm:w-52 bg-slate-950/60 border-r border-slate-800/80 overflow-y-auto p-1.5 select-none shrink-0">
                    <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500 px-2 py-1">Carpetas Nube</div>
                    <div id="remoteFolderTree" class="space-y-0.5 text-xs">
                        <div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>
                    </div>
                </div>

                <!-- Panel Derecho: Archivos y Subcarpetas de la Carpeta Seleccionada -->
                <div class="flex-1 overflow-auto min-w-0">
                    <table class="w-full text-left border-collapse text-xs select-none">
                        <thead class="bg-slate-950/70 text-slate-400 sticky top-0 z-10">
                            <tr>
                                <th class="py-2 px-2.5 font-medium">Nombre</th>
                                <th class="py-2 px-2.5 font-medium">Tamaño</th>
                                <th class="py-2 px-2.5 font-medium">Actualizado</th>
                                <th class="py-2 px-2.5 font-medium text-right">ID / Estado</th>
                            </tr>
                        </thead>
                        <tbody id="remoteTableBody" class="divide-y divide-slate-800/60">
                            <tr><td colspan="4" class="py-12 text-center text-slate-500">Selecciona una tarea para consultar FileJump</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </main>

    <!-- CONSOLA DE ACTIVIDAD EN TIEMPO REAL -->
    <section class="max-w-7xl w-full mx-auto px-4 sm:px-6 py-4">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4">
            <div class="flex items-center justify-between mb-2">
                <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-400">Registro de Actividad en Vivo</h3>
                <span class="text-[11px] text-slate-500">Endpoint Alta Velocidad Activo</span>
            </div>
            <div id="logConsole" class="bg-slate-950 rounded-xl p-3 h-32 overflow-y-auto font-mono text-xs space-y-1 text-slate-300 border border-slate-800/80">
                <div class="text-slate-500">Esperando acciones...</div>
            </div>
        </div>
    </section>

    <!-- MODAL DE CONFIGURACIÓN (.env + TAREAS) -->
    <div id="settingsModal" class="fixed inset-0 bg-black/75 backdrop-blur-sm hidden items-center justify-center p-4 z-50">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-2xl w-full max-h-[90vh] flex flex-col shadow-2xl">
            <div class="px-6 py-4 border-b border-slate-800 flex items-center justify-between">
                <h2 class="text-base font-bold text-white">Configuración del Sistema y Credenciales (.env)</h2>
                <button onclick="closeSettingsModal()" class="text-slate-400 hover:text-white text-xl leading-none">&times;</button>
            </div>

            <div class="p-6 overflow-y-auto space-y-6 text-sm">
                <!-- Sección Credenciales .env -->
                <div class="space-y-3 bg-slate-950/60 p-4 rounded-xl border border-slate-800">
                    <h3 class="text-xs font-semibold uppercase tracking-wider text-indigo-400">Seguridad API (.env)</h3>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">Bearer Token de FileJump (se guarda cifrado en .env local)</label>
                        <input type="password" id="envApiKey" placeholder="Dejar vacío para mantener el token actual..." class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white focus:border-indigo-500 focus:outline-none">
                    </div>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">REST API Base URL</label>
                            <input type="text" id="envBaseUrl" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-200">
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">High-Speed Upload URL</label>
                            <input type="text" id="envUploadUrl" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-slate-200">
                        </div>
                    </div>
                </div>

                <!-- Parámetros Globales -->
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">Hilos Simultáneos</label>
                        <input type="number" id="cfgWorkers" min="1" max="15" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">Umbral Impacto (%)</label>
                        <input type="number" id="cfgThreshold" min="1" max="100" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
                    </div>
                    <div class="flex items-center pt-5">
                        <label class="flex items-center gap-2 cursor-pointer text-xs text-slate-300">
                            <input type="checkbox" id="cfgHighSpeed" class="rounded bg-slate-950 border-slate-700 text-indigo-600">
                            <span>Subida Directa Alta Velocidad</span>
                        </label>
                    </div>
                </div>

                <!-- Gestión de Carpetas / Tareas -->
                <div class="space-y-3">
                    <div class="flex items-center justify-between">
                        <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-400">Carpetas a Sincronizar</h3>
                        <button onclick="addTaskRow()" class="text-xs px-2.5 py-1 rounded-lg bg-indigo-600/20 text-indigo-400 hover:bg-indigo-600/30 border border-indigo-500/30">+ Añadir Carpeta</button>
                    </div>
                    <div id="tasksListContainer" class="space-y-2.5"></div>
                </div>
            </div>

            <div class="px-6 py-4 border-t border-slate-800 flex justify-end gap-3">
                <button onclick="closeSettingsModal()" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs font-medium text-slate-300">Cancelar</button>
                <button onclick="saveSettings()" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white">Guardar Cambios</button>
            </div>
        </div>
    </div>

    <!-- MODAL DE NOTIFICACIÓN / CONFIRMACIÓN (Sin usar alert/confirm nativos) -->
    <div id="noticeModal" class="fixed inset-0 bg-black/75 backdrop-blur-sm hidden items-center justify-center p-4 z-50">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <h3 id="noticeTitle" class="text-base font-bold text-white">Aviso</h3>
            <p id="noticeMessage" class="text-xs text-slate-300 leading-relaxed"></p>
            <div id="noticeActions" class="flex justify-end gap-2.5 pt-2"></div>
        </div>
    </div>

    <!-- MODAL DE VISTA PREVIA Y REPRODUCTOR MULTIMEDIA -->
    <div id="previewModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm hidden items-center justify-center p-4 z-50">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-4xl w-full max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
            <div class="px-4 sm:px-5 py-3.5 border-b border-slate-800 flex items-center justify-between gap-3 bg-slate-900">
                <div class="flex items-center gap-2 min-w-0">
                    <span id="previewBadge" class="px-2 py-0.5 rounded-md text-[10px] font-semibold uppercase bg-indigo-600/25 text-indigo-300 border border-indigo-500/30 shrink-0">Vista Previa</span>
                    <h3 id="previewTitle" class="text-sm font-bold text-white truncate">Archivo</h3>
                    <span id="previewCounter" class="text-[11px] text-slate-400 shrink-0"></span>
                </div>
                <div class="flex items-center gap-1.5 sm:gap-2 shrink-0">
                    <button id="previewPrevBtn" onclick="navigatePreview(-1)" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 disabled:opacity-35 disabled:pointer-events-none text-xs text-slate-200 transition flex items-center gap-1" title="Archivo anterior (Flecha Izquierda)">
                        <span>◀</span><span class="hidden sm:inline">Anterior</span>
                    </button>
                    <button id="previewNextBtn" onclick="navigatePreview(1)" class="px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 disabled:opacity-35 disabled:pointer-events-none text-xs text-slate-200 transition flex items-center gap-1" title="Archivo siguiente (Flecha Derecha)">
                        <span class="hidden sm:inline">Siguiente</span><span>▶</span>
                    </button>
                    <a id="previewOpenTabBtn" href="#" target="_blank" rel="noopener" class="px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-200 transition">Abrir en Nueva Pestaña ↗</a>
                    <button onclick="closePreviewModal()" class="w-8 h-8 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white flex items-center justify-center text-lg leading-none">&times;</button>
                </div>
            </div>
            <div id="previewBody" class="relative p-5 overflow-y-auto flex-1 bg-slate-950/80 flex items-center justify-center min-h-[320px]">
                <!-- Contenido dinámico del visor -->
            </div>
        </div>
    </div>

    <!-- REPRODUCTOR DE MÚSICA FLOTANTE MINIMIZABLE Y MAXIMIZABLE (ESTILO CHAT EN VIVO EN ESQUINA DERECHA) -->
    <div id="musicDockWidget" class="fixed bottom-4 right-4 z-50 hidden transition-all duration-200">
        <!-- Vista Minimizada (Burbuja Compacta) -->
        <div id="musicDockMinimized" onclick="setMusicDockMode('normal')" class="hidden cursor-pointer bg-slate-900/95 hover:bg-slate-800 border border-indigo-500/40 rounded-full pl-2 pr-4 py-2 shadow-2xl flex items-center gap-3 backdrop-blur">
            <div id="miniCoverWrap" class="w-9 h-9 rounded-full bg-indigo-600/30 border border-indigo-500/40 overflow-hidden flex items-center justify-center shrink-0">
                <span class="text-base">🎵</span>
            </div>
            <div class="min-w-0 max-w-[160px]">
                <div id="miniTrackTitle" class="text-xs font-bold text-white truncate">Reproduciendo...</div>
                <div id="miniTrackArtist" class="text-[10px] text-indigo-300 truncate">Clic para expandir</div>
            </div>
            <button onclick="event.stopPropagation(); toggleDockPlayPause()" id="miniPlayPauseBtn" class="w-8 h-8 rounded-full bg-indigo-600 hover:bg-indigo-500 text-white flex items-center justify-center text-xs shrink-0">⏸</button>
        </div>

        <!-- Vista Normal / Maximizada (Tarjeta Flotante o Modal Amplio con Carátula Grande + Playlist) -->
        <div id="musicDockExpanded" class="w-80 sm:w-[410px] bg-slate-900/95 backdrop-blur border border-slate-700/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col">
            <div class="px-3.5 py-2.5 bg-slate-950/90 border-b border-slate-800 flex items-center justify-between shrink-0">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse shrink-0"></span>
                    <span class="text-[11px] font-semibold uppercase tracking-wider text-indigo-300 truncate">Reproductor Musical</span>
                    <span id="dockTrackCounter" class="text-[10px] text-slate-400 shrink-0"></span>
                    <span id="dockPrefetchBadge" class="hidden text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 shrink-0" title="Siguiente pista remota precargada en caché">⚡ Siguiente lista</span>
                </div>
                <div class="flex items-center gap-1 shrink-0">
                    <button onclick="setMusicDockMode('minimized')" class="w-6 h-6 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white flex items-center justify-center text-sm" title="Minimizar a burbuja">—</button>
                    <button id="dockMaxRestoreBtn" onclick="toggleMusicDockMaximize()" class="w-6 h-6 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white flex items-center justify-center text-xs" title="Maximizar reproductor">⛶</button>
                    <button onclick="closeMusicDock()" class="w-6 h-6 rounded-lg hover:bg-rose-600/80 text-slate-400 hover:text-white flex items-center justify-center text-sm" title="Cerrar reproductor">&times;</button>
                </div>
            </div>

            <!-- Contenedor Principal (En modo normal es vertical; en modo maximizado se divide en 2 columnas) -->
            <div id="musicDockMainGrid" class="flex flex-col flex-1 min-h-0 overflow-hidden">
                <!-- Columna Izquierda / Superior: Carátula + Info + Controles -->
                <div id="musicDockPlayerCol" class="p-4 space-y-3.5 flex flex-col justify-between shrink-0">
                    <!-- Carátula ID3 + Metadatos -->
                    <div id="dockMediaHeader" class="flex items-center gap-3.5">
                        <div id="dockCoverBox" onclick="openCoverLightbox()" class="w-16 h-16 rounded-xl bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center shrink-0 shadow-inner transition-all">
                            <span id="dockCoverEmoji" class="text-3xl">🎵</span>
                        </div>
                        <div id="dockMetaTextWrap" class="min-w-0 flex-1">
                            <div id="dockTrackTitle" class="text-sm font-bold text-white truncate" title="">Título de canción</div>
                            <div id="dockTrackArtist" class="text-xs text-indigo-300 truncate mt-0.5">Artista desconocido</div>
                            <div id="dockTrackAlbum" class="text-[11px] text-slate-400 truncate mt-0.5"></div>
                        </div>
                    </div>

                    <!-- Barra de Progreso Interactiva -->
                    <div class="space-y-1">
                        <input type="range" id="dockSeekBar" min="0" max="100" value="0" oninput="seekDockAudio(this.value)" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
                        <div class="flex justify-between text-[10px] text-slate-400 font-mono">
                            <span id="dockCurrentTime">0:00</span>
                            <span id="dockTotalTime">0:00</span>
                        </div>
                    </div>

                    <!-- Visualizador Rítmico en Tiempo Real (Canvas Web Audio Analyser) -->
                    <div id="dockVisualizerWrap" class="bg-slate-950/90 border border-slate-800/90 rounded-xl p-2 relative overflow-hidden">
                        <canvas id="dockVisualizerCanvas" width="360" height="54" class="w-full h-12 sm:h-14 block rounded-lg"></canvas>
                        <button onclick="cycleVisualizerStyle()" id="dockVisStyleBtn" class="absolute top-1.5 right-2 px-1.5 py-0.5 rounded bg-slate-900/85 hover:bg-slate-800 border border-slate-700/80 text-[9px] text-indigo-300 font-mono transition" title="Cambiar estilo visual">Barras</button>
                    </div>

                    <!-- Botones de Control: Aleatorio, Anterior, Play/Pausa, Siguiente -->
                    <div class="flex items-center justify-between gap-1.5 pt-1">
                        <button id="dockShuffleBtn" onclick="toggleDockShuffle()" class="px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-400 transition flex items-center gap-1" title="Modo Aleatorio (reordena y precarga la siguiente)">
                            <span>🔀</span><span class="text-[10px] font-medium hidden sm:inline">Aleatorio</span>
                        </button>

                        <div class="flex items-center gap-2">
                            <button onclick="playDockPrev()" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 flex items-center justify-center text-sm transition" title="Anterior">⏮</button>
                            <button id="dockPlayPauseBtn" onclick="toggleDockPlayPause()" class="w-11 h-11 rounded-full bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/30 flex items-center justify-center text-base transition" title="Play / Pausa">⏸</button>
                            <button onclick="playDockNext()" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 flex items-center justify-center text-sm transition" title="Siguiente">⏭</button>
                        </div>

                        <div class="flex items-center gap-1.5">
                            <button id="dockVisToggleBtn" onclick="toggleDockVisualizer()" class="px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-[10px] font-medium text-indigo-300 transition flex items-center gap-1" title="Activar / Desactivar visualización rítmica">
                                <span>📊</span><span>Vis</span>
                            </button>
                            <button id="dockEqToggleBtn" onclick="toggleDockEqPanel()" class="px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-[10px] font-medium text-slate-300 transition flex items-center gap-1" title="Ecualizador de 5 bandas">
                                <span>🎛️</span><span>EQ</span>
                            </button>
                        </div>
                    </div>

                    <!-- Control de Volumen y Origen -->
                    <div class="flex items-center justify-between gap-2.5 pt-1 border-t border-slate-800/70">
                        <div class="flex items-center gap-2 flex-1 min-w-0">
                            <button id="dockMuteBtn" onclick="toggleDockMute()" class="w-7 h-7 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-300 flex items-center justify-center text-xs shrink-0 transition" title="Silenciar / Activar sonido">🔊</button>
                            <input type="range" id="dockVolumeBar" min="0" max="100" value="85" oninput="setDockVolume(this.value)" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-emerald-400" title="Volumen">
                            <span id="dockVolumeLabel" class="text-[10px] font-mono text-slate-400 w-8 text-right shrink-0">85%</span>
                        </div>
                        <span id="dockSourceBadge" class="text-[10px] px-2 py-0.5 rounded-lg bg-slate-950 border border-slate-800 text-slate-400 shrink-0">Local</span>
                    </div>

                    <!-- Panel Ecualizador Gráfico de 5 Bandas (Web Audio API) -->
                    <div id="dockEqPanel" class="hidden bg-slate-950/90 border border-slate-800 rounded-xl p-3 space-y-2.5">
                        <div class="flex items-center justify-between gap-2">
                            <span class="text-[10px] font-semibold uppercase tracking-wider text-indigo-300">Ecualizador 5 Bandas</span>
                            <div class="flex items-center gap-1.5">
                                <select id="dockEqPreset" onchange="applyEqPreset(this.value)" class="bg-slate-900 border border-slate-700 rounded-lg px-2 py-0.5 text-[10px] text-slate-200 focus:outline-none focus:border-indigo-500">
                                    <option value="flat">Plano (0 dB)</option>
                                    <option value="bass">Bass Boost</option>
                                    <option value="electronic">Electrónica / Club</option>
                                    <option value="pop">Pop</option>
                                    <option value="rock">Rock</option>
                                    <option value="vocal">Vocal / Acústico</option>
                                </select>
                                <button onclick="applyEqPreset('flat')" class="px-2 py-0.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-[10px] text-slate-300" title="Restablecer bandas">Reset</button>
                            </div>
                        </div>
                        <div class="grid grid-cols-5 gap-2 pt-1 text-center">
                            <div class="flex flex-col items-center gap-1">
                                <span id="eqVal0" class="text-[9px] font-mono text-indigo-300">0dB</span>
                                <input type="range" id="eqBand0" min="-12" max="12" step="1" value="0" oninput="setEqBandGain(0, this.value)" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
                                <span class="text-[9px] text-slate-400 font-mono">60Hz</span>
                            </div>
                            <div class="flex flex-col items-center gap-1">
                                <span id="eqVal1" class="text-[9px] font-mono text-indigo-300">0dB</span>
                                <input type="range" id="eqBand1" min="-12" max="12" step="1" value="0" oninput="setEqBandGain(1, this.value)" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
                                <span class="text-[9px] text-slate-400 font-mono">230Hz</span>
                            </div>
                            <div class="flex flex-col items-center gap-1">
                                <span id="eqVal2" class="text-[9px] font-mono text-indigo-300">0dB</span>
                                <input type="range" id="eqBand2" min="-12" max="12" step="1" value="0" oninput="setEqBandGain(2, this.value)" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
                                <span class="text-[9px] text-slate-400 font-mono">910Hz</span>
                            </div>
                            <div class="flex flex-col items-center gap-1">
                                <span id="eqVal3" class="text-[9px] font-mono text-indigo-300">0dB</span>
                                <input type="range" id="eqBand3" min="-12" max="12" step="1" value="0" oninput="setEqBandGain(3, this.value)" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
                                <span class="text-[9px] text-slate-400 font-mono">3.6kHz</span>
                            </div>
                            <div class="flex flex-col items-center gap-1">
                                <span id="eqVal4" class="text-[9px] font-mono text-indigo-300">0dB</span>
                                <input type="range" id="eqBand4" min="-12" max="12" step="1" value="0" oninput="setEqBandGain(4, this.value)" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
                                <span class="text-[9px] text-slate-400 font-mono">14kHz</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Columna Derecha / Inferior: Playlist de Canciones -->
                <div id="musicDockPlaylistCol" class="border-t border-slate-800 bg-slate-950/75 flex flex-col min-h-0">
                    <div class="px-3.5 py-2 border-b border-slate-800/80 flex items-center justify-between shrink-0">
                        <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Lista de Reproducción (Playlist)</span>
                        <span id="dockPlaylistOrderTag" class="text-[10px] text-slate-500">Orden normal</span>
                    </div>
                    <div id="dockPlaylistItems" class="max-h-44 overflow-y-auto divide-y divide-slate-800/40 text-xs">
                        <!-- Elementos del playlist -->
                    </div>
                </div>
            </div>
            <audio id="dockAudioElement" class="hidden"></audio>
        </div>
    </div>

    <!-- MODAL DE CARÁTULA GIGANTE (DELANTE DEL REPRODUCTOR MAXIMIZADO) -->
    <div id="coverLightboxModal" onclick="if(event.target===this) closeCoverLightbox()" class="fixed inset-0 bg-black/90 backdrop-blur-md hidden items-center justify-center p-4 z-[60]">
        <div class="relative bg-slate-900 border border-slate-700 rounded-2xl p-3 sm:p-4 max-w-3xl w-full flex flex-col items-center gap-3 shadow-2xl">
            <div class="w-full flex items-center justify-end gap-2">
                <a id="coverDownloadBtn" href="#" download="caratula.jpg" class="px-3.5 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white shadow-lg shadow-indigo-600/20 transition flex items-center gap-1.5">
                    <span>⬇</span><span>Descargar Carátula</span>
                </a>
                <button onclick="closeCoverLightbox()" class="w-8 h-8 rounded-xl bg-slate-800 hover:bg-rose-600/80 text-slate-300 hover:text-white flex items-center justify-center text-lg leading-none transition" title="Cerrar">&times;</button>
            </div>
            <div class="w-full flex items-center justify-center overflow-hidden rounded-xl bg-slate-950 border border-slate-800">
                <img id="coverLightboxImg" src="" alt="Carátula MP3" class="max-h-[78vh] max-w-full w-auto object-contain">
            </div>
        </div>
    </div>

    <script>
        let currentConfig = { tasks: [] };
        let wasConnected = false;
        let currentPreviewList = [];
        let currentPreviewIndex = -1;
        let currentPreviewSource = 'local';

        // Estado del Reproductor Musical Flotante
        const AUDIO_EXTS = ['mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg'];
        let musicPlaylist = [];
        let musicPlayOrder = []; // Índices de reproducción (secuencial o barajado)
        let musicOrderPos = -1;  // Posición actual dentro de musicPlayOrder
        let musicSource = 'local';
        let musicShuffle = false;
        let musicDockMode = 'normal'; // 'minimized' | 'normal' | 'maximized'
        let currentCoverDataUrl = null;
        let currentCoverFilename = 'caratula.jpg';
        const prefetchedRemoteIds = new Set();

        // Estado de Volumen y Ecualizador Web Audio API (5 Bandas)
        let audioCtx = null;
        let eqFilters = [];
        let analyserNode = null;
        let eqInitialized = false;
        let lastNonZeroVolume = 85;
        let visualizerEnabled = true;
        let visualizerStyle = 'bars'; // 'bars' | 'mirror' | 'wave'
        let visualizerRafId = null;
        const EQ_BANDS_CONFIG = [
            { freq: 60, type: 'lowshelf' },
            { freq: 230, type: 'peaking' },
            { freq: 910, type: 'peaking' },
            { freq: 3600, type: 'peaking' },
            { freq: 14000, type: 'highshelf' }
        ];
        const EQ_PRESETS = {
            flat: [0, 0, 0, 0, 0],
            bass: [7, 4, 0, -1, 1],
            electronic: [6, 3, -2, 3, 6],
            pop: [-1, 2, 4, 2, -1],
            rock: [5, 2, -2, 3, 5],
            vocal: [-2, -1, 4, 5, 2]
        };

        // Estado de los Exploradores Estilo Windows (Árbol Lazy + Contenido)
        let localNodes = new Map();
        let localSelectedPath = '';
        let localActiveDriveRoot = '';
        let remoteNodes = new Map();
        let remoteBaseRootId = 'ROOT';
        let remoteSelectedKey = 'ROOT';

        function formatBytes(bytes) {
            if (!bytes || bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        function getStatusBadge(status) {
            const map = {
                synced: '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/20">Sincronizado</span>',
                new: '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-sky-500/15 text-sky-400 border border-sky-500/20">Nuevo</span>',
                modified: '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/15 text-amber-400 border border-amber-500/20">Modificado</span>',
                remote_only: '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-purple-500/15 text-purple-400 border border-purple-500/20">Solo Remoto</span>',
                local_only: '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-700/40 text-slate-300 border border-slate-600/30">Disco Local</span>'
            };
            return map[status] || status;
        }

        function showNotice(title, message, confirmCallback = null) {
            document.getElementById('noticeTitle').textContent = title;
            document.getElementById('noticeMessage').textContent = message;
            const actions = document.getElementById('noticeActions');
            actions.innerHTML = '';

            const closeBtn = document.createElement('button');
            closeBtn.className = 'px-3.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-300';
            closeBtn.textContent = confirmCallback ? 'Cancelar' : 'Cerrar';
            closeBtn.onclick = () => {
                document.getElementById('noticeModal').classList.add('hidden');
                document.getElementById('noticeModal').classList.remove('flex');
            };
            actions.appendChild(closeBtn);

            if (confirmCallback) {
                const okBtn = document.createElement('button');
                okBtn.className = 'px-3.5 py-1.5 rounded-xl bg-amber-600 hover:bg-amber-500 text-xs font-semibold text-white';
                okBtn.textContent = 'Forzar Sincronización';
                okBtn.onclick = () => {
                    closeBtn.click();
                    confirmCallback();
                };
                actions.appendChild(okBtn);
            }

            document.getElementById('noticeModal').classList.remove('hidden');
            document.getElementById('noticeModal').classList.add('flex');
        }

        async function refreshStatusAndLogs() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                const dot = document.getElementById('apiStatusDot');
                const txt = document.getElementById('apiStatusText');

                if (data.connected) {
                    dot.className = 'w-2 h-2 rounded-full bg-emerald-400';
                    txt.textContent = `Conectado (${data.masked_key})`;
                    // Al conectarse por primera vez o reconectarse, actualizar directorios remotos automáticamente
                    if (!wasConnected) {
                        wasConnected = true;
                        await fetchRemoteDirectories(true);
                    }
                } else {
                    wasConnected = false;
                    dot.className = 'w-2 h-2 rounded-full bg-rose-500';
                    txt.textContent = data.has_key ? 'Error de Token API' : 'Falta API Key en .env';
                }

                const consoleEl = document.getElementById('logConsole');
                if (data.logs && data.logs.length > 0) {
                    consoleEl.innerHTML = data.logs.map(l => {
                        const color = l.level === 'error' ? 'text-rose-400' : l.level === 'success' ? 'text-emerald-400' : l.level === 'warning' ? 'text-amber-400' : 'text-slate-300';
                        return `<div><span class="text-slate-500">[${l.time}]</span> <span class="${color}">${l.message}</span></div>`;
                    }).join('');
                    consoleEl.scrollTop = consoleEl.scrollHeight;
                }
            } catch (e) {}
        }

        async function fetchRemoteDirectories(autoRefreshInspect = false) {
            try {
                const res = await fetch('/api/remote/directories');
                const data = await res.json();
                if (!data.connected) return;

                const dirSelector = document.getElementById('remoteDirSelector');
                const currentVal = dirSelector.value;
                dirSelector.innerHTML = `
                    <option value="">Auto (Carpeta de Tarea)</option>
                    <option value="ROOT">/ (Raíz de FileJump)</option>
                `;
                (data.directories || []).forEach(dir => {
                    const opt = document.createElement('option');
                    opt.value = dir.id;
                    opt.textContent = `📁 ${dir.name} (#${dir.id})`;
                    dirSelector.appendChild(opt);
                });

                if (currentVal) {
                    dirSelector.value = currentVal;
                }

                if (autoRefreshInspect) {
                    if (document.getElementById('taskSelector').value) {
                        await inspectSelectedTask();
                    } else {
                        // Si no hay tarea local seleccionada, mostrar la raíz remota por defecto
                        dirSelector.value = 'ROOT';
                        await inspectSelectedTask();
                    }
                }
            } catch (e) {}
        }

        function onRemoteDirectoryChange() {
            const selectedRemoteId = document.getElementById('remoteDirSelector').value;
            inspectSelectedTask(selectedRemoteId);
        }

        async function fetchLocalDrives(autoBrowse = false) {
            try {
                const res = await fetch('/api/local/drives');
                const data = await res.json();
                const driveSelector = document.getElementById('localDriveSelector');
                const prevVal = driveSelector.value;
                driveSelector.innerHTML = '<option value="">Auto (Carpeta de Tarea)</option>';
                (data.drives || []).forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = d.id;
                    opt.textContent = `💾 ${d.name}`;
                    driveSelector.appendChild(opt);
                });
                if (prevVal) driveSelector.value = prevVal;

                if (autoBrowse && !document.getElementById('taskSelector').value && (data.drives || []).length > 0) {
                    driveSelector.value = data.drives[0].id;
                    await onLocalDriveChange();
                }
            } catch (e) {}
        }

        async function onLocalDriveChange() {
            const drivePath = document.getElementById('localDriveSelector').value;
            if (!drivePath) {
                localActiveDriveRoot = '';
                await inspectSelectedTask();
                return;
            }
            localActiveDriveRoot = drivePath;
            const localBody = document.getElementById('localTableBody');
            localBody.innerHTML = '<tr><td colspan="4" class="py-10 text-center text-slate-400">Cargando unidad local...</td></tr>';
            try {
                const res = await fetch(`/api/local/browse?root_path=${encodeURIComponent(drivePath)}`);
                const data = await res.json();
                if (data.error) {
                    showNotice('Error', data.error);
                    return;
                }
                document.getElementById('localPathSubtitle').textContent = drivePath;
                initLocalExplorer(data.items || [], drivePath);
            } catch (e) {
                showNotice('Error', 'No se pudo leer la unidad local.');
            }
        }

        // =========================================================================
        // REPRODUCTOR MUSICAL FLOTANTE MINIMIZABLE Y MAXIMIZABLE CON TAGS ID3 + PLAYLIST
        // =========================================================================
        function formatAudioTime(sec) {
            if (!sec || isNaN(sec) || !isFinite(sec)) return '0:00';
            const m = Math.floor(sec / 60);
            const s = Math.floor(sec % 60);
            return `${m}:${s < 10 ? '0' : ''}${s}`;
        }

        function updateCoverBoxCursor() {
            const coverBox = document.getElementById('dockCoverBox');
            if (!coverBox) return;
            if (currentCoverDataUrl) {
                coverBox.classList.add('cursor-zoom-in', 'hover:ring-2', 'hover:ring-indigo-400/70');
                coverBox.title = 'Clic para ampliar y descargar carátula';
            } else {
                coverBox.classList.remove('cursor-zoom-in', 'hover:ring-2', 'hover:ring-indigo-400/70');
                coverBox.removeAttribute('title');
            }
        }

        function openCoverLightbox() {
            if (!currentCoverDataUrl) return;
            const modal = document.getElementById('coverLightboxModal');
            const img = document.getElementById('coverLightboxImg');
            const dlBtn = document.getElementById('coverDownloadBtn');
            img.src = currentCoverDataUrl;
            dlBtn.href = currentCoverDataUrl;
            dlBtn.download = currentCoverFilename;
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        }

        function closeCoverLightbox() {
            const modal = document.getElementById('coverLightboxModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        function setMusicDockMode(mode) {
            musicDockMode = mode;
            const widget = document.getElementById('musicDockWidget');
            const minEl = document.getElementById('musicDockMinimized');
            const expEl = document.getElementById('musicDockExpanded');
            const mainGrid = document.getElementById('musicDockMainGrid');
            const playerCol = document.getElementById('musicDockPlayerCol');
            const mediaHeader = document.getElementById('dockMediaHeader');
            const coverBox = document.getElementById('dockCoverBox');
            const coverEmoji = document.getElementById('dockCoverEmoji');
            const metaWrap = document.getElementById('dockMetaTextWrap');
            const playlistCol = document.getElementById('musicDockPlaylistCol');
            const playlistItems = document.getElementById('dockPlaylistItems');
            const maxBtn = document.getElementById('dockMaxRestoreBtn');
            const eqPanel = document.getElementById('dockEqPanel');

            if (mode === 'minimized') {
                widget.className = 'fixed bottom-4 right-4 z-50 transition-all duration-200';
                expEl.classList.add('hidden');
                minEl.classList.remove('hidden');
                updateCoverBoxCursor();
                return;
            }

            minEl.classList.add('hidden');
            expEl.classList.remove('hidden');

            if (mode === 'maximized') {
                widget.className = 'fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 transition-all duration-200';
                expEl.className = 'w-full max-w-5xl h-[86vh] bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                mainGrid.className = 'grid grid-cols-1 md:grid-cols-12 flex-1 min-h-0 overflow-hidden';
                playerCol.className = 'md:col-span-6 p-6 flex flex-col justify-between bg-slate-900/90 border-b md:border-b-0 md:border-r border-slate-800 overflow-y-auto gap-3';
                mediaHeader.className = 'flex flex-col items-center text-center gap-3 my-auto';
                coverBox.className = 'w-44 h-44 sm:w-52 sm:h-52 rounded-2xl bg-slate-950 border border-slate-700/80 overflow-hidden flex items-center justify-center shrink-0 shadow-2xl transition-all';
                if (coverEmoji) coverEmoji.className = 'text-7xl';
                metaWrap.className = 'w-full min-w-0 px-2';
                playlistCol.className = 'md:col-span-6 bg-slate-950/80 flex flex-col min-h-0 overflow-hidden';
                playlistItems.className = 'flex-1 overflow-y-auto divide-y divide-slate-800/50 text-xs';
                if (eqPanel) eqPanel.classList.remove('hidden');
                const visCanvas = document.getElementById('dockVisualizerCanvas');
                if (visCanvas) visCanvas.className = 'w-full h-20 block rounded-lg';
                maxBtn.textContent = '❐';
                maxBtn.title = 'Restaurar tamaño normal';
            } else {
                widget.className = 'fixed bottom-4 right-4 z-50 transition-all duration-200';
                expEl.className = 'w-80 sm:w-[410px] bg-slate-900/95 backdrop-blur border border-slate-700/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                mainGrid.className = 'flex flex-col flex-1 min-h-0 overflow-hidden';
                playerCol.className = 'p-4 space-y-3 flex flex-col justify-between shrink-0';
                mediaHeader.className = 'flex items-center gap-3.5';
                coverBox.className = 'w-16 h-16 rounded-xl bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center shrink-0 shadow-inner transition-all';
                if (coverEmoji) coverEmoji.className = 'text-3xl';
                metaWrap.className = 'min-w-0 flex-1';
                playlistCol.className = 'border-t border-slate-800 bg-slate-950/75 flex flex-col min-h-0';
                playlistItems.className = 'max-h-40 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                const visCanvas = document.getElementById('dockVisualizerCanvas');
                if (visCanvas) visCanvas.className = 'w-full h-12 sm:h-14 block rounded-lg';
                maxBtn.textContent = '⛶';
                maxBtn.title = 'Maximizar reproductor';
            }
            updateCoverBoxCursor();
        }

        function toggleMusicDockMaximize() {
            setMusicDockMode(musicDockMode === 'maximized' ? 'normal' : 'maximized');
        }

        function closeMusicDock() {
            closeCoverLightbox();
            const audio = document.getElementById('dockAudioElement');
            audio.pause();
            audio.src = '';
            if (visualizerRafId) {
                cancelAnimationFrame(visualizerRafId);
                visualizerRafId = null;
            }
            document.getElementById('musicDockWidget').classList.add('hidden');
        }

        function rebuildMusicPlayOrder(keepCurrentOriginalIdx) {
            const total = musicPlaylist.length;
            const indices = Array.from({ length: total }, (_, i) => i);
            if (!musicShuffle || total <= 1) {
                musicPlayOrder = indices;
                musicOrderPos = Math.max(0, keepCurrentOriginalIdx);
                document.getElementById('dockPlaylistOrderTag').textContent = 'Orden normal';
                return;
            }
            const others = indices.filter(i => i !== keepCurrentOriginalIdx);
            for (let i = others.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [others[i], others[j]] = [others[j], others[i]];
            }
            musicPlayOrder = keepCurrentOriginalIdx >= 0 ? [keepCurrentOriginalIdx, ...others] : others;
            musicOrderPos = 0;
            document.getElementById('dockPlaylistOrderTag').textContent = '🔀 Orden aleatorio';
        }

        function toggleDockShuffle() {
            musicShuffle = !musicShuffle;
            const btn = document.getElementById('dockShuffleBtn');
            btn.className = musicShuffle
                ? 'px-2.5 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-xs text-indigo-300 transition flex items-center gap-1'
                : 'px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-400 transition flex items-center gap-1';

            const currentOrigIdx = musicPlayOrder[musicOrderPos] ?? 0;
            rebuildMusicPlayOrder(currentOrigIdx);
            document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
            renderMusicPlaylist();
            prefetchNextRemoteTrack();
        }

        function playDockAtOrderPos(orderPos) {
            if (orderPos < 0 || orderPos >= musicPlayOrder.length) return;
            musicOrderPos = orderPos;
            const origIdx = musicPlayOrder[musicOrderPos];
            openMusicDockPlayer(musicSource, musicPlaylist[origIdx], true);
        }

        function renderMusicPlaylist() {
            const container = document.getElementById('dockPlaylistItems');
            if (!container) return;
            if (musicPlayOrder.length === 0) {
                container.innerHTML = '<div class="px-3 py-4 text-center text-slate-500">Sin canciones en la cola</div>';
                return;
            }
            container.innerHTML = musicPlayOrder.map((origIdx, pos) => {
                const item = musicPlaylist[origIdx];
                const isCurrent = pos === musicOrderPos;
                const displayTitle = item.tagTitle || (item.name || '').replace(/\\.[^/.]+$/, '');
                const displayArtist = item.tagArtist || '';
                const isCached = musicSource === 'remote' && prefetchedRemoteIds.has(String(item.id));
                return `
                    <div onclick="playDockAtOrderPos(${pos})"
                         class="px-3.5 py-2 flex items-center justify-between gap-2 cursor-pointer transition ${isCurrent ? 'bg-indigo-600/25 text-white font-semibold' : 'text-slate-300 hover:bg-slate-800/70'}">
                        <div class="flex items-center gap-2 min-w-0">
                            <span class="w-4 text-[10px] font-mono ${isCurrent ? 'text-indigo-300' : 'text-slate-500'} shrink-0">${isCurrent ? '▶' : (pos + 1)}</span>
                            <div class="min-w-0">
                                <div class="truncate">${displayTitle}</div>
                                ${displayArtist ? `<div class="text-[10px] text-slate-400 truncate">${displayArtist}</div>` : ''}
                            </div>
                        </div>
                        <div class="flex items-center gap-1.5 shrink-0">
                            ${isCached ? '<span class="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/25">⚡ Caché</span>' : ''}
                        </div>
                    </div>`;
            }).join('');
        }

        async function prefetchNextRemoteTrack() {
            const badge = document.getElementById('dockPrefetchBadge');
            if (musicSource !== 'remote' || musicPlayOrder.length <= 1) {
                if (badge) badge.classList.add('hidden');
                return;
            }
            const nextOrderPos = (musicOrderPos + 1) % musicPlayOrder.length;
            const nextTrack = musicPlaylist[musicPlayOrder[nextOrderPos]];
            if (!nextTrack || !nextTrack.id) return;

            const rid = String(nextTrack.id);
            if (prefetchedRemoteIds.has(rid)) {
                if (badge) badge.classList.remove('hidden');
                renderMusicPlaylist();
                return;
            }
            if (badge) badge.classList.add('hidden');
            try {
                const res = await fetch(`/api/file/prefetch?source=remote&remote_id=${encodeURIComponent(rid)}&name=${encodeURIComponent(nextTrack.name || 'audio.mp3')}`);
                const data = await res.json();
                if (data.prefetched) {
                    prefetchedRemoteIds.add(rid);
                    if (badge) badge.classList.remove('hidden');
                    renderMusicPlaylist();
                }
            } catch (e) {}
        }

        function setDockVolume(val) {
            const num = Math.max(0, Math.min(100, parseInt(val, 10) || 0));
            const audio = document.getElementById('dockAudioElement');
            audio.volume = num / 100;
            audio.muted = (num === 0);
            if (num > 0) {
                lastNonZeroVolume = num;
            }
            document.getElementById('dockVolumeBar').value = num;
            document.getElementById('dockVolumeLabel').textContent = `${num}%`;
            document.getElementById('dockMuteBtn').textContent = num === 0 ? '🔇' : (num < 45 ? '🔉' : '🔊');
            try { localStorage.setItem('fj_player_vol', String(num)); } catch (e) {}
        }

        function toggleDockMute() {
            const audio = document.getElementById('dockAudioElement');
            if (audio.muted || audio.volume === 0) {
                setDockVolume(lastNonZeroVolume || 85);
            } else {
                setDockVolume(0);
            }
        }

        function ensureWebAudioEqualizer() {
            if (eqInitialized) {
                if (audioCtx && audioCtx.state === 'suspended') {
                    audioCtx.resume().catch(() => {});
                }
                return;
            }
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) return;
            try {
                const audioEl = document.getElementById('dockAudioElement');
                audioCtx = new AudioContextClass();
                const sourceNode = audioCtx.createMediaElementSource(audioEl);
                eqFilters = EQ_BANDS_CONFIG.map((cfg, idx) => {
                    const filter = audioCtx.createBiquadFilter();
                    filter.type = cfg.type;
                    filter.frequency.value = cfg.freq;
                    filter.Q.value = 1.1;
                    const slider = document.getElementById(`eqBand${idx}`);
                    filter.gain.value = slider ? parseFloat(slider.value) : 0;
                    return filter;
                });
                sourceNode.connect(eqFilters[0]);
                for (let i = 0; i < eqFilters.length - 1; i++) {
                    eqFilters[i].connect(eqFilters[i + 1]);
                }
                analyserNode = audioCtx.createAnalyser();
                analyserNode.fftSize = 128;
                analyserNode.smoothingTimeConstant = 0.82;
                eqFilters[eqFilters.length - 1].connect(analyserNode);
                analyserNode.connect(audioCtx.destination);
                eqInitialized = true;
                startVisualizerLoop();
            } catch (e) {}
        }

        function toggleDockVisualizer() {
            visualizerEnabled = !visualizerEnabled;
            const wrap = document.getElementById('dockVisualizerWrap');
            const btn = document.getElementById('dockVisToggleBtn');
            if (visualizerEnabled) {
                wrap.classList.remove('hidden');
                btn.className = 'px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-[10px] font-medium text-indigo-300 transition flex items-center gap-1';
                startVisualizerLoop();
            } else {
                wrap.classList.add('hidden');
                btn.className = 'px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-[10px] font-medium text-slate-400 transition flex items-center gap-1';
                if (visualizerRafId) {
                    cancelAnimationFrame(visualizerRafId);
                    visualizerRafId = null;
                }
            }
            try { localStorage.setItem('fj_player_vis', visualizerEnabled ? '1' : '0'); } catch (e) {}
        }

        function cycleVisualizerStyle() {
            const order = ['bars', 'mirror', 'wave'];
            const labels = { bars: 'Barras', mirror: 'Espejo', wave: 'Onda' };
            visualizerStyle = order[(order.indexOf(visualizerStyle) + 1) % order.length];
            const styleBtn = document.getElementById('dockVisStyleBtn');
            if (styleBtn) styleBtn.textContent = labels[visualizerStyle];
            try { localStorage.setItem('fj_player_vis_style', visualizerStyle); } catch (e) {}
        }

        function startVisualizerLoop() {
            if (visualizerRafId) cancelAnimationFrame(visualizerRafId);
            const canvas = document.getElementById('dockVisualizerCanvas');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');

            function renderFrame() {
                if (!visualizerEnabled) {
                    visualizerRafId = null;
                    return;
                }
                visualizerRafId = requestAnimationFrame(renderFrame);

                const rect = canvas.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 && (canvas.width !== Math.floor(rect.width) || canvas.height !== Math.floor(rect.height))) {
                    canvas.width = Math.floor(rect.width);
                    canvas.height = Math.floor(rect.height);
                }
                const w = canvas.width;
                const h = canvas.height;
                ctx.clearRect(0, 0, w, h);

                if (!analyserNode) {
                    ctx.strokeStyle = 'rgba(99, 102, 241, 0.25)';
                    ctx.lineWidth = 1.5;
                    ctx.beginPath();
                    ctx.moveTo(0, h / 2);
                    ctx.lineTo(w, h / 2);
                    ctx.stroke();
                    return;
                }

                const grad = ctx.createLinearGradient(0, h, w, 0);
                grad.addColorStop(0, '#38bdf8');
                grad.addColorStop(0.5, '#818cf8');
                grad.addColorStop(1, '#c084fc');

                if (visualizerStyle === 'wave') {
                    const timeData = new Uint8Array(analyserNode.fftSize);
                    analyserNode.getByteTimeDomainData(timeData);
                    ctx.lineWidth = 2;
                    ctx.strokeStyle = grad;
                    ctx.beginPath();
                    const sliceW = w / timeData.length;
                    let x = 0;
                    for (let i = 0; i < timeData.length; i++) {
                        const v = timeData[i] / 128.0;
                        const y = (v * h) / 2;
                        if (i === 0) ctx.moveTo(x, y);
                        else ctx.lineTo(x, y);
                        x += sliceW;
                    }
                    ctx.lineTo(w, h / 2);
                    ctx.stroke();
                    return;
                }

                const bufferLen = analyserNode.frequencyBinCount;
                const freqData = new Uint8Array(bufferLen);
                analyserNode.getByteFrequencyData(freqData);
                const usableBins = Math.min(44, bufferLen);
                const gap = 2;
                const barW = Math.max(2, (w - gap * (usableBins - 1)) / usableBins);

                ctx.fillStyle = grad;
                for (let i = 0; i < usableBins; i++) {
                    const val = freqData[i] / 255;
                    const barH = Math.max(2, val * (h - 4));
                    const x = i * (barW + gap);
                    if (visualizerStyle === 'mirror') {
                        const y = (h - barH) / 2;
                        ctx.beginPath();
                        if (ctx.roundRect) ctx.roundRect(x, y, barW, barH, 2);
                        else ctx.rect(x, y, barW, barH);
                        ctx.fill();
                    } else {
                        const y = h - barH;
                        ctx.beginPath();
                        if (ctx.roundRect) ctx.roundRect(x, y, barW, barH, [2, 2, 0, 0]);
                        else ctx.rect(x, y, barW, barH);
                        ctx.fill();
                    }
                }
            }
            renderFrame();
        }

        function toggleDockEqPanel() {
            const panel = document.getElementById('dockEqPanel');
            const btn = document.getElementById('dockEqToggleBtn');
            const isHidden = panel.classList.toggle('hidden');
            btn.className = isHidden
                ? 'px-2.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-[10px] font-medium text-slate-300 transition flex items-center gap-1'
                : 'px-2.5 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-[10px] font-medium text-indigo-300 transition flex items-center gap-1';
        }

        function setEqBandGain(bandIdx, dbVal, markCustom = true) {
            const db = Math.max(-12, Math.min(12, parseFloat(dbVal) || 0));
            const slider = document.getElementById(`eqBand${bandIdx}`);
            const label = document.getElementById(`eqVal${bandIdx}`);
            if (slider) slider.value = db;
            if (label) label.textContent = `${db > 0 ? '+' : ''}${db}dB`;
            if (eqFilters[bandIdx]) {
                eqFilters[bandIdx].gain.value = db;
            }
            if (markCustom) {
                const presetSel = document.getElementById('dockEqPreset');
                if (presetSel && !document.getElementById(`eqPresetCustomOpt`)) {
                    const opt = document.createElement('option');
                    opt.id = 'eqPresetCustomOpt';
                    opt.value = 'custom';
                    opt.textContent = 'Personalizado';
                    presetSel.appendChild(opt);
                }
                if (presetSel) presetSel.value = 'custom';
            }
            try {
                const currentGains = [0, 1, 2, 3, 4].map(i => parseFloat(document.getElementById(`eqBand${i}`).value) || 0);
                localStorage.setItem('fj_player_eq', JSON.stringify(currentGains));
            } catch (e) {}
        }

        function applyEqPreset(presetName) {
            const gains = EQ_PRESETS[presetName];
            if (!gains) return;
            gains.forEach((db, idx) => setEqBandGain(idx, db, false));
            const presetSel = document.getElementById('dockEqPreset');
            if (presetSel) presetSel.value = presetName;
        }

        function toggleDockPlayPause() {
            const audio = document.getElementById('dockAudioElement');
            if (!audio.src) return;
            ensureWebAudioEqualizer();
            if (audio.paused) {
                audio.play();
            } else {
                audio.pause();
            }
        }

        function seekDockAudio(val) {
            const audio = document.getElementById('dockAudioElement');
            if (audio.duration && isFinite(audio.duration)) {
                audio.currentTime = (parseFloat(val) / 100) * audio.duration;
            }
        }

        function playDockPrev() {
            if (musicPlayOrder.length === 0) return;
            const prevOrderPos = (musicOrderPos - 1 + musicPlayOrder.length) % musicPlayOrder.length;
            playDockAtOrderPos(prevOrderPos);
        }

        function playDockNext() {
            if (musicPlayOrder.length === 0) return;
            const nextOrderPos = (musicOrderPos + 1) % musicPlayOrder.length;
            playDockAtOrderPos(nextOrderPos);
        }

        async function openMusicDockPlayer(source, fileMeta, fromPlaylistNav = false) {
            const name = fileMeta.name || 'audio.mp3';
            const taskName = document.getElementById('taskSelector').value || '';

            if (!fromPlaylistNav) {
                musicSource = source;
                const folderFiles = getCurrentFolderFiles(source);
                musicPlaylist = folderFiles.filter(f => {
                    const fExt = (f.name || '').includes('.') ? f.name.split('.').pop().toLowerCase() : '';
                    return AUDIO_EXTS.includes(fExt);
                });
                let origIdx = musicPlaylist.findIndex(f =>
                    source === 'local' ? f.rel_path === fileMeta.rel_path : String(f.id) === String(fileMeta.id)
                );
                if (origIdx === -1) {
                    musicPlaylist = [fileMeta];
                    origIdx = 0;
                }
                rebuildMusicPlayOrder(origIdx);
            }

            const queryParams = source === 'local'
                ? (fileMeta.full_path
                    ? `source=local&full_path=${encodeURIComponent(fileMeta.full_path)}`
                    : `source=local&task=${encodeURIComponent(taskName)}&rel_path=${encodeURIComponent(fileMeta.rel_path)}`)
                : `source=remote&remote_id=${encodeURIComponent(fileMeta.id)}&name=${encodeURIComponent(name)}`;

            const streamUrl = `/api/file/stream?${queryParams}`;
            const metaUrl = `/api/file/audio_meta?${queryParams}`;

            // Mostrar widget
            const widget = document.getElementById('musicDockWidget');
            widget.classList.remove('hidden');
            setMusicDockMode(musicDockMode);

            // Resetear UI temporalmente con el nombre de archivo mientras llegan los tags ID3
            const fallbackTitle = name.replace(/\\.[^/.]+$/, '');
            currentCoverDataUrl = null;
            currentCoverFilename = `${fallbackTitle.replace(/[^a-zA-Z0-9_-]/g, '_') || 'caratula'}.jpg`;
            closeCoverLightbox();
            updateCoverBoxCursor();

            document.getElementById('dockTrackTitle').textContent = fallbackTitle;
            document.getElementById('dockTrackTitle').title = fallbackTitle;
            document.getElementById('dockTrackArtist').textContent = 'Leyendo etiquetas ID3...';
            document.getElementById('dockTrackAlbum').textContent = '';
            document.getElementById('miniTrackTitle').textContent = fallbackTitle;
            document.getElementById('miniTrackArtist').textContent = 'Cargando...';
            document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
            document.getElementById('dockSourceBadge').textContent = source === 'remote' ? '☁️ FileJump' : '💾 Local';
            const emojiSizeClass = musicDockMode === 'maximized' ? 'text-7xl' : 'text-3xl';
            document.getElementById('dockCoverBox').innerHTML = `<span id="dockCoverEmoji" class="${emojiSizeClass}">🎵</span>`;
            document.getElementById('miniCoverWrap').innerHTML = '<span class="text-base">🎵</span>';

            renderMusicPlaylist();
            // Iniciar precarga en caché de la siguiente pista remota
            prefetchNextRemoteTrack();

            ensureWebAudioEqualizer();
            const audio = document.getElementById('dockAudioElement');
            audio.src = streamUrl;
            audio.play().catch(() => {});

            // Consultar metadatos ID3 y carátula incrustada
            try {
                const res = await fetch(metaUrl);
                if (res.ok) {
                    const meta = await res.json();
                    const title = meta.title || fallbackTitle;
                    const artist = meta.artist || 'Artista desconocido';
                    const album = meta.album || '';

                    // Guardar metadatos en el objeto del playlist para mostrarlos en la lista
                    const currentOrigIdx = musicPlayOrder[musicOrderPos];
                    if (musicPlaylist[currentOrigIdx]) {
                        musicPlaylist[currentOrigIdx].tagTitle = title;
                        musicPlaylist[currentOrigIdx].tagArtist = artist;
                    }
                    if (source === 'remote' && fileMeta.id) {
                        prefetchedRemoteIds.add(String(fileMeta.id));
                    }

                    document.getElementById('dockTrackTitle').textContent = title;
                    document.getElementById('dockTrackTitle').title = title;
                    document.getElementById('dockTrackArtist').textContent = artist;
                    document.getElementById('dockTrackAlbum').textContent = album;
                    document.getElementById('miniTrackTitle').textContent = title;
                    document.getElementById('miniTrackArtist').textContent = artist;

                    if (meta.cover_data_url) {
                        currentCoverDataUrl = meta.cover_data_url;
                        const mimeSubtype = (meta.cover_data_url.split(';')[0].split('/')[1] || 'jpeg').toLowerCase();
                        const mimeExt = mimeSubtype === 'jpeg' ? 'jpg' : mimeSubtype.replace(/[^a-z0-9]/g, '') || 'jpg';
                        currentCoverFilename = `${title.replace(/[^a-zA-Z0-9_-]/g, '_') || 'caratula'}.${mimeExt}`;
                        document.getElementById('dockCoverBox').innerHTML = `<img src="${meta.cover_data_url}" alt="Carátula" class="w-full h-full object-cover">`;
                        document.getElementById('miniCoverWrap').innerHTML = `<img src="${meta.cover_data_url}" alt="Cover" class="w-full h-full object-cover">`;
                        updateCoverBoxCursor();
                    }
                    renderMusicPlaylist();
                }
            } catch (e) {
                document.getElementById('dockTrackArtist').textContent = source === 'remote' ? 'Audio en nube' : 'Audio local';
                document.getElementById('miniTrackArtist').textContent = 'En reproducción';
            }
        }

        // Eventos del elemento <audio> flotante y restauración de volumen / EQ
        (function initDockAudioEvents() {
            const audio = document.getElementById('dockAudioElement');
            try {
                const savedVol = localStorage.getItem('fj_player_vol');
                setDockVolume(savedVol !== null ? parseInt(savedVol, 10) : 85);
                const savedEq = JSON.parse(localStorage.getItem('fj_player_eq') || 'null');
                if (Array.isArray(savedEq) && savedEq.length === 5) {
                    savedEq.forEach((db, idx) => setEqBandGain(idx, db, true));
                }
                const savedVis = localStorage.getItem('fj_player_vis');
                if (savedVis === '0') {
                    toggleDockVisualizer();
                }
                const savedVisStyle = localStorage.getItem('fj_player_vis_style');
                if (savedVisStyle && ['bars', 'mirror', 'wave'].includes(savedVisStyle)) {
                    visualizerStyle = savedVisStyle;
                    const labels = { bars: 'Barras', mirror: 'Espejo', wave: 'Onda' };
                    const styleBtn = document.getElementById('dockVisStyleBtn');
                    if (styleBtn) styleBtn.textContent = labels[visualizerStyle];
                }
            } catch (e) {
                setDockVolume(85);
            }
            audio.addEventListener('play', () => {
                ensureWebAudioEqualizer();
                if (visualizerEnabled) startVisualizerLoop();
                document.getElementById('dockPlayPauseBtn').textContent = '⏸';
                document.getElementById('miniPlayPauseBtn').textContent = '⏸';
            });
            audio.addEventListener('pause', () => {
                document.getElementById('dockPlayPauseBtn').textContent = '▶';
                document.getElementById('miniPlayPauseBtn').textContent = '▶';
            });
            audio.addEventListener('timeupdate', () => {
                document.getElementById('dockCurrentTime').textContent = formatAudioTime(audio.currentTime);
                document.getElementById('dockTotalTime').textContent = formatAudioTime(audio.duration);
                if (audio.duration && isFinite(audio.duration)) {
                    document.getElementById('dockSeekBar').value = (audio.currentTime / audio.duration) * 100;
                }
            });
            audio.addEventListener('ended', () => {
                playDockNext();
            });
        })();

        // =========================================================================
        // EXPLORADOR LOCAL ESTILO WINDOWS (ÁRBOL LAZY CON + Y CONTENIDO DERECHO)
        // =========================================================================
        function initLocalExplorer(rootItems, driveRootOverride = '') {
            localActiveDriveRoot = driveRootOverride || '';
            localNodes.clear();
            localSelectedPath = '';
            const rootName = localActiveDriveRoot || 'Raíz';
            localNodes.set('', {
                path: '',
                name: rootName,
                expanded: true,
                loaded: true,
                loading: false,
                has_children: rootItems.some(i => i.type === 'folder'),
                items: rootItems
            });
            rootItems.filter(i => i.type === 'folder').forEach(fld => {
                localNodes.set(fld.rel_path, {
                    path: fld.rel_path,
                    name: fld.name,
                    expanded: false,
                    loaded: false,
                    loading: false,
                    has_children: fld.has_children !== false,
                    items: []
                });
            });
            renderLocalTree();
            renderLocalContentsPanel();
        }

        async function toggleLocalTreeNode(relPath) {
            const node = localNodes.get(relPath);
            if (!node) return;
            if (node.expanded) {
                node.expanded = false;
                renderLocalTree();
                return;
            }
            node.expanded = true;
            if (!node.loaded) {
                await fetchLocalNodeChildren(relPath);
            } else {
                renderLocalTree();
            }
        }

        async function selectLocalFolder(relPath) {
            localSelectedPath = relPath;
            let node = localNodes.get(relPath);
            if (!node) {
                node = {
                    path: relPath,
                    name: relPath.split('/').pop() || (localActiveDriveRoot || 'Raíz'),
                    expanded: false,
                    loaded: false,
                    loading: false,
                    has_children: true,
                    items: []
                };
                localNodes.set(relPath, node);
            }
            renderLocalTree();
            if (!node.loaded) {
                await fetchLocalNodeChildren(relPath);
            } else {
                renderLocalContentsPanel();
            }
        }

        async function fetchLocalNodeChildren(relPath) {
            const taskName = document.getElementById('taskSelector').value;
            const node = localNodes.get(relPath);
            if (!node || (!taskName && !localActiveDriveRoot)) return;
            node.loading = true;
            renderLocalTree();
            try {
                const baseParam = localActiveDriveRoot
                    ? `root_path=${encodeURIComponent(localActiveDriveRoot)}`
                    : `task=${encodeURIComponent(taskName)}`;
                const res = await fetch(`/api/local/children?${baseParam}&prefix=${encodeURIComponent(relPath)}`);
                const data = await res.json();
                const items = data.items || [];
                node.items = items;
                node.loaded = true;
                node.loading = false;
                node.has_children = items.some(i => i.type === 'folder');
                items.filter(i => i.type === 'folder').forEach(fld => {
                    if (!localNodes.has(fld.rel_path)) {
                        localNodes.set(fld.rel_path, {
                            path: fld.rel_path,
                            name: fld.name,
                            expanded: false,
                            loaded: false,
                            loading: false,
                            has_children: fld.has_children !== false,
                            items: []
                        });
                    }
                });
            } catch (e) {
                node.loading = false;
            }
            renderLocalTree();
            if (localSelectedPath === relPath) {
                renderLocalContentsPanel();
            }
        }

        function renderLocalTree() {
            const container = document.getElementById('localFolderTree');
            const rootNode = localNodes.get('');
            if (!rootNode) {
                container.innerHTML = '<div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>';
                return;
            }

            function buildHtml(relPath, depth) {
                const node = localNodes.get(relPath);
                if (!node) return '';
                const isSelected = localSelectedPath === relPath;
                const indentPx = depth * 12;
                const btnSymbol = node.loading ? '…' : (node.expanded ? '−' : '+');
                const toggleBtn = node.has_children
                    ? `<button onclick="event.stopPropagation(); toggleLocalTreeNode('${relPath.replace(/'/g, "\\'")}')" class="w-4 h-4 rounded bg-slate-800 hover:bg-sky-600/40 text-slate-300 border border-slate-700 flex items-center justify-center text-[10px] font-bold shrink-0">${btnSymbol}</button>`
                    : `<span class="w-4 inline-block shrink-0"></span>`;

                let html = `
                    <div onclick="selectLocalFolder('${relPath.replace(/'/g, "\\'")}')"
                         style="padding-left: ${indentPx}px"
                         class="flex items-center gap-1.5 py-1 px-1.5 rounded-lg cursor-pointer transition ${isSelected ? 'bg-sky-600/25 text-sky-300 font-semibold border border-sky-500/30' : 'text-slate-300 hover:bg-slate-800/70'}">
                        ${toggleBtn}
                        <span class="shrink-0">${relPath === '' ? '🏠' : '📁'}</span>
                        <span class="truncate">${node.name}</span>
                    </div>`;

                if (node.expanded && node.loaded) {
                    const subfolders = (node.items || []).filter(i => i.type === 'folder');
                    subfolders.forEach(sf => {
                        html += buildHtml(sf.rel_path, depth + 1);
                    });
                }
                return html;
            }

            container.innerHTML = buildHtml('', 0);
        }

        function renderLocalContentsPanel() {
            const tbody = document.getElementById('localTableBody');
            const badge = document.getElementById('localCountBadge');
            const breadcrumbBar = document.getElementById('localBreadcrumbBar');

            const parts = localSelectedPath ? localSelectedPath.split('/') : [];
            const parentPath = parts.length > 0 ? parts.slice(0, -1).join('/') : null;
            const rootLabel = localActiveDriveRoot || 'Raíz';

            let bcHtml = '';
            if (parentPath !== null) {
                bcHtml += `<button onclick="selectLocalFolder('${parentPath.replace(/'/g, "\\'")}')" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium shrink-0">⬅ Subir</button>`;
            }
            bcHtml += `<button onclick="selectLocalFolder('')" class="hover:text-sky-400 font-medium shrink-0">🏠 ${rootLabel}</button>`;
            let acc = '';
            parts.forEach((p, idx) => {
                acc = acc ? `${acc}/${p}` : p;
                const isLast = idx === parts.length - 1;
                bcHtml += `<span class="text-slate-600">/</span><button onclick="selectLocalFolder('${acc.replace(/'/g, "\\'")}')" class="${isLast ? 'text-white font-semibold' : 'hover:text-sky-400'} truncate max-w-[140px]">${p}</button>`;
            });
            breadcrumbBar.innerHTML = bcHtml;

            const node = localNodes.get(localSelectedPath);
            const items = node ? (node.items || []) : [];
            badge.textContent = `${items.length} elementos`;

            if (items.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Esta carpeta está vacía</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(item => {
                const isFolder = item.type === 'folder';
                const safeRel = item.rel_path.replace(/'/g, "\\'");
                const encodedFull = encodeURIComponent(item.full_path || '');
                const encodedName = encodeURIComponent(item.name || '');
                return `
                <tr ${isFolder ? `ondblclick="selectLocalFolder('${safeRel}')"` : `ondblclick="openFileViewer('local', {name: decodeURIComponent('${encodedName}'), rel_path: '${safeRel}', full_path: decodeURIComponent('${encodedFull}')})"`}
                    class="hover:bg-slate-800/50 transition ${isFolder ? 'cursor-pointer' : 'cursor-pointer'}">
                    <td class="py-2 px-2.5 font-medium text-slate-200 max-w-[200px]">
                        <div class="flex items-center gap-1.5 truncate">
                            <span class="shrink-0">${isFolder ? '📁' : '📄'}</span>
                            <span class="truncate" title="${item.rel_path}">${item.name}</span>
                        </div>
                    </td>
                    <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${isFolder ? '-' : formatBytes(item.size)}</td>
                    <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${item.mtime_str}</td>
                    <td class="py-2 px-2.5 text-right whitespace-nowrap">
                        <div class="inline-flex items-center gap-1.5">
                            ${!isFolder ? `<button onclick="event.stopPropagation(); openFileViewer('local', {name: decodeURIComponent('${encodedName}'), rel_path: '${safeRel}', full_path: decodeURIComponent('${encodedFull}')})" class="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-sky-600/30 text-sky-300 text-[10px]" title="Abrir / Vista previa">👁️</button>` : ''}
                            ${getStatusBadge(item.sync_status)}
                        </div>
                    </td>
                </tr>`;
            }).join('');
        }

        // =========================================================================
        // EXPLORADOR REMOTO ESTILO WINDOWS (ÁRBOL LAZY CON + Y CONTENIDO DERECHO)
        // =========================================================================
        function initRemoteExplorer(rootId, rootItems) {
            remoteBaseRootId = String(rootId || 'ROOT');
            remoteNodes.clear();
            remoteSelectedKey = remoteBaseRootId;

            const selector = document.getElementById('remoteDirSelector');
            const selectedOpt = selector.options[selector.selectedIndex];
            const rootLabel = (remoteBaseRootId === 'ROOT')
                ? '/ (Raíz de FileJump)'
                : (selectedOpt && selectedOpt.value === remoteBaseRootId ? selectedOpt.textContent : `Carpeta #${remoteBaseRootId}`);

            remoteNodes.set(remoteBaseRootId, {
                key: remoteBaseRootId,
                id: remoteBaseRootId,
                parentKey: null,
                name: rootLabel,
                prefix: '',
                expanded: true,
                loaded: true,
                loading: false,
                has_children: rootItems.some(i => i.type === 'folder'),
                items: rootItems
            });

            rootItems.filter(i => i.type === 'folder').forEach(fld => {
                const k = String(fld.id);
                remoteNodes.set(k, {
                    key: k,
                    id: fld.id,
                    parentKey: remoteBaseRootId,
                    name: fld.name,
                    prefix: fld.rel_path,
                    expanded: false,
                    loaded: false,
                    loading: false,
                    has_children: true,
                    items: []
                });
            });

            renderRemoteTree();
            renderRemoteContentsPanel();
        }

        async function toggleRemoteTreeNode(nodeKey) {
            const node = remoteNodes.get(String(nodeKey));
            if (!node) return;
            if (node.expanded) {
                node.expanded = false;
                renderRemoteTree();
                return;
            }
            node.expanded = true;
            if (!node.loaded) {
                await fetchRemoteNodeChildren(String(nodeKey));
            } else {
                renderRemoteTree();
            }
        }

        async function selectRemoteNode(nodeKey) {
            const key = String(nodeKey);
            remoteSelectedKey = key;
            const node = remoteNodes.get(key);
            if (!node) return;
            renderRemoteTree();
            if (!node.loaded) {
                await fetchRemoteNodeChildren(key);
            } else {
                renderRemoteContentsPanel();
            }
        }

        async function fetchRemoteNodeChildren(nodeKey) {
            const node = remoteNodes.get(String(nodeKey));
            if (!node) return;
            const taskName = document.getElementById('taskSelector').value || '';
            node.loading = true;
            renderRemoteTree();
            try {
                const url = `/api/remote/children?parent_id=${encodeURIComponent(node.id)}&prefix=${encodeURIComponent(node.prefix)}&task=${encodeURIComponent(taskName)}`;
                const res = await fetch(url);
                const data = await res.json();
                const items = data.items || [];
                node.items = items;
                node.loaded = true;
                node.loading = false;
                node.has_children = items.some(i => i.type === 'folder');

                items.filter(i => i.type === 'folder').forEach(fld => {
                    const childKey = String(fld.id);
                    if (!remoteNodes.has(childKey)) {
                        remoteNodes.set(childKey, {
                            key: childKey,
                            id: fld.id,
                            parentKey: String(nodeKey),
                            name: fld.name,
                            prefix: fld.rel_path,
                            expanded: false,
                            loaded: false,
                            loading: false,
                            has_children: true,
                            items: []
                        });
                    }
                });
            } catch (e) {
                node.loading = false;
            }
            renderRemoteTree();
            if (remoteSelectedKey === String(nodeKey)) {
                renderRemoteContentsPanel();
            }
        }

        function renderRemoteTree() {
            const container = document.getElementById('remoteFolderTree');
            const rootNode = remoteNodes.get(remoteBaseRootId);
            if (!rootNode) {
                container.innerHTML = '<div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>';
                return;
            }

            function buildHtml(nodeKey, depth) {
                const node = remoteNodes.get(String(nodeKey));
                if (!node) return '';
                const isSelected = remoteSelectedKey === String(nodeKey);
                const indentPx = depth * 12;
                const btnSymbol = node.loading ? '…' : (node.expanded ? '−' : '+');
                const toggleBtn = node.has_children
                    ? `<button onclick="event.stopPropagation(); toggleRemoteTreeNode('${node.key}')" class="w-4 h-4 rounded bg-slate-800 hover:bg-indigo-600/40 text-slate-300 border border-slate-700 flex items-center justify-center text-[10px] font-bold shrink-0">${btnSymbol}</button>`
                    : `<span class="w-4 inline-block shrink-0"></span>`;

                let html = `
                    <div onclick="selectRemoteNode('${node.key}')"
                         style="padding-left: ${indentPx}px"
                         class="flex items-center gap-1.5 py-1 px-1.5 rounded-lg cursor-pointer transition ${isSelected ? 'bg-indigo-600/25 text-indigo-300 font-semibold border border-indigo-500/30' : 'text-slate-300 hover:bg-slate-800/70'}">
                        ${toggleBtn}
                        <span class="shrink-0">${node.key === remoteBaseRootId ? '☁️' : '📁'}</span>
                        <span class="truncate">${node.name}</span>
                    </div>`;

                if (node.expanded && node.loaded) {
                    const subfolders = (node.items || []).filter(i => i.type === 'folder');
                    subfolders.forEach(sf => {
                        html += buildHtml(String(sf.id), depth + 1);
                    });
                }
                return html;
            }

            container.innerHTML = buildHtml(remoteBaseRootId, 0);
        }

        function renderRemoteContentsPanel() {
            const tbody = document.getElementById('remoteTableBody');
            const badge = document.getElementById('remoteCountBadge');
            const breadcrumbBar = document.getElementById('remoteBreadcrumbBar');

            const trail = [];
            let cursor = remoteNodes.get(remoteSelectedKey);
            while (cursor) {
                trail.unshift(cursor);
                cursor = cursor.parentKey ? remoteNodes.get(cursor.parentKey) : null;
            }

            const currentNode = remoteNodes.get(remoteSelectedKey);
            let bcHtml = '';
            if (currentNode && currentNode.parentKey) {
                bcHtml += `<button onclick="selectRemoteNode('${currentNode.parentKey}')" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium shrink-0">⬅ Subir</button>`;
            }
            trail.forEach((n, idx) => {
                const isLast = idx === trail.length - 1;
                if (idx > 0) bcHtml += `<span class="text-slate-600">/</span>`;
                bcHtml += `<button onclick="selectRemoteNode('${n.key}')" class="${isLast ? 'text-white font-semibold' : 'hover:text-indigo-400'} truncate max-w-[140px]">${idx === 0 ? '☁️ ' : ''}${n.name}</button>`;
            });
            breadcrumbBar.innerHTML = bcHtml || `<span class="text-slate-400">☁️ Raíz</span>`;

            const items = currentNode ? (currentNode.items || []) : [];
            badge.textContent = `${items.length} elementos`;

            if (items.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Esta carpeta remota está vacía</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(item => {
                const isFolder = item.type === 'folder';
                const folderKey = String(item.id);
                const encodedName = encodeURIComponent(item.name || '');
                return `
                <tr ${isFolder ? `ondblclick="selectRemoteNode('${folderKey}')"` : `ondblclick="openFileViewer('remote', {id: '${item.id}', name: decodeURIComponent('${encodedName}')})"`}
                    class="hover:bg-slate-800/50 transition cursor-pointer">
                    <td class="py-2 px-2.5 font-medium text-slate-200 max-w-[200px]">
                        <div class="flex items-center gap-1.5 truncate">
                            <span class="shrink-0">${isFolder ? '📁' : '☁️'}</span>
                            <span class="truncate" title="${item.rel_path}">${item.name}</span>
                        </div>
                    </td>
                    <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${isFolder ? '-' : formatBytes(item.size)}</td>
                    <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${item.mtime_str}</td>
                    <td class="py-2 px-2.5 text-right whitespace-nowrap">
                        <div class="inline-flex items-center gap-1.5">
                            ${!isFolder ? `<button onclick="event.stopPropagation(); openFileViewer('remote', {id: '${item.id}', name: decodeURIComponent('${encodedName}')})" class="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-indigo-600/30 text-indigo-300 text-[10px]" title="Abrir / Vista previa">👁️</button>` : ''}
                            <span class="text-[10px] text-slate-500 font-mono">#${item.id}</span>
                            ${getStatusBadge(item.sync_status)}
                        </div>
                    </td>
                </tr>`;
            }).join('');
        }

        // =========================================================================
        // VISOR MULTIMEDIA Y PREVISUALIZACIÓN CON CARRUSEL DE ARCHIVOS DE CARPETA
        // =========================================================================
        function closePreviewModal() {
            const modal = document.getElementById('previewModal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
            document.getElementById('previewBody').innerHTML = '';
        }

        function getCurrentFolderFiles(source) {
            if (source === 'local') {
                const node = localNodes.get(localSelectedPath);
                return node ? (node.items || []).filter(i => i.type === 'file') : [];
            } else {
                const node = remoteNodes.get(remoteSelectedKey);
                return node ? (node.items || []).filter(i => i.type === 'file') : [];
            }
        }

        function updatePreviewNavControls() {
            const prevBtn = document.getElementById('previewPrevBtn');
            const nextBtn = document.getElementById('previewNextBtn');
            const counterEl = document.getElementById('previewCounter');
            const total = currentPreviewList.length;

            if (total > 1 && currentPreviewIndex >= 0) {
                counterEl.textContent = `(${currentPreviewIndex + 1} de ${total})`;
                prevBtn.disabled = false;
                nextBtn.disabled = false;
            } else {
                counterEl.textContent = total === 1 ? '(1 de 1)' : '';
                prevBtn.disabled = true;
                nextBtn.disabled = true;
            }
        }

        function navigatePreview(delta) {
            if (currentPreviewList.length <= 1 || currentPreviewIndex < 0) return;
            const nextIdx = (currentPreviewIndex + delta + currentPreviewList.length) % currentPreviewList.length;
            currentPreviewIndex = nextIdx;
            openFileViewer(currentPreviewSource, currentPreviewList[nextIdx], true);
        }

        document.addEventListener('keydown', (e) => {
            const coverModal = document.getElementById('coverLightboxModal');
            if (coverModal && !coverModal.classList.contains('hidden')) {
                if (e.key === 'Escape') {
                    e.preventDefault();
                    closeCoverLightbox();
                }
                return;
            }
            const modal = document.getElementById('previewModal');
            if (modal.classList.contains('hidden')) return;
            if (e.key === 'ArrowLeft') {
                e.preventDefault();
                navigatePreview(-1);
            } else if (e.key === 'ArrowRight') {
                e.preventDefault();
                navigatePreview(1);
            } else if (e.key === 'Escape') {
                e.preventDefault();
                closePreviewModal();
            }
        });

        async function openFileViewer(source, fileMeta, fromCarousel = false) {
            const name = fileMeta.name || 'archivo';
            const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
            const taskName = document.getElementById('taskSelector').value || '';

            if (!fromCarousel) {
                currentPreviewSource = source;
                currentPreviewList = getCurrentFolderFiles(source);
                currentPreviewIndex = currentPreviewList.findIndex(f =>
                    source === 'local' ? f.rel_path === fileMeta.rel_path : String(f.id) === String(fileMeta.id)
                );
            }

            // 3. Audio / MP3 -> Reproductor de música flotante minimizable y maximizable con tags ID3
            if (AUDIO_EXTS.includes(ext)) {
                openMusicDockPlayer(source, fileMeta, false);
                return;
            }

            const queryParams = source === 'local'
                ? (fileMeta.full_path
                    ? `source=local&full_path=${encodeURIComponent(fileMeta.full_path)}`
                    : `source=local&task=${encodeURIComponent(taskName)}&rel_path=${encodeURIComponent(fileMeta.rel_path)}`)
                : `source=remote&remote_id=${encodeURIComponent(fileMeta.id)}&name=${encodeURIComponent(name)}`;

            const streamUrl = `/api/file/stream?${queryParams}`;
            const previewUrl = `/api/file/preview?${queryParams}`;

            // 1. PDF -> Descarga en caché temporal (si es remoto) y abre en nueva ventana
            if (ext === 'pdf') {
                window.open(streamUrl, '_blank', 'noopener');
                return;
            }

            const modal = document.getElementById('previewModal');
            const titleEl = document.getElementById('previewTitle');
            const badge = document.getElementById('previewBadge');
            const body = document.getElementById('previewBody');
            const openTabBtn = document.getElementById('previewOpenTabBtn');

            titleEl.textContent = name;
            openTabBtn.href = streamUrl;
            updatePreviewNavControls();
            modal.classList.remove('hidden');
            modal.classList.add('flex');

            const nativeVideoExts = ['mp4', 'webm', 'ogg'];
            const legacyVideoExts = ['avi', 'mkv', 'mov', 'wmv', 'flv'];
            const imageExts = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
            const textExts = ['txt', 'log', 'json', 'py', 'md', 'csv', 'xml', 'html', 'htm', 'css', 'js', 'env', 'ini', 'yaml', 'yml', 'sql'];
            const officeExts = ['docx', 'xlsx', 'pptx', 'odt', 'ods'];

            // 2A. Video compatible nativamente con navegador (.mp4, .webm, .ogg)
            if (nativeVideoExts.includes(ext)) {
                badge.textContent = 'Reproductor de Video';
                body.innerHTML = `
                    <div class="w-full flex flex-col items-center gap-3">
                        <video id="activeVideoPlayer" controls autoplay class="w-full max-h-[70vh] rounded-xl bg-black shadow-lg">
                            <source src="${streamUrl}">
                        </video>
                    </div>`;
                return;
            }

            // 2B. Video contenedor legado (.avi, .mkv, .mov) -> Intenta transcodificar si hay ffmpeg o abre directo en reproductor externo
            if (legacyVideoExts.includes(ext)) {
                badge.textContent = `Video (.${ext.toUpperCase()})`;
                body.innerHTML = `
                    <div class="w-full max-w-lg bg-slate-900 border border-slate-800 rounded-2xl p-6 text-center space-y-4 shadow-xl">
                        <div class="w-16 h-16 mx-auto rounded-2xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center text-3xl">🎬</div>
                        <div>
                            <h4 class="text-sm font-bold text-white">${name}</h4>
                            <p class="text-xs text-slate-400 mt-1.5 leading-relaxed">
                                Los navegadores web no reproducen contenedores <span class="text-amber-300 font-semibold">.${ext.toUpperCase()}</span> directamente sin conversión.
                            </p>
                        </div>
                        <div class="flex flex-wrap items-center justify-center gap-2.5 pt-1">
                            <a href="${streamUrl}" target="_blank" rel="noopener" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white shadow-lg shadow-indigo-600/20 transition">
                                Abrir en otra pestaña / Reproductor ↗
                            </a>
                            <button onclick="tryTranscodeVideo('${streamUrl}')" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs font-medium text-slate-200 transition">
                                Intentar convertir aquí (FFmpeg)
                            </button>
                        </div>
                    </div>`;
                return;
            }

            // 4. Imágenes
            if (imageExts.includes(ext)) {
                badge.textContent = 'Imagen';
                body.innerHTML = `<img src="${streamUrl}" alt="${name}" class="max-w-full max-h-[72vh] rounded-xl object-contain shadow-lg">`;
                return;
            }

            // 5. Texto y Ofimática
            if (textExts.includes(ext) || officeExts.includes(ext)) {
                badge.textContent = officeExts.includes(ext) ? 'Vista Previa Ofimática' : 'Vista Previa de Texto';
                body.innerHTML = '<div class="text-xs text-slate-400 animate-pulse">Cargando vista previa del archivo...</div>';
                try {
                    const res = await fetch(previewUrl);
                    const data = await res.json();
                    if (data.error) {
                        body.innerHTML = `<div class="text-xs text-rose-400">${data.error}</div>`;
                        return;
                    }
                    if (data.kind === 'text') {
                        const escaped = (data.content || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                        body.innerHTML = `<pre class="w-full h-[68vh] overflow-auto bg-slate-950 p-4 rounded-xl border border-slate-800 font-mono text-xs text-slate-200 whitespace-pre-wrap">${escaped}</pre>`;
                    } else if (data.kind === 'office') {
                        body.innerHTML = `<div class="w-full h-[68vh] overflow-auto bg-slate-950 p-5 rounded-xl border border-slate-800 text-slate-200">${data.html}</div>`;
                    } else {
                        body.innerHTML = `<div class="text-xs text-slate-400">No hay vista previa disponible. Usa "Abrir en Nueva Pestaña".</div>`;
                    }
                } catch (e) {
                    body.innerHTML = '<div class="text-xs text-rose-400">Error cargando la vista previa.</div>';
                }
                return;
            }

            // 6. Otros formatos -> Abrir en pestaña nueva
            badge.textContent = 'Archivo';
            body.innerHTML = `
                <div class="text-center space-y-3">
                    <p class="text-xs text-slate-300">Este tipo de archivo (<span class="font-mono text-indigo-300">.${ext || 'bin'}</span>) se abrirá o descargará en una nueva ventana.</p>
                    <a href="${streamUrl}" target="_blank" rel="noopener" class="inline-block px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white">Abrir / Descargar Archivo ↗</a>
                </div>`;
        }

        function tryTranscodeVideo(streamUrl) {
            const body = document.getElementById('previewBody');
            const transcodeUrl = `${streamUrl}&transcode=1`;
            body.innerHTML = `
                <div class="w-full flex flex-col items-center gap-3">
                    <div class="text-xs text-slate-400">Convirtiendo video para vista previa web (requiere FFmpeg instalado)...</div>
                    <video controls autoplay onerror="this.parentElement.innerHTML='<p class=\\'text-xs text-amber-400\\'>FFmpeg no está instalado en el sistema. Usa el botón superior <b>Abrir en Nueva Pestaña ↗</b> para verlo en tu reproductor local.</p>'" class="w-full max-h-[68vh] rounded-xl bg-black shadow-lg">
                        <source src="${transcodeUrl}" type="video/mp4">
                    </video>
                </div>`;
        }

        async function loadInitialConfig() {
            const res = await fetch('/api/settings');
            const data = await res.json();
            currentConfig = data.config;

            const selector = document.getElementById('taskSelector');
            const prevVal = selector.value;
            selector.innerHTML = '<option value="">-- Selecciona una tarea --</option>';
            (currentConfig.tasks || []).forEach(t => {
                const opt = document.createElement('option');
                opt.value = t.name;
                opt.textContent = `${t.name} (${t.path})`;
                selector.appendChild(opt);
            });

            if (prevVal) selector.value = prevVal;
            else if (currentConfig.tasks.length > 0) {
                selector.value = currentConfig.tasks[0].name;
                inspectSelectedTask();
            }
        }

        async function inspectSelectedTask(overrideRemoteId = '') {
            const taskName = document.getElementById('taskSelector').value;
            const remoteSelector = document.getElementById('remoteDirSelector');
            const effectiveRemoteOverride = overrideRemoteId || remoteSelector.value;

            const localBody = document.getElementById('localTableBody');
            const remoteBody = document.getElementById('remoteTableBody');

            // Si no hay tarea local seleccionada, pero sí un directorio remoto elegido, explorar directo FileJump
            if (!taskName) {
                if (!effectiveRemoteOverride) return;
                remoteBody.innerHTML = '<tr><td colspan="4" class="py-10 text-center text-slate-400">Consultando nivel raíz en FileJump...</td></tr>';
                try {
                    const res = await fetch(`/api/remote/browse?remote_id=${encodeURIComponent(effectiveRemoteOverride)}`);
                    const data = await res.json();
                    if (data.error) {
                        showNotice('Error', data.error);
                        return;
                    }
                    document.getElementById('remotePathSubtitle').textContent = `ID Remoto: ${data.remote_root_id}`;
                    initRemoteExplorer(data.remote_root_id, data.remote_items || []);
                } catch (e) {
                    showNotice('Error de Red', 'No se pudo consultar FileJump.');
                }
                return;
            }

            localBody.innerHTML = '<tr><td colspan="4" class="py-10 text-center text-slate-400">Cargando carpeta local...</td></tr>';
            remoteBody.innerHTML = '<tr><td colspan="4" class="py-10 text-center text-slate-400">Consultando carpeta en FileJump...</td></tr>';

            try {
                const query = effectiveRemoteOverride ? `?remote_id=${encodeURIComponent(effectiveRemoteOverride)}` : '';
                const res = await fetch(`/api/inspect/${encodeURIComponent(taskName)}${query}`);
                const data = await res.json();
                if (data.error) {
                    showNotice('Error', data.error);
                    return;
                }

                document.getElementById('localPathSubtitle').textContent = data.task.path;
                document.getElementById('remotePathSubtitle').textContent = `ID Remoto: ${data.remote_root_id || 'Aún no creada'}`;
                document.getElementById('lastSyncLabel').textContent = data.last_sync ? `Última sync: ${new Date(data.last_sync).toLocaleString()}` : 'Nunca sincronizado';

                // Sincronizar el selector remoto con el ID detectado si existe en la lista
                if (data.remote_root_id && Array.from(remoteSelector.options).some(o => String(o.value) === String(data.remote_root_id))) {
                    remoteSelector.value = String(data.remote_root_id);
                }

                if (!data.local_exists) {
                    localNodes.clear();
                    document.getElementById('localFolderTree').innerHTML = '<div class="px-2 py-4 text-rose-400 text-[11px]">Ruta no encontrada</div>';
                    localBody.innerHTML = '<tr><td colspan="4" class="py-10 text-center text-rose-400">La carpeta local no existe en el disco</td></tr>';
                } else {
                    initLocalExplorer(data.local_items || []);
                }

                initRemoteExplorer(data.remote_root_id || 'ROOT', data.remote_items || []);
            } catch (e) {
                showNotice('Error de Red', 'No se pudo completar la inspección.');
            }
        }

        async function triggerSync(force = false) {
            const taskName = document.getElementById('taskSelector').value;
            if (!taskName) {
                showNotice('Selecciona una Tarea', 'Por favor selecciona primero una carpeta/tarea para sincronizar.');
                return;
            }

            const btn = document.getElementById('syncBtn');
            btn.disabled = true;
            btn.classList.add('opacity-50');

            try {
                const res = await fetch(`/api/sync/${encodeURIComponent(taskName)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force })
                });
                const data = await res.json();
                await refreshStatusAndLogs();

                if (data.requires_confirmation) {
                    showNotice('Umbral de Seguridad Superado', data.message, () => triggerSync(true));
                } else if (data.error) {
                    showNotice('Error de Sincronización', data.error);
                } else {
                    await inspectSelectedTask();
                }
            } catch (e) {
                showNotice('Error', 'Fallo comunicándose con el servidor local.');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-50');
            }
        }

        async function openSettingsModal() {
            const res = await fetch('/api/settings');
            const data = await res.json();
            currentConfig = data.config;

            document.getElementById('envApiKey').value = '';
            document.getElementById('envBaseUrl').value = data.env.FILEJUMP_BASE_URL;
            document.getElementById('envUploadUrl').value = data.env.FILEJUMP_UPLOAD_URL;
            document.getElementById('cfgWorkers').value = currentConfig.max_workers || 4;
            document.getElementById('cfgThreshold').value = currentConfig.min_impact_percentage || 40;
            document.getElementById('cfgHighSpeed').checked = currentConfig.use_high_speed_upload !== false;

            const container = document.getElementById('tasksListContainer');
            container.innerHTML = '';
            (currentConfig.tasks || []).forEach(t => addTaskRow(t.name, t.path));

            document.getElementById('settingsModal').classList.remove('hidden');
            document.getElementById('settingsModal').classList.add('flex');
        }

        function closeSettingsModal() {
            document.getElementById('settingsModal').classList.add('hidden');
            document.getElementById('settingsModal').classList.remove('flex');
        }

        function addTaskRow(name = '', path = '') {
            const container = document.getElementById('tasksListContainer');
            const row = document.createElement('div');
            row.className = 'grid grid-cols-1 sm:grid-cols-12 gap-2 items-center bg-slate-950 p-2.5 rounded-xl border border-slate-800 task-row';
            row.innerHTML = `
                <input type="text" placeholder="Nombre Tarea (ej. Respaldo)" value="${name}" class="sm:col-span-4 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white task-name">
                <input type="text" placeholder="Ruta Local (ej. C:/MisDocumentos)" value="${path}" class="sm:col-span-7 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white task-path">
                <button onclick="this.parentElement.remove()" class="sm:col-span-1 text-rose-400 hover:text-rose-300 text-center font-bold text-sm" title="Eliminar">&times;</button>
            `;
            container.appendChild(row);
        }

        async function saveSettings() {
            const tasks = [];
            document.querySelectorAll('.task-row').forEach(row => {
                const name = row.querySelector('.task-name').value.trim();
                const path = row.querySelector('.task-path').value.trim();
                if (name && path) tasks.push({ name, path });
            });

            const payload = {
                env: {
                    FILEJUMP_API_KEY: document.getElementById('envApiKey').value.trim(),
                    FILEJUMP_BASE_URL: document.getElementById('envBaseUrl').value.trim(),
                    FILEJUMP_UPLOAD_URL: document.getElementById('envUploadUrl').value.trim()
                },
                config: {
                    max_workers: parseInt(document.getElementById('cfgWorkers').value) || 4,
                    min_impact_percentage: parseInt(document.getElementById('cfgThreshold').value) || 40,
                    use_high_speed_upload: document.getElementById('cfgHighSpeed').checked,
                    tasks
                }
            };

            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            closeSettingsModal();
            wasConnected = false; // Forzar recarga de directorios remotos si cambió el token
            await refreshStatusAndLogs();
            await loadInitialConfig();
        }

        // Inicialización
        refreshStatusAndLogs();
        fetchLocalDrives(true);
        loadInitialConfig();
        setInterval(refreshStatusAndLogs, 4000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    log_event("info", "Servidor Web FileJump Sync Studio iniciado en http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
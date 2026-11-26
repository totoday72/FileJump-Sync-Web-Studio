import os
import sys
import json
import time
import base64
import struct
import string
import shutil
import hashlib
import zipfile
import mimetypes
import subprocess
import threading
import webbrowser
import signal
import ctypes
import uuid
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
CACHE_META_FILE = TEMP_CACHE_DIR / ".cache_index.json"
LOCAL_FAVORITES_FALLBACK = BASE_DIR / "favorites_cache.json"
REMOTE_APP_CONFIG_DIR = "filejump_sync_studio"
REMOTE_FAVORITES_FILENAME = "favorites.json"
RECORDS_DIR.mkdir(exist_ok=True)
TEMP_CACHE_DIR.mkdir(exist_ok=True)

# Crear .env por defecto si no existe
if not ENV_FILE.exists():
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write('FILEJUMP_API_KEY=""\n')
        f.write('FILEJUMP_BASE_URL="https://filejump.com/api"\n')
        f.write('FILEJUMP_UPLOAD_URL="https://uploads.filejump.com/api/upload"\n')

load_dotenv(ENV_FILE)

def _compute_api_key_hash(api_key_str):
    """Genera una huella SHA-256 del token actual sin almacenar el token en texto plano."""
    if not api_key_str:
        return ""
    return hashlib.sha256(api_key_str.strip().encode("utf-8")).hexdigest()

def load_cache_index():
    """Carga el índice persistente de archivos en caché y la huella de la API Key."""
    TEMP_CACHE_DIR.mkdir(exist_ok=True)
    if CACHE_META_FILE.exists():
        try:
            with open(CACHE_META_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("api_key_hash", "")
                data.setdefault("files", {})
                return data
        except Exception:
            pass
    return {"api_key_hash": "", "files": {}}

def save_cache_index(index_data):
    """Guarda el índice persistente de la caché."""
    TEMP_CACHE_DIR.mkdir(exist_ok=True)
    with open(CACHE_META_FILE, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=4, ensure_ascii=False)

def ensure_cache_matches_api_key(current_api_key):
    """
    Mantiene la caché persistente entre reinicios, pero la vacía por completo
    si detecta que se cambió la FILEJUMP_API_KEY (cambio de cuenta/credenciales).
    """
    current_hash = _compute_api_key_hash(current_api_key)
    index_data = load_cache_index()
    stored_hash = index_data.get("api_key_hash", "")

    if stored_hash and current_hash and stored_hash != current_hash:
        try:
            if TEMP_CACHE_DIR.exists():
                shutil.rmtree(TEMP_CACHE_DIR, ignore_errors=True)
            TEMP_CACHE_DIR.mkdir(exist_ok=True)
            print("[CACHE] Cambio de API Key detectado: caché anterior eliminada.")
        except Exception:
            pass
        index_data = {"api_key_hash": current_hash, "files": {}}
        save_cache_index(index_data)
    elif current_hash and stored_hash != current_hash:
        index_data["api_key_hash"] = current_hash
        save_cache_index(index_data)

    return index_data

ensure_cache_matches_api_key(os.getenv("FILEJUMP_API_KEY", "").strip())

app = Flask(__name__)

# Estado global en memoria para logs en tiempo real hacia la UI
activity_logs = []
transfer_jobs = {}
audio_scan_jobs = {}

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
        "account_profiles": {},
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
        ensure_cache_matches_api_key(self.api_key)

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

    def get_user_storage_info(self):
        """Obtiene el nombre de usuario y el uso real de almacenamiento desde GET /api/auth/user y GET /api/auth/usage."""
        if not self.api_key:
            return None

        key_hash = _compute_api_key_hash(self.api_key)[:16]
        cfg = load_config()
        saved_profile = cfg.get("account_profiles", {}).get(key_hash, {})

        user_name = None
        used_bytes = 0
        total_bytes = 0
        api_percentage = None

        # Normalizar URL base para apuntar siempre a .../api
        api_root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

        # 1. Consultar GET /api/auth/usage (used, limit, percentage)
        try:
            resp_usage = requests.get(f"{api_root}/auth/usage", headers=self._headers(), timeout=10)
            if resp_usage.status_code == 200:
                payload = resp_usage.json()
                u_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                if isinstance(u_data, dict):
                    if u_data.get("used") is not None:
                        used_bytes = int(float(u_data["used"]))
                    if u_data.get("limit") is not None:
                        total_bytes = int(float(u_data["limit"]))
                    elif u_data.get("available") is not None:
                        total_bytes = int(float(u_data["available"]))
                    if u_data.get("percentage") is not None:
                        api_percentage = float(u_data["percentage"])
        except Exception as e:
            log_event("warning", f"No se pudo leer /auth/usage: {e}")

        # 2. Consultar GET /api/auth/user (perfil autenticado y cuota)
        try:
            resp_user = requests.get(f"{api_root}/auth/user", headers=self._headers(), timeout=10)
            if resp_user.status_code == 200:
                payload = resp_user.json()
                candidates = []
                if isinstance(payload, dict):
                    candidates.append(payload)
                    for key in ("user", "data", "profile", "attributes", "account"):
                        if isinstance(payload.get(key), dict):
                            candidates.append(payload[key])
                            if isinstance(payload[key].get("user"), dict):
                                candidates.append(payload[key]["user"])
                for u_obj in candidates:
                    found_name = (
                        u_obj.get("display_name")
                        or u_obj.get("full_name")
                        or u_obj.get("name")
                        or u_obj.get("username")
                        or (f"{u_obj.get('first_name', '')} {u_obj.get('last_name', '')}".strip())
                        or u_obj.get("email")
                    )
                    if found_name:
                        user_name = str(found_name).strip()
                        break
        except Exception as e:
            log_event("warning", f"No se pudo leer /auth/user: {e}")

        # 3. Respaldo: intentar obtener nombre del token personal en GET /api/user/tokens
        if not user_name:
            try:
                resp_tokens = requests.get(f"{api_root}/user/tokens", headers=self._headers(), timeout=8)
                if resp_tokens.status_code == 200:
                    t_payload = resp_tokens.json()
                    t_list = t_payload.get("data") or t_payload.get("tokens") or (t_payload if isinstance(t_payload, list) else [])
                    if isinstance(t_list, list) and len(t_list) > 0 and isinstance(t_list[0], dict):
                        t_user = t_list[0].get("tokenable") or t_list[0].get("user") or {}
                        if isinstance(t_user, dict):
                            user_name = t_user.get("display_name") or t_user.get("name") or t_user.get("email")
            except Exception:
                pass

        # 4. Nombre personalizado guardado localmente o etiqueta por defecto
        if saved_profile.get("user_name"):
            user_name = saved_profile["user_name"]
        elif not user_name:
            user_name = "Cuenta FileJump"

        free_bytes = max(0, total_bytes - used_bytes) if total_bytes > 0 else 0
        if api_percentage is not None:
            pct = round(api_percentage, 1)
        else:
            pct = round((used_bytes / total_bytes) * 100, 1) if total_bytes > 0 else 0.0

        return {
            "user_name": user_name,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "used_pct": min(100.0, pct)
        }

    def list_folders(self, parent_id=None):
        """Lista todas las carpetas remotas usando GET /folders con paginación completa."""
        base_params = {}
        if parent_id is not None:
            base_params["parent_id"] = parent_id
            base_params["folder_id"] = parent_id
            base_params["parentId"] = parent_id

        all_folders = []
        seen_ids = set()
        page = 1
        per_page = 200

        while True:
            params = {**base_params, "page": page, "per_page": per_page, "perPage": per_page}
            try:
                resp = requests.get(f"{self.base_url}/folders", headers=self._headers(), params=params, timeout=15)
                if resp.status_code != 200:
                    break
                payload = resp.json()
                items = payload.get("data", payload if isinstance(payload, list) else [])
                if not items:
                    break

                new_added = 0
                for item in items:
                    item_id = item.get("id")
                    if item_id is not None and item_id in seen_ids:
                        continue
                    if item_id is not None:
                        seen_ids.add(item_id)
                    all_folders.append(item)
                    new_added += 1

                if new_added == 0 or not isinstance(payload, dict):
                    break

                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
                links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
                last_page = meta.get("last_page")
                next_url = meta.get("next_page_url") or meta.get("next_page") or links.get("next")
                total_items = meta.get("total")
                to_item = meta.get("to")
                current_per_page = meta.get("per_page") or len(items)

                if last_page is not None and page < int(last_page):
                    page += 1
                elif next_url:
                    page += 1
                elif total_items is not None and to_item is not None and int(to_item) < int(total_items):
                    page += 1
                elif total_items is not None and len(all_folders) < int(total_items):
                    page += 1
                elif last_page is None and next_url is None and total_items is None and len(items) >= int(current_per_page):
                    page += 1
                else:
                    break
            except Exception as e:
                log_event("error", f"Error listando carpetas remotas: {e}")
                break

        # Si parent_id es None, filtramos las carpetas que están en la raíz (sin padre)
        if parent_id is None:
            root_items = [
                f for f in all_folders
                if f.get("parent_id") in (None, 0, "0", "")
                and f.get("folder_id") in (None, 0, "0", "")
                and f.get("parentId") in (None, 0, "0", "")
            ]
            return root_items if root_items else all_folders
        return all_folders

    def list_files(self, folder_id=None):
        """Lista todos los archivos remotos usando GET /files recorriendo todas las páginas disponibles."""
        base_params = {}
        if folder_id is not None:
            base_params["folder_id"] = folder_id
            base_params["parent_id"] = folder_id
            base_params["parentId"] = folder_id

        all_files = []
        seen_ids = set()
        page = 1
        per_page = 200

        while True:
            params = {**base_params, "page": page, "per_page": per_page, "perPage": per_page}
            try:
                resp = requests.get(f"{self.base_url}/files", headers=self._headers(), params=params, timeout=15)
                if resp.status_code != 200:
                    log_event("warning", f"GET /files devolvió HTTP {resp.status_code}")
                    break
                payload = resp.json()
                items = payload.get("data", payload if isinstance(payload, list) else [])
                if not items:
                    break

                new_added = 0
                for item in items:
                    item_id = item.get("id")
                    if item_id is not None and item_id in seen_ids:
                        continue
                    if item_id is not None:
                        seen_ids.add(item_id)
                    all_files.append(item)
                    new_added += 1

                # Evitar bucle infinito si la API ignora el parámetro page y devuelve siempre lo mismo
                if new_added == 0 or not isinstance(payload, dict):
                    break

                # Detectar paginación en cualquier estructura (Laravel pagination, BeDrive, meta, links)
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
                links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
                last_page = meta.get("last_page")
                next_url = meta.get("next_page_url") or meta.get("next_page") or links.get("next")
                total_items = meta.get("total")
                to_item = meta.get("to")
                current_per_page = meta.get("per_page") or len(items)

                if last_page is not None and page < int(last_page):
                    page += 1
                elif next_url:
                    page += 1
                elif total_items is not None and to_item is not None and int(to_item) < int(total_items):
                    page += 1
                elif total_items is not None and len(all_files) < int(total_items):
                    page += 1
                elif last_page is None and next_url is None and total_items is None and len(items) >= int(current_per_page):
                    page += 1
                else:
                    break
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

    def upload_file(self, local_path, folder_id=None, use_high_speed=True, progress_callback=None):
        """
        Sube un archivo utilizando el endpoint REST autenticado POST /api/files/upload
        con lectura por bloques (streaming multipart) para reportar bytes enviados,
        porcentaje y velocidad en tiempo real.
        """
        rest_upload_url = f"{self.base_url}/files/upload"
        rest_alias_url = f"{self.base_url}/files"

        if use_high_speed and self.upload_url:
            candidate_urls = [rest_upload_url, self.upload_url, rest_alias_url]
        else:
            candidate_urls = [rest_upload_url, rest_alias_url]

        ordered_urls = []
        for u in candidate_urls:
            if u and u not in ordered_urls:
                ordered_urls.append(u)

        file_name = os.path.basename(local_path)
        mime_type, _ = mimetypes.guess_type(local_path)
        mime_type = mime_type or "application/octet-stream"

        st = os.stat(local_path)
        file_size = st.st_size
        mtime_sec = int(st.st_mtime)
        mtime_ms = int(st.st_mtime * 1000)
        mtime_iso = datetime.datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        fields = {
            "last_modified": str(mtime_ms),
            "lastModified": str(mtime_ms),
            "mtime": str(mtime_sec),
            "updated_at": mtime_iso,
            "created_at": mtime_iso
        }
        if folder_id not in (None, "", "ROOT", 0, "0"):
            fid_str = str(folder_id)
            fields["folder_id"] = fid_str
            fields["parent_id"] = fid_str

        boundary = f"----FileJumpBoundary{uuid.uuid4().hex}"
        preamble_parts = []
        for k, v in fields.items():
            preamble_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n".encode("utf-8")
            )
        safe_fname = file_name.replace('"', '\\"')
        preamble_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_fname}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8")
        )
        preamble_bytes = b"".join(preamble_parts)
        closing_bytes = f"\r\n--{boundary}--\r\n".encode("utf-8")
        total_body_len = len(preamble_bytes) + file_size + len(closing_bytes)

        class _StreamingMultipartBody:
            def __init__(self, f_obj):
                self._f = f_obj
                self._pre = preamble_bytes
                self._pre_pos = 0
                self._end = closing_bytes
                self._end_pos = 0
                self._file_sent = 0
                self._last_cb = 0.0

            def __len__(self):
                return total_body_len

            def read(self, size=-1):
                if size is None or size < 0:
                    size = 65536
                if self._pre_pos < len(self._pre):
                    chunk = self._pre[self._pre_pos:self._pre_pos + size]
                    self._pre_pos += len(chunk)
                    return chunk
                file_chunk = self._f.read(size)
                if file_chunk:
                    self._file_sent += len(file_chunk)
                    now = time.time()
                    if progress_callback and (now - self._last_cb >= 0.12 or self._file_sent >= file_size):
                        self._last_cb = now
                        progress_callback(self._file_sent, file_size)
                    return file_chunk
                if self._end_pos < len(self._end):
                    chunk = self._end[self._end_pos:self._end_pos + size]
                    self._end_pos += len(chunk)
                    return chunk
                return b""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(total_body_len),
            "X-File-Last-Modified": str(mtime_ms),
            "X-File-Mtime": mtime_iso
        }

        last_error_detail = "Sin respuesta del servidor"

        for target_url in ordered_urls:
            try:
                if progress_callback:
                    progress_callback(0, file_size)
                with open(local_path, "rb") as f:
                    stream_body = _StreamingMultipartBody(f)
                    resp = requests.post(
                        target_url,
                        headers=headers,
                        data=stream_body,
                        timeout=600
                    )
                if resp.status_code in (200, 201):
                    if progress_callback:
                        progress_callback(file_size, file_size)
                    try:
                        res_json = resp.json()
                    except Exception:
                        res_json = {"uploaded": True}

                    file_obj = (
                        res_json.get("data")
                        or res_json.get("file")
                        or res_json.get("fileEntry")
                        or res_json
                    )
                    if isinstance(file_obj, list) and len(file_obj) > 0:
                        file_obj = file_obj[0]

                    new_id = file_obj.get("id") if isinstance(file_obj, dict) else None
                    if new_id:
                        self._sync_remote_timestamp("file", new_id, st.st_mtime)

                    if isinstance(file_obj, dict):
                        return file_obj
                    return {"id": new_id, "raw": res_json}
                else:
                    try:
                        err_json = resp.json()
                        last_error_detail = err_json.get("message") or err_json.get("error") or resp.text[:250]
                    except Exception:
                        last_error_detail = f"HTTP {resp.status_code}: {resp.text[:250]}"
                    log_event("warning", f"Intento en {target_url} falló ({resp.status_code}): {last_error_detail}")
            except Exception as e:
                last_error_detail = str(e)
                log_event("warning", f"Excepción subiendo '{file_name}' a {target_url}: {e}")

        log_event("error", f"Error definitivo subiendo '{file_name}': {last_error_detail}")
        self.last_upload_error = last_error_detail
        return None

    def _sync_remote_timestamp(self, item_type, item_id, mtime_float):
        """Intenta actualizar la fecha de modificación en FileJump y guarda respaldo local por ID."""
        if not item_id or not mtime_float:
            return
        mtime_sec = int(mtime_float)
        mtime_ms = int(mtime_float * 1000)
        mtime_iso = datetime.datetime.fromtimestamp(mtime_float, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mtime_sql = datetime.datetime.fromtimestamp(mtime_float, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Guardar respaldo en sync_config.json por si la API de FileJump no permite sobrescribir updated_at
        try:
            cfg = load_config()
            ts_map = cfg.setdefault("preserved_mtimes", {})
            ts_map[f"{item_type}:{item_id}"] = float(mtime_float)
            save_config(cfg)
        except Exception:
            pass

        payload = {
            "updated_at": mtime_iso,
            "created_at": mtime_iso,
            "last_modified": mtime_ms,
            "lastModified": mtime_ms,
            "mtime": mtime_sec,
            "modified_at": mtime_sql
        }
        endpoints = (
            [f"{self.base_url}/folders/{item_id}", f"{self.base_url}/file-entries/{item_id}"]
            if item_type == "folder"
            else [f"{self.base_url}/files/{item_id}", f"{self.base_url}/file-entries/{item_id}"]
        )
        for ep in endpoints:
            try:
                resp = requests.put(ep, headers=self._headers(), json=payload, timeout=8)
                if resp.status_code in (200, 201, 204):
                    break
            except Exception:
                pass

    def delete_file(self, file_id):
        """Elimina un archivo remoto usando DELETE /files/{id}."""
        try:
            resp = requests.delete(f"{self.base_url}/files/{file_id}", headers=self._headers(), timeout=15)
            return resp.status_code in (200, 204)
        except Exception as e:
            log_event("error", f"Error eliminando archivo remoto {file_id}: {e}")
            return False

    def delete_folder(self, folder_id):
        """Elimina una carpeta remota usando DELETE /folders/{id} o /file-entries/{id}."""
        for endpoint in (f"{self.base_url}/folders/{folder_id}", f"{self.base_url}/file-entries/{folder_id}"):
            try:
                resp = requests.delete(endpoint, headers=self._headers(), timeout=15)
                if resp.status_code in (200, 204):
                    return True
            except Exception:
                pass
        return False

    def rename_item(self, item_type, item_id, new_name):
        """Renombra un archivo o carpeta remota en FileJump."""
        endpoints = (
            [f"{self.base_url}/folders/{item_id}", f"{self.base_url}/file-entries/{item_id}"]
            if item_type == "folder"
            else [f"{self.base_url}/files/{item_id}", f"{self.base_url}/file-entries/{item_id}"]
        )
        for ep in endpoints:
            try:
                resp = requests.put(ep, headers=self._headers(), json={"name": new_name}, timeout=12)
                if resp.status_code in (200, 201, 204):
                    return True
            except Exception:
                pass
        return False

    def ensure_remote_app_config_dir(self):
        """Obtiene o crea el directorio remoto 'filejump_sync_studio' en la raíz de FileJump."""
        if not self.api_key:
            return None
        try:
            root_folders = self.list_folders(parent_id=None)
            for fld in root_folders:
                fname = (fld.get("name") or fld.get("file_name") or "").strip()
                if fname.lower() == REMOTE_APP_CONFIG_DIR.lower() and fld.get("id") is not None:
                    return fld.get("id")
            new_id = self.create_folder(REMOTE_APP_CONFIG_DIR, parent_id=None)
            if new_id:
                log_event("info", f"Directorio remoto '{REMOTE_APP_CONFIG_DIR}' creado en la raíz (ID #{new_id}).")
            return new_id
        except Exception as e:
            log_event("warning", f"No se pudo verificar/crear '{REMOTE_APP_CONFIG_DIR}': {e}")
            return None

    def load_remote_favorites(self):
        """Lee la lista de canciones favoritas desde '/filejump_sync_studio/favorites.json' en FileJump."""
        default_payload = {"favorites": {}, "updated_at": None}
        if not self.api_key:
            return default_payload

        folder_id = self.ensure_remote_app_config_dir()
        if not folder_id:
            return default_payload

        try:
            files = self.list_files(folder_id=folder_id)
            target_file = None
            for fl in files:
                fname = (fl.get("name") or fl.get("file_name") or "").strip()
                if fname.lower() == REMOTE_FAVORITES_FILENAME.lower() and fl.get("id") is not None:
                    target_file = fl
                    break

            if not target_file:
                return default_payload

            fid = target_file.get("id")
            headers = {"Authorization": f"Bearer {self.api_key}"}
            for url in (f"{self.base_url}/files/{fid}/download", f"{self.base_url}/drive/file-entries/{fid}/download"):
                try:
                    resp = requests.get(url, headers=headers, timeout=20)
                    if resp.status_code == 200 and resp.text.strip():
                        parsed = json.loads(resp.text)
                        if isinstance(parsed, dict):
                            parsed.setdefault("favorites", {})
                            with open(LOCAL_FAVORITES_FALLBACK, "w", encoding="utf-8") as lf:
                                json.dump(parsed, lf, indent=4, ensure_ascii=False)
                            return parsed
                except Exception:
                    pass
        except Exception as e:
            log_event("warning", f"No se pudo leer favoritos remotos: {e}")

        if LOCAL_FAVORITES_FALLBACK.exists():
            try:
                with open(LOCAL_FAVORITES_FALLBACK, "r", encoding="utf-8") as lf:
                    data = json.load(lf)
                    if isinstance(data, dict):
                        data.setdefault("favorites", {})
                        return data
            except Exception:
                pass
        return default_payload

    def save_remote_favorites(self, favorites_map):
        """Guarda la lista actualizada de canciones favoritas en '/filejump_sync_studio/favorites.json'."""
        payload_data = {
            "favorites": favorites_map if isinstance(favorites_map, dict) else {},
            "updated_at": datetime.datetime.now(timezone.utc).isoformat()
        }
        try:
            with open(LOCAL_FAVORITES_FALLBACK, "w", encoding="utf-8") as lf:
                json.dump(payload_data, lf, indent=4, ensure_ascii=False)
        except Exception:
            pass

        if not self.api_key:
            return False, "Falta FILEJUMP_API_KEY"

        folder_id = self.ensure_remote_app_config_dir()
        if not folder_id:
            return False, f"No se pudo crear ni acceder al directorio remoto '{REMOTE_APP_CONFIG_DIR}'"

        try:
            # Eliminar versión previa de favorites.json si existe para reemplazarla limpiamente
            existing_files = self.list_files(folder_id=folder_id)
            for fl in existing_files:
                fname = (fl.get("name") or fl.get("file_name") or "").strip()
                if fname.lower() == REMOTE_FAVORITES_FILENAME.lower() and fl.get("id") is not None:
                    self.delete_file(fl.get("id"))

            TEMP_CACHE_DIR.mkdir(exist_ok=True)
            tmp_json_path = TEMP_CACHE_DIR / f".tmp_{REMOTE_FAVORITES_FILENAME}"
            with open(tmp_json_path, "w", encoding="utf-8") as tf:
                json.dump(payload_data, tf, indent=4, ensure_ascii=False)

            cfg = load_config()
            use_hs = cfg.get("use_high_speed_upload", True)
            uploaded = self.upload_file(str(tmp_json_path), folder_id=folder_id, use_high_speed=use_hs)
            try:
                tmp_json_path.unlink()
            except Exception:
                pass

            if uploaded is not None:
                new_id = uploaded.get("id") if isinstance(uploaded, dict) else None
                if new_id:
                    self.rename_item("file", new_id, REMOTE_FAVORITES_FILENAME)
                return True, "Favoritos sincronizados en FileJump"
            return False, getattr(self, "last_upload_error", "No se pudo subir favorites.json")
        except Exception as e:
            return False, str(e)

    def download_file_direct(self, file_id, dest_path, remote_mtime=0.0, progress_callback=None):
        """Descarga un archivo remoto directamente a una ruta local reportando bytes por bloque y preservando mtime."""
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        candidate_urls = [
            f"{self.base_url}/files/{file_id}/download",
            f"{self.base_url}/drive/file-entries/{file_id}/download"
        ]
        for url in candidate_urls:
            try:
                with requests.get(url, headers=headers, stream=True, timeout=300) as resp:
                    if resp.status_code == 200:
                        total_len = int(resp.headers.get("Content-Length") or 0)
                        downloaded = 0
                        last_cb = 0.0
                        if progress_callback:
                            progress_callback(0, total_len)
                        with open(dest, "wb") as out_f:
                            for chunk in resp.iter_content(chunk_size=65536):
                                if chunk:
                                    out_f.write(chunk)
                                    downloaded += len(chunk)
                                    now = time.time()
                                    if progress_callback and (now - last_cb >= 0.12):
                                        last_cb = now
                                        progress_callback(downloaded, total_len)
                        if progress_callback:
                            progress_callback(downloaded, max(total_len, downloaded))
                        if remote_mtime and remote_mtime > 0:
                            try:
                                os.utime(dest, (remote_mtime, remote_mtime))
                            except Exception:
                                pass
                        return True
            except Exception as e:
                log_event("warning", f"Error descargando #{file_id} desde {url}: {e}")
        return False

    def get_remote_file_mtime(self, file_id):
        """Consulta la fecha de modificación remota (updated_at) de un archivo específico en FileJump."""
        try:
            resp = requests.get(f"{self.base_url}/files/{file_id}", headers=self._headers(), timeout=10)
            if resp.status_code == 200:
                payload = resp.json()
                file_obj = payload.get("data", payload.get("fileEntry", payload)) if isinstance(payload, dict) else {}
                updated_str = file_obj.get("updated_at") or file_obj.get("created_at") or ""
                return parse_iso_timestamp(updated_str)
        except Exception:
            pass
        return 0.0

    def download_file_to_cache(self, file_id, filename, remote_mtime=0.0):
        """
        Descarga un archivo remoto a TEMP_CACHE_DIR solo si no existe en caché
        o si la versión remota tiene una fecha de modificación más reciente.
        """
        TEMP_CACHE_DIR.mkdir(exist_ok=True)
        cache_index = load_cache_index()
        rid_key = str(file_id)

        safe_name = "".join(c if c.isalnum() or c in (".", "-", "_", " ") else "_" for c in filename).strip() or f"file_{file_id}"
        cached_path = TEMP_CACHE_DIR / f"{file_id}_{safe_name}"

        # Determinar el timestamp remoto más preciso disponible
        effective_remote_mtime = float(remote_mtime or 0.0)
        if effective_remote_mtime <= 0.0:
            effective_remote_mtime = self.get_remote_file_mtime(file_id)

        entry_meta = cache_index.get("files", {}).get(rid_key, {})
        cached_remote_mtime = float(entry_meta.get("remote_mtime", 0.0))

        if cached_path.exists() and cached_path.stat().st_size > 0:
            local_cache_mtime = cached_path.stat().st_mtime
            ref_cached_ts = cached_remote_mtime if cached_remote_mtime > 0 else local_cache_mtime
            # Si el archivo remoto NO es más nuevo (tolerancia de 2s), reutilizamos el archivo en caché
            if effective_remote_mtime <= 0.0 or effective_remote_mtime <= (ref_cached_ts + 2.0):
                return cached_path
            log_event("info", f"Archivo remoto #{file_id} actualizado en nube; descargando versión más reciente...")

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
                        # Actualizar índice persistente con la fecha de modificación remota
                        cache_index.setdefault("files", {})[rid_key] = {
                            "filename": cached_path.name,
                            "remote_mtime": effective_remote_mtime if effective_remote_mtime > 0 else time.time(),
                            "cached_at": time.time()
                        }
                        save_cache_index(cache_index)
                        # Eliminar cualquier versión transcodificada previa para regenerarla si hace falta
                        stale_mp4 = TEMP_CACHE_DIR / f"{cached_path.stem}_web.mp4"
                        if stale_mp4.exists():
                            try:
                                stale_mp4.unlink()
                            except Exception:
                                pass
                        log_event("info", f"Archivo remoto #{file_id} guardado en caché persistente.")
                        return cached_path
            except Exception as e:
                log_event("warning", f"Intento de descarga fallido ({url}): {e}")
        return cached_path if (cached_path.exists() and cached_path.stat().st_size > 0) else None


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
    preserved_mtimes = load_config().get("preserved_mtimes", {})

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
        mtime = preserved_mtimes.get(f"folder:{fid}") or parse_iso_timestamp(updated_str)
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
        mtime = preserved_mtimes.get(f"file:{fid}") or parse_iso_timestamp(updated_str)
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

def scan_remote_tree(client, root_folder_id, current_prefix="", depth=0, max_depth=8, on_item_callback=None):
    """Construye el listado plano ordenado jerárquicamente de carpetas y archivos remotos."""
    if depth > max_depth:
        return []

    is_account_root = (root_folder_id in (None, "ROOT", 0, "0"))
    query_parent = None if is_account_root else root_folder_id

    remote_items = []
    folders = client.list_folders(parent_id=query_parent)
    files = client.list_files(folder_id=query_parent)
    preserved_mtimes = load_config().get("preserved_mtimes", {})

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
        mtime = preserved_mtimes.get(f"folder:{fid}") or parse_iso_timestamp(updated_str)
        folder_entry = {
            "type": "folder",
            "id": fid,
            "name": fname,
            "rel_path": rel_path,
            "depth": depth,
            "parent_id": None if is_account_root else root_folder_id,
            "size": 0,
            "mtime": mtime,
            "mtime_str": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "-"
        }
        remote_items.append(folder_entry)
        if on_item_callback:
            on_item_callback(folder_entry)
        # Recursión jerárquica dentro de la subcarpeta
        remote_items.extend(scan_remote_tree(client, fid, rel_path, depth + 1, max_depth, on_item_callback=on_item_callback))

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
        mtime = preserved_mtimes.get(f"file:{fid}") or parse_iso_timestamp(updated_str)
        size = fl.get("size") or fl.get("file_size") or 0
        file_entry = {
            "type": "file",
            "id": fid,
            "name": fname,
            "rel_path": rel_path,
            "depth": depth,
            "parent_id": None if is_account_root else root_folder_id,
            "size": int(size),
            "mtime": mtime,
            "mtime_str": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "-"
        }
        remote_items.append(file_entry)
        if on_item_callback:
            on_item_callback(file_entry)

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

@app.route("/api/user/profile", methods=["GET", "POST"])
def api_user_profile():
    """Devuelve el perfil/almacenamiento desde FileJump y permite guardar el nombre visible."""
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"connected": False})

    if request.method == "POST":
        data = request.json or {}
        key_hash = _compute_api_key_hash(client.api_key)[:16]
        cfg = load_config()
        profiles = cfg.setdefault("account_profiles", {})
        entry = profiles.setdefault(key_hash, {})

        if "user_name" in data:
            entry["user_name"] = str(data["user_name"]).strip()

        save_config(cfg)
        log_event("info", "Nombre visible de cuenta actualizado.")

    info = client.get_user_storage_info()
    if not info:
        return jsonify({"connected": False})
    return jsonify({"connected": True, **info})


@app.route("/api/music/favorites", methods=["GET", "POST"])
def api_music_favorites():
    """Carga o guarda las canciones favoritas en '/filejump_sync_studio/favorites.json'."""
    client = FileJumpClient()
    if request.method == "GET":
        data = client.load_remote_favorites()
        return jsonify({"success": True, "favorites": data.get("favorites", {})})

    body = request.json or {}
    action = body.get("action", "toggle")
    track_key = str(body.get("key") or "").strip()
    track_meta = body.get("meta") or {}
    if not track_key:
        return jsonify({"error": "Falta identificador de canción"}), 400

    current_data = client.load_remote_favorites()
    favs = current_data.get("favorites", {})

    if action == "remove" or (action == "toggle" and track_key in favs):
        favs.pop(track_key, None)
        is_fav = False
    else:
        favs[track_key] = {
            "name": track_meta.get("name", ""),
            "title": track_meta.get("title", ""),
            "artist": track_meta.get("artist", ""),
            "album": track_meta.get("album", ""),
            "source": track_meta.get("source", "remote"),
            "remote_id": track_meta.get("remote_id"),
            "added_at": datetime.datetime.now(timezone.utc).isoformat()
        }
        is_fav = True

    ok, msg = client.save_remote_favorites(favs)
    if ok:
        title_lbl = track_meta.get("title") or track_meta.get("name") or track_key
        log_event("info", f"{'❤️ Agregada a favoritos' if is_fav else '🤍 Quitada de favoritos'}: {title_lbl}")
        return jsonify({"success": True, "is_favorite": is_fav, "favorites": favs})
    return jsonify({"success": False, "error": msg, "is_favorite": is_fav, "favorites": favs}), 500


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
        return jsonify({"error": "Falta FILEJUMP_API_KEY en el archivo .env"}), 400

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
        new_key = env_updates["FILEJUMP_API_KEY"].strip()
        set_key(str(ENV_FILE), "FILEJUMP_API_KEY", new_key)
        ensure_cache_matches_api_key(new_key)
        log_event("info", "Clave API actualizada en .env (caché verificada/reiniciada por cambio de credenciales).")
    if env_updates.get("FILEJUMP_BASE_URL"):
        set_key(str(ENV_FILE), "FILEJUMP_BASE_URL", env_updates["FILEJUMP_BASE_URL"].strip())
    if env_updates.get("FILEJUMP_UPLOAD_URL"):
        set_key(str(ENV_FILE), "FILEJUMP_UPLOAD_URL", env_updates["FILEJUMP_UPLOAD_URL"].strip())

    if "config" in data:
        existing_cfg = load_config()
        incoming_cfg = data["config"]
        incoming_cfg.setdefault("account_profiles", existing_cfg.get("account_profiles", {}))
        save_config(incoming_cfg)
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
            if api_res is not None:
                uploaded_count += 1
                remote_file_id = api_res.get("id") if isinstance(api_res, dict) else None
                record["files"][f_item["rel_path"]] = {
                    "remote_id": remote_file_id,
                    "folder_id": folder_id,
                    "size": f_item["size"],
                    "local_mtime": f_item["mtime"],
                    "updated_at": datetime.datetime.now(timezone.utc).isoformat()
                }
                log_event("success", f"Sincronizado: {f_item['rel_path']} (ID: {remote_file_id or 'OK'})")
    save_task_record(task_name, record)
    log_event("info", f"Sincronización finalizada: {uploaded_count}/{len(to_upload)} archivos subidos.")
    return jsonify({"success": True, "uploaded": uploaded_count, "total": len(to_upload)})


def _resolve_target_file():
    """Resuelve un archivo local directamente desde disco (sin caché) o valida/descarga remoto a temp_cache."""
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
        try:
            remote_mtime = float(request.args.get("mtime", 0) or 0)
        except ValueError:
            remote_mtime = 0.0
        if not remote_id:
            return None, "Falta remote_id"
        client = FileJumpClient()
        if not client.api_key:
            return None, "Falta FILEJUMP_API_KEY en .env"
        cached = client.download_file_to_cache(remote_id, filename, remote_mtime=remote_mtime)
        if not cached or not cached.exists():
            return None, "No se pudo descargar el archivo desde FileJump"
        return cached, None


def _video_needs_audio_transcode(file_path):
    """Detecta si un video contiene múltiples pistas de audio o códecs no soportados por navegadores (AC3, EAC3, DTS)."""
    ext = file_path.suffix.lower()
    if ext in {".avi", ".mkv", ".mov", ".wmv", ".flv"}:
        return True
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        try:
            out = subprocess.check_output(
                [ffprobe_bin, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)],
                stderr=subprocess.DEVNULL,
                timeout=8
            ).decode("utf-8", errors="ignore").strip().lower()
            codecs = [line.strip() for line in out.splitlines() if line.strip()]
            # Si hay más de 1 pista de audio o alguna usa AC3/DTS, el navegador suele silenciar el audio
            if len(codecs) > 1:
                return True
            for c in codecs:
                if c not in {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "pcm_s24le"}:
                    return True
        except Exception:
            pass
    return False


def _get_video_audio_tracks(file_path):
    """Obtiene la lista de pistas de audio (índice, códec, idioma, título) de un archivo de video."""
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return []
    try:
        out = subprocess.check_output(
            [
                ffprobe_bin, "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index,codec_name,channels:stream_tags=language,title",
                "-of", "json", str(file_path)
            ],
            stderr=subprocess.DEVNULL,
            timeout=8
        ).decode("utf-8", errors="ignore")
        parsed = json.loads(out)
        streams = parsed.get("streams", [])
        tracks = []
        for idx, s in enumerate(streams):
            tags = s.get("tags") or {}
            lang = tags.get("language") or tags.get("LANGUAGE") or ""
            title = tags.get("title") or tags.get("TITLE") or ""
            codec = s.get("codec_name") or "audio"
            channels = s.get("channels") or 2
            label_parts = [f"Pista {idx + 1}"]
            if title:
                label_parts.append(title)
            if lang:
                label_parts.append(f"[{lang}]")
            label_parts.append(f"({codec}, {channels}ch)")
            tracks.append({
                "audio_index": idx,
                "codec": codec,
                "language": lang,
                "title": title,
                "label": " ".join(label_parts)
            })
        return tracks
    except Exception:
        return []


@app.route("/api/video/tracks", methods=["GET"])
def api_video_tracks():
    """Devuelve las pistas de audio disponibles dentro de un archivo de video."""
    file_path, err = _resolve_target_file()
    if err:
        return jsonify({"tracks": []})
    tracks = _get_video_audio_tracks(file_path)
    return jsonify({"tracks": tracks})


@app.route("/api/file/stream", methods=["GET"])
def api_file_stream():
    """Sirve el archivo en línea (PDF en nueva ventana, audio, video con pista única AAC o imágenes)."""
    file_path, err = _resolve_target_file()
    if err:
        return jsonify({"error": err}), 404
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return send_file(file_path, mimetype="application/pdf", as_attachment=False, download_name=file_path.name, conditional=True)

    video_exts = {".mp4", ".webm", ".ogg", ".mov", ".avi", ".mkv", ".wmv", ".flv"}
    force_transcode = request.args.get("transcode") == "1"
    audio_track_param = request.args.get("audio_track", "").strip()
    has_explicit_track = audio_track_param.isdigit()
    audio_idx = int(audio_track_param) if has_explicit_track else 0

    # Si es video con múltiples pistas, pista seleccionada o códec AC3/DTS, remuxear aislando 1 sola pista en AAC
    if ext in video_exts and (force_transcode or has_explicit_track or _video_needs_audio_transcode(file_path)):
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            path_hash = hashlib.md5(f"{file_path}_{file_path.stat().st_mtime}_a{audio_idx}".encode("utf-8")).hexdigest()[:12]
            mp4_cached = TEMP_CACHE_DIR / f"{file_path.stem}_{path_hash}_web.mp4"
            if not mp4_cached.exists() or mp4_cached.stat().st_size == 0:
                try:
                    # Mapear explícitamente 1 pista de video (0:v:0) y 1 pista de audio (0:a:<audio_idx>) convertida a AAC estéreo
                    res = subprocess.run(
                        [
                            ffmpeg_bin, "-y", "-i", str(file_path),
                            "-map", "0:v:0", "-map", f"0:a:{audio_idx}?",
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                            "-movflags", "+faststart", str(mp4_cached)
                        ],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
                    )
                    if res.returncode != 0 or not mp4_cached.exists() or mp4_cached.stat().st_size == 0:
                        subprocess.run(
                            [
                                ffmpeg_bin, "-y", "-i", str(file_path),
                                "-map", "0:v:0", "-map", f"0:a:{audio_idx}?",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                                "-movflags", "+faststart", str(mp4_cached)
                            ],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300
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
    """Descarga/actualiza en segundo plano en temp_cache la siguiente pista remota verificando su fecha."""
    source = request.args.get("source", "local").strip()
    if source != "remote":
        return jsonify({"prefetched": False, "reason": "local_direct"})
    remote_id = request.args.get("remote_id", "").strip()
    filename = request.args.get("name", f"audio_{remote_id}").strip()
    try:
        remote_mtime = float(request.args.get("mtime", 0) or 0)
    except ValueError:
        remote_mtime = 0.0
    if not remote_id:
        return jsonify({"prefetched": False, "error": "Falta remote_id"}), 400
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"prefetched": False, "error": "Sin API key"}), 400
    cached = client.download_file_to_cache(remote_id, filename, remote_mtime=remote_mtime)
    return jsonify({"prefetched": bool(cached and cached.exists()), "remote_id": remote_id})


@app.route("/api/cache/status", methods=["GET"])
def api_cache_status():
    """Devuelve los IDs remotos que ya existen físicamente en la caché persistente."""
    cache_index = load_cache_index()
    cached_ids = set()

    for rid, meta in cache_index.get("files", {}).items():
        fname = meta.get("filename", "")
        if fname and (TEMP_CACHE_DIR / fname).exists():
            cached_ids.add(str(rid))

    # Detectar también archivos en temp_cache con prefijo "{remote_id}_" aunque no estén en el índice
    if TEMP_CACHE_DIR.exists():
        for entry in TEMP_CACHE_DIR.iterdir():
            if entry.is_file() and not entry.name.startswith(".") and "_" in entry.name:
                prefix_id = entry.name.split("_", 1)[0]
                if prefix_id.isdigit() and entry.stat().st_size > 0:
                    cached_ids.add(prefix_id)

    return jsonify({"cached_ids": sorted(cached_ids)})


@app.route("/api/cache/clear", methods=["GET", "POST"])
def api_cache_clear():
    """Consulta el tamaño de la caché o elimina todos los archivos definitivamente sin pasar por la papelera."""
    if request.method == "GET":
        total_size = 0
        file_count = 0
        if TEMP_CACHE_DIR.exists():
            for entry in TEMP_CACHE_DIR.iterdir():
                if entry.is_file() and not entry.name.startswith("."):
                    file_count += 1
                    try:
                        total_size += entry.stat().st_size
                    except OSError:
                        pass
        return jsonify({"file_count": file_count, "total_bytes": total_size})

    # Eliminación definitiva directa en disco (os.unlink / shutil.rmtree, sin papelera)
    deleted_count = 0
    freed_bytes = 0
    current_hash = _compute_api_key_hash(os.getenv("FILEJUMP_API_KEY", "").strip())

    if TEMP_CACHE_DIR.exists():
        for entry in list(TEMP_CACHE_DIR.iterdir()):
            try:
                if entry.is_file() or entry.is_symlink():
                    size = entry.stat().st_size if not entry.name.startswith(".") else 0
                    entry.unlink()
                    if not entry.name.startswith("."):
                        deleted_count += 1
                        freed_bytes += size
                elif entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
            except Exception as e:
                log_event("warning", f"No se pudo eliminar '{entry.name}' de caché: {e}")

    TEMP_CACHE_DIR.mkdir(exist_ok=True)
    save_cache_index({"api_key_hash": current_hash, "files": {}})
    log_event("info", f"Caché local eliminada definitivamente ({deleted_count} archivo(s) borrados sin papelera).")
    return jsonify({"success": True, "deleted_count": deleted_count, "freed_bytes": freed_bytes})


def _resolve_local_explorer_target(data):
    """Resuelve la ruta absoluta de un archivo o carpeta seleccionada en el explorador local."""
    full_path = (data.get("full_path") or "").strip()
    if full_path:
        return Path(full_path).resolve()
    root_path = (data.get("root_path") or "").strip()
    task_name = (data.get("task") or "").strip()
    rel_path = (data.get("rel_path") or "").strip().strip("/")
    if root_path:
        base = Path(root_path).resolve()
        return (base / rel_path).resolve() if rel_path else base
    if task_name:
        cfg = load_config()
        task = next((t for t in cfg.get("tasks", []) if t["name"] == task_name), None)
        if task:
            base = Path(task["path"]).resolve()
            return (base / rel_path).resolve() if rel_path else base
    return None


@app.route("/api/explorer/audio_scan_start", methods=["POST"])
def api_explorer_audio_scan_start():
    """Inicia en segundo plano el escaneo recursivo de archivos de audio con progreso en tiempo real."""
    data = request.json or {}
    source = data.get("source", "local")
    item_name = data.get("name") or "Selección"
    audio_exts = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
    job_id = uuid.uuid4().hex[:12]

    if source == "local":
        target = _resolve_local_explorer_target(data)
        if not target or not target.exists():
            return jsonify({"error": "Ruta local no encontrada"}), 404
        item_name = target.name or item_name
    else:
        client = FileJumpClient()
        if not client.api_key:
            return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400

    audio_scan_jobs[job_id] = {
        "id": job_id,
        "source": source,
        "title": f"Escaneando: {item_name}",
        "status": "running",
        "current_folder": item_name,
        "found_count": 0,
        "scanned_folders": 0,
        "tracks": [],
        "error": None
    }

    def _run_audio_scan():
        job = audio_scan_jobs[job_id]
        found_tracks = []
        try:
            if source == "local":
                if target.is_file():
                    job["scanned_folders"] = 1
                    if target.suffix.lower() in audio_exts:
                        st = target.stat()
                        found_tracks.append({
                            "type": "file",
                            "name": target.name,
                            "rel_path": target.name,
                            "full_path": str(target),
                            "size": st.st_size,
                            "mtime": st.st_mtime
                        })
                        job["found_count"] = len(found_tracks)
                else:
                    for root, dirs, files in os.walk(target):
                        dirs.sort(key=str.lower)
                        job["scanned_folders"] += 1
                        rel_dir = Path(root).relative_to(target).as_posix()
                        job["current_folder"] = item_name if rel_dir == "." else rel_dir
                        for fname in sorted(files, key=str.lower):
                            fp = Path(root) / fname
                            if fp.suffix.lower() in audio_exts:
                                try:
                                    st = fp.stat()
                                    found_tracks.append({
                                        "type": "file",
                                        "name": fname,
                                        "rel_path": fp.relative_to(target).as_posix(),
                                        "full_path": str(fp),
                                        "size": st.st_size,
                                        "mtime": st.st_mtime
                                    })
                                    job["found_count"] = len(found_tracks)
                                except OSError:
                                    pass
            else:
                client = FileJumpClient()
                folder_id = data.get("id")
                if folder_id in (None, "", "ROOT"):
                    folder_id = "ROOT"
                elif str(folder_id).isdigit():
                    folder_id = int(folder_id)

                job["scanned_folders"] = 1

                def _on_remote_entry(entry):
                    if entry["type"] == "folder":
                        job["scanned_folders"] += 1
                        job["current_folder"] = entry.get("rel_path") or entry.get("name") or item_name
                    elif entry["type"] == "file" and Path(entry["name"]).suffix.lower() in audio_exts:
                        found_tracks.append(entry)
                        job["found_count"] = len(found_tracks)

                scan_remote_tree(client, folder_id, current_prefix="", depth=0, max_depth=12, on_item_callback=_on_remote_entry)

            job["tracks"] = found_tracks
            job["found_count"] = len(found_tracks)
            job["status"] = "completed"
            job["current_folder"] = "Escaneo completado"
            origen_lbl = "equipo local" if source == "local" else "nube FileJump"
            log_event("success", f"🎵 Playlist: se agregaron {len(found_tracks)} archivo(s) de audio desde '{item_name}' ({origen_lbl}).")
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            log_event("error", f"Error escaneando audio en '{item_name}': {exc}")

    threading.Thread(target=_run_audio_scan, daemon=True).start()
    return jsonify({"job_id": job_id, "title": f"Escaneando: {item_name}"})


@app.route("/api/explorer/audio_scan_status/<job_id>", methods=["GET"])
def api_explorer_audio_scan_status(job_id):
    """Devuelve el progreso en tiempo real del escaneo de archivos de audio."""
    job = audio_scan_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Escaneo no encontrado"}), 404
    resp = {
        "id": job["id"],
        "source": job["source"],
        "title": job["title"],
        "status": job["status"],
        "current_folder": job["current_folder"],
        "found_count": job["found_count"],
        "scanned_folders": job["scanned_folders"],
        "error": job["error"]
    }
    if job["status"] == "completed":
        resp["tracks"] = job["tracks"]
    return jsonify(resp)


@app.route("/api/explorer/audio_collect", methods=["POST"])
def api_explorer_audio_collect():
    """Recorre recursivamente una carpeta (local o remota) y devuelve todos los archivos multimedia de audio."""
    data = request.json or {}
    source = data.get("source", "local")
    audio_exts = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
    tracks = []

    if source == "local":
        target = _resolve_local_explorer_target(data)
        if not target or not target.exists():
            return jsonify({"error": "Ruta local no encontrada"}), 404
        if target.is_file():
            if target.suffix.lower() in audio_exts:
                st = target.stat()
                tracks.append({
                    "type": "file",
                    "name": target.name,
                    "rel_path": target.name,
                    "full_path": str(target),
                    "size": st.st_size,
                    "mtime": st.st_mtime
                })
        else:
            for root, dirs, files in os.walk(target):
                dirs.sort(key=str.lower)
                for fname in sorted(files, key=str.lower):
                    fp = Path(root) / fname
                    if fp.suffix.lower() in audio_exts:
                        try:
                            st = fp.stat()
                            tracks.append({
                                "type": "file",
                                "name": fname,
                                "rel_path": fp.relative_to(target).as_posix(),
                                "full_path": str(fp),
                                "size": st.st_size,
                                "mtime": st.st_mtime
                            })
                        except OSError:
                            pass
        return jsonify({"tracks": tracks})

    # Remoto: explorar recursivamente la carpeta en FileJump
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400
    folder_id = data.get("id")
    if folder_id in (None, "", "ROOT"):
        folder_id = "ROOT"
    elif str(folder_id).isdigit():
        folder_id = int(folder_id)

    remote_entries = scan_remote_tree(client, folder_id, current_prefix="", depth=0, max_depth=12)
    for item in remote_entries:
        if item["type"] == "file" and Path(item["name"]).suffix.lower() in audio_exts:
            tracks.append(item)
    return jsonify({"tracks": tracks})


@app.route("/api/explorer/rename", methods=["POST"])
def api_explorer_rename():
    """Cambia el nombre de un archivo o carpeta local o remota."""
    data = request.json or {}
    source = data.get("source", "local")
    new_name = (data.get("new_name") or "").strip()
    if not new_name or any(sep in new_name for sep in ("/", "\\")):
        return jsonify({"error": "Nombre inválido"}), 400

    if source == "local":
        target = _resolve_local_explorer_target(data)
        if not target or not target.exists():
            return jsonify({"error": "Elemento local no encontrado"}), 404
        dest = target.parent / new_name
        if dest.exists():
            return jsonify({"error": "Ya existe un elemento con ese nombre"}), 400
        try:
            target.rename(dest)
            log_event("info", f"Local renombrado: '{target.name}' -> '{new_name}'")
            return jsonify({"success": True, "new_path": str(dest)})
        except Exception as e:
            return jsonify({"error": f"No se pudo renombrar: {e}"}), 500

    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400
    item_id = data.get("id")
    item_type = data.get("type", "file")
    if not item_id or item_id == "ROOT":
        return jsonify({"error": "ID remoto inválido"}), 400
    ok = client.rename_item(item_type, item_id, new_name)
    if ok:
        log_event("info", f"Remoto #{item_id} renombrado a '{new_name}'")
        return jsonify({"success": True})
    return jsonify({"error": "La API de FileJump no permitió renombrar este elemento"}), 400


@app.route("/api/explorer/delete", methods=["POST"])
def api_explorer_delete():
    """Elimina un archivo o carpeta local (definitivo) o remoto."""
    data = request.json or {}
    source = data.get("source", "local")

    if source == "local":
        target = _resolve_local_explorer_target(data)
        if not target or not target.exists():
            return jsonify({"error": "Elemento local no encontrado"}), 404
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            log_event("info", f"Eliminado localmente: '{target.name}'")
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": f"No se pudo eliminar: {e}"}), 500

    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400
    item_id = data.get("id")
    item_type = data.get("type", "file")
    if not item_id or item_id == "ROOT":
        return jsonify({"error": "No se puede eliminar la raíz"}), 400
    ok = client.delete_folder(item_id) if item_type == "folder" else client.delete_file(item_id)
    if ok:
        log_event("info", f"Eliminado en FileJump: #{item_id}")
        return jsonify({"success": True})
    return jsonify({"error": "No se pudo eliminar el elemento en FileJump"}), 400


@app.route("/api/explorer/properties", methods=["POST"])
def api_explorer_properties():
    """Obtiene propiedades detalladas (tamaño, conteo de hijos, fechas, IDs) de un archivo o carpeta."""
    data = request.json or {}
    source = data.get("source", "local")

    if source == "local":
        target = _resolve_local_explorer_target(data)
        if not target or not target.exists():
            return jsonify({"error": "Elemento local no encontrado"}), 404
        st = target.stat()
        total_size = 0
        file_count = 0
        folder_count = 0
        if target.is_file():
            total_size = st.st_size
            file_count = 1
        else:
            for root, dirs, files in os.walk(target):
                folder_count += len(dirs)
                for fn in files:
                    file_count += 1
                    try:
                        total_size += (Path(root) / fn).stat().st_size
                    except OSError:
                        pass
        return jsonify({
            "name": target.name,
            "type": "Carpeta Local" if target.is_dir() else "Archivo Local",
            "location": str(target),
            "size": total_size,
            "file_count": file_count,
            "folder_count": folder_count,
            "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "created": datetime.datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
        })

    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400
    item_id = data.get("id")
    item_type = data.get("type", "file")
    item_name = data.get("name", f"#{item_id}")
    mtime = float(data.get("mtime") or 0)
    size = int(data.get("size") or 0)
    file_count = 1 if item_type == "file" else 0
    folder_count = 0

    if item_type == "folder" and item_id not in (None, "", "ROOT"):
        tid = int(item_id) if str(item_id).isdigit() else item_id
        descendants = scan_remote_tree(client, tid, current_prefix="", depth=0, max_depth=10)
        for d in descendants:
            if d["type"] == "folder":
                folder_count += 1
            else:
                file_count += 1
                size += int(d.get("size") or 0)

    return jsonify({
        "name": item_name,
        "type": "Carpeta Remota (FileJump)" if item_type == "folder" else "Archivo Remoto (FileJump)",
        "location": f"FileJump ID #{item_id}",
        "size": size,
        "file_count": file_count,
        "folder_count": folder_count,
        "modified": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime > 0 else "N/D",
        "created": "N/D"
    })


@app.route("/api/explorer/download_to_local", methods=["POST"])
def api_explorer_download_to_local():
    """Descarga un archivo o carpeta completa de FileJump hacia la carpeta local destino copiando su mtime."""
    data = request.json or {}
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400

    dest_dir = _resolve_local_explorer_target({
        "full_path": data.get("dest_full_path"),
        "root_path": data.get("dest_root_path"),
        "task": data.get("dest_task"),
        "rel_path": data.get("dest_rel_path")
    })
    if not dest_dir or not dest_dir.exists() or not dest_dir.is_dir():
        return jsonify({"error": "Selecciona primero una carpeta válida en el explorador local destino"}), 400

    item_type = data.get("type", "file")
    item_id = data.get("id")
    item_name = data.get("name", f"item_{item_id}")
    remote_mtime = float(data.get("mtime") or 0)

    downloaded_files = 0
    if item_type == "file":
        if remote_mtime <= 0:
            remote_mtime = client.get_remote_file_mtime(item_id)
        target_file = dest_dir / item_name
        if client.download_file_direct(item_id, target_file, remote_mtime=remote_mtime):
            downloaded_files = 1
            log_event("success", f"Descargado '{item_name}' -> '{target_file}' (fecha preservada).")
        else:
            return jsonify({"error": f"No se pudo descargar '{item_name}'"}), 500
    else:
        tid = int(item_id) if str(item_id).isdigit() else item_id
        base_local_folder = dest_dir / item_name
        base_local_folder.mkdir(parents=True, exist_ok=True)
        tree_items = scan_remote_tree(client, tid, current_prefix="", depth=0, max_depth=12)

        for entry in tree_items:
            rel_sub = Path(entry["rel_path"])
            local_target = base_local_folder / rel_sub
            if entry["type"] == "folder":
                local_target.mkdir(parents=True, exist_ok=True)
            else:
                rmtime = float(entry.get("mtime") or 0)
                if client.download_file_direct(entry["id"], local_target, remote_mtime=rmtime):
                    downloaded_files += 1

        # Aplicar también la fecha de modificación de la carpeta raíz descargada si está disponible
        if remote_mtime > 0:
            try:
                os.utime(base_local_folder, (remote_mtime, remote_mtime))
            except Exception:
                pass
        log_event("success", f"Carpeta '{item_name}' descargada ({downloaded_files} archivo(s)) hacia '{dest_dir}'.")

    return jsonify({"success": True, "downloaded_files": downloaded_files})


def _update_transfer_job(job_id, **kwargs):
    """Actualiza el estado en tiempo real de una transferencia en curso."""
    job = transfer_jobs.get(job_id)
    if job:
        job.update(kwargs)


def _format_speed_label(bytes_per_sec):
    """Formatea la velocidad instantánea o promedio en KB/s o MB/s."""
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "0.00 MB/s"
    mb_s = bytes_per_sec / (1024.0 * 1024.0)
    if mb_s >= 1.0:
        return f"{mb_s:.2f} MB/s"
    kb_s = bytes_per_sec / 1024.0
    return f"{kb_s:.1f} KB/s"


def _make_speed_tracker(job_id, base_bytes_before_file, overall_total_bytes, items_list=None, item_idx=None):
    """Crea un callback de progreso en tiempo real por archivo con cálculo de velocidad suavizada y porcentaje."""
    state = {
        "last_time": time.time(),
        "last_bytes": 0,
        "ema_bps": 0.0
    }

    def _on_progress(file_sent, file_total):
        now = time.time()
        dt = now - state["last_time"]
        if dt >= 0.10 and file_sent >= state["last_bytes"]:
            inst_bps = (file_sent - state["last_bytes"]) / dt
            state["ema_bps"] = inst_bps if state["ema_bps"] <= 0 else (0.65 * state["ema_bps"] + 0.35 * inst_bps)
            state["last_time"] = now
            state["last_bytes"] = file_sent

        total_done = base_bytes_before_file + file_sent
        eff_total = max(overall_total_bytes, total_done, 1)
        pct = min(99, int(round((total_done / eff_total) * 100)))
        file_pct = min(100, int(round((file_sent / max(file_total, 1)) * 100))) if file_total > 0 else 0

        if items_list is not None and item_idx is not None and 0 <= item_idx < len(items_list):
            items_list[item_idx]["sent"] = file_sent
            items_list[item_idx]["file_pct"] = file_pct

        _update_transfer_job(
            job_id,
            bytes_done=total_done,
            bytes_total=max(overall_total_bytes, total_done),
            percent=pct,
            speed_bps=state["ema_bps"],
            speed_text=_format_speed_label(state["ema_bps"]),
            file_percent=file_pct,
            items=list(items_list) if items_list is not None else transfer_jobs.get(job_id, {}).get("items", [])
        )

    return _on_progress


@app.route("/api/transfer/status/<job_id>", methods=["GET"])
def api_transfer_status(job_id):
    """Devuelve el progreso en tiempo real de una transferencia (carga o descarga)."""
    job = transfer_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Trabajo de transferencia no encontrado"}), 404
    return jsonify(job)


@app.route("/api/explorer/transfer_start", methods=["POST"])
def api_explorer_transfer_start():
    """Inicia una carga o descarga en segundo plano con seguimiento de progreso y velocidad en tiempo real."""
    data = request.json or {}
    direction = data.get("direction", "upload")
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400

    job_id = uuid.uuid4().hex[:12]

    if direction == "download":
        dest_dir = _resolve_local_explorer_target({
            "full_path": data.get("dest_full_path"),
            "root_path": data.get("dest_root_path"),
            "task": data.get("dest_task"),
            "rel_path": data.get("dest_rel_path")
        })
        if not dest_dir or not dest_dir.exists() or not dest_dir.is_dir():
            return jsonify({"error": "Selecciona primero una carpeta válida en el explorador local destino"}), 400

        item_type = data.get("type", "file")
        item_name = data.get("name", "archivo")

        transfer_jobs[job_id] = {
            "id": job_id,
            "direction": "download",
            "title": f"Descargando: {item_name}",
            "status": "running",
            "current_file": "Analizando estructura remota...",
            "processed_files": 0,
            "total_files": 1 if item_type == "file" else 0,
            "bytes_done": 0,
            "bytes_total": int(data.get("size") or 0),
            "percent": 0,
            "file_percent": 0,
            "speed_bps": 0.0,
            "speed_text": "0.00 MB/s",
            "items": [],
            "error": None
        }

        def _run_download():
            try:
                item_id = data.get("id")
                remote_mtime = float(data.get("mtime") or 0)
                log_event("info", f"Iniciando descarga de '{item_name}' hacia '{dest_dir}'...")

                if item_type == "file":
                    if remote_mtime <= 0:
                        remote_mtime = client.get_remote_file_mtime(item_id)
                    f_size = int(data.get("size") or 0)
                    single_items = [{"name": item_name, "size": f_size, "sent": 0, "file_pct": 0, "status": "active"}]
                    _update_transfer_job(
                        job_id,
                        total_files=1,
                        bytes_total=f_size,
                        current_file=item_name,
                        items=list(single_items)
                    )
                    log_event("info", f"⬇ Descargando archivo: {item_name}")
                    target_file = dest_dir / item_name
                    cb = _make_speed_tracker(job_id, 0, f_size, single_items, 0)
                    if client.download_file_direct(item_id, target_file, remote_mtime=remote_mtime, progress_callback=cb):
                        actual_size = target_file.stat().st_size if target_file.exists() else f_size
                        _update_transfer_job(
                            job_id,
                            status="completed",
                            processed_files=1,
                            bytes_done=actual_size,
                            bytes_total=max(f_size, actual_size),
                            percent=100,
                            file_percent=100,
                            speed_text="Completado",
                            current_file="Descarga completada",
                            items=[{"name": item_name, "size": actual_size, "sent": actual_size, "file_pct": 100, "status": "done"}]
                        )
                        log_event("success", f"✔ Descargado '{item_name}' -> '{target_file}'")
                    else:
                        _update_transfer_job(
                            job_id,
                            status="error",
                            error=f"No se pudo descargar '{item_name}'",
                            items=[{"name": item_name, "size": f_size, "sent": 0, "file_pct": 0, "status": "error"}]
                        )
                        log_event("error", f"✖ Error descargando '{item_name}'")
                else:
                    tid = int(item_id) if str(item_id).isdigit() else item_id
                    base_local_folder = dest_dir / item_name
                    base_local_folder.mkdir(parents=True, exist_ok=True)
                    tree_items = scan_remote_tree(client, tid, current_prefix="", depth=0, max_depth=12)

                    files_to_dl = [e for e in tree_items if e["type"] == "file"]
                    folders_to_mk = [e for e in tree_items if e["type"] == "folder"]
                    total_bytes = sum(int(f.get("size") or 0) for f in files_to_dl)
                    items_list = [{"name": f["rel_path"], "size": int(f.get("size") or 0), "sent": 0, "file_pct": 0, "status": "pending"} for f in files_to_dl]

                    _update_transfer_job(
                        job_id,
                        total_files=len(files_to_dl),
                        bytes_total=total_bytes,
                        items=items_list,
                        current_file="Creando subcarpetas locales..."
                    )

                    for fld in folders_to_mk:
                        (base_local_folder / Path(fld["rel_path"])).mkdir(parents=True, exist_ok=True)

                    if len(files_to_dl) == 0:
                        _update_transfer_job(job_id, status="completed", percent=100, current_file="Carpeta vacía descargada")
                        log_event("success", f"✔ Carpeta vacía '{item_name}' creada en '{dest_dir}'.")
                        return

                    done_count = 0
                    done_bytes = 0
                    for idx, f_entry in enumerate(files_to_dl):
                        items_list[idx]["status"] = "active"
                        _update_transfer_job(job_id, current_file=f_entry["rel_path"], file_percent=0, items=list(items_list))
                        log_event("info", f"⬇ Descargando ({idx + 1}/{len(files_to_dl)}): {f_entry['rel_path']}")

                        local_target = base_local_folder / Path(f_entry["rel_path"])
                        rmtime = float(f_entry.get("mtime") or 0)
                        cb = _make_speed_tracker(job_id, done_bytes, total_bytes, items_list, idx)
                        ok = client.download_file_direct(f_entry["id"], local_target, remote_mtime=rmtime, progress_callback=cb)
                        f_sz = int(f_entry.get("size") or 0)
                        if ok:
                            done_count += 1
                            done_bytes += f_sz
                            items_list[idx]["status"] = "done"
                            items_list[idx]["sent"] = f_sz
                            items_list[idx]["file_pct"] = 100
                            log_event("success", f"✔ Descargado: {f_entry['rel_path']}")
                        else:
                            items_list[idx]["status"] = "error"
                            log_event("error", f"✖ Fallo al descargar: {f_entry['rel_path']}")

                        pct = int(round((done_bytes / total_bytes) * 100)) if total_bytes > 0 else int(round(((idx + 1) / len(files_to_dl)) * 100))
                        _update_transfer_job(
                            job_id,
                            processed_files=done_count,
                            bytes_done=done_bytes,
                            percent=min(99 if idx + 1 < len(files_to_dl) else 100, pct),
                            items=list(items_list)
                        )

                    if remote_mtime > 0:
                        try:
                            os.utime(base_local_folder, (remote_mtime, remote_mtime))
                        except Exception:
                            pass

                    _update_transfer_job(
                        job_id,
                        status="completed",
                        percent=100,
                        speed_text="Completado",
                        current_file="Descarga completada"
                    )
                    log_event("success", f"✔ Carpeta '{item_name}' descargada ({done_count}/{len(files_to_dl)} archivo(s)).")
            except Exception as exc:
                _update_transfer_job(job_id, status="error", error=str(exc))
                log_event("error", f"Error crítico en descarga: {exc}")

        threading.Thread(target=_run_download, daemon=True).start()
        return jsonify({"job_id": job_id})

    # === DIRECCIÓN: UPLOAD (CARGA A FILEJUMP) ===
    src_target = _resolve_local_explorer_target(data)
    if not src_target or not src_target.exists():
        return jsonify({"error": "Archivo o carpeta local no encontrado"}), 404

    dest_remote_id = data.get("dest_remote_id", "ROOT")
    target_parent_id = None if dest_remote_id in (None, "", "ROOT", 0, "0") else (int(dest_remote_id) if str(dest_remote_id).isdigit() else dest_remote_id)
    cfg = load_config()
    use_hs = cfg.get("use_high_speed_upload", True)

    transfer_jobs[job_id] = {
        "id": job_id,
        "direction": "upload",
        "title": f"Subiendo: {src_target.name}",
        "status": "running",
        "current_file": "Preparando archivos locales...",
        "processed_files": 0,
        "total_files": 1 if src_target.is_file() else 0,
        "bytes_done": 0,
        "bytes_total": src_target.stat().st_size if src_target.is_file() else 0,
        "percent": 0,
        "file_percent": 0,
        "speed_bps": 0.0,
        "speed_text": "0.00 MB/s",
        "items": [],
        "error": None
    }

    def _run_upload():
        def _upload_single(local_fp, remote_parent, progress_cb=None):
            st = local_fp.stat()
            mtime_iso = datetime.datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
            res = client.upload_file(str(local_fp), folder_id=remote_parent, use_high_speed=use_hs, progress_callback=progress_cb)
            if res is not None:
                new_id = res.get("id") if isinstance(res, dict) else None
                if new_id:
                    try:
                        requests.put(
                            f"{client.base_url}/files/{new_id}",
                            headers=client._headers(),
                            json={"updated_at": mtime_iso, "last_modified": int(st.st_mtime * 1000)},
                            timeout=10
                        )
                    except Exception:
                        pass
                return True
            return False

        try:
            log_event("info", f"Iniciando carga de '{src_target.name}' hacia FileJump (carpeta #{dest_remote_id})...")
            if src_target.is_file():
                f_size = src_target.stat().st_size
                single_items = [{"name": src_target.name, "size": f_size, "sent": 0, "file_pct": 0, "status": "active"}]
                _update_transfer_job(
                    job_id,
                    total_files=1,
                    bytes_total=f_size,
                    current_file=src_target.name,
                    items=list(single_items)
                )
                log_event("info", f"⬆ Subiendo archivo: {src_target.name}")
                cb = _make_speed_tracker(job_id, 0, f_size, single_items, 0)
                if _upload_single(src_target, target_parent_id, progress_cb=cb):
                    _update_transfer_job(
                        job_id,
                        status="completed",
                        processed_files=1,
                        bytes_done=f_size,
                        percent=100,
                        file_percent=100,
                        speed_text="Completado",
                        current_file="Carga completada",
                        items=[{"name": src_target.name, "size": f_size, "sent": f_size, "file_pct": 100, "status": "done"}]
                    )
                    log_event("success", f"✔ Archivo '{src_target.name}' subido a FileJump.")
                else:
                    detail = getattr(client, "last_upload_error", "Error desconocido de la API")
                    _update_transfer_job(
                        job_id,
                        status="error",
                        error=f"Fallo al subir '{src_target.name}': {detail}",
                        items=[{"name": src_target.name, "size": f_size, "sent": 0, "file_pct": 0, "status": "error"}]
                    )
                    log_event("error", f"✖ Fallo al subir '{src_target.name}': {detail}")
            else:
                src_mtime = src_target.stat().st_mtime
                root_remote_folder_id = client.create_folder(src_target.name, parent_id=target_parent_id)
                if not root_remote_folder_id:
                    _update_transfer_job(job_id, status="error", error=f"No se pudo crear la carpeta remota '{src_target.name}'")
                    return
                client._sync_remote_timestamp("folder", root_remote_folder_id, src_mtime)

                files_plan = []
                folders_plan = []
                for root, dirs, files in os.walk(src_target):
                    rel_root = Path(root).relative_to(src_target).as_posix()
                    if rel_root == ".":
                        rel_root = ""
                    for d in sorted(dirs):
                        rel_d = f"{rel_root}/{d}".lstrip("/")
                        folders_plan.append((rel_root, d, rel_d, Path(root) / d))
                    for fn in sorted(files):
                        fp = Path(root) / fn
                        rel_f = f"{rel_root}/{fn}".lstrip("/")
                        files_plan.append((rel_root, rel_f, fp, fp.stat().st_size))

                total_bytes = sum(x[3] for x in files_plan)
                items_list = [{"name": x[1], "size": x[3], "sent": 0, "file_pct": 0, "status": "pending"} for x in files_plan]
                _update_transfer_job(
                    job_id,
                    total_files=len(files_plan),
                    bytes_total=total_bytes,
                    items=items_list,
                    current_file="Creando estructura de carpetas en FileJump..."
                )

                folder_map = {"": root_remote_folder_id}
                for rel_parent, d_name, rel_d, sub_dir_path in folders_plan:
                    curr_parent_id = folder_map.get(rel_parent, root_remote_folder_id)
                    new_fid = client.create_folder(d_name, parent_id=curr_parent_id)
                    if new_fid:
                        folder_map[rel_d] = new_fid
                        client._sync_remote_timestamp("folder", new_fid, sub_dir_path.stat().st_mtime)

                if len(files_plan) == 0:
                    _update_transfer_job(job_id, status="completed", percent=100, current_file="Carpeta vacía subida")
                    log_event("success", f"✔ Carpeta vacía '{src_target.name}' creada en FileJump.")
                    return

                done_count = 0
                done_bytes = 0
                for idx, (rel_parent, rel_f, fp, f_sz) in enumerate(files_plan):
                    items_list[idx]["status"] = "active"
                    _update_transfer_job(job_id, current_file=rel_f, file_percent=0, items=list(items_list))
                    log_event("info", f"⬆ Subiendo ({idx + 1}/{len(files_plan)}): {rel_f}")

                    curr_parent_id = folder_map.get(rel_parent, root_remote_folder_id)
                    cb = _make_speed_tracker(job_id, done_bytes, total_bytes, items_list, idx)
                    if _upload_single(fp, curr_parent_id, progress_cb=cb):
                        done_count += 1
                        done_bytes += f_sz
                        items_list[idx]["status"] = "done"
                        items_list[idx]["sent"] = f_sz
                        items_list[idx]["file_pct"] = 100
                        log_event("success", f"✔ Subido: {rel_f}")
                    else:
                        items_list[idx]["status"] = "error"
                        log_event("error", f"✖ Fallo al subir: {rel_f}")

                    pct = int(round((done_bytes / total_bytes) * 100)) if total_bytes > 0 else int(round(((idx + 1) / len(files_plan)) * 100))
                    _update_transfer_job(
                        job_id,
                        processed_files=done_count,
                        bytes_done=done_bytes,
                        percent=min(99 if idx + 1 < len(files_plan) else 100, pct),
                        items=list(items_list)
                    )

                _update_transfer_job(
                    job_id,
                    status="completed",
                    percent=100,
                    speed_text="Completado",
                    current_file="Carga completada"
                )
                log_event("success", f"✔ Carpeta '{src_target.name}' subida a FileJump ({done_count}/{len(files_plan)} archivo(s)).")
        except Exception as exc:
            _update_transfer_job(job_id, status="error", error=str(exc))
            log_event("error", f"Error crítico en carga: {exc}")

    threading.Thread(target=_run_upload, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/explorer/upload_to_remote", methods=["POST"])
def api_explorer_upload_to_remote():
    """Sube un archivo o carpeta completa desde local hacia la carpeta remota destino copiando su mtime."""
    data = request.json or {}
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY"}), 400

    src_target = _resolve_local_explorer_target(data)
    if not src_target or not src_target.exists():
        return jsonify({"error": "Archivo o carpeta local no encontrado"}), 404

    dest_remote_id = data.get("dest_remote_id", "ROOT")
    target_parent_id = None if dest_remote_id in (None, "", "ROOT", 0, "0") else (int(dest_remote_id) if str(dest_remote_id).isdigit() else dest_remote_id)

    cfg = load_config()
    use_hs = cfg.get("use_high_speed_upload", True)
    uploaded_files = 0

    def _upload_with_mtime(local_file_path, remote_parent):
        st = local_file_path.stat()
        mtime_iso = datetime.datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        res = client.upload_file(str(local_file_path), folder_id=remote_parent, use_high_speed=use_hs)
        if res is not None:
            new_id = res.get("id") if isinstance(res, dict) else None
            # Intentar sincronizar la fecha de modificación original en FileJump si la API devolvió un ID
            if new_id:
                try:
                    requests.put(
                        f"{client.base_url}/files/{new_id}",
                        headers=client._headers(),
                        json={"updated_at": mtime_iso, "last_modified": int(st.st_mtime * 1000)},
                        timeout=10
                    )
                except Exception:
                    pass
            return True
        return False

    if src_target.is_file():
        if _upload_with_mtime(src_target, target_parent_id):
            uploaded_files = 1
            log_event("success", f"Archivo '{src_target.name}' subido a FileJump (carpeta #{dest_remote_id}).")
        else:
            detail = getattr(client, "last_upload_error", "Error desconocido de la API")
            return jsonify({"error": f"Fallo al subir '{src_target.name}': {detail}"}), 500
    else:
        src_mtime = src_target.stat().st_mtime
        root_remote_folder_id = client.create_folder(src_target.name, parent_id=target_parent_id)
        if not root_remote_folder_id:
            return jsonify({"error": f"No se pudo crear la carpeta remota '{src_target.name}'"}), 500
        client._sync_remote_timestamp("folder", root_remote_folder_id, src_mtime)

        folder_map = {"": root_remote_folder_id}
        for root, dirs, files in os.walk(src_target):
            rel_root = Path(root).relative_to(src_target).as_posix()
            if rel_root == ".":
                rel_root = ""
            curr_parent_id = folder_map.get(rel_root, root_remote_folder_id)

            for d in sorted(dirs):
                rel_d = f"{rel_root}/{d}".lstrip("/")
                sub_dir_path = Path(root) / d
                sub_dir_mtime = sub_dir_path.stat().st_mtime
                new_fid = client.create_folder(d, parent_id=curr_parent_id)
                if new_fid:
                    folder_map[rel_d] = new_fid
                    client._sync_remote_timestamp("folder", new_fid, sub_dir_mtime)

            for fn in sorted(files):
                fp = Path(root) / fn
                if _upload_with_mtime(fp, curr_parent_id):
                    uploaded_files += 1

        log_event("success", f"Carpeta '{src_target.name}' subida a FileJump ({uploaded_files} archivo(s)).")

    return jsonify({"success": True, "uploaded_files": uploaded_files})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Finaliza el servidor Python y cierra la ventana de consola (CMD/Terminal) asociada."""
    log_event("warning", "Solicitud de cierre recibida. Cerrando servidor y consola...")

    def _terminate_app():
        time.sleep(0.35)
        try:
            if os.name == "nt":
                # En Windows, cierra la ventana de consola asociada al proceso si existe y finaliza el proceso
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd:
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                subprocess.Popen(
                    f"taskkill /F /T /PID {os.getpid()}",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            else:
                os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_terminate_app, daemon=True).start()
    return jsonify({"success": True, "message": "Cerrando aplicación..."})


# ==============================================================================
# INTERFAZ WEB (HTML + TAILWIND CSS + VANILLA JS)
# ==============================================================================
HTML_TEMPLATE = r"""
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
<body class="bg-slate-950 text-slate-100 min-h-screen lg:h-screen lg:overflow-hidden flex flex-col">

    <!-- Barra Superior -->
    <header class="bg-slate-900/90 backdrop-blur border-b border-slate-800 px-3 sm:px-5 py-2.5 flex flex-wrap items-center justify-between gap-3 shrink-0 z-30">
        <div class="flex items-center gap-3 shrink-0">
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

        <!-- Información de Usuario y Barra de Almacenamiento FileJump -->
        <div id="userStorageWidget" class="hidden flex-1 max-w-2xl mx-2 bg-slate-950/90 border border-slate-800/90 rounded-xl px-3.5 py-2 flex flex-col justify-center gap-1.5 shadow-inner">
            <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="w-5 h-5 rounded-full bg-indigo-600/25 border border-indigo-500/40 text-indigo-300 flex items-center justify-center text-[11px] shrink-0">👤</span>
                    <span id="headerUserName" class="font-semibold text-white truncate">Usuario</span>
                    <button onclick="openAccountQuotaModal()" class="px-1.5 py-0.5 rounded bg-slate-800 hover:bg-indigo-600/30 border border-slate-700 text-[10px] text-slate-300 hover:text-indigo-200 transition shrink-0" title="Ajustar cuota o nombre manualmente">✏️</button>
                </div>
                <div class="flex items-center gap-2 sm:gap-3 text-[11px] font-mono shrink-0">
                    <span class="text-sky-300" title="Espacio ocupado">Ocupado: <strong id="headerStorageUsed">0 B</strong></span>
                    <span class="text-slate-600">|</span>
                    <span class="text-emerald-300" title="Espacio disponible">Libre: <strong id="headerStorageFree">-</strong></span>
                    <span class="text-slate-600">|</span>
                    <span class="text-slate-300" title="Capacidad total">Total: <strong id="headerStorageTotal">-</strong></span>
                </div>
            </div>
            <div class="w-full h-2 bg-slate-800 rounded-full overflow-hidden flex items-center">
                <div id="headerStorageBar" class="h-full bg-gradient-to-r from-sky-500 via-indigo-500 to-purple-500 rounded-full transition-all duration-500" style="width: 0%"></div>
            </div>
        </div>

        <div class="flex items-center gap-3 shrink-0">
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

            <button onclick="shutdownApplication()" class="px-3.5 py-2 rounded-xl bg-rose-600/20 hover:bg-rose-600 border border-rose-500/40 text-xs sm:text-sm font-semibold text-rose-200 hover:text-white flex items-center gap-1.5 transition" title="Cerrar explorador, servidor Python y consola">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
                </svg>
                <span>Cerrar</span>
            </button>
        </div>
    </header>

    <!-- Barra de Control de Tareas -->
    <section class="w-full px-3 sm:px-5 pt-3 pb-2 shrink-0">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl px-4 py-3 flex flex-col md:flex-row md:items-center justify-between gap-3 shadow-sm">
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
        <div class="flex flex-wrap items-center gap-4 mt-2 px-1 text-xs text-slate-400">
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-emerald-500"></span> Sincronizado</span>
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-sky-500"></span> Nuevo (Pendiente subir)</span>
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-amber-500"></span> Modificado (Tamaño/Fecha distinta)</span>
            <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-purple-500"></span> Solo en FileJump</span>
        </div>
    </section>

    <!-- DOBLE VENTANA DE COMPARACIÓN (100% ANCHO Y ALTO RESPONSIVO) -->
    <main class="w-full px-3 sm:px-5 py-1.5 flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-2 gap-4">
        
        <!-- VENTANA 1: EXPLORADOR LOCAL ESTILO WINDOWS -->
        <div class="bg-slate-900 border border-slate-800 rounded-2xl flex flex-col overflow-hidden shadow-md h-[460px] lg:h-full min-h-0">
            <div class="px-3.5 py-2.5 bg-slate-900 border-b border-slate-800 flex items-center justify-between gap-2 shrink-0">
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
                        <p id="localPathSubtitle" class="text-[11px] text-slate-400 truncate">Sin ruta seleccionada</p>
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
                <div class="w-44 sm:w-52 xl:w-64 bg-slate-950/60 border-r border-slate-800/80 overflow-y-auto p-1.5 select-none shrink-0">
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
                                <th onclick="toggleExplorerSort('local', 'name')" class="py-2 px-2.5 font-medium cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center gap-1.5"><span>Nombre</span><span id="sortIcon_local_name" class="text-sky-400 font-bold text-[11px]">↑</span></div>
                                </th>
                                <th onclick="toggleExplorerSort('local', 'size')" class="py-2 px-2.5 font-medium cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center gap-1.5"><span>Tamaño</span><span id="sortIcon_local_size" class="text-slate-500 text-[11px]">⇅</span></div>
                                </th>
                                <th onclick="toggleExplorerSort('local', 'mtime')" class="py-2 px-2.5 font-medium cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center gap-1.5"><span>Modificado</span><span id="sortIcon_local_mtime" class="text-slate-500 text-[11px]">⇅</span></div>
                                </th>
                                <th onclick="toggleExplorerSort('local', 'status')" class="py-2 px-2.5 font-medium text-right cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center justify-end gap-1.5"><span>Estado</span><span id="sortIcon_local_status" class="text-slate-500 text-[11px]">⇅</span></div>
                                </th>
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
        <div class="bg-slate-900 border border-slate-800 rounded-2xl flex flex-col overflow-hidden shadow-md h-[460px] lg:h-full min-h-0">
            <div class="px-3.5 py-2.5 bg-slate-900 border-b border-slate-800 flex items-center justify-between gap-2 shrink-0">
                <div class="flex items-center gap-2 min-w-0 flex-1">
                    <span class="p-1.5 rounded-lg bg-indigo-950 text-indigo-400 shrink-0">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.096A4.001 4.001 0 003 15z"/></svg>
                    </span>
                    <div class="min-w-0 flex-1">
                        <div class="flex items-center gap-2">
                            <h2 class="text-sm font-semibold text-white shrink-0">Nube FileJump</h2>
                            <select id="remoteDirSelector" onchange="onRemoteDirectoryChange()" class="bg-slate-950 border border-slate-700 rounded-lg px-2 py-0.5 text-[11px] text-indigo-300 focus:outline-none focus:border-indigo-500 max-w-[180px] truncate">
                                <option value="ROOT" selected>/ (Raíz de FileJump)</option>
                                <option value="">Auto (Carpeta de Tarea)</option>
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
                <div class="w-44 sm:w-52 xl:w-64 bg-slate-950/60 border-r border-slate-800/80 overflow-y-auto p-1.5 select-none shrink-0">
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
                                <th onclick="toggleExplorerSort('remote', 'name')" class="py-2 px-2.5 font-medium cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center gap-1.5"><span>Nombre</span><span id="sortIcon_remote_name" class="text-indigo-400 font-bold text-[11px]">↑</span></div>
                                </th>
                                <th onclick="toggleExplorerSort('remote', 'size')" class="py-2 px-2.5 font-medium cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center gap-1.5"><span>Tamaño</span><span id="sortIcon_remote_size" class="text-slate-500 text-[11px]">⇅</span></div>
                                </th>
                                <th onclick="toggleExplorerSort('remote', 'mtime')" class="py-2 px-2.5 font-medium cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center gap-1.5"><span>Actualizado</span><span id="sortIcon_remote_mtime" class="text-slate-500 text-[11px]">⇅</span></div>
                                </th>
                                <th onclick="toggleExplorerSort('remote', 'status')" class="py-2 px-2.5 font-medium text-right cursor-pointer hover:text-white hover:bg-slate-800/60 transition">
                                    <div class="inline-flex items-center justify-end gap-1.5"><span>ID / Estado</span><span id="sortIcon_remote_status" class="text-slate-500 text-[11px]">⇅</span></div>
                                </th>
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
    <section class="w-full px-3 sm:px-5 pt-2 pb-3 shrink-0">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-3">
            <div class="flex items-center justify-between mb-1.5">
                <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-400">Registro de Actividad en Vivo</h3>
                <span class="text-[11px] text-slate-500">Caché Persistente Inteligente Activa</span>
            </div>
            <div id="logConsole" class="bg-slate-950 rounded-xl p-2.5 h-24 overflow-y-auto font-mono text-xs space-y-1 text-slate-300 border border-slate-800/80">
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
                        <label class="block text-xs text-slate-400 mb-1">Bearer Token de FileJump (si se cambia, se limpia la caché automáticamente)</label>
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
                            <input type="checkbox" id="cfgHighSpeed" class="rounded bg-slate-950 border border-slate-700 text-indigo-600">
                            <span>Subida Directa Alta Velocidad</span>
                        </label>
                    </div>
                </div>

                <!-- Gestión de Caché Local Persistente -->
                <div class="bg-slate-950/60 p-4 rounded-xl border border-slate-800 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                    <div class="space-y-0.5">
                        <h3 class="text-xs font-semibold uppercase tracking-wider text-rose-400">Almacenamiento Caché Local (temp_cache)</h3>
                        <p id="cacheUsageSummary" class="text-xs text-slate-400">Calculando espacio ocupado en disco...</p>
                    </div>
                    <button type="button" onclick="confirmAndClearLocalCache()" id="clearCacheBtn" class="px-3.5 py-2 rounded-xl bg-rose-600/20 hover:bg-rose-600/35 border border-rose-500/40 text-xs font-semibold text-rose-300 transition flex items-center gap-1.5 shrink-0">
                        <span>🗑️</span>
                        <span>Eliminar Caché Definitivamente</span>
                    </button>
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

    <!-- MODAL DE NOMBRE DE USUARIO Y RESUMEN DE ALMACENAMIENTO (SOLO LECTURA) -->
    <div id="accountQuotaModal" class="fixed inset-0 bg-black/75 backdrop-blur-sm hidden items-center justify-center p-4 z-50">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div class="flex items-center justify-between">
                <h3 class="text-base font-bold text-white">Perfil de Cuenta FileJump</h3>
                <button onclick="closeAccountQuotaModal()" class="text-slate-400 hover:text-white text-xl leading-none">&times;</button>
            </div>
            <div class="space-y-3 text-xs">
                <div>
                    <label class="block text-slate-400 mb-1">Nombre de Usuario / Cuenta (Editable)</label>
                    <input type="text" id="quotaUserNameInput" placeholder="Dejar vacío para usar el nombre de la API..." class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-white focus:border-indigo-500 focus:outline-none">
                </div>
                <div class="bg-slate-950/80 border border-slate-800 rounded-xl p-3 space-y-1.5">
                    <div class="flex items-center justify-between">
                        <span class="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Almacenamiento en Nube</span>
                        <span class="text-[10px] px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/25">Lectura API</span>
                    </div>
                    <div class="grid grid-cols-3 gap-2 pt-1 text-center font-mono">
                        <div class="bg-slate-900/90 rounded-lg p-2 border border-slate-800">
                            <div class="text-[10px] text-slate-400">Ocupado</div>
                            <div id="readonlyModalUsed" class="text-sky-300 font-semibold mt-0.5">-</div>
                        </div>
                        <div class="bg-slate-900/90 rounded-lg p-2 border border-slate-800">
                            <div class="text-[10px] text-slate-400">Libre</div>
                            <div id="readonlyModalFree" class="text-emerald-300 font-semibold mt-0.5">-</div>
                        </div>
                        <div class="bg-slate-900/90 rounded-lg p-2 border border-slate-800">
                            <div class="text-[10px] text-slate-400">Total</div>
                            <div id="readonlyModalTotal" class="text-slate-200 font-semibold mt-0.5">-</div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="flex justify-end gap-2.5 pt-2">
                <button onclick="closeAccountQuotaModal()" class="px-3.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-300">Cancelar</button>
                <button onclick="saveAccountQuota()" class="px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white">Guardar Nombre</button>
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
        <!-- Vista Minimizada (Burbuja Compacta con Controles Rápidos) -->
        <div id="musicDockMinimized" onclick="setMusicDockMode('normal')" class="hidden cursor-pointer bg-slate-900/95 hover:bg-slate-800 border border-indigo-500/40 rounded-full pl-2 pr-3 py-2 shadow-2xl flex items-center gap-2.5 backdrop-blur">
            <div id="miniCoverWrap" class="w-9 h-9 rounded-full bg-indigo-600/30 border border-indigo-500/40 overflow-hidden flex items-center justify-center shrink-0">
                <span class="text-base">🎵</span>
            </div>
            <div class="min-w-0 max-w-[130px] sm:max-w-[160px]">
                <div id="miniTrackTitle" class="text-xs font-bold text-white truncate">Reproduciendo...</div>
                <div id="miniTrackArtist" class="text-[10px] text-indigo-300 truncate">Clic para expandir</div>
            </div>
            <div class="flex items-center gap-1 shrink-0">
                <button onclick="event.stopPropagation(); playDockPrev()" class="w-7 h-7 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-200 flex items-center justify-center text-xs transition" title="Anterior">⏮</button>
                <button onclick="event.stopPropagation(); toggleDockPlayPause()" id="miniPlayPauseBtn" class="w-8 h-8 rounded-full bg-indigo-600 hover:bg-indigo-500 text-white flex items-center justify-center text-xs transition" title="Play / Pausa">⏸</button>
                <button onclick="event.stopPropagation(); playDockNext()" class="w-7 h-7 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-200 flex items-center justify-center text-xs transition" title="Siguiente">⏭</button>
                <button onclick="event.stopPropagation(); toggleCurrentTrackFavorite()" id="miniFavoriteBtn" class="w-7 h-7 rounded-full bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-400 hover:text-rose-300 flex items-center justify-center text-xs transition" title="Marcar / Desmarcar canción favorita">🤍</button>
            </div>
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
                    <!-- Carátula ID3 + Metadatos + Botón Ubicar Canción Actual -->
                    <div id="dockMediaHeader" class="flex items-start justify-between gap-3">
                        <div class="flex items-center gap-3.5 min-w-0 flex-1">
                            <div id="dockCoverBox" onclick="openCoverLightbox()" class="w-16 h-16 rounded-xl bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center shrink-0 shadow-inner transition-all">
                                <span id="dockCoverEmoji" class="text-3xl">🎵</span>
                            </div>
                            <div id="dockMetaTextWrap" class="min-w-0 flex-1">
                                <div id="dockTrackTitle" class="text-sm font-bold text-white truncate" title="">Título de canción</div>
                                <div id="dockTrackArtist" class="text-xs text-indigo-300 truncate mt-0.5">Artista desconocido</div>
                                <div id="dockTrackAlbum" class="text-[11px] text-slate-400 truncate mt-0.5"></div>
                            </div>
                        </div>
                        <button type="button" id="dockNormalLocateBtn" onclick="locateCurrentTrackInPlaylist()" class="w-8 h-8 rounded-xl bg-slate-800/90 hover:bg-indigo-600/30 border border-slate-700 hover:border-indigo-500/50 text-indigo-300 hover:text-white flex items-center justify-center text-sm transition shrink-0" title="Ubicar canción en reproducción en la lista">
                            🎯
                        </button>
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
                    <!-- Botones de Control: Aleatorio, Anterior, Play/Pausa, Siguiente, Favorito -->
                    <div class="flex items-center justify-between gap-1.5 pt-1">
                        <button id="dockShuffleBtn" onclick="toggleDockShuffle()" class="px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-400 transition flex items-center gap-1" title="Modo Aleatorio (reordena y precarga la siguiente)">
                            <span>🔀</span><span class="text-[10px] font-medium hidden sm:inline">Aleatorio</span>
                        </button>

                        <div class="flex items-center gap-2">
                            <button onclick="playDockPrev()" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 flex items-center justify-center text-sm transition" title="Anterior">⏮</button>
                            <button id="dockPlayPauseBtn" onclick="toggleDockPlayPause()" class="w-11 h-11 rounded-full bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/30 flex items-center justify-center text-base transition" title="Play / Pausa">⏸</button>
                            <button onclick="playDockNext()" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 flex items-center justify-center text-sm transition" title="Siguiente">⏭</button>
                            <button id="dockFavoriteBtn" onclick="toggleCurrentTrackFavorite()" class="w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-400 hover:text-rose-300 flex items-center justify-center text-base transition" title="Marcar / Desmarcar canción favorita (guardado en FileJump)">🤍</button>
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
                    <div class="px-3.5 py-2 border-b border-slate-800/80 flex items-center justify-between gap-2 shrink-0">
                        <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Lista de Reproducción (Playlist)</span>
                        <span id="dockPlaylistOrderTag" class="text-[10px] text-slate-500">Orden normal</span>
                    </div>
                    <!-- Buscador Rápido + Localizador de Pista Actual dentro de la Playlist (visible al maximizar) -->
                    <div id="dockPlaylistSearchWrap" class="hidden px-3 py-2 border-b border-slate-800/70 bg-slate-900/60 shrink-0">
                        <div class="flex items-center gap-2">
                            <div class="relative flex items-center flex-1 min-w-0">
                                <span class="absolute left-2.5 text-slate-400 text-xs pointer-events-none">🔍</span>
                                <input type="text" id="dockPlaylistSearchInput" oninput="filterMusicPlaylist(this.value)" placeholder="Buscar canción o artista en la playlist..." class="w-full bg-slate-950 border border-slate-700/90 focus:border-indigo-500 rounded-xl pl-8 pr-7 py-1.5 text-xs text-white placeholder-slate-500 focus:outline-none">
                                <button type="button" id="dockPlaylistSearchClearBtn" onclick="clearMusicPlaylistSearch()" class="hidden absolute right-2 text-slate-400 hover:text-white text-xs px-1" title="Limpiar búsqueda">✕</button>
                            </div>
                            <button type="button" onclick="locateCurrentTrackInPlaylist()" class="px-2.5 py-1.5 rounded-xl bg-indigo-600/25 hover:bg-indigo-600/40 border border-indigo-500/40 text-indigo-300 hover:text-white text-xs font-medium transition flex items-center gap-1.5 shrink-0" title="Ubicar canción en reproducción actual">
                                <span>🎯</span><span class="hidden sm:inline">Ubicar</span>
                            </button>
                        </div>
                    </div>
                    <div id="dockPlaylistItems" class="max-h-44 overflow-y-auto divide-y divide-slate-800/40 text-xs">
                        <!-- Elementos del playlist -->
                    </div>
                </div>
            </div>
            <audio id="dockAudioElement" class="hidden"></audio>
        </div>
    </div>

    <!-- VENTANA FLOTANTE DE PROGRESO DE TRANSFERENCIA (CARGA / DESCARGA) -->
    <div id="transferDockWidget" class="fixed bottom-4 left-4 z-50 hidden transition-all duration-200">
        <!-- Vista Minimizada (Solo barra compacta) -->
        <div id="transferDockMinimized" onclick="setTransferDockMode('normal')" class="hidden cursor-pointer bg-slate-900/95 hover:bg-slate-800 border border-sky-500/40 rounded-2xl px-3.5 py-2.5 shadow-2xl w-72 sm:w-80 space-y-1.5 backdrop-blur">
            <div class="flex items-center justify-between gap-2 text-xs">
                <div class="flex items-center gap-2 min-w-0">
                    <span id="transferMiniIcon" class="text-sm shrink-0">⬆️</span>
                    <span id="transferMiniTitle" class="font-semibold text-white truncate">Transfiriendo...</span>
                </div>
                <div class="flex items-center gap-2 shrink-0 font-mono text-[11px]">
                    <span id="transferMiniSpeed" class="text-emerald-300">0.00 MB/s</span>
                    <span id="transferMiniPct" class="font-bold text-sky-300">0%</span>
                </div>
            </div>
            <div class="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden">
                <div id="transferMiniBar" class="h-full bg-gradient-to-r from-sky-500 to-emerald-400 rounded-full transition-all duration-150" style="width: 0%"></div>
            </div>
        </div>

        <!-- Vista Normal / Maximizada (Progreso + Detalle de Archivos) -->
        <div id="transferDockExpanded" class="w-80 sm:w-[440px] bg-slate-900/95 backdrop-blur border border-slate-700/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col">
            <div class="px-3.5 py-2.5 bg-slate-950/90 border-b border-slate-800 flex items-center justify-between shrink-0">
                <div class="flex items-center gap-2 min-w-0">
                    <span id="transferStatusDot" class="w-2 h-2 rounded-full bg-sky-400 animate-pulse shrink-0"></span>
                    <span id="transferHeaderBadge" class="text-[11px] font-semibold uppercase tracking-wider text-sky-300 truncate">Transferencia en Curso</span>
                </div>
                <div class="flex items-center gap-1 shrink-0">
                    <button onclick="setTransferDockMode('minimized')" class="w-6 h-6 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white flex items-center justify-center text-sm" title="Minimizar a barra">—</button>
                    <button id="transferMaxRestoreBtn" onclick="toggleTransferDockMaximize()" class="w-6 h-6 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white flex items-center justify-center text-xs" title="Maximizar detalle">⛶</button>
                    <button onclick="closeTransferDock()" class="w-6 h-6 rounded-lg hover:bg-rose-600/80 text-slate-400 hover:text-white flex items-center justify-center text-sm" title="Cerrar ventana">&times;</button>
                </div>
            </div>

            <div class="p-4 space-y-3 shrink-0">
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0 flex-1">
                        <div id="transferTitleText" class="text-sm font-bold text-white truncate">Preparando...</div>
                        <div id="transferCurrentFileText" class="text-xs text-slate-400 truncate mt-0.5">Analizando archivos...</div>
                    </div>
                    <div class="text-right shrink-0">
                        <div class="flex items-center justify-end gap-2">
                            <span id="transferSpeedBadge" class="px-2 py-0.5 rounded-lg bg-emerald-500/15 border border-emerald-500/30 text-[11px] font-mono font-semibold text-emerald-300">0.00 MB/s</span>
                            <span id="transferPctText" class="text-base font-bold font-mono text-sky-300">0%</span>
                        </div>
                        <div id="transferCountText" class="text-[10px] font-mono text-slate-400 mt-0.5">0 / 0 archivo(s)</div>
                    </div>
                </div>

                <!-- Barra de Progreso Principal -->
                <div class="space-y-1">
                    <div class="w-full h-2.5 bg-slate-800 rounded-full overflow-hidden">
                        <div id="transferMainBar" class="h-full bg-gradient-to-r from-sky-500 via-indigo-500 to-emerald-400 rounded-full transition-all duration-150" style="width: 0%"></div>
                    </div>
                    <div class="flex justify-between text-[10px] text-slate-300 font-mono">
                        <span id="transferBytesText">0.00 MB / 0.00 MB</span>
                        <span id="transferStateLabel">En progreso...</span>
                    </div>
                </div>
            </div>

            <!-- Lista Detallada de Archivos (Visible en Normal y Ampliada en Maximizado) -->
            <div id="transferDetailContainer" class="border-t border-slate-800 bg-slate-950/75 flex flex-col min-h-0">
                <div class="px-3.5 py-2 border-b border-slate-800/80 flex items-center justify-between shrink-0">
                    <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Detalle de Archivos</span>
                    <span id="transferDetailCountTag" class="text-[10px] text-slate-500">0 elementos</span>
                </div>
                <div id="transferItemsList" class="max-h-36 overflow-y-auto divide-y divide-slate-800/40 text-xs">
                    <!-- Archivos procesados -->
                </div>
            </div>
        </div>
    </div>

    <!-- VENTANA FLOTANTE DE ESCANEO DE AUDIO PARA PLAYLIST (CERRABLE CON ACTUALIZACIÓN ACTIVA) -->
    <div id="audioScanDockWidget" class="fixed bottom-4 left-4 z-50 hidden w-80 sm:w-96 bg-slate-900/95 backdrop-blur border border-indigo-500/40 rounded-2xl shadow-2xl overflow-hidden transition-all duration-200">
        <div class="px-3.5 py-2.5 bg-slate-950/90 border-b border-slate-800 flex items-center justify-between gap-2">
            <div class="flex items-center gap-2 min-w-0">
                <span id="audioScanDot" class="w-2 h-2 rounded-full bg-indigo-400 animate-pulse shrink-0"></span>
                <span class="text-[11px] font-semibold uppercase tracking-wider text-indigo-300 truncate">Escaneando Música</span>
            </div>
            <button onclick="closeAudioScanDock()" class="w-6 h-6 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white flex items-center justify-center text-sm transition" title="Cerrar ventana (continúa en segundo plano)">&times;</button>
        </div>
        <div class="p-4 space-y-3">
            <div class="flex items-center justify-between gap-3">
                <div class="min-w-0 flex-1">
                    <div id="audioScanTitle" class="text-xs font-bold text-white truncate">Escaneando carpeta...</div>
                    <div id="audioScanCurrentFolder" class="text-[11px] text-slate-400 truncate mt-0.5">Explorando subcarpetas...</div>
                </div>
                <div class="text-right shrink-0 bg-indigo-600/20 border border-indigo-500/30 rounded-xl px-2.5 py-1">
                    <div id="audioScanCount" class="text-base font-bold font-mono text-indigo-300 leading-tight">0</div>
                    <div class="text-[9px] uppercase tracking-wider text-slate-400">pistas</div>
                </div>
            </div>
            <div class="w-full h-2 bg-slate-800 rounded-full overflow-hidden">
                <div id="audioScanBar" class="h-full bg-gradient-to-r from-indigo-500 via-sky-400 to-emerald-400 rounded-full transition-all duration-200" style="width: 15%"></div>
            </div>
            <div class="flex items-center justify-between text-[10px] text-slate-400 font-mono">
                <span id="audioScanFoldersLabel">0 carpeta(s) revisada(s)</span>
                <span id="audioScanStatusLabel" class="text-indigo-300">Agregando a playlist...</span>
            </div>
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
                <img id="coverLightboxImg" src="" alt="Carátula MP3" class="max-h-[78vh] w-auto object-contain">
            </div>
        </div>
    </div>

    <!-- MENÚ CONTEXTUAL PARA EXPLORADORES LOCAL Y REMOTO -->
    <div id="explorerContextMenu" class="fixed hidden z-[70] min-w-[235px] bg-slate-900/95 backdrop-blur border border-slate-700 rounded-xl shadow-2xl py-1.5 text-xs text-slate-200 select-none">
        <!-- Opciones dinámicas -->
    </div>

    <!-- MODAL DE CAMBIO DE NOMBRE -->
    <div id="renameItemModal" class="fixed inset-0 bg-black/75 backdrop-blur-sm hidden items-center justify-center p-4 z-[65]">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div class="flex items-center justify-between">
                <h3 class="text-base font-bold text-white">Cambiar de Nombre</h3>
                <button onclick="closeRenameModal()" class="text-slate-400 hover:text-white text-xl leading-none">&times;</button>
            </div>
            <div>
                <label class="block text-xs text-slate-400 mb-1.5">Nuevo nombre:</label>
                <input type="text" id="renameNewNameInput" onkeydown="if(event.key==='Enter') submitRenameModal()" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white focus:border-indigo-500 focus:outline-none">
            </div>
            <div class="flex justify-end gap-2.5 pt-1">
                <button onclick="closeRenameModal()" class="px-3.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-300">Cancelar</button>
                <button onclick="submitRenameModal()" class="px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white">Guardar</button>
            </div>
        </div>
    </div>

    <!-- MODAL DE PROPIEDADES -->
    <div id="propertiesItemModal" class="fixed inset-0 bg-black/75 backdrop-blur-sm hidden items-center justify-center p-4 z-[65]">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div class="flex items-center justify-between">
                <h3 class="text-base font-bold text-white flex items-center gap-2"><span>ℹ️</span><span>Propiedades</span></h3>
                <button onclick="closePropertiesModal()" class="text-slate-400 hover:text-white text-xl leading-none">&times;</button>
            </div>
            <div id="propertiesModalBody" class="space-y-2.5 text-xs bg-slate-950/80 border border-slate-800 rounded-xl p-4">
                <div class="text-slate-400 animate-pulse">Calculando propiedades...</div>
            </div>
            <div class="flex justify-end">
                <button onclick="closePropertiesModal()" class="px-4 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-200">Cerrar</button>
            </div>
        </div>
    </div>

    <script>
        let currentConfig = { tasks: [] };
        let wasConnected = false;
        let lastProfileInfo = null;
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
        let musicPlaylistSearchQuery = '';
        let currentCoverDataUrl = null;
        let currentCoverFilename = 'caratula.jpg';
        const prefetchedRemoteIds = new Set();
        let favoriteTracksMap = {};
        let favoritesSyncInProgress = false;
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
        let activeContextTarget = null;
        let renamePendingTarget = null;

        // Estado de la Ventana Flotante de Transferencias (Carga / Descarga)
        let transferDockMode = 'normal'; // 'minimized' | 'normal' | 'maximized'
        let activeTransferPollTimer = null;

        // Estado de la Ventana Flotante de Escaneo de Audio para Playlist
        let activeAudioScanPollTimer = null;
        let audioScanDockClosedByUser = false;

        // Estado de Ordenamiento por Explorador ('name' | 'size' | 'mtime' | 'status')
        const explorerSortState = {
            local: { field: 'name', dir: 'asc' },
            remote: { field: 'name', dir: 'asc' }
        };

        function updateSortHeaderIcons(source) {
            const state = explorerSortState[source];
            const activeColor = source === 'local' ? 'text-sky-400' : 'text-indigo-400';
            ['name', 'size', 'mtime', 'status'].forEach(col => {
                const iconEl = document.getElementById(`sortIcon_${source}_${col}`);
                if (!iconEl) return;
                if (state.field === col) {
                    iconEl.textContent = state.dir === 'asc' ? '↑' : '↓';
                    iconEl.className = `${activeColor} font-bold text-[11px]`;
                } else {
                    iconEl.textContent = '⇅';
                    iconEl.className = 'text-slate-500 text-[11px]';
                }
            });
        }

        function sortExplorerItems(items, source) {
            const state = explorerSortState[source];
            const mult = state.dir === 'asc' ? 1 : -1;
            const statusOrder = { modified: 1, new: 2, remote_only: 3, local_only: 4, synced: 5 };

            return [...items].sort((a, b) => {
                // Mantener carpetas separadas arriba de los archivos
                if (a.type !== b.type) {
                    return a.type === 'folder' ? -1 : 1;
                }
                if (state.field === 'size') {
                    const diff = (Number(a.size || 0) - Number(b.size || 0));
                    if (diff !== 0) return diff * mult;
                } else if (state.field === 'mtime') {
                    const diff = (Number(a.mtime || 0) - Number(b.mtime || 0));
                    if (diff !== 0) return diff * mult;
                } else if (state.field === 'status') {
                    const sa = statusOrder[a.sync_status] || 99;
                    const sb = statusOrder[b.sync_status] || 99;
                    if (sa !== sb) return (sa - sb) * mult;
                    if (source === 'remote' && a.id !== b.id) {
                        return (Number(a.id || 0) - Number(b.id || 0)) * mult;
                    }
                }
                return (a.name || '').localeCompare(b.name || '', undefined, { numeric: true, sensitivity: 'base' }) * mult;
            });
        }

        function toggleExplorerSort(source, field) {
            const state = explorerSortState[source];
            if (state.field === field) {
                state.dir = state.dir === 'asc' ? 'desc' : 'asc';
            } else {
                state.field = field;
                state.dir = (field === 'size' || field === 'mtime') ? 'desc' : 'asc';
            }
            updateSortHeaderIcons(source);
            if (source === 'local') {
                renderLocalContentsPanel();
            } else {
                renderRemoteContentsPanel();
            }
        }

        function formatBytes(bytes) {
            const num = Number(bytes || 0);
            if (num === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(num) / Math.log(k));
            return `${parseFloat((num / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
        }

        function getStatusBadge(status) {
            const badges = {
                synced: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/20">Sincronizado</span>',
                new: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-sky-500/15 text-sky-400 border border-sky-500/20">Nuevo</span>',
                modified: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-500/15 text-amber-400 border border-amber-500/20">Modificado</span>',
                remote_only: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-purple-500/15 text-purple-400 border border-purple-500/20">Solo Remoto</span>',
                local_only: '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-700/50 text-slate-300 border border-slate-600/40">Disco Local</span>'
            };
            return badges[status] || '';
        }

        function showNotice(title, message, onConfirm = null) {
            document.getElementById('noticeTitle').textContent = title;
            document.getElementById('noticeMessage').textContent = message;
            const actions = document.getElementById('noticeActions');
            actions.innerHTML = '';

            if (onConfirm) {
                const cancelBtn = document.createElement('button');
                cancelBtn.className = 'px-3.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-300';
                cancelBtn.textContent = 'Cancelar';
                cancelBtn.onclick = closeNoticeModal;
                actions.appendChild(cancelBtn);

                const confirmBtn = document.createElement('button');
                confirmBtn.className = 'px-3.5 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white';
                confirmBtn.textContent = 'Confirmar y Forzar';
                confirmBtn.onclick = () => { closeNoticeModal(); onConfirm(); };
                actions.appendChild(confirmBtn);
            } else {
                const okBtn = document.createElement('button');
                okBtn.className = 'px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white';
                okBtn.textContent = 'Entendido';
                okBtn.onclick = closeNoticeModal;
                actions.appendChild(okBtn);
            }

            document.getElementById('noticeModal').classList.remove('hidden');
            document.getElementById('noticeModal').classList.add('flex');
        }

        function closeNoticeModal() {
            document.getElementById('noticeModal').classList.add('hidden');
            document.getElementById('noticeModal').classList.remove('flex');
        }

        async function fetchUserStorageInfo() {
            const widget = document.getElementById('userStorageWidget');
            if (!widget) return;
            try {
                const res = await fetch('/api/user/profile');
                const data = await res.json();
                if (!data.connected) {
                    widget.classList.add('hidden');
                    return;
                }
                lastProfileInfo = data;
                document.getElementById('headerUserName').textContent = data.user_name || 'Cuenta FileJump';
                document.getElementById('headerStorageUsed').textContent = formatBytes(data.used_bytes || 0);
                document.getElementById('headerStorageFree').textContent = data.total_bytes > 0 ? formatBytes(data.free_bytes || 0) : 'N/D';
                document.getElementById('headerStorageTotal').textContent = data.total_bytes > 0 ? formatBytes(data.total_bytes) : 'N/D';

                const bar = document.getElementById('headerStorageBar');
                const pct = data.total_bytes > 0 ? Math.min(100, Math.max(2, data.used_pct || 0)) : 8;
                bar.style.width = `${pct}%`;
                if ((data.used_pct || 0) >= 90) {
                    bar.className = 'h-full bg-gradient-to-r from-amber-500 to-rose-500 rounded-full transition-all duration-500';
                } else {
                    bar.className = 'h-full bg-gradient-to-r from-sky-500 via-indigo-500 to-purple-500 rounded-full transition-all duration-500';
                }
                widget.classList.remove('hidden');
            } catch (e) {
                widget.classList.add('hidden');
            }
        }

        function openAccountQuotaModal() {
            const modal = document.getElementById('accountQuotaModal');
            if (!modal) return;
            const info = lastProfileInfo || {};
            document.getElementById('quotaUserNameInput').value = info.user_name || '';
            document.getElementById('readonlyModalUsed').textContent = formatBytes(info.used_bytes || 0);
            document.getElementById('readonlyModalFree').textContent = info.total_bytes > 0 ? formatBytes(info.free_bytes || 0) : 'N/D';
            document.getElementById('readonlyModalTotal').textContent = info.total_bytes > 0 ? formatBytes(info.total_bytes) : 'N/D';
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        }

        function closeAccountQuotaModal() {
            const modal = document.getElementById('accountQuotaModal');
            if (!modal) return;
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        async function saveAccountQuota() {
            const userName = document.getElementById('quotaUserNameInput').value.trim();
            await fetch('/api/user/profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_name: userName })
            });
            closeAccountQuotaModal();
            await fetchUserStorageInfo();
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
                    if (!wasConnected) {
                        wasConnected = true;
                        fetchUserStorageInfo();
                        fetchRemoteDirectories(true);
                        loadRemoteMusicFavorites();
                    }
                } else {
                    wasConnected = false;
                    const widget = document.getElementById('userStorageWidget');
                    if (widget) widget.classList.add('hidden');
                    dot.className = 'w-2 h-2 rounded-full bg-rose-500';
                    txt.textContent = data.has_key ? 'Error de Autenticación' : 'Sin API Key (.env)';
                }

                const consoleEl = document.getElementById('logConsole');
                if (data.logs && data.logs.length > 0) {
                    consoleEl.innerHTML = data.logs.map(l => {
                        const color = l.level === 'error' ? 'text-rose-400' : (l.level === 'success' ? 'text-emerald-400' : (l.level === 'warning' ? 'text-amber-400' : 'text-slate-300'));
                        return `<div class="${color}"><span class="text-slate-500">[${l.time}]</span> ${l.message}</div>`;
                    }).join('');
                    consoleEl.scrollTop = consoleEl.scrollHeight;
                }
            } catch (e) {}
        }

        async function syncCachedRemoteIds() {
            try {
                const res = await fetch('/api/cache/status');
                const data = await res.json();
                if (Array.isArray(data.cached_ids)) {
                    data.cached_ids.forEach(id => prefetchedRemoteIds.add(String(id)));
                    if (musicSource === 'remote' && musicPlaylist.length > 0) {
                        renderMusicPlaylist();
                    }
                }
            } catch (e) {}
        }

        async function fetchLocalDrives(autoBrowseFirst = false) {
            const selector = document.getElementById('localDriveSelector');
            if (!selector) return;
            try {
                const res = await fetch('/api/local/drives');
                const data = await res.json();
                const drives = data.drives || [];
                const prevVal = selector.value;
                selector.innerHTML = '<option value="">Auto (Carpeta de Tarea)</option>';
                drives.forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = d.id;
                    opt.textContent = d.name;
                    selector.appendChild(opt);
                });
                if (prevVal && Array.from(selector.options).some(o => o.value === prevVal)) {
                    selector.value = prevVal;
                } else if (autoBrowseFirst && !document.getElementById('taskSelector').value && drives.length > 0) {
                    selector.value = drives[0].id;
                    onLocalDriveChange();
                }
            } catch (e) {}
        }

        async function onLocalDriveChange() {
            const driveVal = document.getElementById('localDriveSelector').value;
            if (!driveVal) {
                localActiveDriveRoot = '';
                inspectSelectedTask();
                return;
            }
            localActiveDriveRoot = driveVal;
            document.getElementById('localPathSubtitle').textContent = driveVal;
            document.getElementById('localTableBody').innerHTML = '<tr><td colspan="4" class="py-10 text-center text-slate-400">Cargando unidad local...</td></tr>';
            try {
                const res = await fetch(`/api/local/browse?root_path=${encodeURIComponent(driveVal)}`);
                const data = await res.json();
                initLocalExplorer(data.items || []);
            } catch (e) {
                showNotice('Error', 'No se pudo leer la unidad local seleccionada.');
            }
        }

        async function fetchRemoteDirectories(autoBrowseIfNoTask = false) {
            const selector = document.getElementById('remoteDirSelector');
            if (!selector) return;
            try {
                const res = await fetch('/api/remote/directories');
                const data = await res.json();
                if (!data.connected) return;

                const prevVal = selector.value;
                selector.innerHTML = `
                    <option value="ROOT">/ (Raíz de FileJump)</option>
                    <option value="">Auto (Carpeta de Tarea)</option>
                `;
                (data.directories || []).forEach(dir => {
                    const opt = document.createElement('option');
                    opt.value = String(dir.id);
                    opt.textContent = `📁 ${dir.name} (#${dir.id})`;
                    selector.appendChild(opt);
                });

                if (prevVal && Array.from(selector.options).some(o => o.value === prevVal)) {
                    selector.value = prevVal;
                    if (autoBrowseIfNoTask && prevVal === 'ROOT') {
                        inspectSelectedTask('ROOT');
                    }
                } else {
                    selector.value = 'ROOT';
                    inspectSelectedTask('ROOT');
                }
            } catch (e) {}
        }

        function onRemoteDirectoryChange() {
            const selectedDirId = document.getElementById('remoteDirSelector').value;
            inspectSelectedTask(selectedDirId);
        }

        // =========================================================================
        // REPRODUCTOR MUSICAL FLOTANTE (ID3 + PLAYLIST + EQ 5 BANDAS + VISUALIZADOR)
        // =========================================================================
        function rebuildMusicPlayOrder(startIndex = 0) {
            const total = musicPlaylist.length;
            if (total === 0) {
                musicPlayOrder = [];
                musicOrderPos = -1;
                return;
            }
            const indices = Array.from({ length: total }, (_, i) => i);
            if (musicShuffle && total > 1) {
                const others = indices.filter(i => i !== startIndex);
                for (let i = others.length - 1; i > 0; i--) {
                    const j = Math.floor(Math.random() * (i + 1));
                    [others[i], others[j]] = [others[j], others[i]];
                }
                musicPlayOrder = [startIndex, ...others];
            } else {
                musicPlayOrder = indices;
            }
            musicOrderPos = Math.max(0, musicPlayOrder.indexOf(startIndex));
        }

        function setMusicDockMode(mode) {
            musicDockMode = mode;
            const widget = document.getElementById('musicDockWidget');
            const minEl = document.getElementById('musicDockMinimized');
            const expEl = document.getElementById('musicDockExpanded');
            const mainGrid = document.getElementById('musicDockMainGrid');
            const playerCol = document.getElementById('musicDockPlayerCol');
            const playlistCol = document.getElementById('musicDockPlaylistCol');
            const playlistItems = document.getElementById('dockPlaylistItems');
            const searchWrap = document.getElementById('dockPlaylistSearchWrap');
            const coverBox = document.getElementById('dockCoverBox');
            const visWrap = document.getElementById('dockVisualizerWrap');
            const visCanvas = document.getElementById('dockVisualizerCanvas');
            const maxBtn = document.getElementById('dockMaxRestoreBtn');

            widget.classList.remove('hidden');

            if (mode === 'minimized') {
                widget.className = 'fixed bottom-4 right-4 z-50 transition-all duration-200';
                minEl.classList.remove('hidden');
                expEl.classList.add('hidden');
                return;
            }

            minEl.classList.add('hidden');
            expEl.classList.remove('hidden');

            if (mode === 'maximized') {
                widget.className = 'fixed inset-0 w-screen h-screen p-3 sm:p-6 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-md transition-all duration-200';
                expEl.className = 'w-full h-full max-w-5xl max-h-[90vh] bg-slate-900/95 backdrop-blur border border-slate-700/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                mainGrid.className = 'grid grid-cols-1 md:grid-cols-12 flex-1 min-h-0 overflow-hidden';
                playerCol.className = 'md:col-span-6 p-5 sm:p-6 space-y-4 flex flex-col justify-between overflow-y-auto';
                playlistCol.className = 'md:col-span-6 border-t md:border-t-0 md:border-l border-slate-800 bg-slate-950/75 flex flex-col min-h-0';
                playlistItems.className = 'flex-1 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (searchWrap) searchWrap.classList.remove('hidden');
                coverBox.className = 'w-28 h-28 sm:w-36 sm:h-36 rounded-2xl bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center shrink-0 shadow-xl cursor-zoom-in transition-all';
                if (visWrap) {
                    visWrap.className = `bg-slate-950/90 border border-slate-800/90 rounded-2xl p-3 relative overflow-hidden flex-1 min-h-[150px] flex items-center${visualizerEnabled ? '' : ' hidden'}`;
                }
                if (visCanvas) {
                    visCanvas.width = 560;
                    visCanvas.height = 200;
                    visCanvas.className = 'w-full h-full block rounded-xl';
                }
                if (maxBtn) { maxBtn.textContent = '🗗'; maxBtn.title = 'Restaurar tamaño'; }
            } else {
                widget.className = 'fixed bottom-4 right-4 z-50 transition-all duration-200';
                expEl.className = 'w-80 sm:w-[410px] bg-slate-900/95 backdrop-blur border border-slate-700/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                mainGrid.className = 'flex flex-col flex-1 min-h-0 overflow-hidden';
                playerCol.className = 'p-4 space-y-3.5 flex flex-col justify-between shrink-0';
                playlistCol.className = 'border-t border-slate-800 bg-slate-950/75 flex flex-col min-h-0';
                playlistItems.className = 'max-h-44 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (searchWrap) searchWrap.classList.add('hidden');
                coverBox.className = 'w-16 h-16 rounded-xl bg-slate-950 border border-slate-800 overflow-hidden flex items-center justify-center shrink-0 shadow-inner cursor-zoom-in transition-all';
                if (visWrap) {
                    visWrap.className = `bg-slate-950/90 border border-slate-800/90 rounded-xl p-2 relative overflow-hidden${visualizerEnabled ? '' : ' hidden'}`;
                }
                if (visCanvas) {
                    visCanvas.width = 360;
                    visCanvas.height = 54;
                    visCanvas.className = 'w-full h-12 sm:h-14 block rounded-lg';
                }
                if (maxBtn) { maxBtn.textContent = '⛶'; maxBtn.title = 'Maximizar reproductor'; }
            }
        }

        function toggleMusicDockMaximize() {
            setMusicDockMode(musicDockMode === 'maximized' ? 'normal' : 'maximized');
        }

        function closeMusicDock() {
            const audioEl = document.getElementById('dockAudioElement');
            if (audioEl) {
                audioEl.pause();
                audioEl.removeAttribute('src');
                audioEl.load();
            }
            if (visualizerRafId) {
                cancelAnimationFrame(visualizerRafId);
                visualizerRafId = null;
            }
            musicPlaylist = [];
            musicPlayOrder = [];
            musicOrderPos = -1;
            musicPlaylistSearchQuery = '';
            const searchInput = document.getElementById('dockPlaylistSearchInput');
            if (searchInput) searchInput.value = '';
            const clearBtn = document.getElementById('dockPlaylistSearchClearBtn');
            if (clearBtn) clearBtn.classList.add('hidden');
            document.getElementById('musicDockWidget').classList.add('hidden');
        }

        function buildAudioQueryParams(source, track) {
            const taskName = document.getElementById('taskSelector').value || '';
            const mtimeParam = track.mtime ? `&mtime=${encodeURIComponent(track.mtime)}` : '';
            if (source === 'local') {
                return track.full_path
                    ? `source=local&full_path=${encodeURIComponent(track.full_path)}`
                    : `source=local&task=${encodeURIComponent(taskName)}&rel_path=${encodeURIComponent(track.rel_path || '')}`;
            }
            return `source=remote&remote_id=${encodeURIComponent(track.id)}&name=${encodeURIComponent(track.name || 'audio')}${mtimeParam}`;
        }

        function initDockWebAudio(audioEl) {
            if (eqInitialized) return;
            try {
                const AudioContextClass = window.AudioContext || window.webkitAudioContext;
                if (!AudioContextClass) return;
                audioCtx = new AudioContextClass();
                const sourceNode = audioCtx.createMediaElementSource(audioEl);

                eqFilters = EQ_BANDS_CONFIG.map((cfg, idx) => {
                    const filter = audioCtx.createBiquadFilter();
                    filter.type = cfg.type;
                    filter.frequency.value = cfg.freq;
                    filter.Q.value = 1.0;
                    const slider = document.getElementById(`eqBand${idx}`);
                    filter.gain.value = slider ? Number(slider.value || 0) : 0;
                    return filter;
                });

                analyserNode = audioCtx.createAnalyser();
                analyserNode.fftSize = 128;

                let prevNode = sourceNode;
                eqFilters.forEach(f => {
                    prevNode.connect(f);
                    prevNode = f;
                });
                prevNode.connect(analyserNode);
                analyserNode.connect(audioCtx.destination);
                eqInitialized = true;
            } catch (e) {}
        }

        function startVisualizerLoop() {
            if (visualizerRafId) cancelAnimationFrame(visualizerRafId);
            const canvas = document.getElementById('dockVisualizerCanvas');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');

            function draw() {
                visualizerRafId = requestAnimationFrame(draw);
                if (!visualizerEnabled) return;
                const w = canvas.width;
                const h = canvas.height;
                ctx.clearRect(0, 0, w, h);

                if (!analyserNode) return;
                const bufferLength = analyserNode.frequencyBinCount;
                const dataArray = new Uint8Array(bufferLength);

                if (visualizerStyle === 'wave') {
                    analyserNode.getByteTimeDomainData(dataArray);
                    ctx.lineWidth = 2;
                    ctx.strokeStyle = '#818cf8';
                    ctx.beginPath();
                    const sliceWidth = w / bufferLength;
                    let x = 0;
                    for (let i = 0; i < bufferLength; i++) {
                        const v = dataArray[i] / 128.0;
                        const y = (v * h) / 2;
                        if (i === 0) ctx.moveTo(x, y);
                        else ctx.lineTo(x, y);
                        x += sliceWidth;
                    }
                    ctx.lineTo(w, h / 2);
                    ctx.stroke();
                } else {
                    analyserNode.getByteFrequencyData(dataArray);
                    const bars = Math.min(40, bufferLength);
                    const barWidth = (w / bars) - 2;
                    for (let i = 0; i < bars; i++) {
                        const val = dataArray[i] / 255;
                        const barHeight = Math.max(2, val * (h - 4));
                        const x = i * (barWidth + 2);
                        const grad = ctx.createLinearGradient(0, h, 0, 0);
                        grad.addColorStop(0, '#4f46e5');
                        grad.addColorStop(0.6, '#38bdf8');
                        grad.addColorStop(1, '#34d399');
                        ctx.fillStyle = grad;

                        if (visualizerStyle === 'mirror') {
                            const halfH = barHeight / 2;
                            ctx.fillRect(x, (h / 2) - halfH, barWidth, barHeight);
                        } else {
                            ctx.fillRect(x, h - barHeight, barWidth, barHeight);
                        }
                    }
                }
            }
            draw();
        }

        function formatTrackTime(secs) {
            if (!isFinite(secs) || secs < 0) return '0:00';
            const m = Math.floor(secs / 60);
            const s = Math.floor(secs % 60);
            return `${m}:${s < 10 ? '0' : ''}${s}`;
        }

        function getTrackFavoriteKey(track, source = musicSource) {
            if (!track) return '';
            if (source === 'remote' && track.id != null) {
                return `remote:${track.id}`;
            }
            const cleanName = (track.name || '').trim().toLowerCase();
            return `name:${cleanName}`;
        }

        function isTrackFavorite(track, source = musicSource) {
            if (!track) return false;
            const primaryKey = getTrackFavoriteKey(track, source);
            if (primaryKey && favoriteTracksMap[primaryKey]) return true;
            const byNameKey = `name:${(track.name || '').trim().toLowerCase()}`;
            return Boolean(favoriteTracksMap[byNameKey]);
        }

        function updateDockFavoriteButtonUI() {
            const btn = document.getElementById('dockFavoriteBtn');
            const miniBtn = document.getElementById('miniFavoriteBtn');
            const currentTrack = (musicOrderPos >= 0 && musicPlayOrder.length > 0)
                ? musicPlaylist[musicPlayOrder[musicOrderPos]]
                : null;
            const isFav = isTrackFavorite(currentTrack, musicSource);

            if (btn) {
                if (isFav) {
                    btn.textContent = '❤️';
                    btn.className = 'w-9 h-9 rounded-xl bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/50 text-rose-400 flex items-center justify-center text-base shadow-md shadow-rose-500/15 transition';
                    btn.title = 'Quitar de favoritos (guardado en filejump_sync_studio)';
                } else {
                    btn.textContent = '🤍';
                    btn.className = 'w-9 h-9 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-400 hover:text-rose-300 flex items-center justify-center text-base transition';
                    btn.title = 'Agregar a favoritos (guardado en filejump_sync_studio)';
                }
            }

            if (miniBtn) {
                if (isFav) {
                    miniBtn.textContent = '❤️';
                    miniBtn.className = 'w-7 h-7 rounded-full bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/50 text-rose-400 flex items-center justify-center text-xs transition';
                    miniBtn.title = 'Quitar de favoritos';
                } else {
                    miniBtn.textContent = '🤍';
                    miniBtn.className = 'w-7 h-7 rounded-full bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-400 hover:text-rose-300 flex items-center justify-center text-xs transition';
                    miniBtn.title = 'Agregar a favoritos';
                }
            }
        }

        async function loadRemoteMusicFavorites() {
            try {
                const res = await fetch('/api/music/favorites');
                const data = await res.json();
                if (data && data.favorites && typeof data.favorites === 'object') {
                    favoriteTracksMap = data.favorites;
                    updateDockFavoriteButtonUI();
                    if (musicPlaylist.length > 0) {
                        renderMusicPlaylist();
                    }
                }
            } catch (e) {}
        }

        async function toggleCurrentTrackFavorite() {
            if (favoritesSyncInProgress || musicOrderPos < 0 || musicPlayOrder.length === 0) return;
            const track = musicPlaylist[musicPlayOrder[musicOrderPos]];
            if (!track) return;

            const key = getTrackFavoriteKey(track, musicSource);
            const nameKey = `name:${(track.name || '').trim().toLowerCase()}`;
            const currentlyFav = isTrackFavorite(track, musicSource);
            const activeKey = favoriteTracksMap[key] ? key : (favoriteTracksMap[nameKey] ? nameKey : key);

            // Actualización optimista inmediata en la interfaz
            if (currentlyFav) {
                delete favoriteTracksMap[key];
                delete favoriteTracksMap[nameKey];
            } else {
                favoriteTracksMap[key] = {
                    name: track.name || '',
                    title: track.id3_title || '',
                    artist: track.id3_artist || '',
                    album: track.id3_album || '',
                    source: musicSource,
                    remote_id: track.id || null
                };
            }
            updateDockFavoriteButtonUI();
            renderMusicPlaylist();

            favoritesSyncInProgress = true;
            const btn = document.getElementById('dockFavoriteBtn');
            const miniBtn = document.getElementById('miniFavoriteBtn');
            if (btn) btn.classList.add('opacity-70');
            if (miniBtn) miniBtn.classList.add('opacity-70');

            try {
                const res = await fetch('/api/music/favorites', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        action: currentlyFav ? 'remove' : 'add',
                        key: activeKey,
                        meta: {
                            name: track.name || '',
                            title: track.id3_title || '',
                            artist: track.id3_artist || '',
                            album: track.id3_album || '',
                            source: musicSource,
                            remote_id: track.id || null
                        }
                    })
                });
                const data = await res.json();
                if (data && data.favorites) {
                    favoriteTracksMap = data.favorites;
                }
                if (!data.success && data.error) {
                    showNotice('Aviso de Favoritos', data.error);
                }
                refreshStatusAndLogs();
            } catch (e) {
                showNotice('Error', 'No se pudo guardar la lista de favoritos en FileJump.');
            } finally {
                favoritesSyncInProgress = false;
                if (btn) btn.classList.remove('opacity-70');
                if (miniBtn) miniBtn.classList.remove('opacity-70');
                updateDockFavoriteButtonUI();
                renderMusicPlaylist();
            }
        }

        async function openMusicDockPlayer(source, fileMeta, keepExistingPlaylist = false) {
            musicSource = source;
            if (!keepExistingPlaylist) {
                const folderFiles = getCurrentFolderFiles(source);
                const audioFiles = folderFiles.filter(f => isAudioFilename(f.name));
                musicPlaylist = audioFiles.length > 0 ? audioFiles : [fileMeta];
                const clickedIdx = musicPlaylist.findIndex(f =>
                    source === 'local'
                        ? (f.full_path && fileMeta.full_path ? f.full_path === fileMeta.full_path : f.rel_path === fileMeta.rel_path)
                        : String(f.id) === String(fileMeta.id)
                );
                rebuildMusicPlayOrder(clickedIdx >= 0 ? clickedIdx : 0);
            }

            if (musicDockMode === 'minimized') {
                setMusicDockMode('normal');
            } else {
                setMusicDockMode(musicDockMode || 'normal');
            }

            await playDockTrackAtOrderPos(musicOrderPos >= 0 ? musicOrderPos : 0);
        }

        async function playDockTrackAtOrderPos(orderPos) {
            if (musicPlayOrder.length === 0 || orderPos < 0 || orderPos >= musicPlayOrder.length) return;
            musicOrderPos = orderPos;
            const realIdx = musicPlayOrder[musicOrderPos];
            const track = musicPlaylist[realIdx];
            if (!track) return;

            const audioEl = document.getElementById('dockAudioElement');
            const qParams = buildAudioQueryParams(musicSource, track);
            const streamUrl = `/api/file/stream?${qParams}`;
            const metaUrl = `/api/file/audio_meta?${qParams}`;

            // Actualizar etiquetas iniciales antes de recibir ID3
            const fallbackTitle = (track.name || 'Pista de audio').replace(/\.[^/.]+$/, '');
            document.getElementById('dockTrackTitle').textContent = fallbackTitle;
            document.getElementById('dockTrackTitle').title = track.name || fallbackTitle;
            document.getElementById('dockTrackArtist').textContent = musicSource === 'remote' ? 'Cargando desde FileJump...' : 'Archivo local';
            document.getElementById('dockTrackAlbum').textContent = '';
            document.getElementById('miniTrackTitle').textContent = fallbackTitle;
            document.getElementById('miniTrackArtist').textContent = 'Reproduciendo...';
            document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
            document.getElementById('dockSourceBadge').textContent = musicSource === 'remote' ? '☁️ Nube' : '💻 Local';

            currentCoverDataUrl = null;
            currentCoverFilename = `${fallbackTitle}_caratula.jpg`;
            document.getElementById('dockCoverBox').innerHTML = '<span id="dockCoverEmoji" class="text-3xl">🎵</span>';
            document.getElementById('miniCoverWrap').innerHTML = '<span class="text-base">🎵</span>';

            updateDockFavoriteButtonUI();
            renderMusicPlaylist();

            initDockWebAudio(audioEl);
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }

            audioEl.src = streamUrl;
            const volSlider = document.getElementById('dockVolumeBar');
            if (volSlider) audioEl.volume = Number(volSlider.value || 85) / 100;

            audioEl.onloadedmetadata = () => {
                document.getElementById('dockTotalTime').textContent = formatTrackTime(audioEl.duration);
            };
            audioEl.ontimeupdate = () => {
                const cur = audioEl.currentTime || 0;
                const dur = audioEl.duration || 0;
                document.getElementById('dockCurrentTime').textContent = formatTrackTime(cur);
                document.getElementById('dockTotalTime').textContent = formatTrackTime(dur);
                document.getElementById('dockSeekBar').value = dur > 0 ? Math.round((cur / dur) * 100) : 0;
            };
            audioEl.onplay = () => {
                document.getElementById('dockPlayPauseBtn').textContent = '⏸';
                document.getElementById('miniPlayPauseBtn').textContent = '⏸';
                startVisualizerLoop();
            };
            audioEl.onpause = () => {
                document.getElementById('dockPlayPauseBtn').textContent = '▶';
                document.getElementById('miniPlayPauseBtn').textContent = '▶';
            };
            audioEl.onended = () => {
                if (musicOrderPos + 1 < musicPlayOrder.length) {
                    playDockTrackAtOrderPos(musicOrderPos + 1);
                } else if (musicPlayOrder.length > 1) {
                    playDockTrackAtOrderPos(0);
                }
            };

            try {
                await audioEl.play();
            } catch (e) {}

            if (musicSource === 'remote' && track.id) {
                prefetchedRemoteIds.add(String(track.id));
                renderMusicPlaylist();
            }

            // Cargar metadatos ID3 y carátula incrustada en paralelo
            fetch(metaUrl)
                .then(r => r.json())
                .then(meta => {
                    if (!meta || meta.error) return;
                    const title = meta.title || fallbackTitle;
                    const artist = meta.artist || 'Artista desconocido';
                    const album = meta.album || '';
                    track.id3_title = title;
                    track.id3_artist = artist;
                    track.id3_album = album;
                    document.getElementById('dockTrackTitle').textContent = title;
                    document.getElementById('dockTrackTitle').title = title;
                    document.getElementById('dockTrackArtist').textContent = artist;
                    document.getElementById('dockTrackAlbum').textContent = album;
                    document.getElementById('miniTrackTitle').textContent = title;
                    document.getElementById('miniTrackArtist').textContent = artist;
                    renderMusicPlaylist();

                    if (meta.cover_data_url) {
                        currentCoverDataUrl = meta.cover_data_url;
                        document.getElementById('dockCoverBox').innerHTML = `<img src="${meta.cover_data_url}" alt="Cover" class="w-full h-full object-cover">`;
                        document.getElementById('miniCoverWrap').innerHTML = `<img src="${meta.cover_data_url}" alt="Cover" class="w-full h-full object-cover">`;
                    }
                })
                .catch(() => {});

            prefetchNextRemoteTrack();
        }
        function filterMusicPlaylist(query) {
            musicPlaylistSearchQuery = (query || '').trim().toLowerCase();
            const clearBtn = document.getElementById('dockPlaylistSearchClearBtn');
            if (clearBtn) {
                clearBtn.classList.toggle('hidden', musicPlaylistSearchQuery.length === 0);
            }
            renderMusicPlaylist();
        }

        function clearMusicPlaylistSearch() {
            const input = document.getElementById('dockPlaylistSearchInput');
            if (input) input.value = '';
            filterMusicPlaylist('');
        }

        function locateCurrentTrackInPlaylist() {
            if (musicOrderPos < 0 || musicPlayOrder.length === 0) return;

            // Si hay un filtro de búsqueda activo que oculta la pista actual, limpiarlo para mostrarla en su posición
            if (musicPlaylistSearchQuery) {
                const input = document.getElementById('dockPlaylistSearchInput');
                if (input) input.value = '';
                musicPlaylistSearchQuery = '';
                const clearBtn = document.getElementById('dockPlaylistSearchClearBtn');
                if (clearBtn) clearBtn.classList.add('hidden');
                renderMusicPlaylist();
            }

            const rowEl = document.getElementById(`dockPlaylistRow_${musicOrderPos}`);
            if (!rowEl) return;

            rowEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            rowEl.classList.add('ring-2', 'ring-indigo-400', 'bg-indigo-500/35');
            setTimeout(() => {
                rowEl.classList.remove('ring-2', 'ring-indigo-400', 'bg-indigo-500/35');
            }, 1300);
        }

        function renderMusicPlaylist() {
            const container = document.getElementById('dockPlaylistItems');
            const orderTag = document.getElementById('dockPlaylistOrderTag');
            if (!container) return;

            if (orderTag) {
                orderTag.textContent = musicShuffle ? '🔀 Aleatorio activo' : 'Orden normal';
            }

            if (musicPlayOrder.length === 0) {
                container.innerHTML = '<div class="px-3.5 py-4 text-center text-slate-500">Cola vacía</div>';
                return;
            }

            const q = musicPlaylistSearchQuery;
            const visibleEntries = [];
            musicPlayOrder.forEach((realIdx, pos) => {
                const tr = musicPlaylist[realIdx];
                if (!tr) return;
                if (q) {
                    const haystack = `${tr.name || ''} ${tr.id3_title || ''} ${tr.id3_artist || ''} ${tr.id3_album || ''}`.toLowerCase();
                    if (!haystack.includes(q)) return;
                }
                visibleEntries.push({ tr, pos });
            });

            if (visibleEntries.length === 0) {
                container.innerHTML = `<div class="px-3.5 py-6 text-center text-slate-500">No se encontraron canciones o artistas para "<span class="text-slate-300">${escapeAttr(musicPlaylistSearchQuery)}</span>"</div>`;
                return;
            }

            container.innerHTML = visibleEntries.map(({ tr, pos }) => {
                const isCurrent = pos === musicOrderPos;
                const isCached = musicSource === 'remote' && tr.id && prefetchedRemoteIds.has(String(tr.id));
                const isFav = isTrackFavorite(tr, musicSource);
                const displayTitle = tr.id3_title || tr.name;
                const displaySub = tr.id3_artist && tr.id3_artist !== 'Artista desconocido' ? `${tr.id3_artist} • ${tr.name}` : tr.name;
                return `
                    <div id="dockPlaylistRow_${pos}" onclick="playDockTrackAtOrderPos(${pos})"
                         class="px-3.5 py-2 flex items-center justify-between gap-2 cursor-pointer transition ${isCurrent ? 'bg-indigo-600/25 text-white font-semibold' : 'text-slate-300 hover:bg-slate-800/70'}">
                        <div class="flex items-center gap-2 min-w-0">
                            <span class="text-[10px] font-mono ${isCurrent ? 'text-indigo-300' : 'text-slate-500'} w-5 shrink-0">${isCurrent ? '▶' : (pos + 1)}</span>
                            <div class="min-w-0">
                                <div class="flex items-center gap-1.5 min-w-0">
                                    <span class="truncate" title="${escapeAttr(displayTitle)}">${escapeAttr(displayTitle)}</span>
                                    ${isFav ? '<span class="text-[11px] text-rose-400 shrink-0" title="Canción favorita">❤️</span>' : ''}
                                </div>
                                ${tr.id3_title ? `<div class="text-[10px] text-slate-400 truncate font-normal">${escapeAttr(displaySub)}</div>` : ''}
                            </div>
                        </div>
                        <div class="flex items-center gap-1.5 shrink-0">
                            ${isCached ? '<span class="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/25">⚡ Caché</span>' : ''}
                            <span class="text-[10px] text-slate-500">${formatBytes(tr.size)}</span>
                        </div>
                    </div>`;
            }).join('');
        }

        function toggleDockPlayPause() {
            const audioEl = document.getElementById('dockAudioElement');
            if (!audioEl || !audioEl.src) return;
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }
            if (audioEl.paused) audioEl.play().catch(() => {});
            else audioEl.pause();
        }

        function playDockPrev() {
            const audioEl = document.getElementById('dockAudioElement');
            if (audioEl && audioEl.currentTime > 4) {
                audioEl.currentTime = 0;
                return;
            }
            if (musicPlayOrder.length === 0) return;
            const prevPos = (musicOrderPos - 1 + musicPlayOrder.length) % musicPlayOrder.length;
            playDockTrackAtOrderPos(prevPos);
        }

        function playDockNext() {
            if (musicPlayOrder.length === 0) return;
            const nextPos = (musicOrderPos + 1) % musicPlayOrder.length;
            playDockTrackAtOrderPos(nextPos);
        }

        function seekDockAudio(pct) {
            const audioEl = document.getElementById('dockAudioElement');
            if (!audioEl || !isFinite(audioEl.duration) || audioEl.duration <= 0) return;
            audioEl.currentTime = (Number(pct) / 100) * audioEl.duration;
        }

        function toggleDockShuffle() {
            musicShuffle = !musicShuffle;
            const btn = document.getElementById('dockShuffleBtn');
            if (btn) {
                btn.className = musicShuffle
                    ? 'px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-xs text-indigo-300 transition flex items-center gap-1'
                    : 'px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-400 transition flex items-center gap-1';
            }
            const currentRealIdx = musicOrderPos >= 0 ? musicPlayOrder[musicOrderPos] : 0;
            rebuildMusicPlayOrder(currentRealIdx);
            document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
            renderMusicPlaylist();
            prefetchNextRemoteTrack();
        }

        function setDockVolume(val) {
            const num = Math.max(0, Math.min(100, Number(val)));
            const audioEl = document.getElementById('dockAudioElement');
            if (audioEl) {
                audioEl.volume = num / 100;
                audioEl.muted = num === 0;
            }
            if (num > 0) lastNonZeroVolume = num;
            document.getElementById('dockVolumeLabel').textContent = `${num}%`;
            document.getElementById('dockMuteBtn').textContent = num === 0 ? '🔇' : (num < 45 ? '🔉' : '🔊');
        }

        function toggleDockMute() {
            const slider = document.getElementById('dockVolumeBar');
            const current = Number(slider.value || 0);
            const target = current > 0 ? 0 : (lastNonZeroVolume || 85);
            slider.value = target;
            setDockVolume(target);
        }

        function toggleDockEqPanel() {
            const panel = document.getElementById('dockEqPanel');
            const btn = document.getElementById('dockEqToggleBtn');
            const isHidden = panel.classList.toggle('hidden');
            btn.className = !isHidden
                ? 'px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-[10px] font-medium text-indigo-300 transition flex items-center gap-1'
                : 'px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-[10px] font-medium text-slate-300 transition flex items-center gap-1';
        }

        function setEqBandGain(bandIdx, gainDb) {
            const val = Number(gainDb || 0);
            const label = document.getElementById(`eqVal${bandIdx}`);
            if (label) label.textContent = `${val > 0 ? '+' : ''}${val}dB`;
            if (eqFilters[bandIdx]) {
                eqFilters[bandIdx].gain.value = val;
            }
        }

        function applyEqPreset(presetName) {
            const gains = EQ_PRESETS[presetName] || EQ_PRESETS.flat;
            const selectEl = document.getElementById('dockEqPreset');
            if (selectEl) selectEl.value = presetName;
            gains.forEach((g, idx) => {
                const slider = document.getElementById(`eqBand${idx}`);
                if (slider) slider.value = g;
                setEqBandGain(idx, g);
            });
        }

        function toggleDockVisualizer() {
            visualizerEnabled = !visualizerEnabled;
            const wrap = document.getElementById('dockVisualizerWrap');
            const btn = document.getElementById('dockVisToggleBtn');
            wrap.classList.toggle('hidden', !visualizerEnabled);
            btn.className = visualizerEnabled
                ? 'px-2 py-1.5 rounded-xl bg-indigo-600/30 border border-indigo-500/60 text-[10px] font-medium text-indigo-300 transition flex items-center gap-1'
                : 'px-2 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-[10px] font-medium text-slate-400 transition flex items-center gap-1';
            if (visualizerEnabled) startVisualizerLoop();
        }

        function cycleVisualizerStyle() {
            const order = ['bars', 'mirror', 'wave'];
            const labels = { bars: 'Barras', mirror: 'Espejo', wave: 'Onda' };
            const nextIdx = (order.indexOf(visualizerStyle) + 1) % order.length;
            visualizerStyle = order[nextIdx];
            const btn = document.getElementById('dockVisStyleBtn');
            if (btn) btn.textContent = labels[visualizerStyle];
        }

        function openCoverLightbox() {
            if (!currentCoverDataUrl) return;
            const modal = document.getElementById('coverLightboxModal');
            document.getElementById('coverLightboxImg').src = currentCoverDataUrl;
            const dlBtn = document.getElementById('coverDownloadBtn');
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

        // =========================================================================
        // EXPLORADORES DUALES (ÁRBOL JERÁRQUICO + CONTENIDO + MENÚ CONTEXTUAL)
        // =========================================================================
        function isAudioFilename(name) {
            const ext = (name || '').split('.').pop().toLowerCase();
            return AUDIO_EXTS.includes(ext);
        }

        function getCurrentFolderFiles(source) {
            if (source === 'local') {
                const node = localNodes.get(localSelectedPath || '');
                return (node && Array.isArray(node.children)) ? node.children.filter(i => i.type === 'file') : [];
            } else {
                const node = remoteNodes.get(String(remoteSelectedKey || 'ROOT'));
                return (node && Array.isArray(node.children)) ? node.children.filter(i => i.type === 'file') : [];
            }
        }

        function escapeAttr(str) {
            return String(str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function initLocalExplorer(rootItems) {
            localNodes.clear();
            localSelectedPath = '';
            localNodes.set('', {
                rel_path: '',
                name: 'Raíz Local',
                loaded: true,
                expanded: true,
                children: rootItems || []
            });
            renderLocalTreePanel();
            renderLocalContentsPanel();
        }

        function renderLocalTreePanel() {
            const treeEl = document.getElementById('localFolderTree');
            if (!treeEl) return;
            const rootNode = localNodes.get('');
            if (!rootNode) {
                treeEl.innerHTML = '<div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>';
                return;
            }

            function buildNodeHtml(relPath, depth) {
                const node = localNodes.get(relPath);
                if (!node) return '';
                const isSelected = localSelectedPath === relPath;
                const subfolders = (node.children || []).filter(c => c.type === 'folder');
                const padLeft = depth * 12 + 4;
                const canExpand = relPath === '' || node.has_children || subfolders.length > 0 || !node.loaded;
                const toggleSymbol = !canExpand ? '·' : (node.expanded ? '−' : '+');

                let html = `
                    <div oncontextmenu="openExplorerContextMenu(event, 'local', { type: 'folder', rel_path: '${escapeAttr(relPath)}', name: '${escapeAttr(node.name)}', full_path: '${escapeAttr(node.full_path || '')}' })"
                         class="flex items-center gap-1 py-1 pr-2 rounded-lg cursor-pointer transition ${isSelected ? 'bg-sky-600/25 text-white font-semibold border border-sky-500/30' : 'text-slate-300 hover:bg-slate-800/70'}"
                         style="padding-left: ${padLeft}px;">
                        <button type="button" onclick="event.stopPropagation(); toggleLocalTreeNode('${escapeAttr(relPath)}')"
                                class="w-4 h-4 rounded bg-slate-800/90 hover:bg-slate-700 text-[10px] font-mono text-slate-300 flex items-center justify-center shrink-0">${toggleSymbol}</button>
                        <div onclick="selectLocalFolder('${escapeAttr(relPath)}')" class="flex items-center gap-1.5 min-w-0 flex-1 truncate">
                            <span class="shrink-0">${relPath === '' ? '🏠' : '📁'}</span>
                            <span class="truncate">${escapeAttr(node.name)}</span>
                        </div>
                    </div>`;

                if (node.expanded && subfolders.length > 0) {
                    subfolders.forEach(sf => {
                        if (!localNodes.has(sf.rel_path)) {
                            localNodes.set(sf.rel_path, {
                                rel_path: sf.rel_path,
                                name: sf.name,
                                full_path: sf.full_path,
                                has_children: sf.has_children,
                                loaded: false,
                                expanded: false,
                                children: []
                            });
                        }
                        html += buildNodeHtml(sf.rel_path, depth + 1);
                    });
                }
                return html;
            }

            treeEl.innerHTML = buildNodeHtml('', 0);
        }

        async function fetchLocalNodeChildren(relPath) {
            const node = localNodes.get(relPath);
            if (!node) return;
            const taskName = document.getElementById('taskSelector').value || '';
            const query = localActiveDriveRoot
                ? `root_path=${encodeURIComponent(localActiveDriveRoot)}&prefix=${encodeURIComponent(relPath)}`
                : `task=${encodeURIComponent(taskName)}&prefix=${encodeURIComponent(relPath)}`;
            const res = await fetch(`/api/local/children?${query}`);
            const data = await res.json();
            node.children = data.items || [];
            node.loaded = true;
            node.children.filter(c => c.type === 'folder').forEach(sf => {
                if (!localNodes.has(sf.rel_path)) {
                    localNodes.set(sf.rel_path, {
                        rel_path: sf.rel_path,
                        name: sf.name,
                        full_path: sf.full_path,
                        has_children: sf.has_children,
                        loaded: false,
                        expanded: false,
                        children: []
                    });
                }
            });
        }

        async function toggleLocalTreeNode(relPath) {
            const node = localNodes.get(relPath);
            if (!node) return;
            if (!node.loaded) {
                await fetchLocalNodeChildren(relPath);
            }
            node.expanded = !node.expanded;
            renderLocalTreePanel();
        }

        async function selectLocalFolder(relPath) {
            localSelectedPath = relPath || '';
            // Expandir ancestros en el árbol
            if (localSelectedPath) {
                const parts = localSelectedPath.split('/');
                let acc = '';
                for (let i = 0; i < parts.length; i++) {
                    acc = acc ? `${acc}/${parts[i]}` : parts[i];
                    const pNode = localNodes.get(acc);
                    if (pNode) pNode.expanded = true;
                }
            }
            const rootNode = localNodes.get('');
            if (rootNode) rootNode.expanded = true;

            const targetNode = localNodes.get(localSelectedPath);
            if (targetNode) {
                if (!targetNode.loaded) {
                    await fetchLocalNodeChildren(localSelectedPath);
                }
                targetNode.expanded = true;
            }
            renderLocalTreePanel();
            renderLocalContentsPanel();
        }

        function renderLocalContentsPanel() {
            const tbody = document.getElementById('localTableBody');
            const badge = document.getElementById('localCountBadge');
            const bcBar = document.getElementById('localBreadcrumbBar');
            const node = localNodes.get(localSelectedPath || '');

            // Breadcrumbs
            let bcHtml = `<button onclick="selectLocalFolder('')" class="hover:text-sky-400 font-medium flex items-center gap-1 shrink-0"><span>🏠 Raíz</span></button>`;
            if (localSelectedPath) {
                const parts = localSelectedPath.split('/');
                let acc = '';
                parts.forEach((part, idx) => {
                    acc = acc ? `${acc}/${part}` : part;
                    const isLast = idx === parts.length - 1;
                    bcHtml += `<span class="text-slate-600">/</span><button onclick="selectLocalFolder('${escapeAttr(acc)}')" class="${isLast ? 'text-white font-semibold' : 'hover:text-sky-400'} truncate max-w-[160px]">${escapeAttr(part)}</button>`;
                });
            }
            bcBar.innerHTML = bcHtml;

            if (!node) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Sin contenido disponible</td></tr>';
                badge.textContent = '0 elementos';
                return;
            }

            const items = sortExplorerItems(node.children || [], 'local');
            badge.textContent = `${items.length} elementos`;

            if (items.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Carpeta vacía</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(item => {
                const isFolder = item.type === 'folder';
                const icon = isFolder ? '📁' : (isAudioFilename(item.name) ? '🎵' : '📄');
                const metaJson = escapeAttr(JSON.stringify(item));
                const dblClick = isFolder
                    ? `selectLocalFolder('${escapeAttr(item.rel_path)}')`
                    : `openExplorerFile('local', ${metaJson})`;

                return `
                    <tr ondblclick="${dblClick}"
                        oncontextmenu="openExplorerContextMenu(event, 'local', ${metaJson})"
                        class="hover:bg-slate-800/65 cursor-pointer transition">
                        <td class="py-2 px-2.5 font-medium text-slate-200 max-w-[240px] truncate" title="${escapeAttr(item.name)}">
                            <span class="mr-1.5">${icon}</span><span>${escapeAttr(item.name)}</span>
                        </td>
                        <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${isFolder ? '-' : formatBytes(item.size)}</td>
                        <td class="py-2 px-2.5 text-slate-400 font-mono text-[11px] whitespace-nowrap">${escapeAttr(item.mtime_str || '-')}</td>
                        <td class="py-2 px-2.5 text-right whitespace-nowrap">${getStatusBadge(item.sync_status)}</td>
                    </tr>`;
            }).join('');
        }

        function initRemoteExplorer(rootItems, rootId = 'ROOT', rootLabel = '/ (Raíz de FileJump)') {
            remoteNodes.clear();
            remoteBaseRootId = String(rootId || 'ROOT');
            remoteSelectedKey = remoteBaseRootId;
            remoteNodes.set(remoteBaseRootId, {
                key: remoteBaseRootId,
                id: remoteBaseRootId,
                rel_path: '',
                name: rootLabel,
                loaded: true,
                expanded: true,
                children: rootItems || []
            });
            renderRemoteTreePanel();
            renderRemoteContentsPanel();
        }

        function renderRemoteTreePanel() {
            const treeEl = document.getElementById('remoteFolderTree');
            if (!treeEl) return;
            const rootNode = remoteNodes.get(remoteBaseRootId);
            if (!rootNode) {
                treeEl.innerHTML = '<div class="px-2 py-4 text-slate-500 text-[11px]">Sin carpetas</div>';
                return;
            }

            function buildNodeHtml(nodeKey, depth) {
                const node = remoteNodes.get(String(nodeKey));
                if (!node) return '';
                const isSelected = String(remoteSelectedKey) === String(nodeKey);
                const subfolders = (node.children || []).filter(c => c.type === 'folder');
                const padLeft = depth * 12 + 4;
                const toggleSymbol = node.expanded ? '−' : '+';

                let html = `
                    <div oncontextmenu="openExplorerContextMenu(event, 'remote', { type: 'folder', id: '${escapeAttr(node.id)}', rel_path: '${escapeAttr(node.rel_path || '')}', name: '${escapeAttr(node.name)}' })"
                         class="flex items-center gap-1 py-1 pr-2 rounded-lg cursor-pointer transition ${isSelected ? 'bg-indigo-600/25 text-white font-semibold border border-indigo-500/30' : 'text-slate-300 hover:bg-slate-800/70'}"
                         style="padding-left: ${padLeft}px;">
                        <button type="button" onclick="event.stopPropagation(); toggleRemoteTreeNode('${escapeAttr(nodeKey)}')"
                                class="w-4 h-4 rounded bg-slate-800/90 hover:bg-slate-700 text-[10px] font-mono text-slate-300 flex items-center justify-center shrink-0">${toggleSymbol}</button>
                        <div onclick="selectRemoteNode('${escapeAttr(nodeKey)}')" class="flex items-center gap-1.5 min-w-0 flex-1 truncate">
                            <span class="shrink-0">${String(nodeKey) === String(remoteBaseRootId) ? '☁️' : '📁'}</span>
                            <span class="truncate">${escapeAttr(node.name)}</span>
                        </div>
                    </div>`;

                if (node.expanded && subfolders.length > 0) {
                    subfolders.forEach(sf => {
                        const childKey = String(sf.id);
                        if (!remoteNodes.has(childKey)) {
                            remoteNodes.set(childKey, {
                                key: childKey,
                                id: sf.id,
                                parent_key: String(nodeKey),
                                rel_path: sf.rel_path,
                                name: sf.name,
                                loaded: false,
                                expanded: false,
                                children: []
                            });
                        }
                        html += buildNodeHtml(childKey, depth + 1);
                    });
                }
                return html;
            }

            treeEl.innerHTML = buildNodeHtml(remoteBaseRootId, 0);
        }

        async function fetchRemoteNodeChildren(nodeKey) {
            const node = remoteNodes.get(String(nodeKey));
            if (!node) return;
            const taskName = document.getElementById('taskSelector').value || '';
            const res = await fetch(`/api/remote/children?parent_id=${encodeURIComponent(node.id)}&prefix=${encodeURIComponent(node.rel_path || '')}&task=${encodeURIComponent(taskName)}`);
            const data = await res.json();
            node.children = data.items || [];
            node.loaded = true;
            node.children.filter(c => c.type === 'folder').forEach(sf => {
                const cKey = String(sf.id);
                if (!remoteNodes.has(cKey)) {
                    remoteNodes.set(cKey, {
                        key: cKey,
                        id: sf.id,
                        parent_key: String(nodeKey),
                        rel_path: sf.rel_path,
                        name: sf.name,
                        loaded: false,
                        expanded: false,
                        children: []
                    });
                }
            });
        }

        async function toggleRemoteTreeNode(nodeKey) {
            const node = remoteNodes.get(String(nodeKey));
            if (!node) return;
            if (!node.loaded) {
                await fetchRemoteNodeChildren(nodeKey);
            }
            node.expanded = !node.expanded;
            renderRemoteTreePanel();
        }

        async function selectRemoteNode(nodeKey) {
            remoteSelectedKey = String(nodeKey || remoteBaseRootId);
            const node = remoteNodes.get(remoteSelectedKey);
            if (!node) return;

            // Expandir cadena de padres
            let cursor = node;
            while (cursor) {
                cursor.expanded = true;
                cursor = cursor.parent_key ? remoteNodes.get(String(cursor.parent_key)) : null;
            }

            if (!node.loaded) {
                await fetchRemoteNodeChildren(remoteSelectedKey);
            }
            node.expanded = true;
            renderRemoteTreePanel();
            renderRemoteContentsPanel();
        }

        function renderRemoteContentsPanel() {
            const tbody = document.getElementById('remoteTableBody');
            const badge = document.getElementById('remoteCountBadge');
            const bcBar = document.getElementById('remoteBreadcrumbBar');
            const subLabel = document.getElementById('remotePathSubtitle');
            const dirSelector = document.getElementById('remoteDirSelector');
            const node = remoteNodes.get(String(remoteSelectedKey));

            // Construir cadena de ancestros para breadcrumbs y subtítulo activo
            const chain = [];
            let cursor = node;
            while (cursor) {
                chain.unshift(cursor);
                cursor = cursor.parent_key ? remoteNodes.get(String(cursor.parent_key)) : null;
            }

            if (node) {
                const pathDisplay = chain.map(n => n.name).join(' / ');
                subLabel.textContent = `ID Remoto: ${node.id} • ${pathDisplay}`;

                // Actualizar selector superior activamente con la carpeta actual y sus subcarpetas
                if (dirSelector) {
                    const subOpts = (node.children || []).filter(c => c.type === 'folder')
                        .map(sf => `<option value="${escapeAttr(sf.id)}">↳ 📁 ${escapeAttr(sf.name)} (#${escapeAttr(sf.id)})</option>`)
                        .join('');
                    const currentLabel = String(node.id) === 'ROOT' ? '/ (Raíz de FileJump)' : `📂 ${node.name} (#${node.id})`;
                    dirSelector.innerHTML = `
                        <option value="${escapeAttr(node.id)}" selected>${escapeAttr(currentLabel)}</option>
                        ${String(node.id) !== 'ROOT' ? '<option value="ROOT">/ (Raíz de FileJump)</option>' : ''}
                        <option value="">Auto (Carpeta de Tarea)</option>
                        ${subOpts}
                    `;
                }
            }

            bcBar.innerHTML = chain.map((n, idx) => {
                const isLast = idx === chain.length - 1;
                const sep = idx > 0 ? '<span class="text-slate-600">/</span>' : '';
                return `${sep}<button onclick="selectRemoteNode('${escapeAttr(n.key)}')" class="${isLast ? 'text-white font-semibold' : 'hover:text-indigo-400'} flex items-center gap-1 shrink-0 max-w-[180px] truncate">${idx === 0 ? '☁️ ' : ''}${escapeAttr(n.name)}</button>`;
            }).join('');

            if (!node) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Sin contenido remoto disponible</td></tr>';
                badge.textContent = '0 elementos';
                return;
            }

            const items = sortExplorerItems(node.children || [], 'remote');
            badge.textContent = `${items.length} elementos`;

            if (items.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="py-12 text-center text-slate-500">Carpeta remota vacía</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(item => {
                const isFolder = item.type === 'folder';
                const icon = isFolder ? '📁' : (isAudioFilename(item.name) ? '🎵' : '☁️');
                const metaJson = escapeAttr(JSON.stringify(item));
                const dblClick = isFolder
                    ? `selectRemoteNode('${escapeAttr(item.id)}')`
                    : `openExplorerFile('remote', ${metaJson})`;

                return `
                    <tr ondblclick="${dblClick}"
                        oncontextmenu="openExplorerContextMenu(event, 'remote', ${metaJson})"
                        class="hover:bg-slate-800/65 cursor-pointer transition">
                        <td class="py-2 px-2.5 font-medium text-slate-200 max-w-[240px] truncate" title="${escapeAttr(item.name)}">
                            <span class="mr-1.5">${icon}</span><span>${escapeAttr(item.name)}</span>
                        </td>
                        <td class="py-2 px-2.5 text-slate-400 whitespace-nowrap">${isFolder ? '-' : formatBytes(item.size)}</td>
                        <td class="py-2 px-2.5 text-slate-400 font-mono text-[11px] whitespace-nowrap">${escapeAttr(item.mtime_str || '-')}</td>
                        <td class="py-2 px-2.5 text-right whitespace-nowrap">
                            <span class="text-[10px] font-mono text-slate-500 mr-1.5">#${escapeAttr(item.id)}</span>
                            ${getStatusBadge(item.sync_status)}
                        </td>
                    </tr>`;
            }).join('');
        }

        function openExplorerFile(source, fileMeta) {
            currentPreviewSource = source;
            const folderFiles = getCurrentFolderFiles(source);
            const nonAudioFiles = folderFiles.filter(f => !isAudioFilename(f.name));
            if (isAudioFilename(fileMeta.name)) {
                openMusicDockPlayer(source, fileMeta, false);
                return;
            }
            currentPreviewList = nonAudioFiles.length > 0 ? nonAudioFiles : [fileMeta];
            currentPreviewIndex = currentPreviewList.findIndex(f =>
                source === 'local'
                    ? (f.full_path && fileMeta.full_path ? f.full_path === fileMeta.full_path : f.rel_path === fileMeta.rel_path)
                    : String(f.id) === String(fileMeta.id)
            );
            if (currentPreviewIndex < 0) currentPreviewIndex = 0;
            renderPreviewItem(currentPreviewList[currentPreviewIndex]);
        }

        function navigatePreview(delta) {
            if (currentPreviewList.length <= 1) return;
            currentPreviewIndex = (currentPreviewIndex + delta + currentPreviewList.length) % currentPreviewList.length;
            renderPreviewItem(currentPreviewList[currentPreviewIndex]);
        }

        function prefetchNextRemoteTrack() {
            if (musicSource !== 'remote' || musicPlayOrder.length <= 1) return;
            const nextPos = (musicOrderPos + 1) % musicPlayOrder.length;
            const nextTrack = musicPlaylist[musicPlayOrder[nextPos]];
            if (!nextTrack || !nextTrack.id || prefetchedRemoteIds.has(String(nextTrack.id))) return;

            const badge = document.getElementById('dockPrefetchBadge');
            const mtimeParam = nextTrack.mtime ? `&mtime=${encodeURIComponent(nextTrack.mtime)}` : '';
            fetch(`/api/file/prefetch?source=remote&remote_id=${encodeURIComponent(nextTrack.id)}&name=${encodeURIComponent(nextTrack.name || 'audio')}${mtimeParam}`)
                .then(r => r.json())
                .then(res => {
                    if (res && res.prefetched) {
                        prefetchedRemoteIds.add(String(nextTrack.id));
                        if (badge) badge.classList.remove('hidden');
                        renderMusicPlaylist();
                    }
                })
                .catch(() => {});
        }

        function forceVideoAudioTranscode(streamUrl, fileName, audioTrackIdx = 0) {
            const body = document.getElementById('previewBody');
            if (!body) return;
            const sep = streamUrl.includes('?') ? '&' : '?';
            const targetUrl = `${streamUrl}${sep}transcode=1&audio_track=${encodeURIComponent(audioTrackIdx)}`;
            const existingControls = document.getElementById('videoAudioControlsBar');
            const controlsHtml = existingControls ? existingControls.outerHTML : '';

            body.innerHTML = `
                <div class="w-full flex flex-col items-center gap-3">
                    <div class="text-xs text-sky-300 animate-pulse">Extrayendo pista de audio #${Number(audioTrackIdx) + 1} y convirtiendo a AAC estéreo...</div>
                    <video id="activePreviewVideo" src="${targetUrl}" controls autoplay playsinline class="max-h-[68vh] w-full rounded-xl bg-black"></video>
                    ${controlsHtml}
                </div>`;
            const vid = document.getElementById('activePreviewVideo');
            if (vid) {
                vid.muted = false;
                vid.volume = 0.85;
            }
            const sel = document.getElementById('videoAudioTrackSelect');
            if (sel) sel.value = String(audioTrackIdx);
        }

        function onSelectVideoAudioTrack(streamUrl, fileName, trackIdx) {
            forceVideoAudioTranscode(streamUrl, fileName, Number(trackIdx || 0));
        }

        async function renderPreviewItem(fileMeta) {
            if (!fileMeta) return;
            const ext = (fileMeta.name || '').split('.').pop().toLowerCase();
            const qParams = buildAudioQueryParams(currentPreviewSource, fileMeta);
            const streamUrl = `/api/file/stream?${qParams}`;

            if (ext === 'pdf') {
                window.open(streamUrl, '_blank', 'noopener');
                return;
            }

            const modal = document.getElementById('previewModal');
            const body = document.getElementById('previewBody');
            document.getElementById('previewTitle').textContent = fileMeta.name || 'Archivo';
            document.getElementById('previewBadge').textContent = ext.toUpperCase() || 'ARCHIVO';
            document.getElementById('previewCounter').textContent = currentPreviewList.length > 1 ? `(${currentPreviewIndex + 1}/${currentPreviewList.length})` : '';
            document.getElementById('previewOpenTabBtn').href = streamUrl;
            document.getElementById('previewPrevBtn').disabled = currentPreviewList.length <= 1;
            document.getElementById('previewNextBtn').disabled = currentPreviewList.length <= 1;

            modal.classList.remove('hidden');
            modal.classList.add('flex');

            const imgExts = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
            const vidExts = ['mp4', 'webm', 'ogg', 'mov', 'avi', 'mkv'];

            if (imgExts.includes(ext)) {
                body.innerHTML = `<img src="${streamUrl}" alt="${escapeAttr(fileMeta.name)}" class="max-h-[74vh] max-w-full object-contain rounded-xl shadow-lg">`;
                return;
            }

            if (vidExts.includes(ext)) {
                const transcodeParam = ['avi', 'mkv', 'mov'].includes(ext) ? '&transcode=1&audio_track=0' : '';
                body.innerHTML = `
                    <div class="w-full flex flex-col items-center gap-2.5">
                        <video id="activePreviewVideo" src="${streamUrl}${transcodeParam}" controls autoplay playsinline class="max-h-[68vh] w-full rounded-xl bg-black"></video>
                        <div id="videoAudioControlsBar" class="w-full flex flex-wrap items-center justify-between gap-2 px-1 text-[11px] text-slate-400">
                            <div class="flex items-center gap-2">
                                <span id="videoTrackStatusLabel">Pista de audio:</span>
                                <select id="videoAudioTrackSelect" onchange="onSelectVideoAudioTrack('${escapeAttr(streamUrl)}', '${escapeAttr(fileMeta.name)}', this.value)" class="hidden bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"></select>
                            </div>
                            <button type="button" onclick="forceVideoAudioTranscode('${escapeAttr(streamUrl)}', '${escapeAttr(fileMeta.name)}', Number(document.getElementById('videoAudioTrackSelect')?.value || 0))" class="px-2.5 py-1 rounded-lg bg-indigo-600/25 hover:bg-indigo-600/40 border border-indigo-500/40 text-indigo-300 font-medium transition">
                                🔊 Extraer Pista a AAC Compatible
                            </button>
                        </div>
                    </div>`;
                const vid = document.getElementById('activePreviewVideo');
                if (vid) {
                    vid.muted = false;
                    vid.volume = 0.85;
                    vid.play().catch(() => {});
                }
                // Consultar pistas de audio disponibles en el contenedor (ej. [spa] y [jpn])
                fetch(`/api/video/tracks?${qParams}`)
                    .then(r => r.json())
                    .then(data => {
                        const tracks = data.tracks || [];
                        const sel = document.getElementById('videoAudioTrackSelect');
                        const lbl = document.getElementById('videoTrackStatusLabel');
                        if (!sel || !lbl) return;
                        if (tracks.length > 0) {
                            sel.innerHTML = tracks.map(t => `<option value="${t.audio_index}">${escapeAttr(t.label)}</option>`).join('');
                            sel.classList.remove('hidden');
                            lbl.textContent = tracks.length > 1 ? `Pista (${tracks.length} disponibles):` : 'Pista de audio:';
                        } else {
                            lbl.textContent = 'Si el video tiene doble pista o AC3:';
                        }
                    })
                    .catch(() => {});
                return;
            }

            body.innerHTML = '<div class="text-xs text-slate-400 animate-pulse">Generando vista previa...</div>';
            try {
                const res = await fetch(`/api/file/preview?${qParams}`);
                const data = await res.json();
                if (data.kind === 'text') {
                    body.innerHTML = `<pre class="w-full max-h-[72vh] overflow-auto bg-slate-900 border border-slate-800 rounded-xl p-4 text-xs font-mono text-slate-200 whitespace-pre-wrap">${escapeAttr(data.content)}</pre>`;
                } else if (data.kind === 'office') {
                    body.innerHTML = `<div class="w-full max-h-[72vh] overflow-auto bg-slate-900 border border-slate-800 rounded-xl p-5 text-xs text-slate-200">${data.html}</div>`;
                } else {
                    body.innerHTML = `<div class="text-center space-y-3"><p class="text-xs text-slate-400">Vista previa directa no disponible para <strong>.${escapeAttr(ext)}</strong>.</p><a href="${streamUrl}" target="_blank" class="inline-block px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white">Descargar / Abrir archivo</a></div>`;
                }
            } catch (e) {
                body.innerHTML = '<div class="text-xs text-rose-400">Error cargando vista previa del archivo.</div>';
            }
        }

        function closePreviewModal() {
            const modal = document.getElementById('previewModal');
            const vid = document.getElementById('activePreviewVideo');
            if (vid) {
                vid.pause();
                vid.removeAttribute('src');
                vid.load();
            }
            document.getElementById('previewBody').innerHTML = '';
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        // =========================================================================
        // MENÚ CONTEXTUAL (ABRIR, PLAYLIST, SUBIR/DESCARGAR, RENOMBRAR, ELIMINAR)
        // =========================================================================
        function closeExplorerContextMenu() {
            const menu = document.getElementById('explorerContextMenu');
            if (menu) menu.classList.add('hidden');
        }

        document.addEventListener('click', closeExplorerContextMenu);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeExplorerContextMenu();
                closePreviewModal();
                closeRenameModal();
                closePropertiesModal();
            }
        });

        function openExplorerContextMenu(event, source, itemMeta) {
            event.preventDefault();
            event.stopPropagation();
            activeContextTarget = { source, item: itemMeta };

            const menu = document.getElementById('explorerContextMenu');
            const isFolder = itemMeta.type === 'folder';
            const canQueueAudio = isFolder || isAudioFilename(itemMeta.name);
            const itemsHtml = [];

            itemsHtml.push(`<button onclick="ctxActionOpen()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 flex items-center gap-2.5"><span>📂</span><span>Abrir</span></button>`);

            if (canQueueAudio) {
                itemsHtml.push(`<button onclick="ctxActionAddToPlaylist()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-indigo-300 flex items-center gap-2.5"><span>🎵</span><span>Agregar a la lista de reproducción</span></button>`);
            }

            if (source === 'remote' && String(itemMeta.id) !== 'ROOT') {
                itemsHtml.push(`<button onclick="ctxActionDownloadToLocal()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-sky-300 flex items-center gap-2.5"><span>⬇️</span><span>Descargar a carpeta local abierta</span></button>`);
            }

            if (source === 'local' && (itemMeta.rel_path !== '' || itemMeta.full_path)) {
                itemsHtml.push(`<button onclick="ctxActionUploadToRemote()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-emerald-300 flex items-center gap-2.5"><span>⬆️</span><span>Cargar a carpeta remota abierta</span></button>`);
            }

            if ((source === 'local' && (itemMeta.rel_path !== '' || itemMeta.full_path)) || (source === 'remote' && String(itemMeta.id) !== 'ROOT')) {
                itemsHtml.push(`<div class="my-1 border-t border-slate-800"></div>`);
                itemsHtml.push(`<button onclick="ctxActionRename()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 flex items-center gap-2.5"><span>✏️</span><span>Cambiar de nombre</span></button>`);
                itemsHtml.push(`<button onclick="ctxActionDelete()" class="w-full px-3.5 py-2 text-left hover:bg-rose-950/60 text-rose-400 flex items-center gap-2.5"><span>🗑️</span><span>Eliminar</span></button>`);
            }

            itemsHtml.push(`<div class="my-1 border-t border-slate-800"></div>`);
            itemsHtml.push(`<button onclick="ctxActionProperties()" class="w-full px-3.5 py-2 text-left hover:bg-slate-800 text-slate-300 flex items-center gap-2.5"><span>ℹ️</span><span>Propiedades</span></button>`);

            menu.innerHTML = itemsHtml.join('');
            menu.classList.remove('hidden');

            const x = Math.min(event.clientX, window.innerWidth - 250);
            const y = Math.min(event.clientY, window.innerHeight - 280);
            menu.style.left = `${Math.max(8, x)}px`;
            menu.style.top = `${Math.max(8, y)}px`;
        }

        function ctxActionOpen() {
            if (!activeContextTarget) return;
            const { source, item } = activeContextTarget;
            closeExplorerContextMenu();
            if (item.type === 'folder') {
                if (source === 'local') selectLocalFolder(item.rel_path || '');
                else selectRemoteNode(String(item.id || remoteBaseRootId));
            } else {
                openExplorerFile(source, item);
            }
        }

        function closeAudioScanDock() {
            audioScanDockClosedByUser = true;
            const dock = document.getElementById('audioScanDockWidget');
            if (dock) dock.classList.add('hidden');
        }

        function renderAudioScanProgressUI(job) {
            const dock = document.getElementById('audioScanDockWidget');
            if (!dock || audioScanDockClosedByUser) return;

            dock.classList.remove('hidden');
            document.getElementById('audioScanTitle').textContent = job.title || 'Escaneando música...';
            document.getElementById('audioScanCurrentFolder').textContent = job.error ? job.error : (job.current_folder || '');
            document.getElementById('audioScanCount').textContent = String(job.found_count || 0);
            document.getElementById('audioScanFoldersLabel').textContent = `${job.scanned_folders || 0} carpeta(s) revisada(s)`;

            const dot = document.getElementById('audioScanDot');
            const bar = document.getElementById('audioScanBar');
            const statusLbl = document.getElementById('audioScanStatusLabel');

            if (job.status === 'completed') {
                dot.className = 'w-2 h-2 rounded-full bg-emerald-400 shrink-0';
                bar.style.width = '100%';
                bar.className = 'h-full bg-emerald-500 rounded-full transition-all duration-200';
                statusLbl.textContent = '¡Listo! ✔';
                statusLbl.className = 'text-emerald-400 font-semibold';
            } else if (job.status === 'error') {
                dot.className = 'w-2 h-2 rounded-full bg-rose-500 shrink-0';
                bar.style.width = '100%';
                bar.className = 'h-full bg-rose-500 rounded-full transition-all duration-200';
                statusLbl.textContent = 'Error ✖';
                statusLbl.className = 'text-rose-400 font-semibold';
            } else {
                dot.className = 'w-2 h-2 rounded-full bg-indigo-400 animate-pulse shrink-0';
                const pseudoPct = Math.min(92, 18 + ((job.scanned_folders || 1) * 7) % 74);
                bar.style.width = `${pseudoPct}%`;
                bar.className = 'h-full bg-gradient-to-r from-indigo-500 via-sky-400 to-emerald-400 rounded-full transition-all duration-200';
                statusLbl.textContent = 'Escaneando subcarpetas...';
                statusLbl.className = 'text-indigo-300';
            }
        }

        async function ctxActionAddToPlaylist() {
            if (!activeContextTarget) return;
            const { source, item } = activeContextTarget;
            closeExplorerContextMenu();

            const taskName = document.getElementById('taskSelector').value || '';
            const payload = {
                source,
                type: item.type,
                id: item.id,
                name: item.name || 'Selección',
                full_path: item.full_path || '',
                root_path: localActiveDriveRoot || '',
                task: taskName,
                rel_path: item.rel_path || ''
            };

            try {
                const res = await fetch('/api/explorer/audio_scan_start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const startData = await res.json();
                if (!startData.job_id) {
                    showNotice('Error', startData.error || 'No se pudo iniciar el escaneo de audio.');
                    return;
                }

                if (activeAudioScanPollTimer) {
                    clearInterval(activeAudioScanPollTimer);
                    activeAudioScanPollTimer = null;
                }

                audioScanDockClosedByUser = false;
                renderAudioScanProgressUI({
                    title: startData.title || `Escaneando: ${item.name}`,
                    status: 'running',
                    current_folder: item.name || 'Iniciando...',
                    found_count: 0,
                    scanned_folders: 0
                });

                activeAudioScanPollTimer = setInterval(async () => {
                    try {
                        const stRes = await fetch(`/api/explorer/audio_scan_status/${encodeURIComponent(startData.job_id)}`);
                        if (!stRes.ok) return;
                        const job = await stRes.json();
                        renderAudioScanProgressUI(job);

                        if (job.status === 'completed' || job.status === 'error') {
                            clearInterval(activeAudioScanPollTimer);
                            activeAudioScanPollTimer = null;
                            refreshStatusAndLogs();

                            if (job.status === 'error') {
                                showNotice('Error de Escaneo', job.error || 'Ocurrió un error al buscar pistas de audio.');
                                return;
                            }

                            const tracks = job.tracks || [];
                            if (tracks.length === 0) {
                                showNotice('Sin archivos de audio', 'No se encontraron pistas de audio compatibles en la selección.');
                                setTimeout(() => {
                                    const d = document.getElementById('audioScanDockWidget');
                                    if (d) d.classList.add('hidden');
                                }, 1800);
                                return;
                            }

                            const dockWidget = document.getElementById('musicDockWidget');
                            const isDockHidden = !dockWidget || dockWidget.classList.contains('hidden');
                            const wasEmpty = isDockHidden || musicPlaylist.length === 0 || musicSource !== source;
                            if (wasEmpty) {
                                musicSource = source;
                                musicPlaylist = tracks;
                                rebuildMusicPlayOrder(0);
                                setMusicDockMode(musicDockMode === 'minimized' ? 'normal' : (musicDockMode || 'normal'));
                                await playDockTrackAtOrderPos(0);
                            } else {
                                tracks.forEach(t => {
                                    musicPlaylist.push(t);
                                    musicPlayOrder.push(musicPlaylist.length - 1);
                                });
                                document.getElementById('dockTrackCounter').textContent = `(${musicOrderPos + 1}/${musicPlayOrder.length})`;
                                renderMusicPlaylist();
                                prefetchNextRemoteTrack();
                            }

                            setTimeout(() => {
                                const d = document.getElementById('audioScanDockWidget');
                                if (d) d.classList.add('hidden');
                            }, 2400);
                        }
                    } catch (e) {}
                }, 220);
            } catch (e) {
                showNotice('Error', 'No se pudieron recopilar los archivos multimedia de audio.');
            }
        }

        function setTransferDockMode(mode) {
            transferDockMode = mode;
            const widget = document.getElementById('transferDockWidget');
            const minEl = document.getElementById('transferDockMinimized');
            const expEl = document.getElementById('transferDockExpanded');
            const itemsList = document.getElementById('transferItemsList');
            const maxBtn = document.getElementById('transferMaxRestoreBtn');

            widget.classList.remove('hidden');

            if (mode === 'minimized') {
                widget.className = 'fixed bottom-4 left-4 z-50 transition-all duration-200';
                minEl.classList.remove('hidden');
                expEl.classList.add('hidden');
                return;
            }

            minEl.classList.add('hidden');
            expEl.classList.remove('hidden');

            if (mode === 'maximized') {
                widget.className = 'fixed inset-0 w-screen h-screen p-3 sm:p-6 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-md transition-all duration-200';
                expEl.className = 'w-full h-full max-w-3xl max-h-[85vh] bg-slate-900/95 backdrop-blur border border-slate-700/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                itemsList.className = 'flex-1 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (maxBtn) { maxBtn.textContent = '🗗'; maxBtn.title = 'Restaurar tamaño'; }
            } else {
                widget.className = 'fixed bottom-4 left-4 z-50 transition-all duration-200';
                expEl.className = 'w-80 sm:w-[440px] bg-slate-900/95 backdrop-blur border border-slate-700/90 rounded-2xl shadow-2xl overflow-hidden flex flex-col';
                itemsList.className = 'max-h-36 overflow-y-auto divide-y divide-slate-800/40 text-xs';
                if (maxBtn) { maxBtn.textContent = '⛶'; maxBtn.title = 'Maximizar detalle'; }
            }
        }

        function toggleTransferDockMaximize() {
            setTransferDockMode(transferDockMode === 'maximized' ? 'normal' : 'maximized');
        }

        function closeTransferDock() {
            if (activeTransferPollTimer) {
                clearInterval(activeTransferPollTimer);
                activeTransferPollTimer = null;
            }
            document.getElementById('transferDockWidget').classList.add('hidden');
        }

        function formatMB(bytes) {
            const num = Number(bytes || 0);
            return `${(num / (1024 * 1024)).toFixed(2)} MB`;
        }

        function renderTransferJobUI(job) {
            const isUpload = job.direction === 'upload';
            const pct = Math.max(0, Math.min(100, Number(job.percent || 0)));
            const speedStr = job.speed_text || '0.00 MB/s';

            // Minimizado
            document.getElementById('transferMiniIcon').textContent = isUpload ? '⬆️' : '⬇️';
            document.getElementById('transferMiniTitle').textContent = job.title || 'Transfiriendo...';
            document.getElementById('transferMiniSpeed').textContent = job.status === 'running' ? speedStr : '';
            document.getElementById('transferMiniPct').textContent = `${pct}%`;
            document.getElementById('transferMiniBar').style.width = `${pct}%`;

            // Expandido / Maximizado
            document.getElementById('transferHeaderBadge').textContent = isUpload ? '⬆️ Carga a FileJump' : '⬇️ Descarga a Equipo Local';
            document.getElementById('transferTitleText').textContent = job.title || 'Transferencia';
            document.getElementById('transferCurrentFileText').textContent = job.error ? job.error : (job.current_file || '');
            document.getElementById('transferSpeedBadge').textContent = speedStr;
            document.getElementById('transferPctText').textContent = `${pct}%`;
            document.getElementById('transferCountText').textContent = `${job.processed_files || 0} / ${job.total_files || 0} archivo(s)`;
            document.getElementById('transferMainBar').style.width = `${pct}%`;
            document.getElementById('transferBytesText').textContent = `${formatMB(job.bytes_done || 0)} / ${formatMB(job.bytes_total || 0)}`;

            const dot = document.getElementById('transferStatusDot');
            const stateLabel = document.getElementById('transferStateLabel');
            const mainBar = document.getElementById('transferMainBar');

            if (job.status === 'completed') {
                dot.className = 'w-2 h-2 rounded-full bg-emerald-400 shrink-0';
                stateLabel.textContent = 'Completado ✔';
                stateLabel.className = 'text-emerald-400 font-semibold';
                mainBar.className = 'h-full bg-emerald-500 rounded-full transition-all duration-150';
            } else if (job.status === 'error') {
                dot.className = 'w-2 h-2 rounded-full bg-rose-500 shrink-0';
                stateLabel.textContent = 'Error ✖';
                stateLabel.className = 'text-rose-400 font-semibold';
                mainBar.className = 'h-full bg-rose-500 rounded-full transition-all duration-150';
            } else {
                dot.className = 'w-2 h-2 rounded-full bg-sky-400 animate-pulse shrink-0';
                stateLabel.textContent = job.file_percent > 0 ? `Archivo actual: ${job.file_percent}%` : 'Transfiriendo...';
                stateLabel.className = 'text-sky-300';
                mainBar.className = 'h-full bg-gradient-to-r from-sky-500 via-indigo-500 to-emerald-400 rounded-full transition-all duration-150';
            }

            const items = job.items || [];
            document.getElementById('transferDetailCountTag').textContent = `${items.length} archivo(s)`;
            const listEl = document.getElementById('transferItemsList');
            if (items.length === 0) {
                listEl.innerHTML = '<div class="px-3.5 py-4 text-center text-slate-500">Esperando lista de archivos...</div>';
            } else {
                listEl.innerHTML = items.map(it => {
                    let stIcon = '⏳';
                    let rowCls = 'text-slate-400';
                    let progressInfo = formatMB(it.size);
                    if (it.status === 'active') {
                        stIcon = '🔄';
                        rowCls = 'bg-sky-600/15 text-white font-semibold';
                        const fp = Number(it.file_pct || 0);
                        progressInfo = `${formatMB(it.sent || 0)} / ${formatMB(it.size)} (${fp}%)`;
                    } else if (it.status === 'done') {
                        stIcon = '✅';
                        rowCls = 'text-emerald-300';
                        progressInfo = `${formatMB(it.size)} (100%)`;
                    } else if (it.status === 'error') {
                        stIcon = '❌';
                        rowCls = 'text-rose-400';
                    }
                    return `
                        <div class="px-3.5 py-1.5 flex items-center justify-between gap-2 ${rowCls}">
                            <div class="flex items-center gap-2 min-w-0">
                                <span class="shrink-0 text-[11px]">${stIcon}</span>
                                <span class="truncate" title="${escapeAttr(it.name)}">${escapeAttr(it.name)}</span>
                            </div>
                            <span class="text-[10px] font-mono text-slate-400 shrink-0">${progressInfo}</span>
                        </div>`;
                }).join('');
            }
        }

        function startTransferTracking(jobId, initialTitle, direction) {
            if (activeTransferPollTimer) {
                clearInterval(activeTransferPollTimer);
                activeTransferPollTimer = null;
            }

            renderTransferJobUI({
                direction,
                title: initialTitle,
                status: 'running',
                current_file: 'Iniciando transferencia...',
                processed_files: 0,
                total_files: 0,
                bytes_done: 0,
                bytes_total: 0,
                percent: 2,
                items: []
            });
            setTransferDockMode(transferDockMode === 'minimized' ? 'normal' : (transferDockMode || 'normal'));

            activeTransferPollTimer = setInterval(async () => {
                try {
                    const res = await fetch(`/api/transfer/status/${encodeURIComponent(jobId)}`);
                    if (!res.ok) return;
                    const job = await res.json();
                    renderTransferJobUI(job);
                    refreshStatusAndLogs();

                    if (job.status === 'completed' || job.status === 'error') {
                        clearInterval(activeTransferPollTimer);
                        activeTransferPollTimer = null;
                        if (job.direction === 'download') {
                            await fetchLocalNodeChildren(localSelectedPath || '');
                            renderLocalTreePanel();
                            renderLocalContentsPanel();
                        } else {
                            await fetchRemoteNodeChildren(remoteSelectedKey || 'ROOT');
                            renderRemoteTreePanel();
                            renderRemoteContentsPanel();
                            fetchUserStorageInfo();
                        }
                    }
                } catch (e) {}
            }, 220);
        }

        async function ctxActionDownloadToLocal() {
            if (!activeContextTarget) return;
            const { item } = activeContextTarget;
            closeExplorerContextMenu();

            const taskName = document.getElementById('taskSelector').value || '';
            const currentLocalNode = localNodes.get(localSelectedPath || '');
            const payload = {
                direction: 'download',
                type: item.type,
                id: item.id,
                name: item.name,
                size: item.size || 0,
                mtime: item.mtime || 0,
                dest_full_path: (currentLocalNode && currentLocalNode.full_path) || '',
                dest_root_path: localActiveDriveRoot || '',
                dest_task: taskName,
                dest_rel_path: localSelectedPath || ''
            };

            try {
                const res = await fetch('/api/explorer/transfer_start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.job_id) {
                    startTransferTracking(data.job_id, `Descargando: ${item.name}`, 'download');
                } else {
                    showNotice('Error de Descarga', data.error || 'No se pudo iniciar la descarga.');
                }
            } catch (e) {
                showNotice('Error de Descarga', 'Fallo de comunicación al iniciar la descarga desde FileJump.');
            }
        }

        async function ctxActionUploadToRemote() {
            if (!activeContextTarget) return;
            const { item } = activeContextTarget;
            closeExplorerContextMenu();

            const taskName = document.getElementById('taskSelector').value || '';
            const payload = {
                direction: 'upload',
                type: item.type,
                name: item.name,
                size: item.size || 0,
                full_path: item.full_path || '',
                root_path: localActiveDriveRoot || '',
                task: taskName,
                rel_path: item.rel_path || '',
                dest_remote_id: remoteSelectedKey || 'ROOT'
            };

            try {
                const res = await fetch('/api/explorer/transfer_start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (data.job_id) {
                    startTransferTracking(data.job_id, `Subiendo: ${item.name}`, 'upload');
                } else {
                    showNotice('Error de Carga', data.error || `Fallo al iniciar la subida de '${item.name}'`);
                }
            } catch (e) {
                showNotice('Error de Carga', `Fallo de comunicación al subir '${item.name}'`);
            }
        }

        function ctxActionRename() {
            if (!activeContextTarget) return;
            renamePendingTarget = activeContextTarget;
            closeExplorerContextMenu();
            const modal = document.getElementById('renameItemModal');
            const input = document.getElementById('renameNewNameInput');
            input.value = renamePendingTarget.item.name || '';
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            setTimeout(() => { input.focus(); input.select(); }, 50);
        }

        function closeRenameModal() {
            document.getElementById('renameItemModal').classList.add('hidden');
            document.getElementById('renameItemModal').classList.remove('flex');
        }

        async function submitRenameModal() {
            if (!renamePendingTarget) return;
            const newName = document.getElementById('renameNewNameInput').value.trim();
            if (!newName) return;
            const { source, item } = renamePendingTarget;
            closeRenameModal();

            const taskName = document.getElementById('taskSelector').value || '';
            const res = await fetch('/api/explorer/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    source,
                    type: item.type,
                    id: item.id,
                    full_path: item.full_path || '',
                    root_path: localActiveDriveRoot || '',
                    task: taskName,
                    rel_path: item.rel_path || '',
                    new_name: newName
                })
            });
            const data = await res.json();
            if (data.success) {
                if (source === 'local') {
                    await fetchLocalNodeChildren(localSelectedPath || '');
                    renderLocalTreePanel();
                    renderLocalContentsPanel();
                } else {
                    await fetchRemoteNodeChildren(remoteSelectedKey || 'ROOT');
                    renderRemoteTreePanel();
                    renderRemoteContentsPanel();
                }
            } else {
                showNotice('Error al renombrar', data.error || 'No se pudo cambiar el nombre.');
            }
        }

        function ctxActionDelete() {
            if (!activeContextTarget) return;
            const target = activeContextTarget;
            closeExplorerContextMenu();
            showNotice(
                'Confirmar Eliminación',
                `¿Deseas eliminar definitivamente '${target.item.name}'? Esta acción no se puede deshacer.`,
                async () => {
                    const taskName = document.getElementById('taskSelector').value || '';
                    const res = await fetch('/api/explorer/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            source: target.source,
                            type: target.item.type,
                            id: target.item.id,
                            full_path: target.item.full_path || '',
                            root_path: localActiveDriveRoot || '',
                            task: taskName,
                            rel_path: target.item.rel_path || ''
                        })
                    });
                    const data = await res.json();
                    if (data.success) {
                        if (target.source === 'local') {
                            await fetchLocalNodeChildren(localSelectedPath || '');
                            renderLocalTreePanel();
                            renderLocalContentsPanel();
                        } else {
                            await fetchRemoteNodeChildren(remoteSelectedKey || 'ROOT');
                            renderRemoteTreePanel();
                            renderRemoteContentsPanel();
                            fetchUserStorageInfo();
                        }
                    } else {
                        showNotice('Error al eliminar', data.error || 'No se pudo eliminar el elemento.');
                    }
                }
            );
        }

        async function ctxActionProperties() {
            if (!activeContextTarget) return;
            const { source, item } = activeContextTarget;
            closeExplorerContextMenu();

            const modal = document.getElementById('propertiesItemModal');
            const body = document.getElementById('propertiesModalBody');
            body.innerHTML = '<div class="text-slate-400 animate-pulse">Calculando propiedades...</div>';
            modal.classList.remove('hidden');
            modal.classList.add('flex');

            const taskName = document.getElementById('taskSelector').value || '';
            try {
                const res = await fetch('/api/explorer/properties', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        source,
                        type: item.type,
                        id: item.id,
                        name: item.name,
                        size: item.size || 0,
                        mtime: item.mtime || 0,
                        full_path: item.full_path || '',
                        root_path: localActiveDriveRoot || '',
                        task: taskName,
                        rel_path: item.rel_path || ''
                    })
                });
                const p = await res.json();
                body.innerHTML = `
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Nombre:</span><span class="text-white font-semibold text-right break-all">${escapeAttr(p.name)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Tipo:</span><span class="text-slate-200">${escapeAttr(p.type)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Ubicación:</span><span class="text-slate-300 font-mono text-[11px] text-right break-all">${escapeAttr(p.location)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Tamaño total:</span><span class="text-sky-300 font-mono">${formatBytes(p.size)}</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Contenido:</span><span class="text-slate-200">${p.file_count} archivo(s), ${p.folder_count} carpeta(s)</span></div>
                    <div class="flex justify-between gap-3"><span class="text-slate-400">Modificado:</span><span class="text-slate-200 font-mono">${escapeAttr(p.modified)}</span></div>
                `;
            } catch (e) {
                body.innerHTML = '<div class="text-rose-400">No se pudieron obtener las propiedades.</div>';
            }
        }

        function closePropertiesModal() {
            document.getElementById('propertiesItemModal').classList.add('hidden');
            document.getElementById('propertiesItemModal').classList.remove('flex');
        }

        function shutdownApplication() {
            showNotice(
                'Cerrar FileJump Sync Studio',
                'Se cerrará esta ventana del navegador, el servidor Python y la consola asociada. ¿Deseas salir ahora?',
                async () => {
                    try {
                        await fetch('/api/shutdown', { method: 'POST' });
                    } catch (e) {}

                    // Intentar cerrar la pestaña/ventana del navegador
                    try { window.open('', '_self'); } catch (e) {}
                    try { window.close(); } catch (e) {}

                    // Pantalla de respaldo si el navegador bloquea window.close() por seguridad
                    setTimeout(() => {
                        document.body.innerHTML = `
                            <div class="min-h-screen bg-slate-950 text-slate-200 flex flex-col items-center justify-center p-6 text-center space-y-3">
                                <div class="w-12 h-12 rounded-2xl bg-rose-600/20 border border-rose-500/40 text-rose-400 flex items-center justify-center text-2xl">⏻</div>
                                <h1 class="text-lg font-bold text-white">Servidor y consola cerrados correctamente</h1>
                                <p class="text-xs text-slate-400 max-w-md">El proceso de Python y la ventana de comandos han finalizado. Ya puedes cerrar esta pestaña.</p>
                            </div>`;
                    }, 300);
                }
            );
        }

        // =========================================================================
        // INSPECCIÓN DE TAREAS, SINCRONIZACIÓN Y CONFIGURACIÓN (.ENV + CACHÉ)
        // =========================================================================
        async function loadInitialConfig() {
            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                currentConfig = data.config || { tasks: [] };
                const sel = document.getElementById('taskSelector');
                const prev = sel.value;
                sel.innerHTML = '<option value="">-- Selecciona o crea una tarea --</option>';
                (currentConfig.tasks || []).forEach(t => {
                    const opt = document.createElement('option');
                    opt.value = t.name;
                    opt.textContent = `${t.name} (${t.path})`;
                    sel.appendChild(opt);
                });
                if (prev && Array.from(sel.options).some(o => o.value === prev)) {
                    sel.value = prev;
                }
            } catch (e) {}
        }

        async function inspectSelectedTask(overrideRemoteId = '') {
            const taskName = document.getElementById('taskSelector').value;
            const remoteSelectorVal = overrideRemoteId || document.getElementById('remoteDirSelector').value || 'ROOT';

            if (!taskName) {
                document.getElementById('lastSyncLabel').textContent = '';
                if (remoteSelectorVal) {
                    try {
                        const res = await fetch(`/api/remote/browse?remote_id=${encodeURIComponent(remoteSelectorVal)}`);
                        const data = await res.json();
                        const label = remoteSelectorVal === 'ROOT' ? '/ (Raíz de FileJump)' : `Carpeta #${remoteSelectorVal}`;
                        initRemoteExplorer(data.remote_items || [], remoteSelectorVal, label);
                    } catch (e) {}
                }
                return;
            }

            try {
                const q = remoteSelectorVal ? `?remote_id=${encodeURIComponent(remoteSelectorVal)}` : '';
                const res = await fetch(`/api/inspect/${encodeURIComponent(taskName)}${q}`);
                const data = await res.json();
                if (data.error) {
                    showNotice('Aviso', data.error);
                    return;
                }

                document.getElementById('lastSyncLabel').textContent = data.last_sync
                    ? `Última sync: ${new Date(data.last_sync).toLocaleString()}`
                    : 'Sin sincronizaciones previas';

                if (!localActiveDriveRoot) {
                    document.getElementById('localPathSubtitle').textContent = data.task.path;
                    initLocalExplorer(data.local_items || []);
                }

                const rId = data.remote_root_id || remoteSelectorVal || 'ROOT';
                const rLabel = String(rId) === 'ROOT' ? '/ (Raíz de FileJump)' : `${data.task.name} (#${rId})`;
                initRemoteExplorer(data.remote_items || [], rId, rLabel);
            } catch (e) {
                showNotice('Error', 'No se pudo inspeccionar la tarea seleccionada.');
            }
        }

        async function triggerSync(force = false) {
            const taskName = document.getElementById('taskSelector').value;
            if (!taskName) {
                showNotice('Selecciona una tarea', 'Elige primero una tarea activa antes de sincronizar.');
                return;
            }
            const btn = document.getElementById('syncBtn');
            btn.disabled = true;
            btn.classList.add('opacity-60');
            try {
                const res = await fetch(`/api/sync/${encodeURIComponent(taskName)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force })
                });
                const data = await res.json();
                if (data.requires_confirmation) {
                    showNotice('Confirmación de Seguridad', data.message, () => triggerSync(true));
                } else if (data.success) {
                    await inspectSelectedTask();
                    fetchUserStorageInfo();
                    showNotice('Sincronización Completa', `Se subieron ${data.uploaded} de ${data.total} archivo(s) pendientes.`);
                } else {
                    showNotice('Error de Sincronización', data.error || 'Ocurrió un problema durante la sincronización.');
                }
            } catch (e) {
                showNotice('Error de Red', 'No se pudo comunicar con el servidor local.');
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-60');
            }
        }

        async function refreshCacheUsageDisplay() {
            const label = document.getElementById('cacheUsageSummary');
            if (!label) return;
            try {
                const res = await fetch('/api/cache/clear');
                const data = await res.json();
                label.textContent = `${data.file_count || 0} archivo(s) en caché • ${formatBytes(data.total_bytes || 0)} ocupados en disco`;
            } catch (e) {
                label.textContent = 'No se pudo calcular el tamaño de la caché.';
            }
        }

        function confirmAndClearLocalCache() {
            showNotice(
                'Eliminar Caché Definitivamente',
                'Se eliminarán permanentemente todos los archivos almacenados en temp_cache sin pasar por la Papelera de Reciclaje. ¿Continuar?',
                async () => {
                    const res = await fetch('/api/cache/clear', { method: 'POST' });
                    const data = await res.json();
                    if (data.success) {
                        prefetchedRemoteIds.clear();
                        if (musicSource === 'remote' && musicPlaylist.length > 0) {
                            renderMusicPlaylist();
                        }
                        await refreshCacheUsageDisplay();
                        showNotice('Caché Eliminada', `Se borraron definitivamente ${data.deleted_count} archivo(s) (${formatBytes(data.freed_bytes)} liberados).`);
                    }
                }
            );
        }

        async function openSettingsModal() {
            const modal = document.getElementById('settingsModal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            refreshCacheUsageDisplay();

            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                currentConfig = data.config || { tasks: [] };
                document.getElementById('envApiKey').value = '';
                document.getElementById('envBaseUrl').value = data.env.FILEJUMP_BASE_URL || 'https://filejump.com/api';
                document.getElementById('envUploadUrl').value = data.env.FILEJUMP_UPLOAD_URL || 'https://uploads.filejump.com/api/upload';
                document.getElementById('cfgWorkers').value = currentConfig.max_workers || 4;
                document.getElementById('cfgThreshold').value = currentConfig.min_impact_percentage || 40;
                document.getElementById('cfgHighSpeed').checked = currentConfig.use_high_speed_upload !== false;

                const container = document.getElementById('tasksListContainer');
                container.innerHTML = '';
                (currentConfig.tasks || []).forEach(t => addTaskRow(t.name, t.path, t.min_impact_percentage));
            } catch (e) {}
        }

        function closeSettingsModal() {
            document.getElementById('settingsModal').classList.add('hidden');
            document.getElementById('settingsModal').classList.remove('flex');
        }

        function addTaskRow(name = '', path = '', threshold = 40) {
            const container = document.getElementById('tasksListContainer');
            const row = document.createElement('div');
            row.className = 'task-row grid grid-cols-1 sm:grid-cols-12 gap-2 bg-slate-950 p-2.5 rounded-xl border border-slate-800 items-center';
            row.innerHTML = `
                <input type="text" placeholder="Nombre tarea" value="${escapeAttr(name)}" class="task-name sm:col-span-3 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white">
                <input type="text" placeholder="Ruta local (ej. C:\\MisArchivos)" value="${escapeAttr(path)}" class="task-path sm:col-span-6 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200">
                <input type="number" min="1" max="100" value="${threshold}" title="Umbral %" class="task-threshold sm:col-span-2 bg-slate-900 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-white">
                <button type="button" onclick="this.parentElement.remove()" class="sm:col-span-1 text-rose-400 hover:text-rose-300 text-sm font-bold text-center" title="Quitar">✕</button>
            `;
            container.appendChild(row);
        }

        async function saveSettings() {
            const tasks = [];
            document.querySelectorAll('#tasksListContainer .task-row').forEach(row => {
                const name = row.querySelector('.task-name').value.trim();
                const path = row.querySelector('.task-path').value.trim();
                const min_impact_percentage = Number(row.querySelector('.task-threshold').value || 40);
                if (name && path) {
                    tasks.push({ name, path, min_impact_percentage });
                }
            });

            const payload = {
                env: {
                    FILEJUMP_API_KEY: document.getElementById('envApiKey').value.trim(),
                    FILEJUMP_BASE_URL: document.getElementById('envBaseUrl').value.trim(),
                    FILEJUMP_UPLOAD_URL: document.getElementById('envUploadUrl').value.trim()
                },
                config: {
                    max_workers: Number(document.getElementById('cfgWorkers').value || 4),
                    min_impact_percentage: Number(document.getElementById('cfgThreshold').value || 40),
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
            await loadInitialConfig();
            wasConnected = false;
            refreshStatusAndLogs();
        }

        // Inicialización
        refreshStatusAndLogs();
        syncCachedRemoteIds();
        loadRemoteMusicFavorites();
        fetchLocalDrives(true);
        loadInitialConfig();
        setInterval(refreshStatusAndLogs, 4000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    host = "127.0.0.1"
    port = 5000
    url = f"http://{host}:{port}"
    log_event("info", f"Iniciando FileJump Sync Studio en {url}")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False)
import os
import sys
import time
import uuid
import shutil
import hashlib
import mimetypes
import subprocess
import threading
import signal
import ctypes
import datetime
from datetime import timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from flask import Blueprint, request, jsonify, render_template, send_file
from dotenv import load_dotenv, set_key

from config_manager import (
    ENV_FILE,
    TEMP_CACHE_DIR,
    activity_logs,
    transfer_jobs,
    audio_scan_jobs,
    log_event,
    compute_api_key_hash,
    load_cache_index,
    save_cache_index,
    ensure_cache_matches_api_key,
    load_config,
    save_config,
    load_task_record,
    save_task_record,
)
from filejump_client import FileJumpClient
from media_utils import (
    extract_office_preview_html,
    extract_id3_metadata,
    video_needs_audio_transcode,
    get_video_audio_tracks,
)
from sync_engine import (
    scan_local_directory,
    list_local_immediate_children,
    get_local_drives,
    list_local_path_immediate_children,
    list_remote_immediate_children,
    scan_remote_tree,
    compare_local_and_remote,
)

api_bp = Blueprint("api_bp", __name__)


# ==============================================================================
# RUTA PRINCIPAL (FRONTEND)
# ==============================================================================
@api_bp.route("/")
def index():
    return render_template("index.html")


# ==============================================================================
# ESTADO, PERFIL DE USUARIO Y FAVORITOS MUSICALES
# ==============================================================================
@api_bp.route("/api/status", methods=["GET"])
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


@api_bp.route("/api/user/profile", methods=["GET", "POST"])
def api_user_profile():
    """Devuelve el perfil/almacenamiento desde FileJump y permite guardar el nombre visible."""
    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"connected": False})

    if request.method == "POST":
        data = request.json or {}
        key_hash = compute_api_key_hash(client.api_key)[:16]
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


@api_bp.route("/api/music/favorites", methods=["GET", "POST"])
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


# ==============================================================================
# NAVEGACIÓN LOCAL Y REMOTA (LAZY LOADING)
# ==============================================================================
@api_bp.route("/api/remote/directories", methods=["GET"])
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


@api_bp.route("/api/local/drives", methods=["GET"])
def api_local_drives():
    """Devuelve todas las unidades de almacenamiento locales disponibles."""
    return jsonify({"drives": get_local_drives()})


@api_bp.route("/api/local/browse", methods=["GET"])
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


@api_bp.route("/api/remote/browse", methods=["GET"])
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


@api_bp.route("/api/local/children", methods=["GET"])
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


@api_bp.route("/api/remote/children", methods=["GET"])
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


# ==============================================================================
# CONFIGURACIÓN (.env + sync_config.json) Y SINCRONIZACIÓN
# ==============================================================================
@api_bp.route("/api/settings", methods=["GET", "POST"])
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


@api_bp.route("/api/inspect/<task_name>", methods=["GET"])
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


@api_bp.route("/api/sync/<task_name>", methods=["POST"])
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

    local_items = scan_local_directory(local_root)
    remote_items = scan_remote_tree(client, task_remote_id)
    comp_local, _ = compare_local_and_remote(local_items, remote_items, record)

    folder_id_map = {"": task_remote_id}
    for r_item in remote_items:
        if r_item["type"] == "folder":
            folder_id_map[r_item["rel_path"]] = r_item["id"]

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

    to_upload = [i for i in comp_local if i["type"] == "file" and i["sync_status"] in ("new", "modified")]

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


# ==============================================================================
# STREAMING, VISTAS PREVIAS, MULTIMEDIA Y CACHÉ
# ==============================================================================
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


@api_bp.route("/api/video/tracks", methods=["GET"])
def api_video_tracks():
    """Devuelve las pistas de audio disponibles dentro de un archivo de video."""
    file_path, err = _resolve_target_file()
    if err:
        return jsonify({"tracks": []})
    tracks = get_video_audio_tracks(file_path)
    return jsonify({"tracks": tracks})


@api_bp.route("/api/file/stream", methods=["GET"])
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

    if ext in video_exts and (force_transcode or has_explicit_track or video_needs_audio_transcode(file_path)):
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            path_hash = hashlib.md5(f"{file_path}_{file_path.stat().st_mtime}_a{audio_idx}".encode("utf-8")).hexdigest()[:12]
            mp4_cached = TEMP_CACHE_DIR / f"{file_path.stem}_{path_hash}_web.mp4"
            if not mp4_cached.exists() or mp4_cached.stat().st_size == 0:
                try:
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


@api_bp.route("/api/file/preview", methods=["GET"])
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
                content = f.read(250_000)
            return jsonify({"kind": "text", "name": file_path.name, "content": content})
        except Exception as e:
            return jsonify({"error": f"Error leyendo archivo de texto: {e}"}), 500

    if ext in office_exts:
        html_preview = extract_office_preview_html(file_path)
        return jsonify({"kind": "office", "name": file_path.name, "html": html_preview})

    return jsonify({"kind": "unsupported", "name": file_path.name, "ext": ext})


@api_bp.route("/api/file/audio_meta", methods=["GET"])
def api_file_audio_meta():
    """Devuelve las etiquetas ID3 y la imagen incrustada en Base64 para archivos de audio."""
    file_path, err = _resolve_target_file()
    if err:
        return jsonify({"error": err}), 404
    meta = extract_id3_metadata(file_path)
    return jsonify(meta)


@api_bp.route("/api/file/prefetch", methods=["GET"])
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


@api_bp.route("/api/cache/status", methods=["GET"])
def api_cache_status():
    """Devuelve los IDs remotos que ya existen físicamente en la caché persistente."""
    cache_index = load_cache_index()
    cached_ids = set()

    for rid, meta in cache_index.get("files", {}).items():
        fname = meta.get("filename", "")
        if fname and (TEMP_CACHE_DIR / fname).exists():
            cached_ids.add(str(rid))

    if TEMP_CACHE_DIR.exists():
        for entry in TEMP_CACHE_DIR.iterdir():
            if entry.is_file() and not entry.name.startswith(".") and "_" in entry.name:
                prefix_id = entry.name.split("_", 1)[0]
                if prefix_id.isdigit() and entry.stat().st_size > 0:
                    cached_ids.add(prefix_id)

    return jsonify({"cached_ids": sorted(cached_ids)})


@api_bp.route("/api/cache/clear", methods=["GET", "POST"])
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

    deleted_count = 0
    freed_bytes = 0
    current_hash = compute_api_key_hash(os.getenv("FILEJUMP_API_KEY", "").strip())

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


# ==============================================================================
# ACCIONES DEL EXPLORADOR Y TRANSFERENCIAS EN SEGUNDO PLANO
# ==============================================================================
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


@api_bp.route("/api/explorer/open_native", methods=["POST"])
def api_explorer_open_native():
    """Abre un archivo local directamente o descarga un remoto a caché con barra de progreso y luego lo abre."""
    data = request.json or {}
    source = (data.get("source") or "local").strip()

    if source == "local":
        target = _resolve_local_explorer_target(data)
        if not target or not target.exists() or not target.is_file():
            return jsonify({"error": "Archivo local no encontrado"}), 404
        try:
            if os.name == "nt":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            log_event("info", f"🖥️ Abierto en programa local del sistema: {target.name}")
            return jsonify({"success": True, "path": str(target)})
        except Exception as e:
            log_event("error", f"No se pudo abrir '{target.name}' en programa local: {e}")
            return jsonify({"error": f"No se pudo abrir en el programa del sistema: {e}"}), 500

    remote_id = str(data.get("id") or data.get("remote_id") or "").strip()
    filename = (data.get("name") or f"archivo_{remote_id}").strip()
    f_size = int(data.get("size") or 0)
    try:
        remote_mtime = float(data.get("mtime") or 0)
    except (ValueError, TypeError):
        remote_mtime = 0.0

    if not remote_id or remote_id == "ROOT":
        return jsonify({"error": "ID remoto inválido"}), 400

    client = FileJumpClient()
    if not client.api_key:
        return jsonify({"error": "Falta FILEJUMP_API_KEY en el archivo .env"}), 400

    job_id = uuid.uuid4().hex[:12]
    single_items = [{"name": filename, "size": f_size, "sent": 0, "file_pct": 0, "status": "active"}]
    transfer_jobs[job_id] = {
        "id": job_id,
        "direction": "download",
        "title": f"Preparando en caché: {filename}",
        "status": "running",
        "current_file": filename,
        "processed_files": 0,
        "total_files": 1,
        "bytes_done": 0,
        "bytes_total": f_size,
        "percent": 0,
        "file_percent": 0,
        "speed_bps": 0.0,
        "speed_text": "0.00 MB/s",
        "items": list(single_items),
        "error": None
    }

    def _download_to_cache_and_open():
        try:
            log_event("info", f"⬇ Descargando '{filename}' a caché local para abrirlo con el programa del sistema...")
            cb = _make_speed_tracker(job_id, 0, f_size, single_items, 0)
            cached_target = client.download_file_to_cache(
                remote_id,
                filename,
                remote_mtime=remote_mtime,
                progress_callback=cb
            )
            if not cached_target or not cached_target.exists():
                _update_transfer_job(
                    job_id,
                    status="error",
                    error=f"No se pudo descargar '{filename}' en caché",
                    items=[{"name": filename, "size": f_size, "sent": 0, "file_pct": 0, "status": "error"}]
                )
                log_event("error", f"✖ No se pudo descargar '{filename}' en caché.")
                return

            actual_size = cached_target.stat().st_size
            _update_transfer_job(
                job_id,
                status="completed",
                processed_files=1,
                bytes_done=actual_size,
                bytes_total=max(f_size, actual_size),
                percent=100,
                file_percent=100,
                speed_text="Listo",
                current_file="Abriendo en programa local...",
                items=[{"name": filename, "size": actual_size, "sent": actual_size, "file_pct": 100, "status": "done"}]
            )

            if os.name == "nt":
                os.startfile(str(cached_target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(cached_target)])
            else:
                subprocess.Popen(["xdg-open", str(cached_target)])

            log_event("success", f"🖥️ '{filename}' guardado en caché y abierto en programa local.")
        except Exception as exc:
            _update_transfer_job(
                job_id,
                status="error",
                error=f"Error al abrir '{filename}': {exc}",
                items=[{"name": filename, "size": f_size, "sent": 0, "file_pct": 0, "status": "error"}]
            )
            log_event("error", f"✖ Error preparando/abriendo '{filename}': {exc}")

    threading.Thread(target=_download_to_cache_and_open, daemon=True).start()
    return jsonify({"success": True, "job_id": job_id, "remote_id": remote_id})


@api_bp.route("/api/explorer/audio_scan_start", methods=["POST"])
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


@api_bp.route("/api/explorer/audio_scan_status/<job_id>", methods=["GET"])
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


@api_bp.route("/api/explorer/audio_collect", methods=["POST"])
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


@api_bp.route("/api/explorer/rename", methods=["POST"])
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


@api_bp.route("/api/explorer/delete", methods=["POST"])
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


@api_bp.route("/api/explorer/properties", methods=["POST"])
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


@api_bp.route("/api/explorer/download_to_local", methods=["POST"])
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


@api_bp.route("/api/transfer/status/<job_id>", methods=["GET"])
def api_transfer_status(job_id):
    """Devuelve el progreso en tiempo real de una transferencia (carga o descarga)."""
    job = transfer_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Trabajo de transferencia no encontrado"}), 404
    return jsonify(job)


@api_bp.route("/api/explorer/transfer_start", methods=["POST"])
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


@api_bp.route("/api/explorer/upload_to_remote", methods=["POST"])
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


@api_bp.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Finaliza el servidor Python y cierra la ventana de consola (CMD/Terminal) asociada."""
    log_event("warning", "Solicitud de cierre recibida. Cerrando servidor y consola...")

    def _terminate_app():
        time.sleep(0.35)
        try:
            if os.name == "nt":
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd:
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
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

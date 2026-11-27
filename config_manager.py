import os
import socket
import platform
import json
import time
import threading
import shutil
import hashlib
import datetime
from datetime import timezone
from pathlib import Path
from dotenv import load_dotenv

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
REMOTE_PLAYLISTS_SUBDIR = "playlist"
REMOTE_FAVORITES_FILENAME = "favorites.json"
REMOTE_FAVORITES_M3U_FILENAME = "favorites.m3u"
REMOTE_MACHINE_PROFILES_FILENAME = "sync_profiles.json"
LOCAL_PLAYLISTS_DIR = BASE_DIR / "playlists"
LOCAL_PLAYLISTS_DIR.mkdir(exist_ok=True)


def get_machine_name():
    """Obtiene el nombre neto del equipo (ej. DELL3040CASA en Windows o hostname en Linux/macOS)."""
    raw_name = (
        os.environ.get("COMPUTERNAME")
        or platform.node()
        or socket.gethostname()
        or "EQUIPO_LOCAL"
    ).strip()
    clean_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw_name).strip("_")
    return clean_name or "EQUIPO_LOCAL"


RECORDS_DIR.mkdir(exist_ok=True)
TEMP_CACHE_DIR.mkdir(exist_ok=True)

# Crear .env por defecto si no existe
if not ENV_FILE.exists():
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write('FILEJUMP_API_KEY=""\n')
        f.write('FILEJUMP_BASE_URL="https://filejump.com/api"\n')
        f.write('FILEJUMP_UPLOAD_URL="https://uploads.filejump.com/api/upload"\n')

load_dotenv(ENV_FILE)

# Estado global en memoria para logs y trabajos en segundo plano
activity_logs = []
transfer_jobs = {}
audio_scan_jobs = {}
compare_jobs = {}


def log_event(level, message):
    """Registra eventos con marca de tiempo para mostrarlos en la interfaz web."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "level": level, "message": message}
    activity_logs.append(entry)
    if len(activity_logs) > 200:
        activity_logs.pop(0)
    print(f"[{timestamp}] [{level.upper()}] {message}")


def compute_api_key_hash(api_key_str):
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
    current_hash = compute_api_key_hash(current_api_key)
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


# Verificar coherencia de caché al importar el módulo
ensure_cache_matches_api_key(os.getenv("FILEJUMP_API_KEY", "").strip())


# ==============================================================================
# GESTIÓN DE CONFIGURACIÓN Y REGISTROS JSON (THREAD-SAFE Y ESCRITURA ATÓMICA)
# ==============================================================================
CONFIG_LOCK = threading.RLock()
CONFIG_BACKUP_FILE = BASE_DIR / "sync_config.json.bak"
_LAST_VALID_CONFIG = None


def _atomic_write_json(target_path, data_dict, backup_path=None):
    """Escribe un archivo JSON de manera atómica mediante archivo temporal + os.replace."""
    target_path = Path(target_path)
    tmp_path = target_path.with_name(f"{target_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    serialized = json.dumps(data_dict, indent=4, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(serialized)
        f.flush()
        os.fsync(f.fileno())
    if backup_path and target_path.exists():
        try:
            shutil.copy2(target_path, backup_path)
        except Exception:
            pass
    os.replace(tmp_path, target_path)


def load_config():
    """Carga la configuración de tareas y parámetros globales desde sync_config.json de forma segura."""
    global _LAST_VALID_CONFIG
    default_config = {
        "max_workers": 4,
        "min_impact_percentage": 40,
        "use_high_speed_upload": True,
        "account_profiles": {},
        "preserved_mtimes": {},
        "tasks": []
    }
    with CONFIG_LOCK:
        if not CONFIG_FILE.exists():
            if CONFIG_BACKUP_FILE.exists():
                try:
                    shutil.copy2(CONFIG_BACKUP_FILE, CONFIG_FILE)
                except Exception:
                    pass
            if not CONFIG_FILE.exists():
                save_config(default_config)
                return default_config

        last_err = None
        for attempt in range(4):
            try:
                raw_text = CONFIG_FILE.read_text(encoding="utf-8").strip()
                if not raw_text:
                    raise ValueError("Archivo vacío temporalmente")
                data = json.loads(raw_text)
                for k, v in default_config.items():
                    data.setdefault(k, v)
                _LAST_VALID_CONFIG = json.loads(json.dumps(data))
                return data
            except Exception as e:
                last_err = e
                time.sleep(0.03 * (attempt + 1))

        # Si falló la lectura, recuperar desde memoria o respaldo .bak en lugar de devolver vacío
        if _LAST_VALID_CONFIG is not None:
            return json.loads(json.dumps(_LAST_VALID_CONFIG))
        if CONFIG_BACKUP_FILE.exists():
            try:
                data = json.loads(CONFIG_BACKUP_FILE.read_text(encoding="utf-8"))
                for k, v in default_config.items():
                    data.setdefault(k, v)
                _LAST_VALID_CONFIG = json.loads(json.dumps(data))
                return data
            except Exception:
                pass

        log_event("error", f"Error leyendo {CONFIG_FILE.name}: {last_err}")
        return default_config


def save_config(config_data):
    """Guarda la configuración en sync_config.json usando bloqueo de hilo y reemplazo atómico."""
    global _LAST_VALID_CONFIG
    with CONFIG_LOCK:
        _atomic_write_json(CONFIG_FILE, config_data, backup_path=CONFIG_BACKUP_FILE)
        _LAST_VALID_CONFIG = json.loads(json.dumps(config_data))


def get_task_record_path(task_name):
    """Devuelve la ruta del JSON de historial para una tarea específica."""
    safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in task_name)
    return RECORDS_DIR / f"{safe_name}_record.json"


def load_task_record(task_name):
    """Carga el registro JSON local de archivos/carpetas sincronizados."""
    path = get_task_record_path(task_name)
    with CONFIG_LOCK:
        if path.exists():
            for attempt in range(3):
                try:
                    raw = path.read_text(encoding="utf-8").strip()
                    if not raw:
                        raise ValueError("Registro vacío")
                    data = json.loads(raw)
                    data.setdefault("files", {})
                    data.setdefault("folders", {})
                    return data
                except Exception:
                    time.sleep(0.02 * (attempt + 1))
    return {"files": {}, "folders": {}, "last_sync": None}


def save_task_record(task_name, record_data):
    """Guarda el registro JSON local de archivos/carpetas sincronizados de manera atómica."""
    with CONFIG_LOCK:
        record_data["last_sync"] = datetime.datetime.now(timezone.utc).isoformat()
        path = get_task_record_path(task_name)
        _atomic_write_json(path, record_data)

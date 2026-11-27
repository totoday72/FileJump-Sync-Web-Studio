import os
import json
import time
import uuid
import mimetypes
import datetime
from datetime import timezone
from pathlib import Path
import requests
from dotenv import load_dotenv

from config_manager import (
    ENV_FILE,
    TEMP_CACHE_DIR,
    LOCAL_FAVORITES_FALLBACK,
    LOCAL_PLAYLISTS_DIR,
    REMOTE_APP_CONFIG_DIR,
    REMOTE_PLAYLISTS_SUBDIR,
    REMOTE_FAVORITES_FILENAME,
    REMOTE_FAVORITES_M3U_FILENAME,
    REMOTE_MACHINE_PROFILES_FILENAME,
    get_machine_name,
    log_event,
    compute_api_key_hash,
    ensure_cache_matches_api_key,
    load_cache_index,
    save_cache_index,
    load_config,
    save_config,
)
from sync_engine import parse_iso_timestamp


class FileJumpClient:
    """Cliente REST API de FileJump para gestión de archivos, carpetas, cuota, favoritos y caché."""

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
        """Verifica si el token Bearer es válido consultando el endpoint de archivos."""
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

        key_hash = compute_api_key_hash(self.api_key)[:16]
        cfg = load_config()
        saved_profile = cfg.get("account_profiles", {}).get(key_hash, {})

        user_name = None
        user_email = None
        user_avatar = None
        user_role = None
        used_bytes = 0
        total_bytes = 0
        api_percentage = None

        api_root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

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
                    )
                    if found_name and not user_name:
                        user_name = str(found_name).strip()
                    if u_obj.get("email") and not user_email:
                        user_email = str(u_obj["email"]).strip()
                    avatar_val = u_obj.get("avatar") or u_obj.get("avatar_url") or u_obj.get("profile_photo_url")
                    if avatar_val and not user_avatar:
                        user_avatar = str(avatar_val).strip()
                    if isinstance(u_obj.get("roles"), list) and len(u_obj["roles"]) > 0 and not user_role:
                        first_role = u_obj["roles"][0]
                        user_role = first_role.get("name") if isinstance(first_role, dict) else str(first_role)
                    elif u_obj.get("plan") and not user_role:
                        user_role = str(u_obj["plan"]).strip()
        except Exception as e:
            log_event("warning", f"No se pudo leer /auth/user: {e}")

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
                            if not user_email and t_user.get("email"):
                                user_email = str(t_user["email"]).strip()
            except Exception:
                pass

        if saved_profile.get("user_name"):
            user_name = saved_profile["user_name"]
        elif not user_name:
            user_name = user_email or "Cuenta FileJump"

        free_bytes = max(0, total_bytes - used_bytes) if total_bytes > 0 else 0
        if api_percentage is not None:
            pct = round(api_percentage, 1)
        else:
            pct = round((used_bytes / total_bytes) * 100, 1) if total_bytes > 0 else 0.0

        return {
            "user_name": user_name,
            "user_email": user_email or "",
            "user_avatar": user_avatar or "",
            "user_role": user_role or "Cuenta Activa",
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
        con lectura por bloques (streaming multipart).
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

    def ensure_remote_machine_profiles_dir(self):
        """Asegura '/filejump_sync_studio/<NOMBRE_DEL_EQUIPO>' en FileJump y devuelve su ID."""
        if not self.api_key:
            return None
        app_dir_id = self.ensure_remote_app_config_dir()
        if not app_dir_id:
            return None
        machine_name = get_machine_name()
        subfolders = self.list_folders(parent_id=app_dir_id)
        for sf in subfolders:
            if (sf.get("name") or "").strip().lower() == machine_name.lower() and sf.get("id") is not None:
                return sf.get("id")
        return self.create_folder(machine_name, parent_id=app_dir_id)

    def load_remote_machine_profiles(self):
        """Descarga el archivo único '/filejump_sync_studio/<EQUIPO>/sync_profiles.json' hacia local."""
        if not self.api_key:
            return None
        machine_dir_id = self.ensure_remote_machine_profiles_dir()
        if not machine_dir_id:
            return None
        try:
            files = self.list_files(folder_id=machine_dir_id)
            matching_files = [
                fl for fl in files
                if (fl.get("name") or fl.get("file_name") or "").strip().lower() == REMOTE_MACHINE_PROFILES_FILENAME.lower()
                and fl.get("id") is not None
            ]
            if not matching_files:
                return None

            # Tomar el más reciente (mayor ID o updated_at) y limpiar duplicados si existieran
            matching_files.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or x.get("id") or ""), reverse=True)
            target_file = matching_files[0]
            for extra_dup in matching_files[1:]:
                try:
                    self.delete_file(extra_dup.get("id"))
                except Exception:
                    pass

            fid = target_file.get("id")
            headers = {"Authorization": f"Bearer {self.api_key}"}
            for url in (f"{self.base_url}/files/{fid}/download", f"{self.base_url}/drive/file-entries/{fid}/download"):
                try:
                    resp = requests.get(url, headers=headers, timeout=20)
                    if resp.status_code == 200 and resp.text.strip():
                        parsed = json.loads(resp.text)
                        if isinstance(parsed, dict):
                            return parsed
                except Exception:
                    pass
        except Exception as e:
            log_event("warning", f"No se pudieron leer perfiles remotos del equipo: {e}")
        return None

    def save_remote_machine_profiles(self, config_data):
        """
        Reemplaza el archivo único '/filejump_sync_studio/<NOMBRE_DEL_EQUIPO>/sync_profiles.json'
        en FileJump cada vez que se crea, modifica o elimina un perfil localmente.
        """
        if not self.api_key:
            return False, "Falta FILEJUMP_API_KEY"
        machine_name = get_machine_name()
        machine_dir_id = self.ensure_remote_machine_profiles_dir()
        if not machine_dir_id:
            return False, f"No se pudo crear la carpeta remota '{REMOTE_APP_CONFIG_DIR}/{machine_name}'"

        payload_data = {
            "machine_name": machine_name,
            "updated_at": datetime.datetime.now(timezone.utc).isoformat(),
            "max_workers": config_data.get("max_workers", 4),
            "min_impact_percentage": config_data.get("min_impact_percentage", 40),
            "use_high_speed_upload": config_data.get("use_high_speed_upload", True),
            "tasks": config_data.get("tasks", [])
        }

        try:
            # Eliminar cualquier archivo previo de perfiles en la carpeta del equipo para mantener uno solo
            existing_files = self.list_files(folder_id=machine_dir_id)
            for fl in existing_files:
                fname = (fl.get("name") or fl.get("file_name") or "").strip()
                if fname.lower() == REMOTE_MACHINE_PROFILES_FILENAME.lower() and fl.get("id") is not None:
                    self.delete_file(fl.get("id"))

            TEMP_CACHE_DIR.mkdir(exist_ok=True)
            tmp_json_path = TEMP_CACHE_DIR / REMOTE_MACHINE_PROFILES_FILENAME
            with open(tmp_json_path, "w", encoding="utf-8") as f:
                json.dump(payload_data, f, indent=4, ensure_ascii=False)

            res = self.upload_file(str(tmp_json_path), folder_id=machine_dir_id, use_high_speed=False)
            try:
                tmp_json_path.unlink()
            except Exception:
                pass

            if res is not None:
                return True, f"Archivo '{REMOTE_APP_CONFIG_DIR}/{machine_name}/{REMOTE_MACHINE_PROFILES_FILENAME}' actualizado en FileJump."
            return False, "FileJump no confirmó la subida del archivo de perfiles."
        except Exception as e:
            return False, str(e)

    def ensure_remote_playlists_dir(self):
        """Asegura '/filejump_sync_studio/playlist' en FileJump y devuelve su folder_id."""
        if not self.api_key:
            return None
        app_dir_id = self.ensure_remote_app_config_dir()
        if not app_dir_id:
            return None
        subfolders = self.list_folders(parent_id=app_dir_id)
        for sf in subfolders:
            if (sf.get("name") or "").strip().lower() == REMOTE_PLAYLISTS_SUBDIR.lower() and sf.get("id") is not None:
                return sf.get("id")
        return self.create_folder(REMOTE_PLAYLISTS_SUBDIR, parent_id=app_dir_id)

    def sync_favorites_m3u_playlist(self, favorites_map):
        """
        Genera y actualiza '/filejump_sync_studio/playlist/favorites.m3u' (y copia local)
        a partir del mapa actual de canciones favoritas.
        """
        favs = favorites_map if isinstance(favorites_map, dict) else {}
        lines = ["#EXTM3U", "#PLAYLIST:Favoritos"]

        # Ordenar por added_at ascendente para conservar el orden en que se fueron marcando
        ordered_entries = sorted(
            favs.values(),
            key=lambda x: str(x.get("added_at") or "") if isinstance(x, dict) else ""
        )

        for item in ordered_entries:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "Pista").strip()
            artist = str(item.get("artist") or "").strip()
            display_label = f"{artist} - {title}" if artist and artist.lower() != "artista desconocido" else title
            source = str(item.get("source") or ("remote" if item.get("remote_id") else "local")).strip()

            if source == "remote" and item.get("remote_id"):
                rem_id = item.get("remote_id")
                rel_or_name = item.get("rel_path") or item.get("name") or f"{title}.mp3"
                meta_json = json.dumps({
                    "source": "remote",
                    "id": rem_id,
                    "name": item.get("name") or Path(rel_or_name).name,
                    "rel_path": rel_or_name,
                    "size": item.get("size", 0),
                    "mtime": item.get("mtime", 0)
                }, ensure_ascii=False)
                lines.append(f"#EXTFJ:{meta_json}")
                lines.append(f"#EXTINF:-1,{display_label}")
                lines.append(f"filejump://{rem_id}/{rel_or_name}")
            elif item.get("full_path"):
                full_path = str(item.get("full_path")).strip()
                meta_json = json.dumps({
                    "source": "local",
                    "full_path": full_path,
                    "name": item.get("name") or Path(full_path).name,
                    "rel_path": item.get("rel_path") or Path(full_path).name,
                    "size": item.get("size", 0),
                    "mtime": item.get("mtime", 0)
                }, ensure_ascii=False)
                lines.append(f"#EXTFJ:{meta_json}")
                lines.append(f"#EXTINF:-1,{display_label}")
                lines.append(full_path)

        m3u_content = "\n".join(lines) + "\n"
        LOCAL_PLAYLISTS_DIR.mkdir(exist_ok=True)
        local_fav_m3u = LOCAL_PLAYLISTS_DIR / REMOTE_FAVORITES_M3U_FILENAME
        try:
            with open(local_fav_m3u, "w", encoding="utf-8") as lf:
                lf.write(m3u_content)
        except Exception:
            pass

        if not self.api_key:
            return False, "Falta FILEJUMP_API_KEY en .env"

        playlists_dir_id = self.ensure_remote_playlists_dir()
        if not playlists_dir_id:
            return False, f"No se pudo acceder o crear '{REMOTE_APP_CONFIG_DIR}/{REMOTE_PLAYLISTS_SUBDIR}'."

        try:
            existing_files = self.list_files(folder_id=playlists_dir_id)
            for fl in existing_files:
                fname = (fl.get("name") or fl.get("file_name") or "").strip()
                if fname.lower() == REMOTE_FAVORITES_M3U_FILENAME.lower() and fl.get("id") is not None:
                    self.delete_file(fl.get("id"))

            res = self.upload_file(str(local_fav_m3u), folder_id=playlists_dir_id, use_high_speed=False)
            if res is not None:
                return True, f"'{REMOTE_FAVORITES_M3U_FILENAME}' actualizado en '{REMOTE_APP_CONFIG_DIR}/{REMOTE_PLAYLISTS_SUBDIR}/'."
            return False, f"No se pudo subir '{REMOTE_FAVORITES_M3U_FILENAME}' a FileJump."
        except Exception as e:
            return False, str(e)

    def save_remote_m3u_playlist(self, filename_m3u, m3u_content):
        """
        Guarda un archivo .m3u en '/filejump_sync_studio/playlist/' verificando antes que no exista
        otro archivo con el mismo nombre para evitar cualquier reemplazo.
        """
        if not self.api_key:
            return False, "Falta FILEJUMP_API_KEY en .env", None

        playlists_dir_id = self.ensure_remote_playlists_dir()
        if not playlists_dir_id:
            return False, f"No se pudo acceder o crear la carpeta '{REMOTE_APP_CONFIG_DIR}/{REMOTE_PLAYLISTS_SUBDIR}' en FileJump.", None

        existing_files = self.list_files(folder_id=playlists_dir_id)
        for fl in existing_files:
            existing_name = (fl.get("name") or fl.get("file_name") or "").strip()
            if existing_name.lower() == filename_m3u.lower():
                return False, f"Ya existe una lista llamada '{existing_name}' en '{REMOTE_APP_CONFIG_DIR}/{REMOTE_PLAYLISTS_SUBDIR}'. Elige otro nombre para evitar reemplazarla.", None

        LOCAL_PLAYLISTS_DIR.mkdir(exist_ok=True)
        local_copy = LOCAL_PLAYLISTS_DIR / filename_m3u
        if local_copy.exists():
            return False, f"Ya existe una lista local llamada '{filename_m3u}'. Elige otro nombre para evitar reemplazarla.", None

        try:
            with open(local_copy, "w", encoding="utf-8") as lf:
                lf.write(m3u_content)

            res = self.upload_file(str(local_copy), folder_id=playlists_dir_id, use_high_speed=False)
            if res is not None:
                remote_id = res.get("id") if isinstance(res, dict) else None
                return True, f"Lista '{filename_m3u}' guardada correctamente en '{REMOTE_APP_CONFIG_DIR}/{REMOTE_PLAYLISTS_SUBDIR}/'.", remote_id
            return False, "No se pudo subir el archivo .m3u a FileJump.", None
        except Exception as e:
            return False, str(e), None

    def _count_m3u_tracks(self, file_id, filename, remote_mtime=0.0):
        """Cuenta las pistas de audio válidas dentro de un archivo .m3u (usando copia local o caché)."""
        try:
            local_copy = LOCAL_PLAYLISTS_DIR / Path(filename).name
            m3u_text = ""
            if local_copy.exists() and local_copy.stat().st_size > 0:
                m3u_text = local_copy.read_text(encoding="utf-8", errors="replace")
            else:
                cached = self.download_file_to_cache(file_id, filename, remote_mtime=remote_mtime)
                if cached and cached.exists():
                    m3u_text = cached.read_text(encoding="utf-8", errors="replace")
            if not m3u_text:
                return 0
            count = 0
            for raw_line in m3u_text.splitlines():
                line = raw_line.strip()
                if line and not line.startswith("#"):
                    count += 1
            return count
        except Exception:
            return 0

    def list_remote_m3u_playlists(self):
        """Lista todos los archivos .m3u / .m3u8 guardados en '/filejump_sync_studio/playlist/' junto con su cantidad de pistas."""
        if not self.api_key:
            return False, "Falta FILEJUMP_API_KEY en .env", []
        playlists_dir_id = self.ensure_remote_playlists_dir()
        if not playlists_dir_id:
            return False, f"No se pudo acceder a '{REMOTE_APP_CONFIG_DIR}/{REMOTE_PLAYLISTS_SUBDIR}' en FileJump.", []

        try:
            files = self.list_files(folder_id=playlists_dir_id)
            playlists = []
            for fl in files:
                fname = (fl.get("name") or fl.get("file_name") or "").strip()
                if fname.lower().endswith((".m3u", ".m3u8")) and fl.get("id") is not None:
                    fid = fl.get("id")
                    upd = fl.get("updated_at") or fl.get("created_at") or ""
                    rmtime = parse_iso_timestamp(upd)
                    track_count = self._count_m3u_tracks(fid, fname, remote_mtime=rmtime)
                    playlists.append({
                        "id": fid,
                        "name": fname,
                        "display_name": Path(fname).stem,
                        "track_count": track_count,
                        "size": int(fl.get("file_size") or fl.get("size") or 0),
                        "updated_at": upd
                    })
            playlists.sort(key=lambda x: x["name"].lower())
            return True, "OK", playlists
        except Exception as e:
            return False, str(e), []

    def delete_remote_m3u_playlist(self, file_id, filename=""):
        """Elimina un archivo .m3u de '/filejump_sync_studio/playlist/' en FileJump y su copia local."""
        if not self.api_key:
            return False, "Falta FILEJUMP_API_KEY en .env"
        try:
            ok = self.delete_file(file_id)
            if filename:
                local_copy = LOCAL_PLAYLISTS_DIR / Path(filename).name
                if local_copy.exists():
                    try:
                        local_copy.unlink()
                    except Exception:
                        pass
            if ok:
                return True, f"Lista '{filename or file_id}' eliminada de '{REMOTE_APP_CONFIG_DIR}/{REMOTE_PLAYLISTS_SUBDIR}/'."
            return False, "FileJump no confirmó la eliminación del archivo .m3u."
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

    def download_file_to_cache(self, file_id, filename, remote_mtime=0.0, progress_callback=None):
        """
        Descarga un archivo remoto a TEMP_CACHE_DIR solo si no existe en caché
        o si la versión remota tiene una fecha de modificación más reciente.
        """
        TEMP_CACHE_DIR.mkdir(exist_ok=True)
        cache_index = load_cache_index()
        rid_key = str(file_id)

        safe_name = "".join(c if c.isalnum() or c in (".", "-", "_", " ") else "_" for c in filename).strip() or f"file_{file_id}"
        cached_path = TEMP_CACHE_DIR / f"{file_id}_{safe_name}"

        effective_remote_mtime = float(remote_mtime or 0.0)
        if effective_remote_mtime <= 0.0:
            effective_remote_mtime = self.get_remote_file_mtime(file_id)

        entry_meta = cache_index.get("files", {}).get(rid_key, {})
        cached_remote_mtime = float(entry_meta.get("remote_mtime", 0.0))

        if cached_path.exists() and cached_path.stat().st_size > 0:
            local_cache_mtime = cached_path.stat().st_mtime
            ref_cached_ts = cached_remote_mtime if cached_remote_mtime > 0 else local_cache_mtime
            if effective_remote_mtime <= 0.0 or effective_remote_mtime <= (ref_cached_ts + 2.0):
                if progress_callback:
                    sz = cached_path.stat().st_size
                    progress_callback(sz, sz)
                return cached_path
            log_event("info", f"Archivo remoto #{file_id} actualizado en nube; descargando versión más reciente...")

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
                        with open(cached_path, "wb") as out_f:
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
                        cache_index.setdefault("files", {})[rid_key] = {
                            "filename": cached_path.name,
                            "remote_mtime": effective_remote_mtime if effective_remote_mtime > 0 else time.time(),
                            "cached_at": time.time()
                        }
                        save_cache_index(cache_index)
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

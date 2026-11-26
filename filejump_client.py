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
    REMOTE_APP_CONFIG_DIR,
    REMOTE_FAVORITES_FILENAME,
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

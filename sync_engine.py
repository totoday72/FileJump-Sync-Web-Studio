import os
import string
import datetime
from datetime import timezone
from pathlib import Path
from config_manager import load_config, log_event


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
        remote_items.extend(scan_remote_tree(client, fid, rel_path, depth + 1, max_depth, on_item_callback=on_item_callback))

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

import os
import json
import base64
import struct
import shutil
import zipfile
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


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
                            cursor = mime_end + 2
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


def video_needs_audio_transcode(file_path):
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
            if len(codecs) > 1:
                return True
            for c in codecs:
                if c not in {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "pcm_s24le"}:
                    return True
        except Exception:
            pass
    return False


def get_video_audio_tracks(file_path):
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

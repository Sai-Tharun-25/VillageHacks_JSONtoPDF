from __future__ import annotations
from pathlib import Path
import json, re, base64
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional
from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent
ASSETS_DIR  = BASE_DIR / "report_assets"
IMAGES_DIR  = ASSETS_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

ALLOW_REMOTE_HTTP = True 
# Canonical order for output
ORDERED_MAJOR = [
    "I. STRUCTURAL SYSTEMS",
    "II. ELECTRICAL SYSTEMS",
    "III. HEATING, VENTILATION AND AIR CONDITIONING SYSTEMS",
    "IV. PLUMBING SYSTEMS",
    "V. APPLIANCES",
    "VI. OPTIONAL SYSTEMS",
]

MAJOR_NORMALIZE = {
    "Structural Systems": ORDERED_MAJOR[0],
    "Electrical Systems": ORDERED_MAJOR[1],
    "Heating, Ventilation and Air Conditioning Systems": ORDERED_MAJOR[2],
    "Plumbing Systems": ORDERED_MAJOR[3],
    "Appliances": ORDERED_MAJOR[4],
    "Optional Systems": ORDERED_MAJOR[5],
}

MAJOR_TO_SUBSECTION_ORDER = {
    ORDERED_MAJOR[0]: [
        "Foundations","Grading and Drainage","Roof Covering Materials","Roof Structures and Attics",
        "Walls (Interior and Exterior)","Ceilings and Floors","Doors (Interior and Exterior)","Windows",
        "Stairways (Interior and Exterior)","Fireplaces and Chimneys","Porches, Balconies, Decks, and Carports","Other"
    ],
    ORDERED_MAJOR[1]: [
        "Service Entrance and Panels","Branch Circuits, Connected Devices, and Fixtures","Other (Electrical)"
    ],
    ORDERED_MAJOR[2]: [
        "Heating Equipment","Cooling Equipment","Duct Systems, Chases, and Vents","Other (HVAC)"
    ],
    ORDERED_MAJOR[3]: [
        "Plumbing Supply, Distribution Systems and Fixtures","Drains, Wastes, and Vents","Water Heating Equipment",
        "Hydro-Massage Therapy Equipment","Gas Distribution Systems and Gas Appliances","Other (Plumbing)"
    ],
    ORDERED_MAJOR[4]: [
        "Dishwashers","Food Waste Disposers","Range Hood and Exhaust Systems","Ranges, Cooktops, and Ovens",
        "Microwave Ovens","Mechanical Exhaust Vents and Bathroom Heaters","Garage Door Operators",
        "Dryer Exhaust Systems","Other (Appliances)"
    ],
    ORDERED_MAJOR[5]: [
        "Landscape Irrigation (Sprinkler) Systems","Swimming Pools, Spas, Hot Tubs, and Equipment","Outbuildings",
        "Private Water Wells","Private Sewage Disposal Systems","Private Sewage Systems","Other Built-in Appliances","Other (Optional)"
    ],
}

def load_json(p: Path|str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))

def ms_to_date(ms: int|float|None) -> str:
    try:
        return datetime.fromtimestamp(int(ms)/1000, tz=timezone.utc).strftime("%m/%d/%Y")
    except Exception:
        return ""

def extract_header_info(data: dict) -> dict:
    insp = data.get("inspection", {})
    client = insp.get("clientInfo", {}).get("name", "")
    inspector = insp.get("inspector", {}).get("name", "")
    address = insp.get("address", {})
    full_addr = address.get("fullAddress") or " ".join(filter(None, [
        address.get("street"), address.get("city"), address.get("state"), address.get("zipcode")
    ]))
    date_str = ms_to_date(insp.get("schedule", {}).get("date"))
    return {
        "client": client,
        "inspector": inspector,
        "address": full_addr,
        "date": date_str,
        "trec_license": insp.get("inspector", {}).get("trec_license", ""),
        "sponsor": insp.get("inspector", {}).get("sponsor", ""),
        "sponsor_license": insp.get("inspector", {}).get("sponsor_license", ""),
    }

def normalize_title(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+"," ", s.strip().lower())

def build_title_lookup(mapping: dict) -> Dict[str, Tuple[str,str]]:
    out={}
    for major, m in mapping.items():
        major_norm = MAJOR_NORMALIZE.get(major)
        if not major_norm:
            continue
        for src, canonical in m.items():
            out[normalize_title(src)] = (major_norm, canonical)
    return out

def resolve_item(title: str, lookup: dict) -> Optional[Tuple[str,str]]:
    k = normalize_title(title)
    if k in lookup:
        return lookup[k]
    for key, val in lookup.items():
        if k in key or key in k:
            return val
    return None

# def _collect_images(line_item: dict) -> list[tuple[str, int, int]]:
#     """
#     Save embeddable images to report_assets/images and return their saved paths.
#     Accepts:
#       - local paths (string or dict: path/imagePath/photoPath/file/filePath/localPath)
#       - data-URIs (dict: data_uri/dataURI/uri/url/downloadURL when value startswith 'data:')
#     Remote http(s) URLs remain ignored (no downloads).
#     """
#     import base64, hashlib, os, re
#     from pathlib import Path

#     def _save_bytes_to_images_dir(content: bytes, ext: str, key: str) -> str:
#         h = hashlib.sha1(content).hexdigest()[:12]
#         fname = f"{key}_{h}{ext}"
#         dest = IMAGES_DIR / fname
#         if not dest.exists():
#             dest.write_bytes(content)
#         return str(dest)

#     def _copy_local(p: str) -> str | None:
#         if not isinstance(p, str) or p.startswith("http"):
#             return None  # no downloads
#         src = Path(p)
#         if not src.exists():
#             return None
#         try:
#             data = src.read_bytes()
#             ext  = src.suffix.lower() or ".jpg"
#             return _save_bytes_to_images_dir(data, ext, "local")
#         except Exception:
#             return None

#     def _save_data_uri(s: str) -> str | None:
#         if not (isinstance(s, str) and s.startswith("data:")):
#             return None
#         try:
#             head, b64 = s.split(",", 1)
#             ext = ".jpg"
#             if "png" in head: ext = ".png"
#             if "webp" in head: ext = ".webp"
#             data = base64.b64decode(b64)
#             return _save_bytes_to_images_dir(data, ext, "datauri")
#         except Exception:
#             return None

#     out: list[tuple[str, int, int]] = []
#     debug_lines: list[str] = []

#     def harvest(obj):
#         if isinstance(obj, (list, tuple)):
#             for x in obj:
#                 harvest(x)
#             return
#         if isinstance(obj, str):
#             # direct string path or data-uri
#             saved = _save_data_uri(obj) if obj.startswith("data:") else _copy_local(obj)
#             if saved:
#                 out.append((saved, 0, 0)); debug_lines.append(f"saved:{saved}")
#             return
#         if isinstance(obj, dict):
#             # local-ish keys
#             for k in ("path","imagePath","photoPath","file","filePath","localPath"):
#                 v = obj.get(k)
#                 if isinstance(v, str):
#                     saved = _copy_local(v)
#                     if saved:
#                         out.append((saved, 0, 0)); debug_lines.append(f"local:{v} -> {saved}")
#             # data-uri under url-ish keys
#             for k in ("data_uri","dataURI","uri","url","downloadURL"):
#                 v = obj.get(k)
#                 if isinstance(v, str) and v.startswith("data:"):
#                     saved = _save_data_uri(v)
#                     if saved:
#                         out.append((saved, 0, 0)); debug_lines.append(f"data_uri -> {saved}")
#             # nested lists
#             for k in ("images","photos","attachments","media"):
#                 if k in obj:
#                     harvest(obj[k])

#     # line-item level
#     harvest(line_item.get("images"))
#     harvest(line_item.get("photos"))
#     harvest(line_item.get("attachments"))
#     # comment level
#     for c in (line_item.get("comments") or []):
#         harvest(c.get("images")); harvest(c.get("photos")); harvest(c.get("attachments"))

#     # optional manifest (helps verify what saved)
#     try:
#         (IMAGES_DIR / "_manifest.txt").write_text("\n".join(debug_lines), encoding="utf-8")
#     except Exception as E:
#         print("Failed to write image manifest:", E)
#         pass

#     return out

def _collect_images(line_item: dict) -> list[tuple[str, int, int]]:
    """
    Save embeddable images under report_assets/images/ and return ABSOLUTE paths.
    Accepts:
      - local paths (string or dict: path/imagePath/photoPath/file/filePath/localPath)
      - data-URIs (dict: data_uri/dataURI/uri/url/downloadURL that start with 'data:')
      - http(s) URLs only if ALLOW_REMOTE_HTTP=True (downloaded into IMAGES_DIR)
    Searches line-item level and comment level: images/photos/attachments/media.
    """
    import base64, hashlib, os, re, urllib.request
    from pathlib import Path

    def _save_bytes(content: bytes, ext: str, tag: str) -> str:
        # content-hashed filename to dedupe
        h = hashlib.sha1(content).hexdigest()[:12]
        if not ext.startswith("."):
            ext = f".{ext}"
        dest = IMAGES_DIR / f"{tag}_{h}{ext.lower() or '.jpg'}"
        if not dest.exists():
            dest.write_bytes(content)
        return str(dest.resolve())  # absolute

    def _resolve_local_candidate(p: str) -> Path | None:
        """Try BASE_DIR first, then CWD, then absolute if already absolute."""
        cand = Path(p).expanduser()
        candidates = []
        if cand.is_absolute():
            candidates.append(cand)
        else:
            candidates.append((BASE_DIR / cand).resolve())
            candidates.append((Path.cwd() / cand).resolve())
        for c in candidates:
            if c.exists() and c.is_file():
                return c
        return None

    def _copy_local(p: str) -> str | None:
        if not isinstance(p, str) or p.lower().startswith(("http://","https://")):
            return None
        src = _resolve_local_candidate(p)
        if not src:
            return None
        try:
            # if it's already in our destination tree, reuse it
            if IMAGES_DIR in src.parents:
                return str(src.resolve())
            data = src.read_bytes()
            ext  = src.suffix or ".jpg"
            return _save_bytes(data, ext, "local")
        except Exception:
            return None

    def _save_data_uri(s: str) -> str | None:
        if not (isinstance(s, str) and s.startswith("data:")):
            return None
        try:
            head, b64 = s.split(",", 1)
            ext = ".jpg"
            if "png" in head.lower():  ext = ".png"
            if "webp" in head.lower(): ext = ".webp"
            data = base64.b64decode(b64)
            return _save_bytes(data, ext, "datauri")
        except Exception:
            return None

    def _download_remote(u: str) -> str | None:
        if not (isinstance(u, str) and u.lower().startswith(("http://","https://"))):
            return None
        if not ALLOW_REMOTE_HTTP:
            manifest.append(f"skip_remote(disabled): {u}")
            return None
        try:
            # pick a reasonable ext; default jpg
            ext = os.path.splitext(u.split("?",1)[0])[1].lower()
            if ext not in (".jpg",".jpeg",".png",".gif",".webp"):
                ext = ".jpg"
            # basic fetch with timeout
            req = urllib.request.Request(u, headers={"User-Agent": "TREC-Report/1.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read()
            path = _save_bytes(data, ext, "remote")
            manifest.append(f"remote:{u[:80]} -> {Path(path).name}")
            return path
        except Exception as e:
            manifest.append(f"remote_error: {u[:80]} -> {e}")
            return None

    out: list[tuple[str, int, int]] = []
    manifest: list[str] = []

    def harvest(obj):
        if isinstance(obj, (list, tuple)):
            for x in obj:
                harvest(x)
            return

        if isinstance(obj, str):
            # direct string: try data-URI, then local, then remote
            saved = (_save_data_uri(obj) or
                     _copy_local(obj) or
                     _download_remote(obj))
            if saved:
                out.append((saved, 0, 0)); manifest.append(f"saved:{saved}")
            else:
                manifest.append(f"skipped:{obj[:80]}")
            return

        if isinstance(obj, dict):
            # Local-ish keys
            for k in ("path","imagePath","photoPath","file","filePath","localPath"):
                v = obj.get(k)
                if isinstance(v, str):
                    saved = _copy_local(v)
                    if saved:
                        out.append((saved, 0, 0)); manifest.append(f"local:{v} -> {saved}")
            # URL-ish keys (data-URI or http)
            for k in ("data_uri","dataURI","uri","url","downloadURL","publicUrl","publicURL","signedUrl","signedURL"):
                v = obj.get(k)
                if isinstance(v, str):
                    saved = _save_data_uri(v) or _download_remote(v)
                    if saved:
                        out.append((saved, 0, 0)); manifest.append(f"url:{k} -> {Path(saved).name}")
                    else:
                        manifest.append(f"skip_url:{k} {v[:80]}")
            # Nested containers
            for k in ("images","photos","attachments","media"):
                if k in obj:
                    harvest(obj[k])

    # line-item level
    harvest(line_item.get("images"))
    harvest(line_item.get("photos"))
    harvest(line_item.get("attachments"))
    # comment level
    for c in (line_item.get("comments") or []):
        harvest(c.get("images"))
        harvest(c.get("photos"))
        harvest(c.get("attachments"))

    # write manifest for troubleshooting
    try:
        (IMAGES_DIR / "_manifest.txt").write_text("\n".join(manifest), encoding="utf-8")
    except Exception:
        pass

    return out



def group_items_detailed(data: dict, lookup: dict) -> Dict[str, Dict[str, List[dict]]]:
    """
    Return:
      { MAJOR: { SUBSECTION: [ {label, text, type, images}, ... ] } }
    """
    grouped: Dict[str, Dict[str, List[dict]]] = {}
    for section in data.get("inspection", {}).get("sections", []):
        for li in section.get("lineItems", []) or []:
            title = li.get("title") or li.get("name") or ""
            if not title:
                continue
            r = resolve_item(title, lookup)
            if not r:
                continue
            major, canonical = r
            texts, severity = [], "info"
            for c in li.get("comments", []) or []:
                v = c.get("value")
                if isinstance(v, str) and v.strip():
                    texts.append(v.strip())
                t = (c.get("type") or "").lower()
                if t == "defect":
                    severity = "defect"
                elif t == "limit" and severity != "defect":
                    severity = "limit"
            item = {
                "label": title,
                "text": " ".join(texts).strip(),
                "type": severity,
                "images": _collect_images(li),
            }
            grouped.setdefault(major, {}).setdefault(canonical, []).append(item)
    return grouped

def compute_subsection_status(items: List[dict]) -> Dict[str, bool]:
    if not items:
        return {"I": False, "NI": False, "NP": True, "D": False}
    has_defect = any((it.get("type") or "") == "defect" for it in items)
    return {"I": True, "NI": False, "NP": False, "D": has_defect}

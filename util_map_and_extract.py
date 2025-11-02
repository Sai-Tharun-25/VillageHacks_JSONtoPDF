from __future__ import annotations
from pathlib import Path
import json, re, base64
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

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

# ----------- images (no downloads) -----------
def _maybe_add_local(pathlike: str, out: List[Tuple[str,int,int]]):
    if not isinstance(pathlike, str): return
    p = Path(pathlike)
    if p.exists():
        out.append((str(p), 0, 0))

def _maybe_add_data_uri(data_uri: str, out: List[Tuple[str,int,int]]):
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        return
    try:
        head, b64 = data_uri.split(",", 1)
        ext = ".jpg"
        if "png" in head: ext = ".png"
        tmp = Path(f"~img_{abs(hash(data_uri))}{ext}")
        tmp.write_bytes(base64.b64decode(b64))
        out.append((str(tmp), 0, 0))
    except Exception:
        pass

def _collect_images(line_item: dict) -> List[Tuple[str,int,int]]:
    """
    Collect local-embeddable images WITHOUT downloading:
      - Accept: {path:"..."}, {imagePath:"..."}, {photoPath:"..."}, "data_uri": "data:image/...;base64,..."
      - Ignore: http(s) URLs (by design)
      - Works at line-item level and comment level
    """
    out: List[Tuple[str,int,int]] = []

    def handle(container):
        if not container: return
        for img in container:
            # direct string path
            if isinstance(img, str):
                if img.startswith("http"):  # ignore remote
                    continue
                _maybe_add_local(img, out)
                continue
            if not isinstance(img, dict):
                continue
            # local paths
            for key in ("path","imagePath","photoPath"):
                if key in img and isinstance(img[key], str) and not img[key].startswith("http"):
                    _maybe_add_local(img[key], out)
            # data URI
            if "data_uri" in img and isinstance(img["data_uri"], str):
                _maybe_add_data_uri(img["data_uri"], out)

    # item-level
    handle(line_item.get("images"))
    handle(line_item.get("photos"))
    # comment-level
    for c in (line_item.get("comments") or []):
        handle(c.get("images"))
        handle(c.get("photos"))

    return out
# ---------------------------------------------

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

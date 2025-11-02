#!/usr/bin/env python3
"""
Fill the TREC PDF from inspection.json by targeting the top text widgets per page.

What it does:
- Page 1: fills client/date/address/inspector fields.
- Page 3: puts Structural comments into the large text widget.
- Page 4: puts Electrical + HVAC + Plumbing into the large text widget.
- Page 5: puts Appliances into the large text widget.
- Page 6: puts Optional Systems into the large text widget.
- Sets AcroForm appearance defaults so values render in any viewer.

Notes:
- Based on your widget dump, the large text widgets are the ones with parent names
  Page3[0], Page4[0], Page5[0], Page6[0] and big rects like [129, 745, 574, 762]. (See widgets_by_page.txt)
"""

from pathlib import Path
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    NameObject, BooleanObject, TextStringObject, DictionaryObject,
    IndirectObject, ArrayObject
)

# ----- Paths (same folder) -----
TEMPLATE = Path("TREC_Template_Blank.pdf")
JSON_FILE = Path("inspection.json")
OUTPUT    = Path("TREC_Filled_From_JSON.pdf")

# ----- Page → sections mapping -----
PAGE_BUCKETS = {
    3: ["STRUCTURAL"],
    4: ["ELECTRICAL", "HVAC", "PLUMBING"],
    5: ["APPLIANCES"],
    6: ["OPTIONAL"],
}

# ----- Helpers -----
def ms_to_date(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%m/%d/%Y")
    except Exception:
        return ""

def load_json() -> dict:
    return json.loads(JSON_FILE.read_text(encoding="utf-8"))

def as_obj(x):
    try:
        return x.get_object() if isinstance(x, IndirectObject) else x
    except Exception:
        return x

def get_root(reader: PdfReader):
    return as_obj(reader.trailer.get("/Root"))

def get_acroform(root) -> DictionaryObject:
    root = as_obj(root)
    if not isinstance(root, dict):
        return None
    af = root.get("/AcroForm")
    return as_obj(af) if af else None

def add_appearances(writer: PdfWriter, reader: PdfReader):
    """Copy AcroForm & add appearance defaults so text shows in all viewers."""
    root = get_root(reader)
    acro = get_acroform(root)
    if acro:
        writer._root_object.update({NameObject("/AcroForm"): acro})
        acroform = writer._root_object["/AcroForm"]
        acroform.update({NameObject("/NeedAppearances"): BooleanObject(True)})
        acroform.update({NameObject("/DA"): TextStringObject("/Helv 0 Tf 0 g")})
        # Provide Helvetica in resources for /DA
        font_dict = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font_dict)
        dr = acroform.get("/DR") or DictionaryObject()
        fonts = dr.get("/Font") or DictionaryObject()
        fonts.update({NameObject("/Helv"): font_ref})
        dr.update({NameObject("/Font"): fonts})
        acroform.update({NameObject("/DR"): dr})

# ----- Page 1 fill -----
def fill_page1(writer: PdfWriter, data: dict):
    insp = data.get("inspection", {})
    client = insp.get("clientInfo", {}).get("name", "")
    inspector = insp.get("inspector", {}).get("name", "")
    address = insp.get("address", {})
    full_addr = address.get("fullAddress") or " ".join(filter(None, [
        address.get("street"), address.get("city"), address.get("state"), address.get("zipcode")
    ]))
    date_str = ms_to_date(insp.get("schedule", {}).get("date"))

    mapping = {
        "Name of Client": client,
        "Date of Inspection": date_str,
        "Address of Inspected Property": full_addr,
        "Name of Inspector": inspector,
        # Add these if present in your JSON
        "TREC License": "",
        "Name of Sponsor if applicable": "",
        "TREC License_2": "",
    }
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, mapping)
        except Exception:
            pass

# ----- Build section text from JSON -----
BUCKET_KEYWORDS = {
    "STRUCTURAL": [
        "foundation","structural","crawlspace","stair","roof structure","attic",
        "overhead space","interior elements","doors","windows","walls",
        "porches","balconies","decks","carports","fireplace","venting",
        "ground-level exterior"
    ],
    "ELECTRICAL": ["electrical"],
    "HVAC":       ["heating","hvac","ventilation","air conditioning"],
    "PLUMBING":   ["water","waste","plumbing","drain","sewage","water heating"],
    "APPLIANCES": ["appliances","integrated","garage","dryer","range","cooktop","oven","microwave","hood","disposer","dishwasher"],
    "OPTIONAL":   ["detached","pool","spa","hot tub","outbuilding","irrigation","sprinkler","private water","private sewage"]
}

def bucket_of(sec_name: str) -> str:
    n = (sec_name or "").lower()
    for b, kws in BUCKET_KEYWORDS.items():
        for kw in kws:
            if kw in n:
                return b
    return None

def build_bucket_texts(data: dict) -> Dict[str, str]:
    out = {k: [] for k in BUCKET_KEYWORDS.keys()}
    for s in data.get("inspection", {}).get("sections", []):
        b = bucket_of(s.get("name",""))
        if not b:
            continue
        for li in s.get("lineItems", []):
            title = li.get("title") or li.get("name") or "Item"
            comments = []
            for c in li.get("comments", []):
                v = c.get("value")
                if isinstance(v, str) and v.strip():
                    comments.append(v.strip())
            txt = " ".join(comments).strip()
            out[b].append(f"• {title}: {txt}" if txt else f"• {title}")
    # Join and slightly cap size for single-line fields
    joined = {k: "\n".join(v) for k, v in out.items() if v}
    return {k: (t[:1500] + " …") if len(t) > 1500 else t for k, t in joined.items()}

# ----- Find and fill the BIG text widget on a given page -----
def page_widgets(reader: PdfReader, page_no: int) -> List[Tuple[str, DictionaryObject, List[float], DictionaryObject]]:
    """
    Return list of (parent_name, parent_obj, rect, widget_annot) for /Widget annots on page_no (1-based),
    where parent is a /Tx field.
    """
    page = as_obj(reader.pages[page_no-1])
    ann = page.get("/Annots")
    if not ann:
        return []
    ann = as_obj(ann)
    if isinstance(ann, ArrayObject):
        items = ann
    elif isinstance(ann, list):
        items = ann
    else:
        items = [ann]

    out = []
    for a in items:
        annot = as_obj(a)
        if not isinstance(annot, dict):
            continue
        if annot.get("/Subtype") != NameObject("/Widget"):
            continue
        parent = as_obj(annot.get("/Parent")) if annot.get("/Parent") else None
        if not isinstance(parent, dict):
            continue
        if parent.get("/FT") != NameObject("/Tx"):
            continue
        name = parent.get("/T")
        rect = annot.get("/Rect") or parent.get("/Rect")
        out.append((name, parent, rect, annot))
    return out

def rect_area(rect) -> float:
    try:
        x0,y0,x1,y1 = [float(v) for v in rect] if rect else (0,0,0,0)
        return abs((x1-x0)*(y1-y0))
    except Exception:
        return 0.0

def fill_big_text_widget_on_page(writer: PdfWriter, reader: PdfReader, page_no: int, text: str):
    widgets = page_widgets(reader, page_no)
    if not widgets or not text.strip():
        return
    # Pick the largest rect widget on this page (your dump shows these as the top bars ~129→574 wide)
    widgets.sort(key=lambda w: rect_area(w[2]), reverse=True)
    big_name = widgets[0][0]
    if not big_name:
        # fallback: set /V directly if name missing
        parent = widgets[0][1]
        parent.update({NameObject("/V"): TextStringObject(text)})
        return
    try:
        writer.update_page_form_field_values(writer.pages[page_no-1], {big_name: text})
    except Exception:
        # fallback direct
        parent = widgets[0][1]
        parent.update({NameObject("/V"): TextStringObject(text)})

# ----- Main -----
def main():
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"Missing template: {TEMPLATE}")
    if not JSON_FILE.exists():
        raise FileNotFoundError(f"Missing JSON: {JSON_FILE}")

    data = load_json()
    reader = PdfReader(str(TEMPLATE))
    writer = PdfWriter()

    # copy all pages
    for p in reader.pages:
        writer.add_page(p)

    # ensure appearances/fonts
    add_appearances(writer, reader)

    # page 1 basics
    fill_page1(writer, data)

    # build section text
    bucket_texts = build_bucket_texts(data)

    # fill the big text widget on pages 3–6
    for page_no, buckets in PAGE_BUCKETS.items():
        page_text = "\n\n".join([bucket_texts.get(b, "") for b in buckets if bucket_texts.get(b)])
        fill_big_text_widget_on_page(writer, reader, page_no, page_text)

    with open(OUTPUT, "wb") as f:
        writer.write(f)

    print(f"Wrote: {OUTPUT.resolve()}")

if __name__ == "__main__":
    main()

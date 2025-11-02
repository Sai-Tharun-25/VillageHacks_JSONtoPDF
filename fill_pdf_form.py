#!/usr/bin/env python3
from pathlib import Path
import json, re, math
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Optional

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    NameObject, BooleanObject, TextStringObject, DictionaryObject,
    IndirectObject, ArrayObject, NumberObject
)

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, ListFlowable, ListItem
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

# -------- Files --------
TEMPLATE = Path("TREC_Template_Blank.pdf")
JSON_FILE = Path("inspection.json")
MAP_FILE  = Path("mappings.json")
OUTPUT    = Path("TREC_Filled_From_JSON.pdf")
UNMAPPED  = Path("unmapped_titles.txt")
PAYLOADS  = Path("page_payload_stats.txt")
NARRATIVE = Path("~trec_narrative_tmp.pdf")

# -------- Per-page subsection order (top→bottom) --------
PAGE_SUBSECTIONS = {
    3: ["Foundations","Grading and Drainage","Roof Covering Materials","Roof Structures and Attics",
        "Walls (Interior and Exterior)","Ceilings and Floors","Doors (Interior and Exterior)","Windows",
        "Stairways (Interior and Exterior)","Fireplaces and Chimneys","Porches, Balconies, Decks, and Carports","Other"],
    4: ["Service Entrance and Panels","Branch Circuits, Connected Devices, and Fixtures","Other (Electrical)",
        "Heating Equipment","Cooling Equipment","Duct Systems, Chases, and Vents","Other (HVAC)",
        "Plumbing Supply, Distribution Systems and Fixtures","Drains, Wastes, and Vents","Water Heating Equipment"],
    5: ["Hydro-Massage Therapy Equipment","Gas Distribution Systems and Gas Appliances","Other (Plumbing)",
        "Dishwashers","Food Waste Disposers","Range Hood and Exhaust Systems","Ranges, Cooktops, and Ovens",
        "Microwave Ovens","Mechanical Exhaust Vents and Bathroom Heaters","Garage Door Operators","Dryer Exhaust Systems","Other (Appliances)"],
    6: ["Landscape Irrigation (Sprinkler) Systems","Swimming Pools, Spas, Hot Tubs, and Equipment","Outbuildings",
        "Private Water Wells","Private Sewage Disposal Systems","Other Built-in Appliances","Other (Optional)"],
}

MAJOR_TO_BUCKET = {
    "Structural Systems":"STRUCTURAL","Electrical Systems":"ELECTRICAL",
    "Heating, Ventilation and Air Conditioning Systems":"HVAC",
    "Plumbing Systems":"PLUMBING","Appliances":"APPLIANCES","Optional Systems":"OPTIONAL",
}

MULTILINE_BIT = 1<<12

# -------- utils --------
def as_obj(x): return x.get_object() if isinstance(x, IndirectObject) else x
def ms_to_date(ms): 
    try: return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%m/%d/%Y")
    except: return ""

def normalize_title(s:str)->str:
    if not s: return ""
    return re.sub(r"\s+"," ",s.strip().lower())

def load_json(p:Path)->dict: return json.loads(p.read_text(encoding="utf-8"))

def add_appearances_helv8(writer: PdfWriter, reader: PdfReader):
    root = as_obj(reader.trailer.get("/Root"))
    if not isinstance(root, dict): return
    acro = as_obj(root.get("/AcroForm"))
    if not isinstance(acro, dict): return
    writer._root_object.update({NameObject("/AcroForm"): acro})
    acroform = writer._root_object["/AcroForm"]
    acroform.update({NameObject("/NeedAppearances"): BooleanObject(True)})
    # KEY: font size 8 instead of 0
    acroform.update({NameObject("/DA"): TextStringObject("/Helv 8 Tf 0 g")})
    # ensure /Helv present
    font_dict = DictionaryObject({NameObject("/Type"):NameObject("/Font"),
                                  NameObject("/Subtype"):NameObject("/Type1"),
                                  NameObject("/BaseFont"):NameObject("/Helvetica")})
    font_ref = writer._add_object(font_dict)
    dr = acroform.get("/DR") or DictionaryObject()
    fonts = dr.get("/Font") or DictionaryObject()
    fonts.update({NameObject("/Helv"): font_ref})
    dr.update({NameObject("/Font"): fonts})
    acroform.update({NameObject("/DR"): dr})

def page_text_widgets(reader: PdfReader, page_no:int)->List[dict]:
    page = as_obj(reader.pages[page_no-1])
    ann = page.get("/Annots"); 
    if not ann: return []
    items = as_obj(ann) if isinstance(ann, IndirectObject) else ann
    items = items if isinstance(items, ArrayObject) else [items]
    out=[]
    for a in items:
        an = as_obj(a)
        if not isinstance(an, dict): continue
        if an.get("/Subtype") != NameObject("/Widget"): continue
        parent = as_obj(an.get("/Parent")) if an.get("/Parent") else None
        # parent /Tx
        if isinstance(parent, dict) and parent.get("/FT") == NameObject("/Tx"):
            nm = parent.get("/T"); ff = int(parent.get("/Ff") or 0); rect = an.get("/Rect") or parent.get("/Rect")
        # direct /Tx on widget
        elif an.get("/FT") == NameObject("/Tx"):
            nm = an.get("/T"); ff = int(an.get("/Ff") or 0); rect = an.get("/Rect")
        else:
            continue
        try:
            x0,y0,x1,y1 = [float(v) for v in rect]; area = abs((x1-x0)*(y1-y0)); y=max(y0,y1)
            out.append({"name":nm,"ff":ff,"multiline":bool(ff & MULTILINE_BIT),"area":area,"y":y})
        except: pass
    return out

def build_title_lookup(mapping: dict)->Dict[str, Tuple[str,str]]:
    out={}
    for major, m in mapping.items():
        bucket=MAJOR_TO_BUCKET.get(major); 
        if not bucket: continue
        for src, canonical in m.items():
            out[normalize_title(src)] = (bucket, canonical)
    return out

def resolve_item(title:str, lookup)->Optional[Tuple[str,str]]:
    k=normalize_title(title)
    if k in lookup: return lookup[k]
    for key,val in lookup.items():
        if k in key or key in k: return val
    return None

def group_by_subsection(data, lookup)->Tuple[Dict[str,List[str]], List[str]]:
    grouped={}; unmatched=[]
    for s in data.get("inspection",{}).get("sections",[]):
        for li in (s.get("lineItems",[]) or []):
            title = li.get("title") or li.get("name") or ""
            if not title: continue
            r = resolve_item(title, lookup)
            if not r: 
                unmatched.append(title); continue
            _, canonical = r
            comments=[]
            for c in (li.get("comments",[]) or []):
                v=c.get("value")
                if isinstance(v,str) and v.strip(): comments.append(v.strip())
            body = " ".join(comments).strip()
            bullet = f"• {title}: {body}" if body else f"• {title}"
            grouped.setdefault(canonical, []).append(bullet)
    return grouped, unmatched

def estimate_visible_chars(area_pts: float, font_pt: float=8.0)->int:
    # super rough heuristic: ~0.45 chars per square point at 8pt
    return int(area_pts * 0.45 / max(font_pt/8.0, 0.5))

def fill_page1(writer: PdfWriter, data: dict):
    insp=data.get("inspection",{})
    client=insp.get("clientInfo",{}).get("name","")
    inspector=insp.get("inspector",{}).get("name","")
    address=insp.get("address",{})
    full_addr=address.get("fullAddress") or " ".join(filter(None,[address.get("street"),address.get("city"),address.get("state"),address.get("zipcode")]))
    date_str=ms_to_date(insp.get("schedule",{}).get("date"))
    mapping={"Name of Client":client,"Date of Inspection":date_str,"Address of Inspected Property":full_addr,
             "Name of Inspector":inspector,"TREC License":"","Name of Sponsor if applicable":"","TREC License_2":""}
    for page in writer.pages:
        try: writer.update_page_form_field_values(page, mapping)
        except: pass

def build_narrative_pdf(grouped: Dict[str,List[str]], out_path: Path):
    styles = getSampleStyleSheet()
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=13)
    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)
    flow=[]
    flow.append(Paragraph("ADDITIONAL NARRATIVE (Continued Sections)", h2))
    flow.append(Spacer(1,8))
    for subsection in sorted(grouped.keys()):
        items = grouped[subsection]
        if not items: continue
        flow.append(Paragraph(f"<b>{subsection}</b>", body))
        for line in items:
            flow.append(Paragraph(line, body))
        flow.append(Spacer(1,8))
    doc.build(flow)

def main():
    if not TEMPLATE.exists(): raise FileNotFoundError("Missing TREC_Template_Blank.pdf")
    if not JSON_FILE.exists(): raise FileNotFoundError("Missing inspection.json")
    if not MAP_FILE.exists(): raise FileNotFoundError("Missing mappings.json")

    data = load_json(JSON_FILE)
    mapping = load_json(MAP_FILE)
    lookup  = build_title_lookup(mapping)
    grouped, unmatched = group_by_subsection(data, lookup)
    UNMAPPED.write_text("\n".join(sorted(set(unmatched))), encoding="utf-8")

    reader = PdfReader(str(TEMPLATE))
    writer = PdfWriter()
    for p in reader.pages: writer.add_page(p)
    add_appearances_helv8(writer, reader)  # 8pt font so more fits
    fill_page1(writer, data)

    payload_log=[]
    narrative_overflow: Dict[str,List[str]] = {}

    for page_no, subsections in PAGE_SUBSECTIONS.items():
        widgets = page_text_widgets(reader, page_no)
        # comment boxes are multiline; order top→bottom
        widgets = [w for w in widgets if w["multiline"]]
        widgets.sort(key=lambda w: (-w["y"], -w["area"]))
        n = min(len(subsections), len(widgets))
        for i in range(n):
            subsection = subsections[i]
            items = grouped.get(subsection, [])
            if not items: 
                payload_log.append(f"Page {page_no}: '{subsection}' -> (no mapped items)")
                continue
            text = subsection + ":\n" + "\n".join(items)
            cap = estimate_visible_chars(widgets[i]["area"], 8.0)
            # leave some safety margin
            cap = int(cap * 0.7)
            to_field = text if len(text) <= cap else (text[:cap] + " … (continued in Narrative)")
            try:
                writer.update_page_form_field_values(writer.pages[page_no-1], {widgets[i]["name"]: to_field})
                payload_log.append(f"Page {page_no}: '{subsection}' -> {widgets[i]['name']} "
                                   f"({len(text)} chars, visible≈{cap}, wrote={len(to_field)} chars)")
            except Exception as e:
                payload_log.append(f"Page {page_no}: FAILED '{subsection}' -> {widgets[i]['name']}: {e}")
            # collect overflow
            if len(text) > cap:
                narrative_overflow.setdefault(subsection, []).extend(items)

    # Append narrative for overflow (if any)
    if narrative_overflow:
        build_narrative_pdf(narrative_overflow, NARRATIVE)
        narr = PdfReader(str(NARRATIVE))
        for p in narr.pages:
            writer.add_page(p)

    with open(OUTPUT, "wb") as f:
        writer.write(f)

    PAYLOADS.write_text("\n".join(payload_log), encoding="utf-8")
    print(f"Wrote: {OUTPUT}")
    print("Also wrote:", UNMAPPED, PAYLOADS)

if __name__ == "__main__":
    main()

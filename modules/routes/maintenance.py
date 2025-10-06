# modules/routes/maintenance.py
from typing import Optional, List, Dict
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from xml.etree import ElementTree as ET
import os
import re

# ------------------------------
# Routers
# ------------------------------
router = APIRouter(prefix="/interventions", tags=["interventions-maintenance"])
public_router = APIRouter(tags=["maintenance-public"])

# ------------------------------
# Jinja (templates)
# ------------------------------
try:
    from ..core.ui import templates  # moteur central
except Exception:
    templates = Jinja2Templates(directory=os.getenv("TEMPLATES_DIR", "templates"))

# ------------------------------
# Word 2003 XML (WordML) helpers
# ------------------------------
W_NS = {"w": "http://schemas.microsoft.com/office/word/2003/wordml"}

def _text_in(elem) -> str:
    return "".join(elem.itertext()).strip()

def _grid_span(tc) -> int:
    g = tc.find(".//w:tcPr/w:gridSpan", W_NS)
    if g is not None:
        val = g.attrib.get(f"{{{W_NS['w']}}}val")
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
    return 1

def _has_title_style(tc) -> bool:
    for p in tc.findall(".//w:p", W_NS):
        st = p.find(".//w:pPr/w:pStyle", W_NS)
        if st is not None:
            val = st.attrib.get(f"{{{W_NS['w']}}}val", "").lower()
            if "titre" in val or "heading" in val:
                return True
    return False

def parse_word2003xml_full(xml_bytes: bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    body = root.find(".//w:body", W_NS)
    if body is None:
        return []

    items = []
    current_section = None
    num_re  = re.compile(r"^\d+\s*$")
    time_re = re.compile(r"\d+[,.]\d+\s*$")

    tbls = body.findall(".//w:tbl", W_NS)
    for tbl in tbls:
        rows = tbl.findall("./w:tr", W_NS)
        if not rows:
            continue

        # -- éventuel titre sur 1re ligne
        tcs0   = rows[0].findall("./w:tc", W_NS)
        cells0 = [_text_in(tc) for tc in tcs0]
        spans0 = [_grid_span(tc) for tc in tcs0]

        is_title_tbl = False
        title_txt = ""

        if len(tcs0) == 1 and spans0[0] >= 2 and cells0[0].strip():
            is_title_tbl = True
            title_txt = cells0[0].strip().strip(":")
        elif len(tcs0) == 2 and not (cells0[0] or "").strip() and (cells0[1] or "").strip():
            is_title_tbl = True
            title_txt = cells0[1].strip().strip(":")
        elif tcs0 and _has_title_style(tcs0[0]) and cells0 and cells0[0].strip():
            is_title_tbl = True
            title_txt = cells0[0].strip().strip(":")

        if is_title_tbl and title_txt and not num_re.fullmatch(title_txt):
            current_section = title_txt
            items.append({"kind": "title", "title": current_section})
            if len(rows) == 1:
                continue

        # -- lignes de tâches
        for tr in rows:
            tcs   = tr.findall("./w:tc", W_NS)
            cells = [_text_in(tc) for tc in tcs]
            if not cells:
                continue

            head = [c.strip().lower() for c in cells[:3]]
            if len(head) >= 3 and head[0] in ("n°", "n°.", "no", "nº") and head[1] == "sujet" and "action" in head[2]:
                continue

            if len(cells) >= 3 and num_re.fullmatch(cells[0] or ""):
                number  = (cells[0] or "").strip()
                subject = (cells[1] or "").strip()
                action  = (cells[2] or "").strip()

                pr          = (cells[3] or "").strip() if len(cells) > 3 else ""
                designation = (cells[4] or "").strip() if len(cells) > 4 else ""
                quantity    = (cells[5] or "").strip() if len(cells) > 5 else ""
                unit        = (cells[6] or "").strip() if len(cells) > 6 else ""

                time = ""
                for c in reversed(cells[3:]):
                    if time_re.search(c or ""):
                        time = c.strip()
                        break

                items.append({
                    "kind": "task",
                    "section": current_section,
                    "number": number,
                    "subject": subject,
                    "action": action,
                    "pr": pr,
                    "designation": designation,
                    "quantity": quantity,
                    "unit": unit,
                    "time": time
                })

    return items

def parse_rtf_best_effort(_: bytes):
    return []

# ------------------------------
# Endpoints JSON (parse)
# ------------------------------
@router.post("/{intervention_id}/maintenance/parse")
async def upload_maintenance_intervention(intervention_id: int, file: UploadFile = File(...)):
    data = await file.read()
    try:
        if data.lstrip().startswith(b"<?xml") and b"<w:wordDocument" in data:
            items = parse_word2003xml_full(data)
        elif data.lstrip().startswith(b"{\\rtf"):
            items = parse_rtf_best_effort(data)
        else:
            try:
                items = parse_word2003xml_full(data)
            except Exception:
                items = parse_rtf_best_effort(data)

        if not items:
            raise ValueError("Aucun titre/tâche détecté dans le fichier.")
        return JSONResponse({"ok": True, "items": items, "count": len(items)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Impossible de lire le fichier: {e}")

@public_router.post("/maintenance/parse")
async def maintenance_parse_no_id(file: UploadFile = File(...)):
    data = await file.read()
    try:
        if data.lstrip().startswith(b"<?xml") and b"<w:wordDocument" in data:
            items = parse_word2003xml_full(data)
        elif data.lstrip().startswith(b"{\\rtf"):
            items = parse_rtf_best_effort(data)
        else:
            try:
                items = parse_word2003xml_full(data)
            except Exception:
                items = parse_rtf_best_effort(data)

        if not items:
            raise ValueError("Aucun titre/tâche détecté dans le fichier.")
        return JSONResponse({"ok": True, "items": items, "count": len(items)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Impossible de lire le fichier: {e}")

# ------------------------------
# Pages (nouvel onglet)
# ------------------------------
@public_router.get("/maintenance", name="maintenance_view")
def maintenance_view(
    request: Request,
    dg: Optional[str] = Query(None, description="N° DG (pré-remplissage)"),
    or_: Optional[str] = Query(None, alias="or", description="N° OR (pré-remplissage)"),
):
    """GET /maintenance : affiche la page d’upload avec dg/or si fournis en query."""
    return templates.TemplateResponse("maintenance_view.html", {
        "request": request,
        "items": None,
        "filename": None,
        "count": 0,
        "dg": (dg or "").strip(),
        "num_or": (or_ or "").strip(),
    })

@public_router.post("/maintenance", name="maintenance_upload")
async def maintenance_upload(
    request: Request,
    file: UploadFile = File(...),
    dg: Optional[str] = Form(None),
    or_: Optional[str] = Form(None, alias="or"),
):
    """POST /maintenance : parse le fichier et réaffiche en conservant dg/or."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Fichier vide.")

    try:
        if data.lstrip().startswith(b"<?xml") and b"<w:wordDocument" in data:
            items = parse_word2003xml_full(data)
        elif data.lstrip().startswith(b"{\\rtf"):
            items = parse_rtf_best_effort(data)
        else:
            try:
                items = parse_word2003xml_full(data)
            except Exception:
                items = parse_rtf_best_effort(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Lecture impossible: {e}")

    return templates.TemplateResponse("maintenance_view.html", {
        "request": request,
        "items": items,
        "filename": file.filename,
        "count": len(items or []),
        "dg": (dg or "").strip(),
        "num_or": (or_ or "").strip(),
    })

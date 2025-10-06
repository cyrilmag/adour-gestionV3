
import re, html
from datetime import datetime, date
from typing import Optional, List, Dict

def digits_only(s) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())

def is_numero_short_header(name: str) -> bool:
    n = name.strip().lower()
    exacts = {"numéro short", "numero short", "numero_short", "numéro court", "numero court", "n° short", "n° or", "n_or", "n°or"}
    if n in exacts: return True
    return (("short" in n or "court" in n or "n°" in n or "nº" in n) and ("num" in n or "or" in n))

def fmt_last4_no_dot(val):
    if val is None: return None
    s = str(val).strip()
    if s.endswith(".0"): s = s[:-2]
    d = digits_only(s)
    return d[-4:] if d else None

def short_len_for_site(site: Optional[str]) -> int:
    s = (site or "").lower()
    if "narrosse" in s or "narros" in s: return 5
    if "peyrehorade" in s or "peyr" in s: return 4
    return 0

def parse_date_fr(s: str):
    if not s: return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d","%Y-%m-%d %H:%M:%S","%d/%m/%Y","%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

def row_age_days(row: dict) -> Optional[int]:
    for k in ("Date_Deb_Trav","Date","Date_creation","Date_Creation","Date_Ouverture"):
        if k in row and row[k]:
            dt = parse_date_fr(row[k])
            if dt:
                return (datetime.now() - dt).days
    return None

def is_pending_row(row: dict) -> bool:
    etat = str(row.get("Etat","") or "").strip().lower()
    av = str(row.get("Avancement","") or "").strip().lower()
    if any(x in av for x in ["atelier", "attente"]): 
        return True
    if etat in {"en cours","ouvert","ouverte","a traiter","à traiter"}:
        return True
    if etat in {"clos","cloture","terminé","terminée","fermé","fermée"}:
        return False
    return True

def to_ddmmyyyy(s: str) -> str:
    from datetime import datetime as _dt
    if s:
        s = str(s).strip()
        for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                d = _dt.strptime(s, fmt)
                return d.strftime("%d/%m/%Y")
            except Exception:
                pass
    d = _dt.now()
    return d.strftime("%d/%m/%Y")

def parse_column_filters(request) -> Dict[str, str]:
    import urllib.parse
    out: Dict[str, str] = {}
    for k, v in request.query_params.multi_items():
        if not k.startswith("c_"): 
            continue
        col_enc = k[2:]
        if not v:
            continue
        col = urllib.parse.unquote(col_enc)
        out[col] = v
    return out

def extract_num_base_and_short(row: dict, col_site: Optional[str], col_numero: Optional[str]) -> tuple[str, str]:
    preferred_num_cols = ["Numéro","Numero","N° OR","N_OR","N°OR","@Numero","@Num","@N_OR","@N°OR","@OR"]

    num_base = ""
    if col_numero and row.get(col_numero):
        num_base = str(row.get(col_numero))
    else:
        for k in preferred_num_cols:
            if row.get(k):
                num_base = str(row.get(k))
                break
        if not num_base:
            for v in row.values():
                if v and re.search(r"\d{4,}", str(v)):
                    num_base = str(v)
                    break

    digits = "".join(ch for ch in str(num_base) if ch.isdigit())
    if len(digits) < 4:
        best = ""
        for k in preferred_num_cols:
            v = row.get(k)
            if v:
                d = "".join(ch for ch in str(v) if ch.isdigit())
                if len(d) >= 4 and len(d) > len(best):
                    best = d
        if not best:
            for v in row.values():
                if v:
                    d = "".join(ch for ch in str(v) if ch.isdigit())
                    if len(d) >= 4 and len(d) > len(best):
                        best = d
        digits = best

    if not digits:
        return (num_base or "", "")

    site_val = row.get(col_site) if col_site else ""
    n_site = short_len_for_site(site_val)
    if n_site:
        n_short = n_site
    else:
        n_short = 5 if len(digits) >= 5 else (4 if len(digits) >= 4 else 0)

    num_short = digits[-n_short:] if n_short else ""
    return (num_base or "", num_short)

def detect_columns_for_detail(cols):
    def _find_col(cols, names: List[str]):
        low = {c.lower(): c for c in cols}
        for n in names:
            if n.lower() in low:
                return low[n.lower()]
        return None
    date_col     = _find_col(cols, ["Date","Date_Deb_Trav","Date_Creation","Date_Ouverture"])
    numero_col   = _find_col(cols, ["Numéro","Numero","N° OR","N_OR","N°OR","Numero short","Numéro short","@Numero","@Num","@N_OR","@N°OR","@OR"])
    nom_col      = _find_col(cols, ["Nom","Client","Raison_Sociale"])
    libcourt_col = _find_col(cols, ["Libellé_Court","Libelle_Court","Libellé court","Libelle court"])
    notes_col    = _find_col(cols, ["Notes","Note","Commentaire","Commentaires"])
    cession_col  = _find_col(cols, ["Cession"])
    site_col     = _find_col(cols, ["Atelier","Site","Agence","Chantier"])
    num_short_col = _find_col(cols, ["num_short", "Numero short", "Numéro short", "numero_short"])
    return {"date": date_col, "numero": numero_col, "num_short": num_short_col, "nom": nom_col,
            "libcourt": libcourt_col, "notes": notes_col, "cession": cession_col, "site": site_col}

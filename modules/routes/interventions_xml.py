from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER
from typing import Optional, List
import re, html, sqlite3, unicodedata

from ..core.ui import templates
from ..core.db import (
    get_conn,
    table_exists,
    pragma_columns,
    ensure_flag_column,
    is_enregistre_flag,
    ensure_schema,
)
from ..core.utils import (
    parse_column_filters,
    detect_columns_for_detail,
    parse_date_fr,
    to_ddmmyyyy,
)

router = APIRouter()

# ---------- Helpers génériques ----------

def _norm(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower().strip()

def _pick_col(tcols, candidates):
    norm_map = {_norm(c): c for c in tcols}
    for cand in candidates:
        nc = _norm(cand)
        if nc in norm_map:
            return norm_map[nc]
    return None

def _to_flag_zero_or_one(v) -> int:
    s = _norm(v)
    if s in ("", "0", "non", "no", "false", "f", "n"):
        return 0
    if s in ("1", "oui", "yes", "true", "t", "y", "x"):
        return 1
    try:
        return 1 if int(str(v)) != 0 else 0
    except Exception:
        return 0  # par défaut, on considère non traité



def _safe_str(value) -> str:
    return "" if value is None else str(value).strip()


def _digits(value) -> str:
    return re.sub(r"\\D+", "", str(value or ""))

# ---------- Helpers atelier/numéro dérivés du numéro complet ----------

def _derive_site_from_num(numero: str) -> str:
    """Détermine l'atelier à partir des 3 premiers chiffres du numéro."""
    digits = re.sub(r"\D+", "", str(numero or ""))
    if len(digits) >= 3:
        pref = digits[:3]
        if pref == "301":
            return "narrosse"
        if pref == "302":
            return "peyrehorade"
    return ""

def _short_len_for_site(site: str) -> int:
    """Longueur du numéro court selon l'atelier dérivé."""
    s = (site or "").lower()
    if "narrosse" in s:
        return 5
    if "peyrehorade" in s:
        return 4
    # défaut conservateur
    return 5

def _extract_num_base_and_short_from_num(numero: str):
    """
    Renvoie (num_base, num_short, site) UNIQUEMENT depuis le numéro, sans utiliser la colonne site.
    """
    digits = re.sub(r"\D+", "", str(numero or ""))
    site = _derive_site_from_num(digits)
    n = _short_len_for_site(site)
    num_short = digits[-n:] if digits else ""
    num_base = digits
    return num_base, num_short, site

# ---------- Liste ----------

@router.get("/interventions-xml", include_in_schema=False)
def page_interventions_xml(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=10, le=5000),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc"),
    atelier: Optional[List[str]] = Query(None),
):
    with get_conn() as conn:
        if not table_exists(conn, "interventions"):
            # Early return : fournir aussi les compteurs pour éviter le "null"
            return templates.TemplateResponse(
                "interventions_xml.html",
                {
                    "request": request,
                    "rows": [],
                    "total": 0,
                    "page": int(page),
                    "page_size": int(page_size),
                    "atelier_values": [],
                    "atelier_selected": [],
                    "filters": {},
                    "sort_col": sort_col or "",
                    "sort_dir": sort_dir or "asc",
                    "cols_real": {
                        "date": None,
                        "numero": None,
                        "num_base": None,
                        "nom": None,
                        "libcourt": None,
                        "notes": None,
                        "cession": None,
                    },
                    "labels": {
                        "date": "Date",
                        "num_short": "Numéro (short)",
                        "num_base": "Numéro (base)",
                        "nom": "Nom",
                        "libcourt": "Libellé_Court",
                        "notes": "Notes",
                        "cession": "Cession",
                    },
                    "count_start": 0,
                    "count_end": 0,
                    "page_count": 0,
                },
            )

        cols = pragma_columns(conn, "interventions")
        cur = conn.execute('SELECT ROWID AS _rowid_, * FROM "interventions"')
        # construit un dict par ligne, avec _rowid_ + les colonnes dans l'ordre de PRAGMA
        raw = [dict(zip(["_rowid_"] + cols, r)) for r in cur.fetchall()]

    # Masquer les lignes déjà "Enregistré"
    raw = [r for r in raw if not is_enregistre_flag(r.get("Enregistré"))]

    # Ne garder que Traiter = 0 (si la colonne existe)
    traiter_col = _pick_col(cols, ["Traiter", "traiter", "Traité", "Traite", "Traitee", "Traitée"])
    if traiter_col:
        raw = [r for r in raw if _to_flag_zero_or_one(r.get(traiter_col)) == 0]

    # Détection des colonnes utiles
    colmap = detect_columns_for_detail(cols)
    col_date = colmap["date"]
    col_numero = colmap["numero"]
    col_num_short = colmap.get("num_short")
    col_nom = colmap["nom"]
    col_lib = colmap["libcourt"]
    col_notes = colmap["notes"]
    col_cession = colmap["cession"]

    # Filtre atelier basé sur le numéro (préfixe 301/302), pas sur une colonne
    atelier_selected = [a.strip().lower() for a in (request.query_params.getlist("atelier") or []) if a and a.strip()]
    if atelier_selected:
        sel = set(atelier_selected)
        def _site_of_row(r):
            numero_val = r.get(colmap["numero"]) if colmap["numero"] else ""
            _, _, site = _extract_num_base_and_short_from_num(numero_val)
            return site
        raw = [r for r in raw if _site_of_row(r) in sel]

    # Valeurs disponibles pour la listbox atelier (dérivées des numéros présents)
    def _site_of_row(r):
        numero_val = r.get(colmap["numero"]) if colmap["numero"] else ""
        _, _, site = _extract_num_base_and_short_from_num(numero_val)
        return site
    atelier_values = sorted({_site_of_row(r) for r in raw if _site_of_row(r)}, key=lambda s: s.lower())

    # Filtres colonnes libres
    filters = parse_column_filters(request)
    if filters:
        def _match(val, needle):
            return str(val or "").lower().find(needle.lower()) >= 0
        new_raw = []
        for r in raw:
            ok = True
            for k, v in filters.items():
                if k in r and not _match(r.get(k), v):
                    ok = False
                    break
            if ok:
                new_raw.append(r)
        raw = new_raw

    # Calculs numéro court/base
    def num_short_for(r: dict) -> str:
        if col_num_short:
            val = r.get(col_num_short)
            if val is not None:
                s = str(val).strip()
                if s:
                    return s
        numero_val = r.get(colmap["numero"]) if colmap["numero"] else ""
        _, num_short, _ = _extract_num_base_and_short_from_num(numero_val)
        return num_short or ""

    def num_base_for(r: dict) -> str:
        numero_val = r.get(colmap["numero"]) if colmap["numero"] else ""
        num_base, _, _ = _extract_num_base_and_short_from_num(numero_val)
        return num_base or ""

    # Tri
    sc = sort_col or ""
    sd = (sort_dir or "asc").lower()
    reverse = sd == "desc"

    if sc and sc in (["_rowid_"] + cols):
        if sc == col_date:
            raw.sort(key=lambda r: parse_date_fr(r.get(col_date)) or __import__("datetime").datetime.min, reverse=reverse)
        elif col_num_short and sc == col_num_short:
            def key_numshort(r):
                s = num_short_for(r)
                try:
                    return int(s) if s else -1
                except Exception:
                    return -1
            raw.sort(key=key_numshort, reverse=reverse)
        elif sc == col_numero:
            raw.sort(key=lambda r: str(r.get(col_numero) or "").lower(), reverse=reverse)
        else:
            raw.sort(key=lambda r: str(r.get(sc) or "").lower(), reverse=reverse)

    # Pagination + compteurs
    total = len(raw)
    max_page = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), max_page)

    start = (page - 1) * page_size
    end = min(start + page_size, total)
    page_raw = raw[start:end]

    count_start = int((start + 1) if total > 0 else 0)
    count_end = int(end)
    page_count = int(len(page_raw))

    # Projection pour le template
    rows = []
    for r in page_raw:
        rows.append(
            {
                "_rowid": r.get("_rowid_"),
                "date": r.get(col_date) if col_date else "",
                "num_short": num_short_for(r),
                "num_base": num_base_for(r),
                "nom": r.get(col_nom) if col_nom else "",
                "libcourt": r.get(col_lib) if col_lib else "",
                "notes": r.get(col_notes) if col_notes else "",
                "cession": r.get(col_cession) if col_cession else "",
            }
        )

    cols_real = {
        "date": col_date,
        "numero": col_num_short or col_numero,
        "num_base": col_numero,   # le "base" vient du numéro d'origine
        "nom": col_nom,
        "libcourt": col_lib,
        "notes": col_notes,
        "cession": col_cession,
    }
    labels = {
        "date": "Date",
        "num_short": "Numéro (short)",
        "num_base": "Numéro (base)",
        "nom": "Nom",
        "libcourt": "Libellé_Court",
        "notes": "Notes",
        "cession": "Cession",
    }

    return templates.TemplateResponse(
        "interventions_xml.html",
        {
            "request": request,
            "rows": rows,
            "total": int(total),
            "page": int(page),
            "page_size": int(page_size),
            "atelier_values": list(atelier_values or []),
            "atelier_selected": list(atelier_selected or []),
            "filters": dict(filters or {}),
            "sort_col": sort_col or "",
            "sort_dir": sort_dir or "asc",
            "cols_real": cols_real,
            "labels": labels,
            "count_start": count_start,
            "count_end": count_end,
            "page_count": page_count,
        },
    )

# ---------- Détail ----------

@router.get("/interventions-xml/item/{rowid}")
def intervention_xml_detail(request: Request, rowid: int):
    with get_conn() as conn:
        if not table_exists(conn, "interventions"):
            return PlainTextResponse("Table interventions inexistante", status_code=404)
        cur = conn.execute('SELECT ROWID AS _rowid_, * FROM "interventions" WHERE ROWID = ?', (rowid,))
        row = cur.fetchone()
        if not row:
            return PlainTextResponse("Intervention introuvable", status_code=404)
        cols = [d[0] for d in cur.description] if cur.description else []
        d = dict(zip(cols, row))

        # Détection colonnes
        colmap = detect_columns_for_detail(cols)
        numero_col = colmap["numero"]
        num_short_col = colmap.get("num_short")
        cession_col = colmap["cession"]

        # Numéro & site dérivés depuis le numéro
        numero_val = d.get(numero_col) if numero_col else ""
        num_base, derived_short, site_val = _extract_num_base_and_short_from_num(numero_val)
        num_short = str(d.get(num_short_col) or "").strip() if num_short_col else ""
        if not num_short:
            num_short = derived_short

        maintenance_defaults = {
            'checked': False,
            'rapport_adour': '',
            'rapport_mol': '',
            'rapport_sol': '',
            'revision_objet': '',
            'revision_desc': '',
        }
        constructeur_raw = (d.get("Constructeur") or "").strip()
        marque_label = constructeur_raw[3:].strip() if len(constructeur_raw) > 3 else constructeur_raw
        if not marque_label:
            marque_label = "Marque"
        garantie_defaults = {
            'checked': False,
            'rapport_adour': '',
            'rapport_marque': '',
            'marque_label': marque_label
        }


        target_short_digits = _digits(num_short)
        target_full_digits = _digits(num_base)
        rows_g: list[tuple[str, str, str, str]] = []

        if num_short or num_base:
            candidates: list[str] = []
            raw_num_short = _safe_str(num_short)
            if raw_num_short:
                candidates.append(raw_num_short)
            if target_short_digits:
                candidates.append(target_short_digits)
                stripped_short = target_short_digits.lstrip("0")
                if stripped_short and stripped_short != target_short_digits:
                    candidates.append(stripped_short)
            if target_full_digits:
                candidates.append(target_full_digits)
                stripped_full = target_full_digits.lstrip("0")
                if stripped_full and stripped_full != target_full_digits:
                    candidates.append(stripped_full)

            seen_tokens: set[str] = set()
            search_tokens: list[str] = []
            for cand in candidates:
                if not cand:
                    continue
                if cand in seen_tokens:
                    continue
                seen_tokens.add(cand)
                search_tokens.append(cand)

            if search_tokens:
                clauses = " OR ".join(["CAST(NUM_OR AS TEXT) LIKE ?"] * len(search_tokens))
                sql = (
                    "SELECT ROWID, NUM_DG, NUM_OR, Type, NUM_Recepice "
                    "FROM garanties "
                    f"WHERE {clauses} "
                    "ORDER BY ROWID DESC"
                )
                like_params = [f"%{token}%" for token in search_tokens]
                try:
                    fetched = conn.execute(sql, like_params).fetchall()
                except Exception:
                    fetched = []
                seen_rowids: set[int] = set()
                for rowid_val, num_dg_val, num_or_val, type_val, recep_val in fetched:
                    if rowid_val in seen_rowids:
                        continue
                    seen_rowids.add(rowid_val)
                    digits_val = _digits(num_or_val)
                    match_short = bool(target_short_digits) and digits_val.endswith(target_short_digits)
                    match_full = bool(target_full_digits) and digits_val == target_full_digits
                    match_raw = bool(raw_num_short) and _safe_str(num_or_val) == raw_num_short
                    if match_short or match_full or match_raw:
                        rows_g.append(
                            (
                                _safe_str(num_dg_val),
                                _safe_str(num_or_val),
                                _safe_str(type_val),
                                _safe_str(recep_val),
                            )
                        )
                if not rows_g:
                    for _, num_dg_val, num_or_val, type_val, recep_val in fetched:
                        rows_g.append(
                            (
                                _safe_str(num_dg_val),
                                _safe_str(num_or_val),
                                _safe_str(type_val),
                                _safe_str(recep_val),
                            )
                        )

        if rows_g:
            marque_upper = marque_label.upper()
            first_recep_any = ""
            first_recep_garantie = ""
            for num_dg_val, num_or_val, type_val, recep_val in rows_g:
                or_digits = _digits(num_or_val)
                if target_short_digits and or_digits.endswith(target_short_digits):
                    or_simple = target_short_digits
                else:
                    or_simple = or_digits or _safe_str(num_or_val)
                or_simple = or_simple.lstrip("0") or or_simple
                if num_dg_val and or_simple:
                    combo = f"{num_dg_val}-{or_simple}"
                    if not maintenance_defaults['rapport_adour']:
                        maintenance_defaults['rapport_adour'] = combo
                    if not garantie_defaults['rapport_adour']:
                        garantie_defaults['rapport_adour'] = combo

                type_norm = type_val.upper()
                if recep_val:
                    if not maintenance_defaults['rapport_mol'] and type_norm.startswith("MOL"):
                        maintenance_defaults['rapport_mol'] = recep_val
                    if not maintenance_defaults['rapport_sol'] and "SOL" in type_norm:
                        maintenance_defaults['rapport_sol'] = recep_val
                    if marque_upper and marque_upper in type_norm and not garantie_defaults['rapport_marque']:
                        garantie_defaults['rapport_marque'] = recep_val
                    if not first_recep_any:
                        first_recep_any = recep_val
                    if not first_recep_garantie and "GARANTIE" in type_norm:
                        first_recep_garantie = recep_val

            if not garantie_defaults['rapport_marque']:
                if first_recep_garantie:
                    garantie_defaults['rapport_marque'] = first_recep_garantie
                elif first_recep_any:
                    garantie_defaults['rapport_marque'] = first_recep_any

        maintenance_checked = bool(
            maintenance_defaults['rapport_adour']
            or maintenance_defaults['rapport_mol']
            or maintenance_defaults['rapport_sol']
        )
        maintenance_defaults['checked'] = maintenance_defaults['checked'] or maintenance_checked

        garantie_checked = bool(
            garantie_defaults['rapport_adour']
            or garantie_defaults['rapport_marque']
        )
        if maintenance_defaults['checked']:
            garantie_defaults['checked'] = False
        else:
            garantie_defaults['checked'] = garantie_defaults['checked'] or garantie_checked

        # Texte travaux        # Texte travaux
        travaux_raw = d.get("Travaux") or ""
        travaux_xml = html.unescape(str(travaux_raw)).replace("\r\n", "\n").replace("\r", "\n").strip()

        # Pré-sélection du type de facturation depuis cession_codes
        type_op_selected = ""
        cession_val = (d.get(cession_col) or "").strip() if cession_col else ""
        if cession_val:
            rows = conn.execute(
                """
                SELECT code_prefix, type_facturation
                FROM cession_codes
                WHERE active = 1 AND type_facturation IS NOT NULL AND type_facturation <> ''
                """
            ).fetchall()
            best_len = -1
            best_type = ""
            fallback_len = -1
            fallback_type = ""
            cession_up = cession_val.upper()
            for pref, tf in rows:
                p = (pref or "").upper().strip()
                if not p:
                    continue
                if cession_up.startswith(p) and len(p) > best_len:
                    best_len = len(p)
                    best_type = (tf or "").strip()
                trimmed = p.rstrip(" 0123456789")
                if trimmed and trimmed != p and cession_up.startswith(trimmed) and len(trimmed) > fallback_len:
                    fallback_len = len(trimmed)
                    fallback_type = (tf or "").strip()
            type_op_selected = best_type or fallback_type

    return templates.TemplateResponse(
        "intervention_xml_detail.html",
        {
            "request": request,
            "row": d,
            "rowid": rowid,
            "num_short": num_short,
            "num_base": num_base,
            "site_val": site_val,
            "travaux_xml": travaux_xml,
            "type_op_selected": type_op_selected,
            "maintenance_defaults": maintenance_defaults,
            "garantie_defaults": garantie_defaults,
            "garantie_rows": rows_g,
        },
    )

# ---------- Save ----------

@router.post("/interventions-xml/item/{rowid}/save")
async def intervention_xml_save(rowid: int, request: Request):
    form = await request.form()
    # Saisie utilisateur
    nom_in = (form.get("nom") or "").strip()  # -> "Client"
    libelle_court_in = (form.get("libelle_court") or "").strip()  # -> "Machine"
    notes_in = (form.get("notes") or "").strip()  # -> "panne"
    date_jour_in = (form.get("date_jour") or "").strip()  # -> "Date Cloture"
    type_in = (form.get("type_op") or "").strip()  # -> "Type"

    date_cloture = to_ddmmyyyy(date_jour_in)

    with get_conn() as conn:
        if not table_exists(conn, "interventions"):
            return PlainTextResponse("Table interventions inexistante", status_code=404)

        # Récup ligne XML
        cur = conn.execute('SELECT ROWID AS _rowid_, * FROM "interventions" WHERE ROWID = ?', (rowid,))
        row = cur.fetchone()
        if not row:
            return PlainTextResponse("Intervention introuvable", status_code=404)
        cols = [d[0] for d in cur.description] if cur.description else []
        drow = dict(zip(cols, row))

        # Résolution insensible aux accents/majuscules
        def _pick_col_local(tcols, candidates):
            return _pick_col(tcols, candidates)

        # Colonne numéro uniquement (on ne se base plus sur la colonne site)
        colmap = detect_columns_for_detail([c for c in drow.keys() if c != "_rowid_"])
        numero_col = colmap["numero"]
        num_short_col = colmap.get("num_short")

        # Numéro court + site dérivés depuis le numéro
        numero_val = drow.get(numero_col) if numero_col else ""
        num_base, derived_short, site_val = _extract_num_base_and_short_from_num(numero_val)
        num_short = str(drow.get(num_short_col) or "").strip() if num_short_col else ""
        if not num_short:
            num_short = derived_short
        if not num_short:
            return RedirectResponse(url=f"/interventions-xml/item/{rowid}?saved=0&msg=num-short-introuvable", status_code=303)

        # Table cible déterminée par le site dérivé
        def _target_from(site: str):
            s = (site or "").lower()
            if "narrosse" in s:
                return "interventions_narrosse"
            if "peyrehorade" in s:
                return "interventions_peyrehorade"
            return None

        target = _target_from(site_val)
        if not target:
            return RedirectResponse(url=f"/interventions-xml/item/{rowid}?saved=0&msg=site-inconnu", status_code=303)

        # Colonnes de la table cible
        tcols = [r[1] for r in conn.execute(f'PRAGMA table_info("{target}")')]

        # Résolution insensible aux accents/majuscules
        num_col = _pick_col_local(tcols, ["Numéro", "Numero", "N° OR", "N_OR", "N°OR", "N OR", "@Numero", "@N_OR"])
        client_col = _pick_col_local(tcols, ["Client", "Nom"])
        machine_col = _pick_col_local(tcols, ["Machine", "Libellé_Court", "Libelle_Court"])
        panne_col = _pick_col_local(tcols, ["panne", "Panne"])
        date_col = _pick_col_local(tcols, ["Date Cloture", "Date_Cloture", "DateCloture"])
        type_col = _pick_col_local(tcols, ["Type", "TYPE"])
        flag_col = _pick_col_local(tcols, ["enregistré", "Enregistré"]) or "enregistré"

        ensure_flag_column(conn, target, flag_col)

        # Valeurs défaut depuis XML si pas saisies
        def _first(keys):
            for k in keys:
                if k in drow and drow[k]:
                    return str(drow[k])
            return ""

        if not nom_in:
            nom_in = _first(["Nom", "Raison_Sociale"])
        if not libelle_court_in:
            libelle_court_in = _first(["Libellé_Court", "Libelle_Court"])
        if not notes_in:
            notes_in = _first(["Notes", "Note", "Commentaire", "Commentaires"])

        # Vérif colonnes minimales
        missing = []
        for label, col in {
            "Numéro": num_col,
            "Client": client_col,
            "Machine": machine_col,
            "panne": panne_col,
            "Date Cloture": date_col,
            "Type": type_col,
            "flag": flag_col,
        }.items():
            if not col:
                missing.append(label)
        if missing:
            return PlainTextResponse(f'Colonnes manquantes dans "{target}" : ' + ", ".join(missing), status_code=400)

        # WHERE: on matche sur la fin du champ numéro (4 ou 5 chiffres)
        n_short = len(num_short)
        t_num_col = num_col

        # Construction UPDATE
        set_parts = [
            f'"{num_col}" = ?',
            f'"{client_col}" = ?',
            f'"{panne_col}" = ?',
            f'"{date_col}" = ?',
            f'"{machine_col}" = ?',
            f'"{type_col}" = ?',
            f'"{flag_col}" = ?',
        ]
        params = [num_short, nom_in, notes_in, date_cloture, libelle_court_in, type_in, "1"]

        sql = f'''
            UPDATE "{target}"
            SET {", ".join(set_parts)}
            WHERE substr(replace(replace(CAST("{t_num_col}" AS TEXT), '.', ''), ' ', ''), -{n_short}, {n_short}) = ?
        '''
        params.append(num_short)

        cur2 = conn.execute(sql, tuple(params))
        rows_affected = cur2.rowcount
        conn.commit()

        # Marquer la ligne XML comme enregistrée + Traiter=1 si colonne existe
        xml_tcols = [r[1] for r in conn.execute('PRAGMA table_info("interventions")')]
        xml_flag = _pick_col(xml_tcols, ["enregistré", "Enregistré"]) or "Enregistré"
        ensure_flag_column(conn, "interventions", xml_flag)
        if rows_affected and rows_affected > 0:
            conn.execute(f'UPDATE "interventions" SET "{xml_flag}" = ? WHERE ROWID = ?', ("1", rowid))
            xml_traiter = _pick_col(xml_tcols, ["Traiter", "traiter", "Traité", "Traite", "Traitee", "Traitée"])
            if xml_traiter:
                conn.execute(f'UPDATE "interventions" SET "{xml_traiter}" = ? WHERE ROWID = ?', ("1", rowid))
            conn.commit()

        # Diagnostic lisible si 0 ligne mise à jour
        if rows_affected == 0:
            travaux_raw = drow.get("Travaux") or ""
            travaux_xml = html.unescape(str(travaux_raw)).replace("\r\n", "\n").replace("\r", "\n").strip()

            debug_sql = " ".join(sql.split())
            clean_col = f"{t_num_col}"
            debug_where = (
                f'WHERE substr(replace(replace(CAST("{clean_col}" AS TEXT), " ", ""), ".", ""), -{n_short}, {n_short}) = "{num_short}"'
            )
            suggested_where = f'WHERE CAST("{clean_col}" AS TEXT) LIKE "%" || "{num_short}"'

            debug_params = list(params)
            print("[DEBUG UPDATE 0]", target, debug_where, debug_params)

            return templates.TemplateResponse(
                "intervention_xml_detail.html",
                {
                    "request": request,
                    "row": {k: (drow.get(k) if k in drow else "") for k in drow.keys() if k != "_rowid_"},
                    "rowid": rowid,
                    "num_short": num_short,
                    "num_base": num_base,
                    "site_val": site_val,
                    "travaux_xml": travaux_xml,
                    "maintenance_defaults": maintenance_defaults,
                    "garantie_defaults": garantie_defaults,
                    "error": (
                        "⚠️ Aucune ligne mise à jour.\n"
                        f"• Atelier détecté: {site_val} | table cible: {target}\n"
                        f"• WHERE utilisé: {debug_where}\n"
                        f"• SQL complet: {debug_sql}\n"
                        f"• PARAMS: {debug_params}\n"
                        f"• Astuce: essaie ce WHERE plus simple pour diagnostiquer : {suggested_where}"
                    ),
                },
            )

    ok = 1 if rows_affected and rows_affected > 0 else 0

    if ok:
        agenda_title_parts = []
        if type_in:
            agenda_title_parts.append(type_in.strip())
        fallback_num = num_short or (drow.get(numero_col) if numero_col else "") or str(rowid)
        if fallback_num:
            agenda_title_parts.append(str(fallback_num).strip())
        agenda_title = " - ".join(part for part in agenda_title_parts if part) or f"Intervention {rowid}"

        event_date_iso = None
        for candidate in (date_jour_in, date_cloture):
            if not candidate:
                continue
            dt = parse_date_fr(candidate)
            if dt:
                event_date_iso = dt.strftime("%Y-%m-%d")
                break

        with get_conn() as agenda_conn:
            ensure_schema(agenda_conn)
            agenda_conn.row_factory = sqlite3.Row
            existing = agenda_conn.execute(
                'SELECT id, status FROM agenda_items WHERE intervention_rowid = ?',
                (rowid,),
            ).fetchone()
            if existing:
                agenda_conn.execute(
                    """
                    UPDATE agenda_items
                    SET title = ?,
                        kind = 'intervention',
                        event_date = ?,
                        notes = ?,
                        status = 'pending',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        agenda_title,
                        event_date_iso,
                        notes_in or None,
                        existing["id"],
                    ),
                )
            else:
                agenda_conn.execute(
                    """
                    INSERT INTO agenda_items (title, kind, event_date, notes, intervention_rowid, status)
                    VALUES (?, 'intervention', ?, ?, ?, 'pending')
                    """,
                    (
                        agenda_title,
                        event_date_iso,
                        notes_in or None,
                        rowid,
                    ),
                )
            agenda_conn.commit()

    return RedirectResponse(url=f"/interventions-xml/item/{rowid}?saved={ok}", status_code=303)

# --- Intégration "Créer garantie" depuis Intervention XML ---

@router.get("/interventions-xml/item/{rowid}/garantie/new")
def intervention_to_garantie_form(rowid: int):
    """Ouvre le formulaire de création garantie prérempli depuis cette intervention."""
    return RedirectResponse(url=f"/garanties/new?from_intervention={rowid}", status_code=HTTP_303_SEE_OTHER)

@router.post("/interventions-xml/item/{rowid}/garantie/create")
async def intervention_to_garantie_create(rowid: int, request: Request):
    """
    Crée directement une garantie (sans passer par le formulaire) à partir de l'intervention.
    N° = MAX(N°)+1. Redirige sur la fiche détail de la garantie créée.
    """
    # Helpers dates
    from datetime import datetime
    def _is_date_col(name: str) -> bool:
        return "date" in (name or "").lower() or name in {
            "Date", "Date OR", "Date facture", "date paiement", "Date rebus", "Date début", "Date fin"
        }

    def _try_parse_date(value: str):
        if not value:
            return None
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%d.%m.%Y",
            "%Y/%m/%d",
            "%Y.%m.%d",
            "%d/%m/%y",
            "%d-%m-%y",
            "%Y%m%d",
        ):
            try:
                return datetime.strptime(value.strip(), fmt)
            except Exception:
                pass
        return None

    def _to_storage_date(value: str) -> str:
        if not value:
            return ""
        dt = _try_parse_date(value)
        return dt.strftime("%Y-%m-%d") if dt else value

    # Résolution nom de table garantie
    def _resolve_garantie_table(conn) -> str:
        for c in ["garantie", "garanties", "registre_garantie", "GARANTIE", "GARANTIES"]:
            r = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (c,)
            ).fetchone()
            if r:
                return c
        raise HTTPException(500, "Table/ vue de garanties introuvable.")

    # Préremplissage depuis l'intervention
    def _prefill_from_intervention(conn, _rowid: int) -> dict:
        for t in ["interventions"]:
            cur = conn.execute(f'SELECT ROWID AS _rowid_, * FROM "{t}" WHERE ROWID = ?', (_rowid,))
            row = cur.fetchone()
            if not row:
                continue
            cols = [d[0] for d in cur.description] if cur.description else []
            d = dict(zip(cols, row))

            def g(*keys):
                for k in keys:
                    if k in d and d[k]:
                        return str(d[k])
                return ""

            pre = {
                "N°OR": g("Numéro"),
                "Client": g("Nom"),
                "Machine": g("Libellé_Court"),
                "NUMÉRO DE SÉRIE": g("NUMÉRO DE SÉRIE", "N° de série", "Serie", "Serial"),
                "Type": g("Type", "TYPE"),
                "Date OR": g("Date"),
                "Panne": g("Panne", "Notes", "Note", "Commentaire", "Commentaires"),
            }
            # normalise dates pour stockage
            for k in list(pre.keys()):
                if _is_date_col(k):
                    pre[k] = _to_storage_date(pre[k])
            return pre
        return {}

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        table_g = _resolve_garantie_table(conn)

        # Colonnes garanties
        gcols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table_g}")')]
        if not gcols:
            raise HTTPException(500, f"Aucune colonne trouvée pour {table_g}")

        # N° = MAX+1
        next_num = conn.execute(f'SELECT COALESCE(MAX(CAST("N°" AS INTEGER)), 0) + 1 FROM "{table_g}"').fetchone()[0]
        numero = str(next_num)

        # payload vide + prefill
        payload = {c: "" for c in gcols}
        pre = _prefill_from_intervention(conn, rowid)
        payload.update(pre)
        payload["N°"] = numero

        # INSERT
        keys = '","'.join(payload.keys())
        qmarks = ",".join(["?"] * len(payload))
        sql = f'INSERT INTO "{table_g}" ("{keys}") VALUES ({qmarks})'
        conn.execute(sql, list(payload.values()))
        conn.commit()

    return RedirectResponse(url=f"/garanties/item/{numero}?saved=1", status_code=HTTP_303_SEE_OTHER)



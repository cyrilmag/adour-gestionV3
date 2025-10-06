
import sqlite3

from app.settings import get_settings
from app.db import connect as app_connect


def get_db_path() -> str:
    return get_settings().sqlite_path()


def get_conn():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = None
    return conn


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Compatibility helper that delegates to the new app.db module."""
    return app_connect(db_path or get_db_path())

def ensure_table_has_column(conn: sqlite3.Connection, table: str, col: str, coltype: str, default_sql: str | None = None):
    """
    Ajoute la colonne si absente. coltype ex: 'TEXT', 'INTEGER', 'NUMERIC'.
    default_sql: valeur SQL par défaut (ex: "''", '0'). Si fourni, on backfill après ajout.
    """
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    if col not in cols:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {coltype}')
        if default_sql is not None:
            conn.execute(f'UPDATE "{table}" SET "{col}" = {default_sql} WHERE "{col}" IS NULL')

def ensure_cession_codes_schema(conn: sqlite3.Connection):
    """
    Garantit l'existence de la table cession_codes et des colonnes attendues
    par l'UI: code_prefix, label, include_gt20, active, type_garantie, type_facturation.
    """
    # 1) Créer la table si absente (schéma minimal compatible)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS "cession_codes" (
            "code_prefix" TEXT PRIMARY KEY,
            "label" TEXT
        )
    """)
    # 2) Colonnes requises par la page paramètres
    ensure_table_has_column(conn, "cession_codes", "include_gt20",   "INTEGER", "0")
    ensure_table_has_column(conn, "cession_codes", "active",         "INTEGER", "1")
    ensure_table_has_column(conn, "cession_codes", "type_garantie",  "TEXT",    "''")
    ensure_table_has_column(conn, "cession_codes", "type_facturation","TEXT",    "''")

    # 3) Index utile (optionnel) pour les recherches par code_prefix
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS "ux_cession_codes_code_prefix" ON "cession_codes"("code_prefix")')
    conn.commit()

def table_exists(conn, name: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type in ('table','view') AND name = ?", (name,))
    return cur.fetchone() is not None

def pragma_columns(conn, table: str):
    """
    Renvoie la liste des colonnes pour 'table' ou 'vue', de manière robuste.
    Essaie d'abord PRAGMA table_info/xinfo, puis un SELECT 0=1 en dernier recours.
    """
    # 1) PRAGMA table_info
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        cols = [r[1] for r in rows]
        if cols:
            return cols
    except Exception:
        pass

    # 2) PRAGMA table_xinfo (voit aussi les colonnes cachées / vues récentes)
    try:
        rows = conn.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        cols = [r[1] for r in rows if r[1] is not None]
        if cols:
            return cols
    except Exception:
        pass

    # 3) Dernier recours : SELECT * WHERE 0=1 (peut planter sur certaines vues)
    try:
        cur = conn.execute(f'SELECT * FROM "{table}" WHERE 0=1')
        return [d[0] for d in (cur.description or [])]
    except Exception:
        return []

def ensure_schema(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS cession_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_prefix TEXT NOT NULL UNIQUE,
        label TEXT,
        include_gt20 INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS agenda_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'autre',
        event_date TEXT,
        event_time TEXT,
        intervention_rowid INTEGER,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_agenda_status_date
      ON agenda_items(status, event_date, event_time);
    """)
    conn.commit()

def _has_column(conn, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in conn.execute(f'PRAGMA table_info("{table}")'))
    except Exception:
        return False

def ensure_flag_column(conn, table: str, col: str = "Enregistré"):
    try:
        if not _has_column(conn, table, col):
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" TEXT')
            conn.commit()
    except Exception:
        pass

def is_enregistre_flag(val) -> bool:
    s = str(val or "").strip().lower()
    return s in {"1", "oui", "true", "x", "✅", "ok"}
# ---- UPSERT helpers & schema utilities ----

def upsert_row(conn: sqlite3.Connection, table: str, key_col: str, row: dict):
    """
    Generic UPSERT: INSERT ... ON CONFLICT(key_col) DO UPDATE
    - table: target table name
    - key_col: exact key column (e.g. "Numéro" or "num unique")
    - row: dict {col: value}
    """
    if key_col not in row:
        raise ValueError(f"Key '{key_col}' missing for {table}")
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(f'"{c}"' for c in cols)
    update_cols = [c for c in cols if c != key_col]
    set_list = ", ".join(f'"{c}" = excluded."{c}"' for c in update_cols) or f'"{key_col}"="{key_col}"'
    sql = f'''
        INSERT INTO "{table}" ({col_list})
        VALUES ({placeholders})
        ON CONFLICT("{key_col}") DO UPDATE SET
        {set_list}
    '''
    conn.execute(sql, [row[c] for c in cols])

def upsert_many(conn: sqlite3.Connection, table: str, key_col: str, rows: list[dict]):
    with conn:
        for r in rows:
            upsert_row(conn, table, key_col, r)


def ensure_unique_indexes(conn: sqlite3.Connection):
    """
    Crée les index UNIQUE nécessaires si la cible est une TABLE (skip pour les vues).
    - interventions_narrosse("Numéro")
    - interventions_peyrehorade("Numéro")
    - garantie("num unique")
    """
    # interventions_narrosse
    if _obj_type(conn, "interventions_narrosse") == "table":
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_interventions_narrosse_Numero
            ON "interventions_narrosse"("Numéro")
        """)

    # interventions_peyrehorade
    if _obj_type(conn, "interventions_peyrehorade") == "table":
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_interventions_peyrehorade_Numero
            ON "interventions_peyrehorade"("Numéro")
        """)

    # garantie : si c'est une vue, on SKIP (on ne peut pas indexer une vue)
    if _obj_type(conn, "garantie") == "table":
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_garantie_num_unique
            ON "garantie"("num unique")
        """)

    conn.commit()
def ensure_interventions_traiter_schema(conn: sqlite3.Connection):
    """
    Garantit la colonne "traiter" (INTEGER DEFAULT 0) dans la table "interventions".
    Backfill initial: si une colonne Etat/@Etat existe et vaut 'validé/valide' (insensible à la casse),
    on met "traiter"=1 UNIQUEMENT pour les lignes où "traiter" est NULL (nouvelle colonne).
    """
    # Vérifie que "interventions" est une TABLE
    try:
        row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", ("interventions",)).fetchone()
        if not row or row[0] != "table":
            return
    except Exception:
        return

    # Ajoute colonne si absente
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info("interventions")')]
    except Exception:
        cols = []
    if "traiter" not in cols:
        try:
            conn.execute('ALTER TABLE "interventions" ADD COLUMN "traiter" INTEGER')
            conn.execute('UPDATE "interventions" SET "traiter" = 0 WHERE "traiter" IS NULL')
            conn.commit()
        except Exception:
            pass

    # Détecte colonne Etat (Etat ou @Etat, avec ou sans accents/majuscules)
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info("interventions")')]
    except Exception:
        cols = []
    etat_col = None
    for c in cols:
        norm = c.strip().casefold().replace("é","e").replace("è","e").replace("ê","e")
        if norm in {"etat", "@etat"} or "etat" in norm:
            etat_col = c
            break
    if etat_col:
        # Backfill seulement là où traiter est NULL (i.e., juste après ajout) ou vide
        try:
            conn.execute(f"""
                UPDATE "interventions"
                SET "traiter" = 1
                WHERE ( "traiter" IS NULL OR "traiter" = 0 )
                  AND LOWER(REPLACE(REPLACE(REPLACE(COALESCE("{etat_col}",''), 'É','E'),'é','e'),'è','e')) IN ('valide','validé','validee','validée')
            """)
            conn.commit()
        except Exception:
            pass


def ensure_unique_index_for_key(conn: sqlite3.Connection, table: str, key_col: str):
    """
    Garantit un index UNIQUE pour (table, key_col) si l'objet est une table.
    Si c'est une vue → ne fait rien (les vues ne sont pas indexables).
    """
    # type de l'objet
    cur = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,))
    row = cur.fetchone()
    if not row or row[0] != "table":
        return  # vue ou inexistante → skip

    # index déjà présent ?
    idxs = conn.execute(f'PRAGMA index_list("{table}")').fetchall() or []
    key_norm = key_col.strip()
    for idx in idxs:
        # pragma: (seq, name, unique, origin, partial) → on veut unique == 1
        if len(idx) >= 3 and int(idx[2]) == 1:
            idx_name = idx[1]
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{idx_name}")').fetchall()]
            if cols == [key_norm]:  # unique exact sur la seule colonne
                return  # ok

    # sinon : créer un index unique sur cette EXACTE colonne
    safe = "".join(ch if ch.isalnum() or ch in "_$" else "_" for ch in f"ux_{table}_{key_norm}")
    conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{safe}" ON "{table}"("{key_norm}")')
    conn.commit()

def _obj_type(conn, name: str) -> str | None:
    row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row[0] if row else None

def ensure_garantie_num_unique_numeric_unique(conn: sqlite3.Connection):
    """
    Garantit que "garantie"."num unique" existe, est de type NUMERIC/INTEGER,
    et possède un index UNIQUE. Si le type est mauvais, on reconstruit la table.
    (Sécurisé ici car tu as vidé la table.)
    """
    if _obj_type(conn, "garantie") != "table":
        return  # si c'est une vue -> on ne touche pas

    info = conn.execute('PRAGMA table_info("garantie")').fetchall()
    # info: cid, name, type, notnull, dflt_value, pk
    names = [r[1] for r in info]
    types = {r[1]: (r[2] or "").upper() for r in info}

    if "num unique" not in names:
        conn.execute('ALTER TABLE "garantie" ADD COLUMN "num unique" NUMERIC')
        conn.commit()
    else:
        coltype = types.get("num unique", "")
        if coltype not in ("INTEGER", "NUMERIC"):
            # Rebuild table with "num unique" NUMERIC, autres colonnes TEXT
            other_cols = [r[1] for r in info if r[1] != "num unique"]
            col_defs = ', '.join([f'"{c}" TEXT' for c in other_cols])
            conn.execute('BEGIN')
            conn.execute('CREATE TABLE "__garantie_new__" ("num unique" NUMERIC' + (',' + col_defs if col_defs else '') + ')')
            if other_cols:
                sel_others = ', '.join(f'"{c}"' for c in other_cols)
                conn.execute(f'INSERT INTO "__garantie_new__" ("num unique",{sel_others}) '
                             f'SELECT CAST("num unique" AS INTEGER), {sel_others} FROM "garantie"')
            else:
                conn.execute('INSERT INTO "__garantie_new__" ("num unique") '
                             'SELECT CAST("num unique" AS INTEGER) FROM "garantie"')
            conn.execute('DROP TABLE "garantie"')
            conn.execute('ALTER TABLE "__garantie_new__" RENAME TO "garantie"')
            conn.commit()

    # Index UNIQUE (idempotent)
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS "ux_garantie_num_unique" ON "garantie"("num unique")')
    conn.commit()

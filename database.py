import sqlite3
from contextlib import contextmanager
from config import DATABASE_PATH

DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS medicamento (
    nregistro   TEXT PRIMARY KEY,
    nombre      TEXT NOT NULL,
    laboratorio TEXT,
    dosis       TEXT,
    forma       TEXT,
    receta      INTEGER DEFAULT 0,
    generico    INTEGER DEFAULT 0,
    imagen_url  TEXT,
    ficha_url   TEXT,
    prospecto_url TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS presentacion (
    cn          TEXT PRIMARY KEY,
    nregistro   TEXT NOT NULL,
    nombre      TEXT,
    comercializado INTEGER DEFAULT 1,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_presentacion_nregistro ON presentacion(nregistro);

CREATE TABLE IF NOT EXISTS precio (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cn          TEXT NOT NULL,
    fuente      TEXT NOT NULL,  -- 'nomenclator' | 'cima' | nombre farmacia
    pvp         REAL,
    pvp_iva     REAL,
    precio_ref  REAL,
    url_fuente  TEXT,
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(cn, fuente)
);

CREATE INDEX IF NOT EXISTS idx_precio_cn ON precio(cn);

CREATE TABLE IF NOT EXISTS nomenclator_producto (
    cn          TEXT PRIMARY KEY,
    nombre      TEXT NOT NULL,
    estado      TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nomenclator_producto_nombre ON nomenclator_producto(nombre);

CREATE TABLE IF NOT EXISTS importacion (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fuente      TEXT NOT NULL,
    registros   INTEGER DEFAULT 0,
    ok          INTEGER DEFAULT 1,
    mensaje     TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS busqueda_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    termino     TEXT NOT NULL,
    tipo        TEXT NOT NULL DEFAULT 'medicamento',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_busqueda_log_termino ON busqueda_log(termino);
CREATE INDEX IF NOT EXISTS idx_busqueda_log_fecha   ON busqueda_log(created_at);
CREATE INDEX IF NOT EXISTS idx_busqueda_log_tipo    ON busqueda_log(tipo);

-- Tabla de features para el matcher híbrido (FTS5 + rapidfuzz)
CREATE TABLE IF NOT EXISTS medicamento_features (
    cn               TEXT PRIMARY KEY,
    nombre           TEXT NOT NULL,
    nombre_norm      TEXT NOT NULL,
    principio_activo TEXT,
    dosis_mg         INTEGER,
    unidades         INTEGER,
    forma            TEXT,
    pvp              REAL
);

CREATE INDEX IF NOT EXISTS idx_mf_dosis ON medicamento_features(dosis_mg);
CREATE INDEX IF NOT EXISTS idx_mf_pa    ON medicamento_features(principio_activo);

-- FTS5 para recuperación rápida de candidatos
CREATE VIRTUAL TABLE IF NOT EXISTS medicamento_fts USING fts5(
    cn          UNINDEXED,
    nombre_norm,
    principio_activo
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_session() as conn:
        # Migración segura: añadir columna tipo si no existe (antes del executescript)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(busqueda_log)").fetchall()]
            if cols and "tipo" not in cols:
                conn.execute("ALTER TABLE busqueda_log ADD COLUMN tipo TEXT NOT NULL DEFAULT 'medicamento'")
        except Exception:
            pass
        conn.executescript(SCHEMA)

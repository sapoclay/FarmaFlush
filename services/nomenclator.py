"""Servicio para importar precios del Nomenclátor del SNS.

El Nomenclátor se ofrece como CSV descargable desde sanidad.gob.es.
Requiere una sesión con cookies: primero GET a la página del formulario
y luego GET al enlace de exportación CSV.

Los precios se indexan por Código Nacional (CN), que luego se cruza
con las presentaciones de CIMA para obtener el nregistro.
"""

import csv
import io
import logging

import httpx

from database import db_session
from services.busqueda_texto import cubre_consulta, puntuar_coincidencia, tokens_consulta

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0, connect=20.0)
_FORM_URL = "https://www.sanidad.gob.es/profesionales/nomenclator.do"
_DETAIL_URL = "https://www.sanidad.gob.es/profesionales/nomenclator.do?metodo=verDetalle&prod={cn}"
_EXPORT_URL = (
    "https://www.sanidad.gob.es/profesionales/nomenclator.do"
    "?metodo=buscarProductos"
    "&especialidad=%25%25%25"
    "&d-4015021-e=1"
    "&6578706f7274=1%20%C2%A0"
)

# Columnas reales del CSV del Nomenclátor
_COL_CN = "Código Nacional"
_COL_NOMBRE = "Nombre del producto farmacéutico"
_COL_PVP_IVA = "Precio venta al público con IVA"
_COL_PRECIO_REF = "Precio de referencia"
_COL_ESTADO = "Estado"


def construir_url_detalle(cn: str) -> str:
    """Devuelve la URL directa al detalle de un producto en Nomenclátor."""
    return _DETAIL_URL.format(cn=cn)


def buscar_productos(consulta: str, limit: int = 10) -> list[dict]:
    tokens = list(tokens_consulta(consulta))
    if not tokens:
        return []

    patron_sql = f"%{sorted(tokens, key=len, reverse=True)[0]}%"
    with db_session() as conn:
        rows = conn.execute(
            """SELECT np.cn, np.nombre, np.estado, p.pvp_iva, p.precio_ref
               FROM nomenclator_producto np
               LEFT JOIN precio p ON p.cn = np.cn AND p.fuente = 'nomenclator'
               WHERE lower(np.nombre) LIKE ?
               ORDER BY np.nombre
               LIMIT 200""",
            (patron_sql,),
        ).fetchall()

        if not rows:
            rows = conn.execute(
                """SELECT pr.cn, pr.nombre, '' AS estado, p.pvp_iva, p.precio_ref
                   FROM presentacion pr
                   JOIN precio p ON p.cn = pr.cn AND p.fuente = 'nomenclator'
                   WHERE lower(pr.nombre) LIKE ?
                   ORDER BY pr.nombre
                   LIMIT 200""",
                (patron_sql,),
            ).fetchall()

    resultados = []
    for row in rows:
        nombre = (row["nombre"] or "").strip()
        if not cubre_consulta(nombre, consulta):
            continue
        resultados.append(
            {
                "cn": row["cn"],
                "nombre": nombre,
                "estado": (row["estado"] or "").strip(),
                "precio": row["pvp_iva"],
                "precio_ref": row["precio_ref"],
                "url": construir_url_detalle(row["cn"]),
                "comparacion_score": puntuar_coincidencia(nombre, consulta),
            }
        )

    resultados.sort(key=lambda item: (item.get("comparacion_score", 0), item.get("precio") is not None), reverse=True)
    return resultados[:limit]


def descargar_nomenclator_csv() -> str:
    """Descarga el CSV completo del Nomenclátor con sesión de cookies."""
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        # Paso 1: obtener cookies de sesión
        client.get(_FORM_URL)
        # Paso 2: descargar CSV
        resp = client.get(_EXPORT_URL)
        resp.raise_for_status()
        contenido = resp.text
        # Verificar que realmente es CSV (no HTML)
        if contenido.lstrip().startswith("<!") or contenido.lstrip().startswith("<html"):
            raise RuntimeError("La descarga devolvió HTML en vez de CSV")
        return contenido


def importar_nomenclator():
    """Descarga e importa precios del Nomenclátor a la BD local.

    Cada fila del CSV contiene: Código Nacional, nombre, PVP con IVA,
    precio de referencia, etc.  Los precios se almacenan indexados por CN.
    """
    try:
        texto = descargar_nomenclator_csv()
    except Exception as exc:
        logger.error("Error descargando nomenclátor: %s", exc)
        _registrar_importacion("nomenclator", 0, ok=False, mensaje=str(exc))
        return

    reader = csv.DictReader(io.StringIO(texto), delimiter=",")
    count = 0

    with db_session() as conn:
        # Limpiar precios nomenclátor previos
        conn.execute("DELETE FROM precio WHERE fuente = 'nomenclator'")
        conn.execute("DELETE FROM nomenclator_producto")

        for row in reader:
            cn = (row.get(_COL_CN) or "").strip()
            nombre = (row.get(_COL_NOMBRE) or "").strip()
            estado = (row.get(_COL_ESTADO) or "").strip()
            if not cn:
                continue

            pvp_iva_raw = (row.get(_COL_PVP_IVA) or "").replace(",", ".").strip()
            precio_ref_raw = (row.get(_COL_PRECIO_REF) or "").replace(",", ".").strip()

            pvp_iva = float(pvp_iva_raw) if pvp_iva_raw else None
            precio_ref = float(precio_ref_raw) if precio_ref_raw else None

            # Guardar incluso si no tiene precio (el CN existe en nomenclátor)
            conn.execute(
                """INSERT OR REPLACE INTO precio
                   (cn, fuente, pvp, pvp_iva, precio_ref, url_fuente)
                   VALUES (?, 'nomenclator', NULL, ?, ?, ?)""",
                (cn, pvp_iva, precio_ref, construir_url_detalle(cn)),
            )
            conn.execute(
                """INSERT OR REPLACE INTO nomenclator_producto
                   (cn, nombre, estado)
                   VALUES (?, ?, ?)""",
                (cn, nombre or cn, estado),
            )
            count += 1

        _registrar_importacion("nomenclator", count, conn=conn)

    logger.info("Nomenclátor importado: %d registros", count)

    # Reconstruir el índice del matcher (FTS5 + features) tras la importación
    try:
        from services import matcher as _matcher
        _matcher.poblar_features(force=True)
    except Exception as exc:
        logger.warning("No se pudo poblar medicamento_features tras importación: %s", exc)


def _registrar_importacion(fuente, registros, ok=True, mensaje=None, conn=None):
    sql = "INSERT INTO importacion (fuente, registros, ok, mensaje) VALUES (?, ?, ?, ?)"
    params = (fuente, registros, 1 if ok else 0, mensaje)
    if conn:
        conn.execute(sql, params)
    else:
        with db_session() as c:
            c.execute(sql, params)

"""Proveedor Dosfarma para productos de parafarmacia y precios online."""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from html import unescape

import httpx
from services.envios import obtener_politica_envio

logger = logging.getLogger(__name__)

FUENTE_ID = "dosfarma"
FUENTE_NOMBRE = "Dosfarma"
FUENTE_URL = "https://www.dosfarma.com"

_APP_ID = "5FYR88UN93"
_API_KEY = "MDcyZWIyZjVlOTk0YzRjMDg2ZTBiNmUzZTcyNWE3YjZhMGZkOWQwYmQ0NzE0NDcwNTc4MWI2ZTFmMzBmMGRmMHRhZ0ZpbHRlcnM9"
_INDEX_NAME = "pro_dosfarma_es_products"
_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_HEADERS = {
    "X-Algolia-Application-Id": _APP_ID,
    "X-Algolia-API-Key": _API_KEY,
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
}
_CLIENT = httpx.Client(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True)


def buscar_productos(consulta: str, limit: int = 12) -> list[dict]:
    productos = []
    vistos = set()

    for query in _variantes_consulta(consulta):
        hits = _buscar_hits(query, limit=max(limit, 12))
        for hit in hits:
            producto = _normalizar_hit(hit)
            if not producto:
                continue

            object_id = producto.get("object_id")
            if not object_id or object_id in vistos:
                continue

            vistos.add(object_id)
            productos.append(producto)
            if len(productos) >= limit:
                return productos

    return productos


def obtener_producto(object_id: str) -> dict | None:
    if not object_id:
        return None

    try:
        resp = _CLIENT.get(f"https://{_APP_ID}-dsn.algolia.net/1/indexes/{_INDEX_NAME}/{object_id}")
        resp.raise_for_status()
        return _normalizar_hit(resp.json(), include_full_description=True)
    except Exception:
        logger.debug("No se pudo recuperar el producto de Dosfarma %s", object_id, exc_info=True)
        return None


@lru_cache(maxsize=128)
def _buscar_hits(consulta: str, limit: int = 12) -> list[dict]:
    query = _normalizar_consulta_externa(consulta)
    if not query:
        return []

    payload = {
        "params": (
            f"query={query}&hitsPerPage={limit}&page=0&clickAnalytics=true"
            "&numericFilters=visibility_search%3D1"
        )
    }

    try:
        resp = _CLIENT.post(
            f"https://{_APP_ID}-dsn.algolia.net/1/indexes/{_INDEX_NAME}/query",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json().get("hits", [])
    except Exception:
        logger.debug("No se pudo consultar Dosfarma para %s", consulta, exc_info=True)
        return []


def _normalizar_hit(hit: dict, include_full_description: bool = False) -> dict | None:
    if not hit:
        return None

    nombre = (hit.get("name") or "").strip()
    object_id = str(hit.get("objectID") or "").strip()
    if not nombre or not object_id:
        return None

    precio = ((hit.get("price") or {}).get("EUR") or {}).get("default")
    descripcion_html = _limpiar_html_externo(hit.get("description") or "")
    descripcion_corta = _extraer_resumen_html(descripcion_html)
    categorias = _categorias_legibles(hit.get("categories") or {})
    marca = (hit.get("brand") or hit.get("item_brand") or "").strip()
    imagen = hit.get("image_url") or hit.get("thumbnail_url") or ""
    url = (hit.get("url") or "").strip()
    rating = hit.get("rating_summary")
    rating_count = hit.get("rating_count")
    formato = _coerce_text(hit.get("format"))
    contenido = _coerce_text(hit.get("content_size"))
    formato_size = _coerce_text(hit.get("format_size"))
    ingredientes = _limpiar_texto_plano(hit.get("ingredient_list") or "")
    etiqueta = _extraer_etiqueta(hit.get("label"))
    meta_titulo = _coerce_text(hit.get("meta_title"))
    dimensiones = _coerce_text(hit.get("product_dimensions"))
    es_otc = _coerce_bool(hit.get("is_otc"))
    es_pack = _coerce_bool(hit.get("is_bundle"))
    stock = bool(hit.get("in_stock"))
    precio_normalizado = round(float(precio), 2) if precio is not None else None
    politica_envio = obtener_politica_envio(FUENTE_ID)
    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": precio_normalizado,
        "precio_anterior": None,
        "url": url,
        "stock": stock,
        "sku": str(hit.get("sku") or "").strip(),
        "envio": politica_envio,
    }

    return {
        "object_id": object_id,
        "nombre": nombre,
        "marca": marca,
        "precio": precio_normalizado,
        "url": url,
        "url_venta": url,
        "imagen_url": imagen,
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "envio": politica_envio,
        "descripcion": descripcion_corta,
        "descripcion_html": descripcion_html if include_full_description else "",
        "sku": oferta["sku"],
        "stock": stock,
        "categorias": categorias,
        "categoria_principal": categorias[0] if categorias else "",
        "formato": formato,
        "contenido": contenido,
        "formato_size": formato_size,
        "ingredientes": ingredientes,
        "etiqueta": etiqueta,
        "meta_titulo": meta_titulo,
        "dimensiones": dimensiones,
        "es_otc": es_otc,
        "es_pack": es_pack,
        "rating": float(rating) if rating is not None else None,
        "rating_count": int(rating_count) if rating_count is not None else 0,
        "ofertas": [oferta],
    }


def _variantes_consulta(consulta: str) -> list[str]:
    original = _normalizar_consulta_externa(consulta)
    if not original:
        return []
    variantes = [original]
    tokens = original.split()
    if len(tokens) > 2:
        variantes.append(" ".join(tokens[:2]))
    return list(dict.fromkeys(variantes))


def _limpiar_html_externo(html: str) -> str:
    limpio = re.sub(r"<script.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    limpio = re.sub(r"<iframe.*?</iframe>", "", limpio, flags=re.IGNORECASE | re.DOTALL)
    limpio = re.sub(r' on[a-z]+="[^"]*"', "", limpio, flags=re.IGNORECASE)
    return limpio.strip()


def _extraer_resumen_html(html: str) -> str:
    if not html:
        return ""
    texto = re.sub(r"<[^>]+>", " ", html)
    texto = unescape(texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:320].rstrip() + ("..." if len(texto) > 320 else "")


def _limpiar_texto_plano(valor: str) -> str:
    texto = unescape(_coerce_text(valor))
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _coerce_text(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, list):
        partes = [_coerce_text(item) for item in valor]
        return " ".join(parte for parte in partes if parte).strip()
    if isinstance(valor, dict):
        partes = [_coerce_text(item) for item in valor.values()]
        return " · ".join(parte for parte in partes if parte).strip()
    return str(valor).strip()


def _coerce_bool(valor) -> bool:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        return valor.strip().lower() in {"1", "true", "yes", "si", "sí", "y"}
    if valor is None:
        return False
    return bool(valor)


def _extraer_etiqueta(valor) -> str:
    texto = _limpiar_texto_plano(_coerce_text(valor))
    return texto[:80].strip()


def _categorias_legibles(categories: dict) -> list[str]:
    valores = []
    for nivel in sorted(categories):
        for item in categories.get(nivel) or []:
            if "Campaign" in item:
                continue
            limpio = item.split(" /// ")[-1].strip()
            if limpio and limpio not in valores:
                valores.append(limpio)
    return valores


def _normalizar_consulta_externa(consulta: str) -> str:
    texto = unescape((consulta or "").strip())
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(char for char in texto if not unicodedata.combining(char))
    texto = re.sub(r"[^A-Za-z0-9+ -]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()
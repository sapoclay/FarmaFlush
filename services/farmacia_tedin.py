"""Proveedor Farmacia Tedin para productos de parafarmacia y precios online."""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from functools import lru_cache
from html import unescape
from urllib.parse import quote_plus

import httpx
from services.envios import obtener_politica_envio

logger = logging.getLogger(__name__)

FUENTE_ID = "tedin"
FUENTE_NOMBRE = "Farmacia Tedin"
FUENTE_URL = "https://www.farmaciatedin.es"

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    "Referer": "https://www.farmaciatedin.es/",
}

# Circuit breaker: tras N fallos consecutivos, dejar de intentar durante _CB_COOLDOWN_S
_CB_MAX_FALLOS = 2
_CB_COOLDOWN_S = 600  # 10 minutos
_cb_fallos = 0
_cb_bloqueado_hasta = 0.0
_HEADERS_HTML = {
    "User-Agent": _HEADERS_JSON["User-Agent"],
    "Accept-Language": _HEADERS_JSON["Accept-Language"],
}
_CLIENT_JSON = httpx.Client(timeout=_TIMEOUT, headers=_HEADERS_JSON, follow_redirects=True)
_CLIENT_HTML = httpx.Client(timeout=_TIMEOUT, headers=_HEADERS_HTML, follow_redirects=True)

# Cadencia mínima entre peticiones al mismo servidor
_last_request_at: float = 0.0
_MIN_DELAY = 0.5  # segundos mínimos entre peticiones


def _esperar_cortesia() -> None:
    """Pausa mínima entre peticiones HTTP al mismo servidor."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_DELAY:
        time.sleep(_MIN_DELAY - elapsed)
    _last_request_at = time.monotonic()


def buscar_productos(consulta: str, limit: int = 12) -> list[dict]:
    global _cb_fallos, _cb_bloqueado_hasta

    if _cb_fallos >= _CB_MAX_FALLOS and time.monotonic() < _cb_bloqueado_hasta:
        logger.debug("Tedin circuit breaker activo, saltando búsqueda")
        return []

    productos = []
    vistos = set()

    for query in _variantes_consulta(consulta):
        payload = _buscar_json(query)
        if payload.get("_blocked"):
            return []
        for hit in payload.get("products") or []:
            producto = _normalizar_producto_lista(hit)
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
        resp = _CLIENT_HTML.get(f"{FUENTE_URL}/index.php?controller=product&id_product={object_id}")
        resp.raise_for_status()
    except Exception:
        logger.debug("No se pudo recuperar el producto de Tedin %s", object_id, exc_info=True)
        return None

    producto = _normalizar_producto_detalle(resp.text, object_id=str(object_id), final_url=str(resp.url))
    if producto:
        return producto

    return None


@lru_cache(maxsize=128)
def _buscar_json(consulta: str) -> dict:
    global _cb_fallos, _cb_bloqueado_hasta

    query = _normalizar_consulta_externa(consulta)
    if not query:
        return {}

    _esperar_cortesia()
    try:
        resp = _CLIENT_JSON.get(f"{FUENTE_URL}/buscar?ajaxSearch=1&s={quote_plus(query)}")
        if resp.status_code == 429:
            espera = int(resp.headers.get("Retry-After", _CB_COOLDOWN_S))
            _cb_bloqueado_hasta = time.monotonic() + espera
            _cb_fallos = _CB_MAX_FALLOS
            logger.warning("Tedin 429 — pausando %ds", espera)
            return {"_blocked": True}
        resp.raise_for_status()
        data = resp.json()

        # Detectar bloqueo de Imunify360 / anti-bot
        if isinstance(data, dict) and "Access denied" in data.get("message", ""):
            logger.info("Tedin bloqueó la petición (anti-bot): %s", data.get("message", ""))
            _cb_fallos += 1
            if _cb_fallos >= _CB_MAX_FALLOS:
                _cb_bloqueado_hasta = time.monotonic() + _CB_COOLDOWN_S
                logger.warning("Tedin circuit breaker activado por %ds", _CB_COOLDOWN_S)
            return {"_blocked": True}

        # Éxito: resetear circuit breaker
        _cb_fallos = 0
        return data
    except Exception:
        logger.debug("No se pudo consultar Tedin para %s", consulta, exc_info=True)
        _cb_fallos += 1
        if _cb_fallos >= _CB_MAX_FALLOS:
            _cb_bloqueado_hasta = time.monotonic() + _CB_COOLDOWN_S
        return {}


def _normalizar_producto_lista(hit: dict) -> dict | None:
    if not hit:
        return None

    object_id = str(hit.get("id_product") or "").strip()
    nombre = _coerce_text(hit.get("name"))
    if not object_id or not nombre:
        return None

    precio = hit.get("price_amount")
    precio_anterior = _to_float(_coerce_text(hit.get("regular_price")))
    descripcion_html = _coerce_text(hit.get("description_short"))
    descripcion = _limpiar_texto_plano(descripcion_html)
    imagen = (((hit.get("cover") or {}).get("large") or {}).get("url")) or (((hit.get("cover") or {}).get("medium") or {}).get("url")) or ""
    url = _coerce_text(hit.get("url"))
    marca = _coerce_text(hit.get("manufacturer_name"))
    sku = _coerce_text(hit.get("reference_to_display"))
    politica_envio = obtener_politica_envio(FUENTE_ID)
    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": round(float(precio), 2) if precio is not None else None,
        "precio_anterior": precio_anterior,
        "url": url,
        "stock": None,
        "sku": sku,
        "envio": politica_envio,
    }

    return {
        "object_id": object_id,
        "nombre": nombre,
        "marca": marca,
        "precio": oferta["precio"],
        "url": url,
        "url_venta": url,
        "imagen_url": imagen,
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "envio": politica_envio,
        "descripcion": descripcion[:320].rstrip() + ("..." if len(descripcion) > 320 else ""),
        "descripcion_html": "",
        "sku": sku,
        "stock": None,
        "categorias": [],
        "categoria_principal": "",
        "formato": "",
        "contenido": "",
        "formato_size": "",
        "ingredientes": "",
        "etiqueta": _extraer_etiqueta(hit.get("flags")),
        "meta_titulo": "",
        "dimensiones": "",
        "es_otc": False,
        "es_pack": False,
        "rating": None,
        "rating_count": 0,
        "ofertas": [oferta],
    }


def _normalizar_producto_detalle(html: str, object_id: str, final_url: str) -> dict | None:
    product_schema = None
    breadcrumb_schema = None

    for raw in re.findall(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html, re.IGNORECASE | re.DOTALL):
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        if isinstance(payload, dict) and payload.get("@type") == "Product":
            product_schema = payload
        elif isinstance(payload, dict) and payload.get("@type") == "BreadcrumbList":
            breadcrumb_schema = payload

    if not product_schema:
        return None

    nombre = _coerce_text(product_schema.get("name"))
    descripcion_html = unescape(_coerce_text(product_schema.get("description")))
    descripcion = _limpiar_texto_plano(descripcion_html)
    brand = product_schema.get("brand") or {}
    marca = _coerce_text(brand.get("name") if isinstance(brand, dict) else brand)
    image = _coerce_text(product_schema.get("image"))
    sku = _coerce_text(product_schema.get("sku") or product_schema.get("mpn"))
    categorias = _extraer_categorias_breadcrumb(breadcrumb_schema)
    categoria_principal = _coerce_text(product_schema.get("category")) or (categorias[-1] if categorias else "")
    offers = product_schema.get("offers") or {}
    aggregate = product_schema.get("aggregateRating") or {}
    precio = _to_float(_coerce_text(offers.get("price")))
    stock = _coerce_text(offers.get("availability")).lower().endswith("instock")
    meta_titulo = _extraer_meta(html, "og:title") or nombre
    meta_descripcion = _extraer_meta_name(html, "description")
    if meta_descripcion and not descripcion:
        descripcion = meta_descripcion
    politica_envio = obtener_politica_envio(FUENTE_ID)

    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": precio,
        "precio_anterior": None,
        "url": final_url,
        "stock": stock,
        "sku": sku,
        "envio": politica_envio,
    }

    return {
        "object_id": object_id,
        "nombre": nombre,
        "marca": marca,
        "precio": precio,
        "url": final_url,
        "url_venta": final_url,
        "imagen_url": image,
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "envio": politica_envio,
        "descripcion": descripcion[:320].rstrip() + ("..." if len(descripcion) > 320 else ""),
        "descripcion_html": descripcion_html,
        "sku": sku,
        "stock": stock,
        "categorias": categorias,
        "categoria_principal": categoria_principal,
        "formato": "",
        "contenido": "",
        "formato_size": "",
        "ingredientes": "",
        "etiqueta": "",
        "meta_titulo": meta_titulo,
        "dimensiones": _coerce_text((product_schema.get("weight") or {}).get("value")),
        "es_otc": False,
        "es_pack": False,
        "rating": _to_float(_coerce_text(aggregate.get("ratingValue"))),
        "rating_count": int(_to_float(_coerce_text(aggregate.get("reviewCount"))) or 0),
        "ofertas": [oferta],
    }


def _extraer_meta(html: str, property_name: str) -> str:
    match = re.search(rf'<meta[^>]+property="{re.escape(property_name)}"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else ""


def _extraer_meta_name(html: str, name: str) -> str:
    match = re.search(rf'<meta[^>]+name="{re.escape(name)}"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else ""


def _extraer_categorias_breadcrumb(payload: dict | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    categorias = []
    for item in payload.get("itemListElement") or []:
        nombre = _coerce_text(item.get("name"))
        if not nombre or nombre.lower() in {"inicio", "tedin"}:
            continue
        if nombre not in categorias:
            categorias.append(nombre)
    if categorias:
        categorias = categorias[:-1]
    return categorias


def _variantes_consulta(consulta: str) -> list[str]:
    original = _normalizar_consulta_externa(consulta)
    if not original:
        return []
    variantes = [original]
    tokens = original.split()
    if len(tokens) > 2:
        variantes.append(" ".join(tokens[:2]))
    return list(dict.fromkeys(variantes))


def _normalizar_consulta_externa(consulta: str) -> str:
    texto = unescape((consulta or "").strip())
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(char for char in texto if not unicodedata.combining(char))
    texto = re.sub(r"[^A-Za-z0-9+ -]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _coerce_text(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, list):
        return " ".join(_coerce_text(item) for item in valor if _coerce_text(item)).strip()
    if isinstance(valor, dict):
        return " · ".join(_coerce_text(item) for item in valor.values() if _coerce_text(item)).strip()
    return str(valor).strip()


def _limpiar_texto_plano(valor: str) -> str:
    texto = unescape(valor or "")
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _to_float(valor: str) -> float | None:
    if not valor:
        return None
    limpio = valor.replace("€", "").replace("\xa0", " ").replace(" ", "").replace(",", ".").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", limpio)
    if not match:
        return None
    try:
        return round(float(match.group(0)), 2)
    except ValueError:
        return None


def _extraer_etiqueta(valor) -> str:
    texto = _limpiar_texto_plano(_coerce_text(valor))
    return texto[:80].strip()
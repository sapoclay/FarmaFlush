"""Proveedor Castrofarma para productos de parafarmacia y precios online (Magento 2)."""

from __future__ import annotations

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

FUENTE_ID = "castrofarma"
FUENTE_NOMBRE = "Castrofarma"
FUENTE_URL = "https://www.castrofarma.com"

_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
}
_CLIENT = httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS)

# Cortesía: circuit breaker y cadencia mínima entre peticiones al mismo servidor
_CB_COOLDOWN = 60.0
_cb_fallo_at: float = 0.0
_last_request_at: float = 0.0
_MIN_DELAY = 0.5  # segundos mínimos entre peticiones

# Caché ligera: almacena productos por object_id para que obtener_producto
# pueda recuperar datos sin necesidad de una API de detalle.
_cache_productos: dict[str, dict] = {}


def _esperar_cortesia() -> None:
    """Garantiza una pausa mínima entre peticiones HTTP al mismo servidor."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < _MIN_DELAY:
        time.sleep(_MIN_DELAY - elapsed)
    _last_request_at = time.monotonic()


# Regex para extraer datos de cada <li class="item product product-item ...">…</li>
_ITEM_RE = re.compile(
    r'<li class="item product product-item.*?</li>', re.DOTALL
)
_NAME_RE = re.compile(
    r'class="product-item-link"[^>]*href="(?P<url>[^"]+)"[^>]*>\s*(?P<name>.*?)\s*</a',
    re.DOTALL,
)
_PRICE_RE = re.compile(r'data-price-amount="(?P<price>[^"]+)"')
_PID_RE = re.compile(r'data-price-box="product-id-(?P<pid>\d+)"')
_IMG_RE = re.compile(
    r'class="product-image-photo"[^>]*src="(?P<src>[^"]+)"'
)


def buscar_productos(consulta: str, limit: int = 12) -> list[dict]:
    productos: list[dict] = []
    vistos: set[str] = set()

    for query in _variantes_consulta(consulta):
        for item in _buscar_pagina(query, limit=max(limit, 12)):
            producto = _normalizar_item(item)
            if not producto:
                continue
            oid = producto.get("object_id")
            if not oid or oid in vistos:
                continue
            vistos.add(oid)
            _cache_productos[oid] = producto  # guardar para obtener_producto
            productos.append(producto)
            if len(productos) >= limit:
                return productos

    return productos


def obtener_producto(object_id: str) -> dict | None:
    if not object_id:
        return None

    cached = _cache_productos.get(object_id)
    if cached and (cached.get("descripcion") or cached.get("descripcion_html")):
        return cached

    from services import _scraper_detail
    url = f"{FUENTE_URL}/catalog/product/view/id/{object_id}"
    producto = _scraper_detail.fetch_detalle_producto(
        _CLIENT, _esperar_cortesia, url, object_id, FUENTE_ID, FUENTE_NOMBRE
    )
    if producto:
        _cache_productos[object_id] = producto
        return producto

    # Fetch fallido: devolver lo que haya en caché aunque no tenga descripción
    if cached:
        return cached

    # Último recurso: objeto mínimo con enlace externo
    politica_envio = obtener_politica_envio(FUENTE_ID)
    oferta = {
        "fuente": FUENTE_ID, "nombre_fuente": FUENTE_NOMBRE, "precio": None,
        "precio_anterior": None, "url": url, "stock": None, "sku": "", "envio": politica_envio,
    }
    return {
        "object_id": object_id, "nombre": "", "marca": "", "precio": None,
        "url": url, "url_venta": url, "imagen_url": "", "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE, "envio": politica_envio, "descripcion": "",
        "descripcion_html": "", "sku": "", "stock": None, "categorias": [],
        "categoria_principal": "", "ofertas": [oferta],
    }


@lru_cache(maxsize=128)
def _buscar_pagina(query: str, limit: int = 24) -> tuple[dict, ...]:
    global _cb_fallo_at
    if time.monotonic() - _cb_fallo_at < _CB_COOLDOWN:
        return ()

    _esperar_cortesia()
    try:
        resp = _CLIENT.get(
            f"{FUENTE_URL}/catalogsearch/result/",
            params={"q": query},
        )
        if resp.status_code == 429:
            espera = int(resp.headers.get("Retry-After", int(_CB_COOLDOWN)))
            _cb_fallo_at = time.monotonic() + espera - _CB_COOLDOWN
            logger.warning("Castrofarma 429 — pausando %ds", espera)
            return ()
        resp.raise_for_status()
    except httpx.TimeoutException:
        _cb_fallo_at = time.monotonic()
        logger.debug("Timeout — fuente pausada %ds", int(_CB_COOLDOWN))
        return ()
    except Exception:
        logger.debug("No se pudo consultar Castrofarma para %s", query, exc_info=True)
        return ()

    items: list[dict] = []
    for match in _ITEM_RE.finditer(resp.text):
        block = match.group(0)
        name_m = _NAME_RE.search(block)
        price_m = _PRICE_RE.search(block)
        pid_m = _PID_RE.search(block)
        img_m = _IMG_RE.search(block)

        if not name_m or not price_m:
            continue

        items.append({
            "name": unescape(name_m.group("name")).strip(),
            "url": name_m.group("url").strip(),
            "price": price_m.group("price").strip(),
            "pid": pid_m.group("pid").strip() if pid_m else "",
            "image": img_m.group("src").strip() if img_m else "",
        })
        if len(items) >= limit:
            break

    return tuple(items)


def _normalizar_item(raw: dict) -> dict | None:
    nombre = raw.get("name", "").strip()
    url = raw.get("url", "").strip()
    if not nombre or not url:
        return None

    try:
        precio = round(float(raw["price"]), 2)
    except (ValueError, KeyError, TypeError):
        precio = None

    object_id = raw.get("pid") or url.rstrip("/").rsplit("/", 1)[-1].replace(".html", "")
    imagen = raw.get("image", "")
    politica_envio = obtener_politica_envio(FUENTE_ID)

    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": precio,
        "precio_anterior": None,
        "url": url,
        "stock": True,
        "sku": "",
        "envio": politica_envio,
    }

    return {
        "object_id": object_id,
        "nombre": nombre,
        "marca": "",
        "precio": precio,
        "url": url,
        "url_venta": url,
        "imagen_url": imagen,
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "envio": politica_envio,
        "descripcion": "",
        "descripcion_html": "",
        "sku": "",
        "stock": True,
        "categorias": [],
        "categoria_principal": "",
        "ofertas": [oferta],
    }


# -- Helpers ----------------------------------------------------------------

# Castrofarma usa terminología propia; estos sinónimos permiten encontrar productos
# cuando el usuario busca con términos coloquiales.
_SINONIMOS: dict[str, str] = {
    "pasta dientes": "pasta dental",
    "pasta de dientes": "pasta dental",
    "gel ducha": "gel de ducha",
    "gel de bano": "gel corporal",
    "gel bano": "gel corporal",
    "jabon manos": "jabon de manos",
    "jabon de manos": "jabon liquido",
    "colonia ninos": "colonia infantil",
    "colonia bebe": "colonia infantil",
    "crema solar": "protector solar",
    "bronceador": "protector solar",
    "higiene bucal": "pasta dental",
    "hilo dental": "seda dental",
}


def _variantes_consulta(consulta: str) -> list[str]:
    original = _normalizar_consulta_externa(consulta)
    if not original:
        return []
    variantes = [original]
    # Sinonimo directo
    sinonimo = _SINONIMOS.get(original.lower())
    if sinonimo:
        variantes.append(sinonimo)
    # Variante más corta (primeras 2 palabras)
    tokens = original.split()
    if len(tokens) > 2:
        corta = " ".join(tokens[:2])
        variantes.append(corta)
        if corta.lower() in _SINONIMOS:
            variantes.append(_SINONIMOS[corta.lower()])
    return list(dict.fromkeys(variantes))


def _normalizar_consulta_externa(consulta: str) -> str:
    texto = unescape((consulta or "").strip())
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^a-zA-Z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:120]

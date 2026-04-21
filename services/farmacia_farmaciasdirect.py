"""Proveedor Farmacias Direct para medicamentos OTC y parafarmacia."""

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

FUENTE_ID = "farmaciasdirect"
FUENTE_NOMBRE = "Farmacias Direct"
FUENTE_URL = "https://www.farmaciasdirect.es"

_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
}
_DATA_JSON_RE = re.compile(r"data-json-product='(?P<payload>\{.*?\})'", re.DOTALL)
_CLIENT = httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS, http2=False)

# Cortesía: circuit breaker y cadencia mínima entre peticiones
_CB_COOLDOWN = 60.0
_cb_fallo_at: float = 0.0
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
    productos = []
    vistos = set()

    for query in _variantes_consulta(consulta):
        for item in _buscar_productos(query):
            producto = _normalizar_producto_search(item)
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
        resp = _CLIENT.get(f"{FUENTE_URL}/products/{object_id}.js")
        resp.raise_for_status()
        return _normalizar_producto_detalle(resp.json())
    except Exception:
        logger.debug("No se pudo recuperar el producto de Farmacias Direct %s", object_id, exc_info=True)
        return None


@lru_cache(maxsize=128)
def _buscar_productos(consulta: str) -> list[dict]:
    global _cb_fallo_at
    if time.monotonic() - _cb_fallo_at < _CB_COOLDOWN:
        return []

    query = _normalizar_consulta_externa(consulta)
    if not query:
        return []

    _esperar_cortesia()
    try:
        resp = _CLIENT.get(f"{FUENTE_URL}/search?q={quote_plus(query)}")
        if resp.status_code == 429:
            espera = int(resp.headers.get("Retry-After", int(_CB_COOLDOWN)))
            _cb_fallo_at = time.monotonic() + espera - _CB_COOLDOWN
            logger.warning("FarmaciasDirects 429 — pausando %ds", espera)
            return []
        resp.raise_for_status()
    except httpx.TimeoutException:
        _cb_fallo_at = time.monotonic()
        logger.debug("Timeout — Farmacias Direct pausada %ds", int(_CB_COOLDOWN))
        return []
    except Exception:
        logger.debug("No se pudo consultar Farmacias Direct para %s", consulta, exc_info=True)
        return []

    productos = []
    for match in _DATA_JSON_RE.finditer(resp.text):
        raw_payload = unescape(match.group("payload"))
        try:
            payload = json.loads(raw_payload)
        except Exception:
            continue
        productos.append(payload)
    return productos


def _normalizar_producto_search(payload: dict) -> dict | None:
    handle = (payload.get("handle") or "").strip()
    nombre = (payload.get("title") or "").strip()
    if not handle or not nombre:
        return None

    variant = ((payload.get("variants") or [{}]) or [{}])[0]
    media = (payload.get("media") or [])
    preview = ((media[0] or {}).get("preview_image") or {}) if media else {}
    imagen = _normalizar_imagen((media[0] or {}).get("src") if media else "") or _normalizar_imagen(preview.get("src")) or _normalizar_imagen(payload.get("featured_image"))
    precio = _centimos_a_float(payload.get("price"))
    precio_anterior = _centimos_a_float(payload.get("compare_at_price"))
    sku = _coerce_text(variant.get("sku"))
    url = f"{FUENTE_URL}/products/{handle}"
    categoria = _coerce_text(payload.get("type"))
    politica_envio = obtener_politica_envio(FUENTE_ID)
    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": precio,
        "precio_anterior": precio_anterior,
        "url": url,
        "stock": bool(variant.get("available", payload.get("available"))),
        "sku": sku,
        "envio": politica_envio,
    }

    return {
        "object_id": handle,
        "nombre": nombre,
        "marca": _coerce_text(payload.get("vendor")),
        "precio": precio,
        "url": url,
        "url_venta": url,
        "imagen_url": imagen,
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "envio": politica_envio,
        "descripcion": "",
        "descripcion_html": "",
        "sku": sku,
        "stock": oferta["stock"],
        "categorias": [categoria] if categoria else [],
        "categoria_principal": categoria,
        "formato": "",
        "contenido": "",
        "formato_size": "",
        "ingredientes": "",
        "etiqueta": "",
        "meta_titulo": nombre,
        "dimensiones": "",
        "es_otc": "medicamentos" in categoria.lower(),
        "es_pack": False,
        "rating": None,
        "rating_count": 0,
        "ofertas": [oferta],
    }


def _normalizar_producto_detalle(payload: dict) -> dict | None:
    handle = (payload.get("handle") or "").strip()
    nombre = (payload.get("title") or "").strip()
    if not handle or not nombre:
        return None

    variant = ((payload.get("variants") or [{}]) or [{}])[0]
    media = (payload.get("media") or [])
    imagen = _normalizar_imagen((media[0] or {}).get("src") if media else "") or _normalizar_imagen(payload.get("featured_image"))
    precio = _centimos_a_float(variant.get("price", payload.get("price")))
    precio_anterior = _centimos_a_float(variant.get("compare_at_price", payload.get("compare_at_price")))
    sku = _coerce_text(variant.get("sku"))
    url = f"{FUENTE_URL}{_coerce_text(payload.get('url')) or f'/products/{handle}'}"
    categoria = _coerce_text(payload.get("type"))
    descripcion_html = unescape(_coerce_text(payload.get("description")))
    descripcion = _limpiar_texto_plano(descripcion_html)
    politica_envio = obtener_politica_envio(FUENTE_ID)
    oferta = {
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "precio": precio,
        "precio_anterior": precio_anterior,
        "url": url,
        "stock": bool(variant.get("available", payload.get("available"))),
        "sku": sku,
        "envio": politica_envio,
    }

    return {
        "object_id": handle,
        "nombre": nombre,
        "marca": _coerce_text(payload.get("vendor")),
        "precio": precio,
        "url": url,
        "url_venta": url,
        "imagen_url": imagen,
        "fuente": FUENTE_ID,
        "nombre_fuente": FUENTE_NOMBRE,
        "envio": politica_envio,
        "descripcion": descripcion[:320].rstrip() + ("..." if len(descripcion) > 320 else ""),
        "descripcion_html": descripcion_html,
        "sku": sku,
        "stock": oferta["stock"],
        "categorias": [categoria] if categoria else [],
        "categoria_principal": categoria,
        "formato": "",
        "contenido": "",
        "formato_size": "",
        "ingredientes": "",
        "etiqueta": "",
        "meta_titulo": nombre,
        "dimensiones": "",
        "es_otc": "medicamentos" in categoria.lower(),
        "es_pack": False,
        "rating": None,
        "rating_count": 0,
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


def _normalizar_consulta_externa(consulta: str) -> str:
    texto = unescape((consulta or "").strip())
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(char for char in texto if not unicodedata.combining(char))
    texto = re.sub(r"[^A-Za-z0-9+ -]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _centimos_a_float(valor) -> float | None:
    if valor in (None, ""):
        return None
    try:
        return round(float(valor) / 100, 2)
    except (TypeError, ValueError):
        return None


def _normalizar_imagen(valor) -> str:
    texto = _coerce_text(valor)
    if not texto:
        return ""
    if texto.startswith("//"):
        return f"https:{texto}"
    if texto.startswith("/"):
        return f"{FUENTE_URL}{texto}"
    return texto


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
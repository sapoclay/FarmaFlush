"""Agregador multi-fuente para parafarmacia y precios online."""

from __future__ import annotations

import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

from services import (
    farmacia_amazon,
    farmacia_castrofarma,
    farmacia_dosfarma,
    farmacia_farmaciabarata,
    farmacia_farmaciasdirect,
    farmacia_farmagalicia,
    farmacia_gomezulla,
    farmacia_openfarma,
    farmacia_pontevea,
    farmacia_tedin,
)

_log = logging.getLogger(__name__)

_PROVEEDORES = [
    farmacia_dosfarma,
    farmacia_tedin,
    farmacia_farmaciasdirect,
    farmacia_castrofarma,
    farmacia_farmaciabarata,
    farmacia_farmagalicia,
    farmacia_openfarma,
    farmacia_pontevea,
    farmacia_gomezulla,
    farmacia_amazon,
]
_TOKENS_IRRELEVANTES = {
    "mg",
    "g",
    "gr",
    "ml",
    "mcg",
    "ui",
    "ug",
    "crema",
    "gel",
    "capsulas",
    "capsula",
    "comprimidos",
    "comprimido",
    "sobres",
    "suspension",
    "oral",
    "solucion",
    "efg",
    "topico",
    "pomada",
    "recubiertos",
    "recubierto",
    "blandas",
    "blanda",
}
FUENTES_FARMACIA = [
    {
        "id": proveedor.FUENTE_ID,
        "nombre": proveedor.FUENTE_NOMBRE,
        "url": proveedor.FUENTE_URL,
    }
    for proveedor in _PROVEEDORES
]


def buscar_productos(consulta: str, limit: int = 12) -> list[dict]:
    acumulados = []
    for _, acumulados in iter_busqueda_productos(consulta, limit=limit):
        pass
    return acumulados


# Términos genéricos usados para explorar parafarmacia sin búsqueda concreta
_TERMINOS_PARAFARMACIA = ["higiene", "crema", "vitamina", "suplemento", "desinfectante", "pañales", "piel", "cabello", "bucal", "solar", "alergia", "deporte", "sexual", "mascarilla", "covid", "gafas", "barba"]


def buscar_productos_parafarmacia(limit: int = 25) -> list[dict]:
    """Devuelve productos de parafarmacia de todos los proveedores sin filtrar por nombre exacto.

    Itera proveedor como bucle externo con una cuota individual para garantizar
    que todos los proveedores (incluidos los nuevos) tengan representación.
    """
    vistos: set = set()
    acumulados: list[dict] = []
    cuota = max(limit // max(len(_PROVEEDORES), 1), 4)

    for proveedor in _PROVEEDORES:
        contrib = 0
        for termino in _TERMINOS_PARAFARMACIA:
            if contrib >= cuota:
                break
            try:
                productos_proveedor = proveedor.buscar_productos(termino, limit=cuota)
            except Exception as exc:
                _log.warning("Proveedor %s falló en parafarmacia: %s", proveedor.FUENTE_ID, exc)
                continue
            for producto in productos_proveedor:
                clave = (producto.get("fuente"), producto.get("object_id"))
                if clave in vistos:
                    continue
                vistos.add(clave)
                producto = dict(producto)
                producto["comparacion_score"] = 1
                acumulados.append(producto)
                contrib += 1
                if contrib >= cuota:
                    break

    return acumulados[:limit]


def iter_busqueda_productos(consulta: str, limit: int = 12):
    candidatos = []
    vistos = set()

    for proveedor in _PROVEEDORES:
        try:
            productos_proveedor = proveedor.buscar_productos(consulta, limit=limit)
        except Exception as exc:
            _log.warning("Proveedor %s falló: %s", proveedor.FUENTE_ID, exc)
            productos_proveedor = []
        for producto in productos_proveedor:
            if not _coincide_nombre_con_consulta(producto, consulta):
                continue
            nombre_producto = producto.get("nombre", "")
            if _es_medicamento_regulado(nombre_producto):
                continue
            if _nombre_en_nomenclator(nombre_producto):
                continue
            clave = (producto.get("fuente"), producto.get("object_id"))
            if clave in vistos:
                continue
            vistos.add(clave)
            producto = dict(producto)
            producto["comparacion_score"] = _puntuar_producto(producto, consulta)
            candidatos.append(producto)

        candidatos.sort(
            key=lambda item: (
                item.get("comparacion_score", 0),
                item.get("precio") is not None,
                -(float(item.get("precio") or 0)),
            ),
            reverse=True,
        )
        yield {
            "id": proveedor.FUENTE_ID,
            "nombre": proveedor.FUENTE_NOMBRE,
            "url": proveedor.FUENTE_URL,
        }, candidatos[:limit]


def buscar_mejor_precio(consulta: str) -> dict | None:
    ofertas = buscar_ofertas(consulta, max_sources=len(_PROVEEDORES))
    ofertas_con_precio = [oferta for oferta in ofertas if oferta.get("precio") is not None]
    if not ofertas_con_precio:
        return None
    return min(ofertas_con_precio, key=lambda item: item["precio"])


def buscar_ofertas(
    consulta: str,
    marca: str = "",
    max_sources: int | None = None,
    consultas_extra: list[str] | None = None,
    incluir_sin_resultado: bool = False,
) -> list[dict]:
    referencias = _construir_referencias(consulta, marca=marca, consultas_extra=consultas_extra)
    fuentes_con_resultado: set[str] = set()
    ofertas: list[dict] = []

    def _buscar_desde_proveedor(proveedor):
        candidatos = _buscar_candidatos_proveedor(proveedor, referencias)
        mejor = _seleccionar_mejor_coincidencia(candidatos, referencias, marca)
        return mejor.get("ofertas") or [] if mejor else []

    with ThreadPoolExecutor(max_workers=len(_PROVEEDORES)) as executor:
        futuros = {executor.submit(_buscar_desde_proveedor, prov): prov for prov in _PROVEEDORES}
        for futuro in as_completed(futuros):
            prov = futuros[futuro]
            try:
                resultado = futuro.result()
                if resultado:
                    fuentes_con_resultado.add(prov.FUENTE_ID)
                    ofertas.extend(resultado)
            except Exception:
                pass

    ofertas = _deduplicar_ofertas(ofertas)
    ofertas.sort(key=lambda item: (item.get("precio") is None, item.get("precio") or 0, item.get("nombre_fuente") or ""))
    if max_sources is not None:
        ofertas = ofertas[:max_sources]

    if incluir_sin_resultado:
        for prov in _PROVEEDORES:
            if prov.FUENTE_ID not in fuentes_con_resultado:
                ofertas.append({
                    "fuente": prov.FUENTE_ID,
                    "nombre_fuente": prov.FUENTE_NOMBRE,
                    "url": prov.FUENTE_URL,
                    "precio": None,
                    "sin_resultado": True,
                })

    return ofertas


def obtener_producto(object_id: str, fuente: str = farmacia_dosfarma.FUENTE_ID) -> dict | None:
    proveedor = _obtener_proveedor(fuente)
    if not proveedor:
        return None
    return proveedor.obtener_producto(object_id)


def obtener_producto_con_comparativa(object_id: str, fuente: str = farmacia_dosfarma.FUENTE_ID) -> dict | None:
    producto = obtener_producto(object_id, fuente=fuente)
    if producto is None:
        return None

    ofertas = buscar_ofertas(producto.get("nombre", ""), marca=producto.get("marca", ""), max_sources=len(_PROVEEDORES))
    if producto.get("ofertas"):
        ofertas = _deduplicar_ofertas((producto.get("ofertas") or []) + ofertas)
        ofertas.sort(key=lambda item: (item.get("precio") is None, item.get("precio") or 0, item.get("nombre_fuente") or ""))

    producto["ofertas"] = ofertas
    producto["mejor_oferta"] = ofertas[0] if ofertas else None
    precios = [oferta["precio"] for oferta in ofertas if oferta.get("precio") is not None]
    producto["precio_minimo"] = min(precios) if precios else None
    producto["precio_maximo"] = max(precios) if precios else None
    producto["numero_fuentes"] = len(ofertas)
    producto["total_proveedores"] = len(_PROVEEDORES)
    producto["fuentes_consultadas"] = [p.FUENTE_NOMBRE for p in _PROVEEDORES]
    producto["fuentes_con_oferta"] = list(dict.fromkeys(
        o.get("nombre_fuente") for o in ofertas if o.get("nombre_fuente")
    ))
    return producto


def resumir_comparativa_medicamento(nombre: str) -> dict:
    ofertas = buscar_ofertas(nombre, max_sources=len(_PROVEEDORES))
    precios = [oferta["precio"] for oferta in ofertas if oferta.get("precio") is not None]
    return {
        "ofertas": ofertas,
        "mejor_oferta": ofertas[0] if ofertas else None,
        "precio_minimo": min(precios) if precios else None,
        "precio_maximo": max(precios) if precios else None,
        "numero_fuentes": len(ofertas),
    }


def buscar_producto_relacionado(
    consulta: str,
    marca: str = "",
    consultas_extra: list[str] | None = None,
) -> dict | None:
    referencias = _construir_referencias(consulta, marca=marca, consultas_extra=consultas_extra)
    candidatos_encontrados = []

    def _candidato_de_proveedor(proveedor):
        candidatos = _buscar_candidatos_proveedor(proveedor, referencias)
        candidato = _seleccionar_mejor_coincidencia(candidatos, referencias, marca)
        if not candidato:
            return None
        detalle = proveedor.obtener_producto(candidato.get("object_id"))
        resultado = detalle or candidato
        resultado["comparacion_score"] = candidato.get("comparacion_score", 0)
        return resultado

    with ThreadPoolExecutor(max_workers=len(_PROVEEDORES)) as executor:
        futuros = [executor.submit(_candidato_de_proveedor, prov) for prov in _PROVEEDORES]
        for futuro in as_completed(futuros):
            try:
                resultado = futuro.result()
                if resultado is not None:
                    candidatos_encontrados.append(resultado)
            except Exception:
                pass

    if not candidatos_encontrados:
        return None
    return max(candidatos_encontrados, key=lambda item: item.get("comparacion_score", 0))


def _obtener_proveedor(fuente: str):
    for proveedor in _PROVEEDORES:
        if proveedor.FUENTE_ID == fuente:
            return proveedor
    return None


_MAX_REFERENCIAS = 3


def _construir_referencias(consulta: str, marca: str = "", consultas_extra: list[str] | None = None) -> list[str]:
    candidatas = [consulta]
    if marca:
        candidatas.append(f"{consulta} {marca}".strip())
    for item in consultas_extra or []:
        if item:
            candidatas.append(item)
    # Deduplicar tras normalizar a minúsculas y espacios
    vistos: set[str] = set()
    unicas: list[str] = []
    for ref in candidatas:
        clave = ref.strip().lower()
        if clave and clave not in vistos:
            vistos.add(clave)
            unicas.append(ref.strip())
    return unicas[:_MAX_REFERENCIAS]


def _buscar_candidatos_proveedor(proveedor, referencias: list[str]) -> list[dict]:
    candidatos = []
    vistos: set[tuple] = set()
    for referencia in referencias:
        try:
            productos_proveedor = proveedor.buscar_productos(referencia, limit=8)
        except Exception as exc:
            _log.warning("Proveedor %s falló en búsqueda de ofertas: %s", proveedor.FUENTE_ID, exc)
            continue
        for producto in productos_proveedor:
            clave = (producto.get("fuente"), producto.get("object_id"))
            if clave in vistos:
                continue
            vistos.add(clave)
            candidatos.append(producto)
        # Si ya tenemos suficientes candidatos, no seguir con más referencias
        if len(candidatos) >= 6:
            break
    return candidatos


def _seleccionar_mejor_coincidencia(candidatos: list[dict], referencias: list[str], marca: str = "") -> dict | None:
    if not candidatos:
        return None

    mejores = []
    for candidato in candidatos:
        score = _puntuar_producto_multi(candidato, referencias, marca)
        if score <= 0:
            continue
        candidato = dict(candidato)
        candidato["comparacion_score"] = score
        mejores.append(candidato)

    if not mejores:
        return None

    mejores.sort(
        key=lambda item: (
            item.get("comparacion_score", 0),
            item.get("precio") is not None,
            -(float(item.get("precio") or 0)),
        ),
        reverse=True,
    )
    mejor = mejores[0]
    return mejor if mejor.get("comparacion_score", 0) >= 45 else None


def _deduplicar_ofertas(ofertas: list[dict]) -> list[dict]:
    unicas = []
    vistas = set()
    for oferta in ofertas:
        url = (oferta.get("url") or "").strip()
        sku = (oferta.get("sku") or "").strip()
        clave = (oferta.get("fuente"), url or sku)
        if clave in vistas:
            continue
        vistas.add(clave)
        unicas.append(oferta)
    return unicas


def _puntuar_producto(producto: dict, referencia: str) -> int:
    nombre = _normalizar_texto(producto.get("nombre", ""))
    marca = _normalizar_texto(producto.get("marca", ""))
    referencia_norm = _normalizar_texto(referencia)
    if not nombre or not referencia_norm:
        return 0

    tokens_referencia_sig = _tokens_consulta(referencia_norm)
    if tokens_referencia_sig and not _nombre_cubre_tokens(nombre, tokens_referencia_sig):
        return 0

    if nombre == referencia_norm:
        return 1000
    if referencia_norm in nombre:
        return 800 + len(referencia_norm)
    if nombre in referencia_norm:
        return 650 + len(nombre)

    tokens_nombre = set(nombre.split())
    tokens_nombre_sig = _tokens_significativos(tokens_nombre)

    comunes_sig = tokens_nombre_sig & tokens_referencia_sig
    if tokens_referencia_sig and not comunes_sig:
        return 0

    score = len(comunes_sig) * 28 + sum(len(token) for token in comunes_sig)

    if marca and marca in referencia_norm:
        score += 20

    if tokens_referencia_sig and len(comunes_sig) / len(tokens_referencia_sig) >= 0.6:
        score += 35
    if len(tokens_referencia_sig) >= 2 and len(comunes_sig) == 1:
        score -= 25

    return score


def _puntuar_producto_multi(producto: dict, referencias: list[str], marca: str = "") -> int:
    if not referencias:
        return 0

    # La referencia primaria (índice 0) debe puntuar por sí sola, o bien la marca
    # debe aparecer en el nombre/marca del producto. Sin este requisito, un producto
    # completamente distinto (ej. "Difenatil paracetamol") podría colarse gracias a
    # una referencia secundaria del principio activo (ej. "paracetamol").
    score_primaria = _puntuar_producto(producto, referencias[0])
    marca_norm = _normalizar_texto(marca)
    nombre_producto_norm = _normalizar_texto(
        (producto.get("marca") or "") + " " + (producto.get("nombre") or "")
    )
    marca_en_producto = bool(marca_norm and marca_norm in nombre_producto_norm)

    if score_primaria == 0 and not marca_en_producto:
        return 0

    mejor = score_primaria
    for referencia in referencias[1:]:
        score = _puntuar_producto(producto, referencia)
        if score > mejor:
            mejor = score

    if marca_norm and marca_norm in _normalizar_texto(producto.get("marca", "")):
        mejor += 12
    return mejor


def _tokens_significativos(tokens: set[str]) -> set[str]:
    significativos = {token for token in tokens if len(token) > 2 and token not in _TOKENS_IRRELEVANTES and not token.isdigit()}
    return significativos or {token for token in tokens if len(token) > 2 and not token.isdigit()}


def _tokens_consulta(consulta_norm: str) -> set[str]:
    return _tokens_significativos(set(consulta_norm.split()))


def _nombre_cubre_tokens(nombre_norm: str, tokens_referencia: set[str]) -> bool:
    if not tokens_referencia:
        return False

    tokens_nombre = set(nombre_norm.split())
    tokens_nombre_sig = _tokens_significativos(tokens_nombre)
    if tokens_referencia.issubset(tokens_nombre_sig):
        return True

    # Comprobar coincidencia por palabra completa (no subcadena) para evitar
    # falsos positivos como "antidol" dentro de "antidolor"
    return all(token in tokens_nombre for token in tokens_referencia)


_RE_DOSIS = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:mg|mcg|ug|g|ml|ui|miu|meg)(?:/(?:g|ml|comprimido|capsula|sobre|dosis))?\b",
    re.IGNORECASE,
)
_FORMAS_FARMACEUTICAS = re.compile(
    r"\b(?:comprimidos?|comprimido|capsulas?|capsula|sobres?|grageas?|ampollas?|"
    r"inyectable|inyectables|jarabe|suspension\s+oral|solucion\s+oral|"
    r"colirio|gotas\s+of[ti]almicas?|supositorios?|parches?\s+transdermicos?|"
    r"aerosol\s+nasal|spray\s+nasal|polvo\s+para\s+inhalar|inhalador|"
    r"gel\s+t[oó]pico|soluci[oó]n\s+t[oó]pica|crema\s+t[oó]pica|pomada\s+t[oó]pica)\b",
    re.IGNORECASE,
)
_SUFIJOS_MEDICAMENTO = re.compile(r"\bEFG\b|\bECG\b|\bEFG,\b", re.IGNORECASE)
_RE_DOSIS_TOPICA = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:mg|g)/(?:g|ml)\b"  # ej. 50mg/g, 10mg/ml → medicamento tópico
    r"|\b\d+(?:[.,]\d+)?\s*%\s+(?:gel|crema|pomada|solucion|solution|emulgel)\b",  # ej. 2% gel
    re.IGNORECASE,
)


@lru_cache(maxsize=512)
def _nombre_en_nomenclator(nombre: str) -> bool:
    """Devuelve True si el nombre tiene coincidencia 'segura' en el Nomenclátor SNS.

    Actúa como capa de refuerzo tras _es_medicamento_regulado. Usa lru_cache
    para evitar consultas repetidas a la BD.
    """
    try:
        from services.matcher import match_producto
        resultado = match_producto(nombre)
        return resultado is not None and resultado.get("confianza") == "seguro"
    except Exception:
        return False


def _es_medicamento_regulado(nombre: str) -> bool:
    """Devuelve True si el nombre del producto parece un medicamento regulado.

    Heurística: tiene forma farmacéutica (comprimidos, cápsulas…) O
    dosis en formato concentración (mg/g, mg/ml) O sufijo EFG/ECG.
    """
    if not nombre:
        return False
    # Comprobar EFG/ECG y dosis tópica en nombre original (antes de normalizar)
    if _SUFIJOS_MEDICAMENTO.search(nombre):
        return True
    if _RE_DOSIS_TOPICA.search(nombre):
        return True
    nombre_norm = _normalizar_texto(nombre)
    if _FORMAS_FARMACEUTICAS.search(nombre_norm):
        return True
    return False


def _coincide_nombre_con_consulta(producto: dict, consulta: str) -> bool:
    nombre = _normalizar_texto(producto.get("nombre", ""))
    consulta_norm = _normalizar_texto(consulta)
    if not nombre or not consulta_norm:
        return False

    if consulta_norm in nombre:
        return True

    tokens_consulta = _tokens_consulta(consulta_norm)
    return _nombre_cubre_tokens(nombre, tokens_consulta)


def _normalizar_texto(valor: str) -> str:
    valor = unicodedata.normalize("NFKD", valor or "")
    valor = "".join(char for char in valor if not unicodedata.combining(char))
    valor = valor.lower()
    valor = re.sub(r"[^a-z0-9]+", " ", valor)
    return re.sub(r"\s+", " ", valor).strip()

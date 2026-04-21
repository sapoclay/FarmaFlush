"""Servicio de consulta a la API REST de CIMA (AEMPS)."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from config import CIMA_API_BASE
from database import db_session
from services.busqueda_texto import cubre_consulta, puntuar_coincidencia

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_TIPOS_IMAGEN_PRIORITARIOS = (
    "materialas",
    "cartonaje",
    "formafarmac",
)


def buscar_medicamentos(nombre: str, pagina: int = 1, tam: int = 25) -> dict:
    """Busca medicamentos por nombre en CIMA.

    Devuelve el JSON tal cual de CIMA:
        { totalFilas, pagina, tamanioPagina, resultados: [...] }
    """
    params = {"nombre": nombre, "pagina": pagina, "tamanioPagina": tam}
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(f"{CIMA_API_BASE}/medicamentos", params=params)
        resp.raise_for_status()
        return resp.json()


def detalle_medicamento(nregistro: str) -> dict | None:
    """Obtiene el detalle de un medicamento por su número de registro."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(f"{CIMA_API_BASE}/medicamento", params={"nregistro": nregistro})
        if resp.status_code in (204, 404):
            return None
        resp.raise_for_status()
        return resp.json()


def extraer_imagen_url(med: dict) -> str | None:
    """Selecciona la mejor imagen disponible para un medicamento."""
    fotos = med.get("fotos") or []
    fotos_con_url = [foto for foto in fotos if foto.get("url")]
    if not fotos_con_url:
        return None

    for tipo in _TIPOS_IMAGEN_PRIORITARIOS:
        foto = next((item for item in fotos_con_url if item.get("tipo") == tipo), None)
        if foto:
            return foto["url"]

    return fotos_con_url[0]["url"]


def _formatear_principios_activos(med: dict) -> list[str]:
    principios = med.get("principiosActivos") or []
    if principios:
        resultado = []
        for principio in principios:
            nombre = (principio.get("nombre") or "").strip()
            cantidad = (principio.get("cantidad") or "").strip()
            unidad = (principio.get("unidad") or "").strip()
            detalle = " ".join(part for part in [cantidad, unidad] if part)
            resultado.append(f"{nombre} ({detalle})" if detalle else nombre)
        return [item for item in resultado if item]

    pactivos = (med.get("pactivos") or "").strip()
    return [pactivos] if pactivos else []


def _formatear_vias_administracion(med: dict) -> list[str]:
    return [via.get("nombre", "").strip() for via in med.get("viasAdministracion") or [] if via.get("nombre")]


def _formatear_presentaciones(med: dict) -> list[dict]:
    presentaciones = []
    for presentacion in med.get("presentaciones") or []:
        nombre = (presentacion.get("nombre") or "").strip()
        cn = str(presentacion.get("cn") or "").strip()
        presentaciones.append(
            {
                "nombre": nombre,
                "cn": cn,
                "comercializado": bool(presentacion.get("comerc")),
                "envase_clinico": bool(presentacion.get("envaseClinico")),
            }
        )
    return presentaciones


def _obtener_grupo_terapeutico(med: dict) -> str:
    atcs = med.get("atcs") or []
    if not atcs:
        return ""
    return (atcs[-1].get("nombre") or "").strip()


def _extraer_datos(med: dict) -> dict:
    """Normaliza un resultado de CIMA al modelo interno."""
    docs = {d.get("tipo"): d for d in med.get("docs", [])}
    imagen = extraer_imagen_url(med)
    principios_activos = _formatear_principios_activos(med)
    vias_administracion = _formatear_vias_administracion(med)
    presentaciones = _formatear_presentaciones(med)

    return {
        "nregistro": med.get("nregistro", ""),
        "nombre": med.get("nombre", ""),
        "laboratorio": med.get("labtitular", ""),
        "laboratorio_comercializador": med.get("labcomercializador", ""),
        "dosis": med.get("dosis", ""),
        "forma": (med.get("formaFarmaceutica") or {}).get("nombre", ""),
        "forma_simplificada": (med.get("formaFarmaceuticaSimplificada") or {}).get("nombre", ""),
        "principios_activos": principios_activos,
        "principio_activo_texto": ", ".join(principios_activos),
        "vias_administracion": vias_administracion,
        "vias_administracion_texto": ", ".join(vias_administracion),
        "tipo_prescripcion": med.get("cpresc", ""),
        "grupo_terapeutico": _obtener_grupo_terapeutico(med),
        "comercializado": bool(med.get("comerc")),
        "conduce": bool(med.get("conduc")),
        "triangulo": bool(med.get("triangulo")),
        "biosimilar": bool(med.get("biosimilar")),
        "huerfano": bool(med.get("huerfano")),
        "presentaciones": presentaciones,
        "receta": 1 if med.get("receta") else 0,
        "generico": 1 if med.get("generico") else 0,
        "imagen_url": imagen,
        "ficha_url": (docs.get(1) or {}).get("urlHtml"),
        "prospecto_url": (docs.get(2) or {}).get("urlHtml"),
    }


def buscar_con_filtros(filtros: dict, pagina: int = 1, tam: int = 25) -> dict:
    """Busca en CIMA usando filtros de categoría (sin requerir nombre).

    filtros admite claves como receta=0/1, comerc=1, generico=1, etc.
    La API devuelve resultados paginados directamente.
    """
    params = {"pagina": pagina, "tamanioPagina": tam, **filtros}
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(f"{CIMA_API_BASE}/medicamentos", params=params)
        resp.raise_for_status()
        raw = resp.json()
    resultados_raw = raw.get("resultados") or []
    resultados = [_extraer_datos(m) for m in resultados_raw]
    return {
        "total": raw.get("totalFilas", len(resultados)),
        "pagina": pagina,
        "tam": tam,
        "modo_todos": False,
        "resultados": resultados,
    }


def buscar_y_normalizar(nombre: str, pagina: int = 1, tam: int = 25) -> dict:
    """Busca en CIMA y devuelve datos normalizados."""
    # La API de CIMA devuelve el bloque completo de resultados en la primera
    # página para estas búsquedas, y las páginas siguientes quedan vacías.
    # Por eso paginamos localmente a partir de la primera respuesta.
    raw = buscar_medicamentos(nombre, pagina=1, tam=tam)
    resultados_raw = raw.get("resultados", [])
    resultados_filtrados = [_extraer_datos(m) for m in resultados_raw if cubre_consulta(m.get("nombre", ""), nombre)]
    resultados_filtrados.sort(key=lambda med: puntuar_coincidencia(med.get("nombre", ""), nombre), reverse=True)
    total = len(resultados_filtrados)
    inicio = max(pagina - 1, 0) * tam
    fin = inicio + tam
    resultados = resultados_filtrados[inicio:fin]
    return {
        "total": total,
        "pagina": pagina,
        "tam": tam,
        "modo_todos": False,
        "resultados": resultados,
    }


def guardar_presentaciones(nregistro: str, med_raw: dict | None = None) -> list[str]:
    """Extrae las presentaciones del detalle CIMA y las guarda en la BD.

    Devuelve la lista de códigos nacionales (CN) asociados al nregistro.
    Si no se pasa med_raw, hace la petición a CIMA.
    """
    if med_raw is None:
        med_raw = detalle_medicamento(nregistro)
    if not med_raw:
        return []

    presentaciones = med_raw.get("presentaciones") or []
    cns = []

    with db_session() as conn:
        for p in presentaciones:
            cn = str(p.get("cn", "")).strip()
            if not cn:
                continue
            cns.append(cn)
            conn.execute(
                """INSERT OR REPLACE INTO presentacion (cn, nregistro, nombre, comercializado)
                   VALUES (?, ?, ?, ?)""",
                (cn, nregistro, p.get("nombre", ""), 1 if p.get("comerc") else 0),
            )

    return cns


def cargar_presentaciones_batch(nregistros: list[str]) -> dict[str, dict]:
    """Carga presentaciones de CIMA para varios nregistros en paralelo.

    Solo consulta CIMA para los que no están ya en la tabla presentacion.
    Devuelve un dict {nregistro: med_raw} con los datos crudos descargados
    para que el llamante pueda reutilizarlos sin repetir peticiones.
    """
    if not nregistros:
        return {}

    with db_session() as conn:
        placeholders = ",".join("?" * len(nregistros))
        ya_cargados = {
            r["nregistro"]
            for r in conn.execute(
                f"SELECT DISTINCT nregistro FROM presentacion WHERE nregistro IN ({placeholders})",
                nregistros,
            ).fetchall()
        }

    pendientes = [nr for nr in nregistros if nr not in ya_cargados]
    detalles: dict[str, dict] = {}

    if not pendientes:
        return detalles

    def _fetch_one(nr: str) -> tuple[str, dict | None]:
        try:
            med_raw = detalle_medicamento(nr)
            guardar_presentaciones(nr, med_raw)
            return nr, med_raw
        except Exception:
            logger.debug("No se pudieron cargar presentaciones para %s", nr)
            return nr, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_one, nr) for nr in pendientes]
        for f in as_completed(futures):
            nr, med_raw = f.result()
            if med_raw is not None:
                detalles[nr] = med_raw

    return detalles

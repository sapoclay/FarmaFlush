import logging
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from flask import Flask, make_response, render_template, request, send_from_directory, url_for

from config import BASE_DIR, SECRET_KEY, DEBUG
from database import init_db
import database
from services import bifimed, cima, farmacia, matcher as matcher_svc, nomenclator, precios, vademecum
from services.envios import obtener_politica_envio

logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["TEMPLATES_AUTO_RELOAD"] = True

init_db()

# Importar Nomenclátor SNS si la tabla de precios está vacía
def _importar_nomenclator_si_vacio():
    from database import db_session
    with db_session() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM precio WHERE fuente = 'nomenclator'").fetchone()[0]
    if cnt == 0:
        logging.getLogger(__name__).info("Nomenclátor vacío, importando…")
        nomenclator.importar_nomenclator()
    else:
        # Si el nomenclator ya está importado pero features aún no están pobladas
        try:
            matcher_svc.poblar_features()
        except Exception as exc:
            logging.getLogger(__name__).warning("No se pudo poblar features: %s", exc)

threading.Thread(target=_importar_nomenclator_si_vacio, daemon=True).start()

_TAMANIOS_PAGINA = {"10": 10, "25": 25, "50": 50, "100": 100}
_BUSQUEDAS_ASYNC: dict[str, dict] = {}
_BUSQUEDAS_ASYNC_LOCK = threading.Lock()
_BUSQUEDA_TTL_SEGUNDOS = 900

_CACHE_BUSQUEDAS: dict[str, dict] = {}
_CACHE_BUSQUEDAS_LOCK = threading.Lock()
_CACHE_TTL_SEGUNDOS = 12 * 3600  # 12 horas

_MAX_WORKERS_ENRIQUECIMIENTO = 6
_MAX_PARAFARMACIA = 200  # máximo de productos a obtener en búsquedas de parafarmacia
_PRODUCT_PLACEHOLDER_URL = "/static/img/product-placeholder.svg"
_FUENTES_CONSULTADAS = [
    {
        "nombre": "CIMA (AEMPS)",
        "url": "https://cima.aemps.es",
        "tipo": "API REST pública",
        "descripcion": "Fuente principal para nombre, laboratorio, forma farmacéutica, dosis, documentación oficial e imágenes del medicamento.",
    },
    {
        "nombre": "Nomenclátor SNS",
        "url": "https://www.sanidad.gob.es/profesionales/nomenclator.do",
        "tipo": "Base oficial del SNS",
        "descripcion": "Fuente oficial para precios de venta al público y precios de referencia cuando están disponibles.",
    },
    {
        "nombre": "Vademécum",
        "url": "https://www.vademecum.es",
        "tipo": "Fuente complementaria",
        "descripcion": "Se utiliza como apoyo para localizar fichas complementarias y ampliar información cuando otras fuentes no aportan todos los datos visibles en la aplicación.",
    },
    {
        "nombre": "Dosfarma",
        "url": "https://www.dosfarma.com",
        "tipo": "Farmacia online",
        "descripcion": "Se usa para comparar precios online de medicamentos y parafarmacia, además de servir como fuente de fallback de catálogo.",
        "fuente_id": "dosfarma",
    },
    {
        "nombre": "Farmacia Tedin",
        "url": "https://www.farmaciatedin.es",
        "tipo": "Farmacia online (Galicia)",
        "descripcion": "Fuente adicional especialmente útil para ampliar la comparación de precios en productos de parafarmacia y escaparates online con presencia en Galicia.",
        "fuente_id": "tedin",
    },
    {
        "nombre": "Farmacias Direct",
        "url": "https://www.farmaciasdirect.es",
        "tipo": "Farmacia online",
        "descripcion": "Tercera fuente online integrada para reforzar la comparativa de precios en España y completar fichas cuando otras fuentes no muestran precio o imagen.",
        "fuente_id": "farmaciasdirect",
    },
    {
        "nombre": "Castrofarma",
        "url": "https://www.castrofarma.com",
        "tipo": "Farmacia online",
        "descripcion": "Fuente adicional de parafarmacia con amplio catálogo de cosmética, higiene y cuidado personal.",
        "fuente_id": "castrofarma",
    },
    {
        "nombre": "Farmacia Barata",
        "url": "https://www.farmaciabarata.es",
        "tipo": "Farmacia online",
        "descripcion": "Farmacia online con catálogo que incluye tanto parafarmacia (champús, cremas, higiene) como medicamentos OTC.",
        "fuente_id": "farmaciabarata",
    },
    {
        "nombre": "FarmaGalicia",
        "url": "https://www.farmagalicia.com",
        "tipo": "Farmacia online (Galicia)",
        "descripcion": "Farmacia online gallega con gran catálogo de parafarmacia, cosmética y dermocosmética.",
        "fuente_id": "farmagalicia",
    },
    {
        "nombre": "OpenFarma",
        "url": "https://www.openfarma.com",
        "tipo": "Farmacia online",
        "descripcion": "Farmacia online con amplio surtido de parafarmacia, higiene personal y productos de salud.",
        "fuente_id": "openfarma",
    },
    {
        "nombre": "Farmacia Pontevea",
        "url": "https://farmaciapontevea.com",
        "tipo": "Farmacia online (Galicia)",
        "descripcion": "Farmacia online en Galicia especializada en dermocosmética, cuidado personal y parafarmacia.",
        "fuente_id": "pontevea",
    },
    {
        "nombre": "Gomezulla en tu Piel",
        "url": "https://www.gomezullaentupiel.com",
        "tipo": "Farmacia online (Galicia)",
        "descripcion": "Farmacia online gallega especializada en cosmética y cuidado dermatológico.",
        "fuente_id": "gomezulla",
    },
    {
        "nombre": "Amazon.es",
        "url": "https://www.amazon.es",
        "tipo": "Tienda online",
        "descripcion": "Marketplace con amplio catálogo de parafarmacia, cosmética, higiene y salud. Permite comparar precios con farmacias online especializadas.",
        "fuente_id": "amazon",
    },
]


def _enriquecer_imagen(med: dict, med_raw_cache: dict | None = None) -> None:
    """Completa imagen y principios activos desde el detalle de CIMA.

    La búsqueda de CIMA (/medicamentos) no devuelve principiosActivos ni
    pactivos, así que siempre necesitamos el detalle (/medicamento) para
    obtener esos campos.  Si *med_raw_cache* contiene el detalle ya
    descargado por ``cargar_presentaciones_batch``, lo reutiliza sin
    repetir la petición.
    """
    nreg = med["nregistro"]
    med_raw = (med_raw_cache or {}).get(nreg) or cima.detalle_medicamento(nreg)
    if med_raw is not None:
        if nreg not in (med_raw_cache or {}):
            cima.guardar_presentaciones(nreg, med_raw)

        if not med.get("imagen_url"):
            med["imagen_url"] = cima.extraer_imagen_url(med_raw)

        # Completar principios activos que la búsqueda no trae
        if not med.get("principio_activo_texto"):
            detalle_completo = cima._extraer_datos(med_raw)
            med["principios_activos"] = detalle_completo["principios_activos"]
            med["principio_activo_texto"] = detalle_completo["principio_activo_texto"]

    if not med.get("imagen_url"):
        med["imagen_url"] = _PRODUCT_PLACEHOLDER_URL


_SUFIJOS_SAL = {
    "TRIHIDRATO", "DIHIDRATO", "MONOHIDRATO", "HIDRATO",
    "SODICO", "SODICA", "POTASICO", "POTASICA",
    "CALCICO", "CALCICA", "MAGNESICO", "MAGNESICA",
    "MESILATO", "MALEATO", "FUMARATO", "SUCCINATO",
    "TARTRATO", "CLORHIDRATO", "BROMHIDRATO", "SULFATO",
    "FOSFATO", "ACETATO", "PROPIONATO", "BENZOATO",
    "HEMIFUMARATO", "BESILATO", "TOSILATO", "CITRATO",
    "LACTATO", "GLUCONATO", "ESTEARATO",
}


def _extraer_inn_base(inn_completo: str) -> str:
    """Extrae el nombre base del INN eliminando sufijos de sal farmacéutica.

    Ejemplo: 'AMOXICILINA TRIHIDRATO' → 'AMOXICILINA'
             'ESOMEPRAZOL SODICO'     → 'ESOMEPRAZOL'
             'IBUPROFENO'             → 'IBUPROFENO'
    """
    partes = inn_completo.strip().split()
    while len(partes) > 1 and partes[-1].upper() in _SUFIJOS_SAL:
        partes.pop()
    return " ".join(partes)


def _consultas_farmacia_alternativas(med: dict, complemento: dict | None = None) -> list[str]:
    consultas = []
    # Priorizar el principio activo específico del medicamento para obtener
    # precios correctos en farmacias (evita que todos los resultados de una
    # búsqueda genérica compartan la misma oferta).
    if med.get("principio_activo_texto"):
        pa = med["principio_activo_texto"]
        # Solo el nombre INN sin dosis (texto antes del primer paréntesis).
        inn = pa.split("(")[0].strip()
        if inn:
            consultas.append(inn)
        # INN base sin sufijo de sal farmacéutica
        inn_base = _extraer_inn_base(inn) if inn else ""
        if inn_base and inn_base.lower() != (inn or "").lower():
            consultas.append(inn_base)
    if complemento:
        if complemento.get("principio_activo"):
            consultas.append(complemento["principio_activo"])
    # La búsqueda original del usuario como alternativa de último recurso
    qorig = (med.get("_query_original") or "").strip()
    if qorig:
        consultas.append(qorig)
    return [item for item in dict.fromkeys(valor.strip() for valor in consultas) if item][:3]


def _enriquecer_desde_farmacias(med: dict, complemento: dict | None = None) -> None:
    consultas_extra = _consultas_farmacia_alternativas(med, complemento=complemento)
    # Usar el término más corto y relevante como consulta principal
    # (query_original o primera alternativa) en lugar del nombre completo del
    # medicamento que suele ser demasiado largo para farmacias online.
    consulta_principal = consultas_extra[0] if consultas_extra else med["nombre"]
    comparativa = farmacia.buscar_ofertas(
        consulta_principal,
        marca=med.get("laboratorio") or med.get("laboratorio_comercializador") or "",
        max_sources=len(farmacia.FUENTES_FARMACIA),
        consultas_extra=consultas_extra[1:],
        incluir_sin_resultado=True,
    )

    if (not med.get("imagen_url") or med.get("imagen_url", "").endswith("product-placeholder.svg")):
        producto_relacionado = farmacia.buscar_producto_relacionado(
            consulta_principal,
            marca=med.get("laboratorio") or med.get("laboratorio_comercializador") or "",
            consultas_extra=consultas_extra[1:],
        )
        if producto_relacionado and producto_relacionado.get("imagen_url"):
            med["imagen_url"] = producto_relacionado["imagen_url"]

    precios_online = [oferta["precio"] for oferta in comparativa if oferta.get("precio") is not None]
    if precios_online:
        # precio_medio solo se calcula desde farmacias cuando no hay precio oficial ni otro medio
        if med["precio_oficial"] is None and med["precio_medio"] is None:
            med["precio_medio"] = round(sum(precios_online) / len(precios_online), 2)
        # Siempre añadir las fuentes online a fuentes_precio para mostrar sus enlaces
        for oferta in comparativa:
            if oferta.get("precio") is None:
                continue
            med["fuentes_precio"].append(
                {
                    "fuente": oferta.get("nombre_fuente") or oferta.get("fuente"),
                    "pvp": oferta["precio"],
                    "pvp_iva": oferta["precio"],
                    "precio_ref": None,
                    "url": oferta.get("url"),
                    "cn": "",
                    "presentacion": med.get("nombre", ""),
                }
            )

    med["ofertas_farmacia"] = comparativa


def _resolver_tam_pagina() -> tuple[str, int]:
    tam_query = request.args.get("tam", "25").strip().lower()
    if tam_query not in _TAMANIOS_PAGINA:
        tam_query = "25"
    return tam_query, _TAMANIOS_PAGINA[tam_query]


def _contexto_busqueda_vacio(q: str, tam_query: str, tam: int, pagina: int = 1) -> dict:
    contexto = {
        "datos": {
            "resultados": [],
            "total": 0,
            "pagina": pagina,
            "tam": tam,
            "modo_todos": False,
        },
        "q": q,
        "tam_query": tam_query,
        "productos_farmacia": [],
        "coincidencias_nomenclator": [],
        "coincidencias_vademecum": [],
        "estado_busqueda": "Preparando búsqueda…",
        "busqueda_completa": False,
        "error_busqueda": "",
        "use_htmx": True,
        "sugerir_parafarmacia": False,
    }
    _actualizar_estado_resultados(contexto)
    return contexto


def _actualizar_estado_resultados(contexto: dict) -> None:
    hay_resultados_cima = bool((contexto.get("datos") or {}).get("resultados"))
    hay_resultados_nomenclator = bool(contexto.get("coincidencias_nomenclator"))
    hay_resultados_vademecum = bool(contexto.get("coincidencias_vademecum"))
    hay_resultados_farmacia = bool(contexto.get("productos_farmacia"))
    contexto["hay_resultados"] = hay_resultados_cima or hay_resultados_nomenclator or hay_resultados_vademecum or hay_resultados_farmacia
    contexto["mostrar_aviso_sin_coincidencias"] = bool(contexto.get("q")) and not contexto["hay_resultados"]


def _crear_snapshot_busqueda(contexto: dict) -> dict:
    return copy.deepcopy(contexto)


def _actualizar_busqueda_async(job_id: str, contexto: dict | None = None, **campos) -> None:
    with _BUSQUEDAS_ASYNC_LOCK:
        trabajo = _BUSQUEDAS_ASYNC.get(job_id)
        if not trabajo:
            return
        if contexto is not None:
            trabajo["contexto"] = _crear_snapshot_busqueda(contexto)
        trabajo.update(campos)
        trabajo["updated_at"] = time.time()


def _limpiar_busquedas_async() -> None:
    ahora = time.time()
    with _BUSQUEDAS_ASYNC_LOCK:
        expirados = [
            job_id
            for job_id, trabajo in _BUSQUEDAS_ASYNC.items()
            if ahora - trabajo.get("updated_at", trabajo.get("created_at", ahora)) > _BUSQUEDA_TTL_SEGUNDOS
        ]
        for job_id in expirados:
            _BUSQUEDAS_ASYNC.pop(job_id, None)


def _clave_cache(q: str, pagina: int, tam: int) -> str:
    return f"{q.lower().strip()}|{pagina}|{tam}"


def _obtener_de_cache(clave: str) -> dict | None:
    with _CACHE_BUSQUEDAS_LOCK:
        entrada = _CACHE_BUSQUEDAS.get(clave)
        if entrada is None:
            return None
        if time.time() - entrada["created_at"] > _CACHE_TTL_SEGUNDOS:
            _CACHE_BUSQUEDAS.pop(clave, None)
            return None
        return copy.deepcopy(entrada["contexto"])


def _guardar_en_cache(clave: str, contexto: dict) -> None:
    with _CACHE_BUSQUEDAS_LOCK:
        _CACHE_BUSQUEDAS[clave] = {
            "contexto": copy.deepcopy(contexto),
            "created_at": time.time(),
        }


def _enriquecer_med_completo(med: dict, query_original: str = "", med_raw_cache: dict | None = None) -> dict:
    """Enriquece un medicamento con imagen, precios, vademécum y farmacias (para uso en ThreadPoolExecutor)."""
    med["_query_original"] = query_original
    _enriquecer_imagen(med, med_raw_cache=med_raw_cache)
    info_precio = precios.obtener_precios(med["nregistro"])
    med["precio_oficial"] = info_precio["precio_oficial"]
    med["precio_medio"] = info_precio["precio_medio"]
    med["fuentes_precio"] = info_precio["fuentes"]
    med["vademecum_busqueda_url"] = vademecum.construir_url_busqueda(med["nombre"])
    # Vademecum y farmacias en paralelo
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_vad = pool.submit(
            vademecum.obtener_complemento,
            med["nombre"],
            med.get("principio_activo_texto", ""),
        )
        fut_far = pool.submit(_enriquecer_desde_farmacias, med)
        try:
            fut_vad.result()
        except Exception:
            pass
        try:
            fut_far.result()
        except Exception:
            med.setdefault("ofertas_farmacia", [])
    med.pop("_query_original", None)
    return med


_FILTROS_CIMA = {
    "sin_receta":   {"receta": 0, "comerc": 1},
    "medicamentos": {"comerc": 1},
}

_ETIQUETAS_FILTRO = {
    "sin_receta":   "Medicamentos sin receta",
    "medicamentos": "Todos los medicamentos",
    "parafarmacia": "Parafarmacia",
}


def _enriquecer_med_ligero(med: dict, med_raw_cache: dict | None = None) -> dict:
    """Enriquecimiento rápido: solo imagen y precio oficial (sin farmacias online)."""
    _enriquecer_imagen(med, med_raw_cache=med_raw_cache)
    info_precio = precios.obtener_precios(med["nregistro"])
    med["precio_oficial"] = info_precio["precio_oficial"]
    med["precio_medio"] = info_precio["precio_medio"]
    med["fuentes_precio"] = info_precio["fuentes"]
    med["vademecum_busqueda_url"] = vademecum.construir_url_busqueda(med["nombre"])
    med["ofertas_farmacia"] = []
    return med


def _aplicar_placeholder_imagen(productos: list[dict]) -> list[dict]:
    """Asegura que todos los productos de parafarmacia tienen imagen_url."""
    for p in productos:
        if not p.get("imagen_url"):
            p["imagen_url"] = _PRODUCT_PLACEHOLDER_URL
    return productos


def _ejecutar_busqueda_filtro(filtro: str, pagina: int, tam: int, tam_query: str) -> dict:
    """Ejecuta una búsqueda por categoría usando filtros (sin query textual).

    Usa paginación aleatoria para variar los resultados en cada clic.
    No cachea los resultados para garantizar la aleatoriedad.
    Solo hace enriquecimiento ligero (sin scraping de farmacias).
    """
    import random

    etiqueta = _ETIQUETAS_FILTRO.get(filtro, filtro)
    contexto = _contexto_busqueda_vacio(etiqueta, tam_query, tam, pagina=1)
    contexto["filtro_activo"] = filtro
    contexto["q"] = etiqueta

    if filtro == "parafarmacia":
        import random as _rnd
        productos = farmacia.buscar_productos_parafarmacia(limit=tam * 4)
        _rnd.shuffle(productos)
        contexto["productos_farmacia"] = _aplicar_placeholder_imagen(productos[:tam])
    elif filtro in _FILTROS_CIMA:
        # CIMA ignora tamanioPagina en búsquedas con filtros y devuelve ~200
        # resultados por página, pero solo las primeras ~5 páginas son válidas.
        # Usamos página aleatoria entre 1 y 5 y luego mezclamos para variar.
        pagina_aleatoria = random.randint(1, 5)

        datos = cima.buscar_con_filtros(_FILTROS_CIMA[filtro], pagina=pagina_aleatoria, tam=tam)
        if datos["resultados"]:
            random.shuffle(datos["resultados"])
            nregistros = [med["nregistro"] for med in datos["resultados"]]
            detalles_cache = cima.cargar_presentaciones_batch(nregistros)
            resultados_enriquecidos = []
            workers = min(len(datos["resultados"]), _MAX_WORKERS_ENRIQUECIMIENTO)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futuros = {executor.submit(_enriquecer_med_ligero, med, detalles_cache): med for med in datos["resultados"]}
                for futuro in as_completed(futuros):
                    try:
                        resultados_enriquecidos.append(futuro.result())
                    except Exception as exc:
                        med = futuros[futuro]
                        logging.warning("Error enriqueciendo %s: %s", med.get("nregistro"), exc)
                        med.setdefault("imagen_url", _PRODUCT_PLACEHOLDER_URL)
                        med.setdefault("precio_oficial", None)
                        med.setdefault("precio_medio", None)
                        med.setdefault("fuentes_precio", [])
                        med.setdefault("ofertas_farmacia", [])
                        resultados_enriquecidos.append(med)
            datos["resultados"] = resultados_enriquecidos
            datos["total"] = tam  # mostramos solo esta página, sin paginación
            datos["modo_todos"] = True
        contexto["datos"] = datos

    _actualizar_estado_resultados(contexto)
    contexto["busqueda_completa"] = True
    contexto["estado_busqueda"] = ""
    return contexto


def _registrar_busqueda(termino: str, tipo: str = "medicamento") -> None:
    """Guarda el término buscado en busqueda_log de forma asíncrona."""
    termino = termino.strip().lower()
    if not termino:
        return
    def _insertar():
        try:
            with database.db_session() as conn:
                conn.execute(
                    "INSERT INTO busqueda_log (termino, tipo) VALUES (?, ?)", (termino, tipo)
                )
        except Exception as exc:
            logging.debug("Error registrando búsqueda: %s", exc)
    threading.Thread(target=_insertar, daemon=True).start()


def _obtener_mas_buscados(dias: int = 7, limit: int = 10, tipo: str = "medicamento") -> list[str]:
    """Devuelve los términos más buscados en los últimos `dias` días."""
    try:
        with database.db_session() as conn:
            rows = conn.execute(
                """SELECT termino, COUNT(*) as cnt
                   FROM busqueda_log
                   WHERE created_at >= datetime('now', ? || ' days')
                     AND tipo = ?
                   GROUP BY termino
                   ORDER BY cnt DESC
                   LIMIT ?""",
                (f"-{dias}", tipo, limit),
            ).fetchall()
        return [row["termino"] for row in rows]
    except Exception as exc:
        logging.debug("Error obteniendo más buscados: %s", exc)
        return []


def _ejecutar_busqueda(q: str, pagina: int, tam: int, tam_query: str, on_update=None) -> dict:
    clave = _clave_cache(q, pagina, tam)
    contexto_cacheado = _obtener_de_cache(clave)
    if contexto_cacheado is not None:
        logging.debug("Caché hit: '%s' (pág. %d, tam. %d)", q, pagina, tam)
        if on_update:
            on_update(contexto_cacheado)
        return contexto_cacheado

    _registrar_busqueda(q)
    contexto = _contexto_busqueda_vacio(q, tam_query, tam, pagina=pagina)

    def emitir(estado: str | None = None):
        if estado is not None:
            contexto["estado_busqueda"] = estado
        if on_update:
            on_update(_crear_snapshot_busqueda(contexto))

    emitir("Buscando coincidencias en CIMA…")
    try:
        datos = cima.buscar_y_normalizar(q, pagina=pagina, tam=tam)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logging.warning("Error de red consultando CIMA para '%s': %s", q, exc)
        contexto["error_busqueda"] = "El servicio oficial de medicamentos no está disponible en este momento. Inténtalo de nuevo en unos instantes."
        contexto["busqueda_completa"] = True
        contexto["estado_busqueda"] = ""
        return contexto
    contexto["datos"] = {
        **datos,
        "resultados": [],
    }

    if datos["resultados"]:
        nregistros = [med["nregistro"] for med in datos["resultados"]]
        detalles_cache = cima.cargar_presentaciones_batch(nregistros)

        resultados_enriquecidos = []
        total_resultados = len(datos["resultados"])
        emitir(f"Preparando {total_resultados} resultados oficiales…")
        workers = min(total_resultados, _MAX_WORKERS_ENRIQUECIMIENTO)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futuros = {executor.submit(_enriquecer_med_completo, med, q, detalles_cache): med for med in datos["resultados"]}
            terminados = 0
            for futuro in as_completed(futuros):
                terminados += 1
                try:
                    resultados_enriquecidos.append(futuro.result())
                except Exception as exc:
                    med = futuros[futuro]
                    logging.warning("Error enriqueciendo %s: %s", med.get("nregistro"), exc)
                    med.setdefault("imagen_url", _PRODUCT_PLACEHOLDER_URL)
                    med.setdefault("precio_oficial", None)
                    med.setdefault("precio_medio", None)
                    med.setdefault("fuentes_precio", [])
                    med.setdefault("ofertas_farmacia", [])
                    resultados_enriquecidos.append(med)
                if total_resultados > 6 and terminados in {max(1, total_resultados // 2), total_resultados}:
                    emitir(f"Preparando {terminados}/{total_resultados} resultados oficiales…")

        contexto["datos"]["resultados"] = resultados_enriquecidos
        _actualizar_estado_resultados(contexto)

        contexto["busqueda_completa"] = True
        contexto["estado_busqueda"] = ""
        _guardar_en_cache(clave, contexto)
        return contexto

    emitir("Buscando en Nomenclátor y Vademécum…")
    with ThreadPoolExecutor(max_workers=2) as executor:
        fut_nom = executor.submit(nomenclator.buscar_productos, q, 8)
        fut_vad = executor.submit(vademecum.buscar_medicamentos, q, 8)

        try:
            coincidencias_nomenclator = fut_nom.result()
        except Exception as exc:
            logging.warning("Error en Nomenclátor: %s", exc)
            coincidencias_nomenclator = []
        try:
            coincidencias_vademecum = fut_vad.result()
        except Exception as exc:
            logging.warning("Error en Vademécum: %s", exc)
            coincidencias_vademecum = []

    contexto["coincidencias_nomenclator"] = coincidencias_nomenclator
    contexto["coincidencias_vademecum"] = coincidencias_vademecum
    contexto["productos_farmacia"] = []
    # Si no hay nada en ninguna fuente oficial, sugerir búsqueda en parafarmacia
    contexto["sugerir_parafarmacia"] = not coincidencias_nomenclator and not coincidencias_vademecum
    _actualizar_estado_resultados(contexto)

    contexto["busqueda_completa"] = True
    contexto["estado_busqueda"] = ""
    _guardar_en_cache(clave, contexto)
    return contexto


def _contexto_parafarmacia_vacio(q: str, tam_query: str, tam: int, pagina: int = 1) -> dict:
    return {
        "q": q,
        "tam_query": tam_query,
        "productos_farmacia": [],
        "parafarmacia_total": 0,
        "parafarmacia_pagina": pagina,
        "estado_busqueda": "Preparando búsqueda…",
        "busqueda_completa": False,
        "error_busqueda": "",
        "use_htmx": True,
        "hay_resultados": False,
        "datos": {"resultados": [], "total": 0, "pagina": pagina, "tam": tam, "modo_todos": False},
    }


def _ejecutar_busqueda_parafarmacia(q: str, pagina: int, tam: int, tam_query: str, on_update=None) -> dict:
    """Búsqueda exclusiva en scrapers de farmacias online + Amazon."""
    clave = f"para|{_clave_cache(q, pagina, tam)}"
    contexto_cacheado = _obtener_de_cache(clave)
    if contexto_cacheado is not None:
        if on_update:
            on_update(contexto_cacheado)
        return contexto_cacheado

    _registrar_busqueda(q, tipo="parafarmacia")
    contexto = _contexto_parafarmacia_vacio(q, tam_query, tam, pagina=pagina)

    def emitir(estado: str | None = None):
        if estado is not None:
            contexto["estado_busqueda"] = estado
        if on_update:
            on_update(_crear_snapshot_busqueda(contexto))

    emitir("Buscando en farmacias online…")
    try:
        todos_farmacia = farmacia.buscar_productos(q, _MAX_PARAFARMACIA)
    except Exception as exc:
        logging.warning("Error en farmacias: %s", exc)
        todos_farmacia = []

    offset = (pagina - 1) * tam
    contexto["productos_farmacia"] = _aplicar_placeholder_imagen(todos_farmacia[offset : offset + tam])
    contexto["parafarmacia_total"] = len(todos_farmacia)
    contexto["parafarmacia_pagina"] = pagina
    contexto["hay_resultados"] = bool(contexto["productos_farmacia"])
    contexto["busqueda_completa"] = True
    contexto["estado_busqueda"] = ""
    _guardar_en_cache(clave, contexto)
    return contexto


def _lanzar_busqueda_parafarmacia_async(job_id: str, q: str, pagina: int, tam: int, tam_query: str) -> None:
    try:
        ctx = _ejecutar_busqueda_parafarmacia(
            q, pagina, tam, tam_query,
            on_update=lambda c: _actualizar_busqueda_async(job_id, contexto=c),
        )
        _actualizar_busqueda_async(job_id, contexto=ctx, done=True)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logging.warning("Error de red en búsqueda parafarmacia async '%s': %s", q, exc)
        ctx_err = _contexto_parafarmacia_vacio(q, tam_query, tam, pagina=pagina)
        ctx_err["busqueda_completa"] = True
        ctx_err["error_busqueda"] = "El servicio no está disponible en este momento. Inténtalo de nuevo en unos instantes."
        ctx_err["estado_busqueda"] = ""
        _actualizar_busqueda_async(job_id, contexto=ctx_err, done=True, error=str(exc))
    except Exception as exc:
        ctx_err = _contexto_parafarmacia_vacio(q, tam_query, tam, pagina=pagina)
        ctx_err["busqueda_completa"] = True
        ctx_err["error_busqueda"] = str(exc)
        ctx_err["estado_busqueda"] = ""
        _actualizar_busqueda_async(job_id, contexto=ctx_err, done=True, error=str(exc))


def _lanzar_busqueda_async(job_id: str, q: str, pagina: int, tam: int, tam_query: str) -> None:
    try:
        contexto_final = _ejecutar_busqueda(
            q,
            pagina,
            tam,
            tam_query,
            on_update=lambda contexto: _actualizar_busqueda_async(job_id, contexto=contexto),
        )
        _actualizar_busqueda_async(job_id, contexto=contexto_final, done=True)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logging.warning("Error de red en búsqueda async '%s': %s", q, exc)
        contexto_error = _contexto_busqueda_vacio(q, tam_query, tam, pagina=pagina)
        contexto_error["busqueda_completa"] = True
        contexto_error["error_busqueda"] = "El servicio oficial de medicamentos no está disponible en este momento. Inténtalo de nuevo en unos instantes."
        contexto_error["estado_busqueda"] = ""
        _actualizar_busqueda_async(job_id, contexto=contexto_error, done=True, error=str(exc))
    except Exception as exc:
        contexto_error = _contexto_busqueda_vacio(q, tam_query, tam, pagina=pagina)
        contexto_error["busqueda_completa"] = True
        contexto_error["error_busqueda"] = str(exc)
        contexto_error["estado_busqueda"] = ""
        _actualizar_busqueda_async(job_id, contexto=contexto_error, done=True, error=str(exc))


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    tam_query, _ = _resolver_tam_pagina()
    if _es_htmx():
        return render_template("_home.html", tam_query=tam_query)
    return render_template("index.html", tam_query=tam_query)


@app.route("/img/<path:filename>")
def media_img(filename: str):
    return send_from_directory(BASE_DIR / "img", filename)


@app.route("/fuentes")
def fuentes():
    fuentes_enriquecidas = []
    for f in _FUENTES_CONSULTADAS:
        entrada: dict = dict(f)
        fid = f.get("fuente_id")
        if fid:
            entrada["envio"] = obtener_politica_envio(fid)
        fuentes_enriquecidas.append(entrada)
    return render_template("fuentes.html", fuentes=fuentes_enriquecidas)


@app.route("/verificar-precio")
def verificar_precio():
    """Verifica si el precio cobrado por un medicamento se ajusta al PVP oficial.

    Parámetros GET:
      - q:      nombre libre del medicamento
      - precio: precio cobrado en euros (float, separador decimal punto o coma)
    """
    q = request.args.get("q", "").strip()
    precio_raw = request.args.get("precio", "").replace(",", ".").strip()

    # Validación básica de entrada
    precio_cobrado: float | None = None
    if precio_raw:
        try:
            precio_cobrado = float(precio_raw)
            if precio_cobrado < 0 or precio_cobrado > 10_000:
                precio_cobrado = None
        except ValueError:
            precio_cobrado = None

    resultado: dict | None = None
    candidatos: list[dict] = []

    if q:
        # Si parece un Código Nacional (CN): solo dígitos de 5 a 7 caracteres
        if q.isdigit() and 5 <= len(q) <= 7:
            match = matcher_svc.buscar_por_cn(q)
            if match:
                resultado = match
            else:
                candidatos = matcher_svc.buscar_candidatos(q, limit=5)
        else:
            match = matcher_svc.match_producto(q)
            if match:
                resultado = match
                # Si la confianza no es "seguro", mostramos candidatos para que el usuario confirme
                if match["confianza"] != "seguro":
                    candidatos = matcher_svc.buscar_candidatos(q, limit=5)
            else:
                candidatos = matcher_svc.buscar_candidatos(q, limit=5)

    diferencia = _calcular_diferencia(resultado, precio_cobrado)

    # Query simplificada para buscar en parafarmacia: usar principio activo + dosis si están
    # disponibles en features, o bien las primeras palabras del nombre oficial (evita abreviaturas)
    q_parafarmacia = ""
    if resultado:
        feats = resultado.get("features") or {}
        partes = []
        if feats.get("principio_activo"):
            partes.append(feats["principio_activo"])
        elif resultado.get("nombre"):
            # Tomar las primeras 2-3 palabras del nombre oficial (antes de abreviaturas en mayúsculas)
            palabras = resultado["nombre"].split()
            partes = [p for p in palabras[:3] if not p.isupper() or len(p) <= 4]
            if not partes:
                partes = palabras[:2]
        if feats.get("dosis_mg"):
            partes.append(f"{feats['dosis_mg']} mg")
        q_parafarmacia = " ".join(partes).lower() if partes else (resultado.get("nombre") or "").lower()

    contexto = {
        "q": q,
        "precio_cobrado": precio_cobrado,
        "resultado": resultado,
        "candidatos": candidatos,
        "diferencia": diferencia,
        "q_parafarmacia": q_parafarmacia,
        "busqueda_por_cn": q.isdigit() and 5 <= len(q) <= 7,
    }

    if _es_htmx():
        return render_template("_verificar_resultado.html", **contexto)
    return render_template("verificar.html", **contexto)


def _calcular_diferencia(match: dict | None, precio_cobrado: float | None) -> dict | None:
    """Calcula la diferencia entre precio cobrado y PVP oficial. Reutilizado en verificar-precio y verificar-ticket."""
    if match is None or precio_cobrado is None or not match.get("pvp"):
        return None
    pvp = match["pvp"]
    diff = precio_cobrado - pvp
    pct = round(diff / pvp * 100, 1) if pvp > 0 else None
    tolerancia = 0.05
    if abs(diff) <= tolerancia:
        nivel = "ok"
    elif diff < -tolerancia:
        nivel = "inferior"
    elif pct is not None and pct > 10:
        nivel = "excesivo"
    else:
        nivel = "superior"
    return {
        "pvp_oficial": pvp,
        "precio_cobrado": precio_cobrado,
        "diff": round(diff, 2),
        "pct": pct,
        "nivel": nivel,
    }


@app.route("/verificar-ticket", methods=["GET", "POST"])
def verificar_ticket():
    """Modo ticket: auditoría multi-producto.

    GET  → formulario vacío
    POST → procesa lista de (q, precio) y devuelve resultados línea a línea
    """
    items: list[dict] = []
    resumen: dict | None = None

    if request.method == "POST":
        nombres = request.form.getlist("q")
        precios_raw = request.form.getlist("precio")

        for nombre, precio_raw in zip(nombres, precios_raw):
            nombre = nombre.strip()
            if not nombre:
                continue
            precio_cobrado: float | None = None
            if precio_raw:
                try:
                    precio_cobrado = float(precio_raw.replace(",", ".").strip())
                    if precio_cobrado < 0 or precio_cobrado > 10_000:
                        precio_cobrado = None
                except ValueError:
                    precio_cobrado = None

            match = matcher_svc.match_producto(nombre)
            diferencia = _calcular_diferencia(match, precio_cobrado)

            items.append({
                "q": nombre,
                "precio_cobrado": precio_cobrado,
                "match": match,
                "diferencia": diferencia,
            })

        if items:
            total = len(items)
            con_pvp = sum(1 for i in items if i["match"] and i["match"].get("pvp"))
            ok = sum(1 for i in items if i["diferencia"] and i["diferencia"]["nivel"] == "ok")
            inferior = sum(1 for i in items if i["diferencia"] and i["diferencia"]["nivel"] == "inferior")
            alertas = sum(
                1 for i in items
                if i["diferencia"] and i["diferencia"]["nivel"] in ("superior", "excesivo")
            )
            excesivos = sum(1 for i in items if i["diferencia"] and i["diferencia"]["nivel"] == "excesivo")
            sin_datos = sum(1 for i in items if not i["match"] or not i["match"].get("pvp"))
            peor = max(
                (
                    i for i in items
                    if i["diferencia"] and i["diferencia"]["nivel"] in ("superior", "excesivo")
                ),
                key=lambda i: i["diferencia"]["diff"],
                default=None,
            )
            resumen = {
                "total": total,
                "con_pvp": con_pvp,
                "ok": ok,
                "inferior": inferior,
                "alertas": alertas,
                "excesivos": excesivos,
                "sin_datos": sin_datos,
                "peor_q": peor["q"] if peor else None,
                "peor_diff": peor["diferencia"]["diff"] if peor else None,
                "peor_pct": peor["diferencia"]["pct"] if peor else None,
                "peor_nivel": peor["diferencia"]["nivel"] if peor else None,
            }

    if _es_htmx():
        return render_template("_ticket_resultado.html", items=items, resumen=resumen)
    return render_template("ticket.html", items=items, resumen=resumen)



@app.route("/mas-buscados")
def mas_buscados():
    """Devuelve el fragmento HTML con los términos más buscados (medicamentos) en 7 días."""
    terminos = _obtener_mas_buscados(dias=7, limit=10, tipo="medicamento")
    if not terminos:
        return '<span class="text-muted" style="font-size:0.85rem">Aún no hay búsquedas registradas esta semana.</span>'
    use_htmx = _es_htmx()
    fragmentos = []
    for termino in terminos:
        etiqueta = termino.capitalize()
        htmx_attrs = (
            f'hx-get="/buscar/progresivo?q={termino}" hx-target="#resultados" hx-push-url="/buscar?q={termino}"'
            if use_htmx else ""
        )
        fragmentos.append(
            f'<a href="/buscar?q={termino}" {htmx_attrs}>{etiqueta}</a>'
        )
    return "\n".join(fragmentos)


@app.route("/mas-buscados/parafarmacia")
def mas_buscados_parafarmacia():
    """Devuelve el fragmento HTML con los términos más buscados en parafarmacia en 7 días."""
    terminos = _obtener_mas_buscados(dias=7, limit=10, tipo="parafarmacia")
    if not terminos:
        return '<span class="text-muted" style="font-size:0.85rem">Aún no hay búsquedas registradas esta semana.</span>'
    fragmentos = []
    for termino in terminos:
        etiqueta = termino.capitalize()
        fragmentos.append(
            f'<a href="/parafarmacia/buscar?q={termino}">{etiqueta}</a>'
        )
    return "\n".join(fragmentos)


def _resolver_precio_mostrador() -> float | None:
    """Extrae y valida el precio de mostrador del query string."""
    raw = request.args.get("precio_mostrador", "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


@app.route("/buscar")
def buscar():
    """Búsqueda de medicamentos.  Responde HTML parcial si es petición HTMX."""
    q = request.args.get("q", "").strip()
    filtro = request.args.get("filtro", "").strip()
    pagina = request.args.get("pagina", 1, type=int)
    tam_query, tam = "10", 10
    precio_mostrador = _resolver_precio_mostrador()

    if filtro and filtro in _ETIQUETAS_FILTRO:
        contexto = _ejecutar_busqueda_filtro(filtro, pagina, tam, tam_query)
        contexto["precio_mostrador"] = precio_mostrador
        if _es_htmx():
            return render_template("_resultados.html", **contexto)
        return render_template("buscar.html", **contexto)

    if not q:
        if _es_htmx():
            return "<p class='text-muted text-center'>Escribe el nombre de un medicamento.</p>"
        return render_template("index.html", tam_query=tam_query)

    contexto = _ejecutar_busqueda(q, pagina, tam, tam_query)
    contexto["precio_mostrador"] = precio_mostrador

    if _es_htmx():
        return render_template(
            "_resultados.html",
            **contexto,
        )

    return render_template(
        "buscar.html",
        **contexto,
    )


@app.route("/buscar/progresivo")
def buscar_progresivo():
    q = request.args.get("q", "").strip()
    pagina = request.args.get("pagina", 1, type=int)
    tam_query, tam = "10", 10
    precio_mostrador = _resolver_precio_mostrador()

    if not q:
        return "<p class='text-muted text-center'>Escribe el nombre de un medicamento.</p>"

    _limpiar_busquedas_async()
    job_id = uuid.uuid4().hex
    contexto = _contexto_busqueda_vacio(q, tam_query, tam, pagina=pagina)
    contexto["precio_mostrador"] = precio_mostrador
    with _BUSQUEDAS_ASYNC_LOCK:
        _BUSQUEDAS_ASYNC[job_id] = {
            "contexto": _crear_snapshot_busqueda(contexto),
            "precio_mostrador": precio_mostrador,
            "created_at": time.time(),
            "updated_at": time.time(),
            "done": False,
            "error": "",
        }

    thread = threading.Thread(
        target=_lanzar_busqueda_async,
        args=(job_id, q, pagina, tam, tam_query),
        daemon=True,
    )
    thread.start()

    push_url = url_for("buscar", q=q, pagina=pagina, tam=tam_query)
    if precio_mostrador is not None:
        push_url += f"&precio_mostrador={precio_mostrador}"
    response = make_response(
        render_template(
            "_resultados_progresivos.html",
            job_id=job_id,
            **contexto,
        )
    )
    response.headers["HX-Push-Url"] = push_url
    return response


@app.route("/buscar/progreso/<job_id>")
def progreso_busqueda(job_id: str):
    _limpiar_busquedas_async()
    with _BUSQUEDAS_ASYNC_LOCK:
        trabajo = _BUSQUEDAS_ASYNC.get(job_id)

    if not trabajo:
        contexto = _contexto_busqueda_vacio("", "25", 25)
        contexto["busqueda_completa"] = True
        contexto["error_busqueda"] = "La búsqueda ha caducado. Vuelve a intentarlo."
        contexto["precio_mostrador"] = None
        return render_template("_resultados_progresivos.html", job_id=job_id, **contexto)

    ctx = trabajo["contexto"]
    ctx["precio_mostrador"] = trabajo.get("precio_mostrador")
    return render_template(
        "_resultados_progresivos.html",
        job_id=job_id,
        **ctx,
    )


@app.route("/medicamento/<nregistro>")
def detalle(nregistro: str):
    """Detalle de un medicamento concreto."""
    med_raw = cima.detalle_medicamento(nregistro)
    if med_raw is None:
        return render_template("404.html"), 404

    # Guardar presentaciones (cn→nregistro) para cruzar con precios
    cima.guardar_presentaciones(nregistro, med_raw)

    med = cima._extraer_datos(med_raw)
    # Reutilizar med_raw ya descargado para no repetir petición a CIMA
    _enriquecer_imagen(med, med_raw_cache={nregistro: med_raw})

    info_precio = precios.obtener_precios(nregistro)
    med["precio_oficial"] = info_precio["precio_oficial"]
    med["precio_medio"] = info_precio["precio_medio"]
    med["fuentes_precio"] = info_precio["fuentes"]
    med["vademecum_busqueda_url"] = vademecum.construir_url_busqueda(med["nombre"])

    # Derivar un query sintético a partir del nombre del medicamento para
    # que _consultas_farmacia_alternativas disponga de un término de búsqueda
    # corto (como lo tendría desde la búsqueda del usuario).
    nombre_corto = med["nombre"].split()[0] if med.get("nombre") else ""
    med["_query_original"] = nombre_corto

    # CN del primer presentación para consultar BIFIMED
    _cn_bifimed = next(
        (p["cn"] for p in med.get("presentaciones", []) if p.get("cn")),
        None,
    )

    # Ejecutar vademecum, farmacias y BIFIMED en paralelo
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_vad = pool.submit(
            vademecum.obtener_complemento,
            med["nombre"],
            med.get("principio_activo_texto", ""),
        )
        fut_far = pool.submit(_enriquecer_desde_farmacias, med)
        fut_bif = pool.submit(bifimed.situacion_por_cn, _cn_bifimed) if _cn_bifimed else None

        try:
            med["vademecum"] = fut_vad.result()
        except Exception:
            med["vademecum"] = None
        try:
            fut_far.result()
        except Exception:
            med.setdefault("ofertas_farmacia", [])
        med["situacion_financiacion"] = None
        if fut_bif is not None:
            try:
                med["situacion_financiacion"] = fut_bif.result()
            except Exception:
                pass

    med.pop("_query_original", None)

    comparativa = {
        "ofertas": med.get("ofertas_farmacia") or [],
        "mejor_oferta": (med.get("ofertas_farmacia") or [None])[0] if med.get("ofertas_farmacia") else None,
        "precio_minimo": min((oferta["precio"] for oferta in med.get("ofertas_farmacia", []) if oferta.get("precio") is not None), default=None),
        "precio_maximo": max((oferta["precio"] for oferta in med.get("ofertas_farmacia", []) if oferta.get("precio") is not None), default=None),
        "numero_fuentes": len(med.get("ofertas_farmacia") or []),
    }
    med["mejor_oferta_farmacia"] = comparativa["mejor_oferta"]
    med["precio_minimo_farmacia"] = comparativa["precio_minimo"]
    med["precio_maximo_farmacia"] = comparativa["precio_maximo"]
    med["numero_fuentes_farmacia"] = comparativa["numero_fuentes"]
    med["ahorro_vs_sns"] = None
    if med["precio_oficial"] is not None and med["mejor_oferta_farmacia"] and med["mejor_oferta_farmacia"].get("precio") is not None:
        med["ahorro_vs_sns"] = round(med["precio_oficial"] - med["mejor_oferta_farmacia"]["precio"], 2)

    # Confianza de coincidencia: viene de URL cuando el usuario llega desde el verificador de precio
    _confianza_param = request.args.get("confianza", "").strip()
    if _confianza_param in ("seguro", "probable", "debil"):
        med["confianza_busqueda"] = _confianza_param

    if _es_htmx():
        return render_template("_detalle.html", med=med)

    return render_template("detalle.html", med=med)


@app.route("/parafarmacia/buscar")
def buscar_parafarmacia():
    """Búsqueda exclusiva de productos de parafarmacia en farmacias online."""
    q = request.args.get("q", "").strip()
    pagina = request.args.get("pagina", 1, type=int)
    tam_query, tam = _resolver_tam_pagina()

    if not q:
        if _es_htmx():
            return "<p class='text-muted text-center'>Escribe el nombre de un producto.</p>"
        return render_template("parafarmacia_buscar.html", q="", tam_query=tam_query,
                               productos_farmacia=[], hay_resultados=False,
                               busqueda_completa=True, estado_busqueda="", error_busqueda="")

    contexto = _ejecutar_busqueda_parafarmacia(q, pagina, tam, tam_query)
    if _es_htmx():
        return render_template("_resultados_parafarmacia.html", **contexto)
    return render_template("parafarmacia_buscar.html", **contexto)


@app.route("/parafarmacia/buscar/progresivo")
def buscar_parafarmacia_progresivo():
    q = request.args.get("q", "").strip()
    pagina = request.args.get("pagina", 1, type=int)
    tam_query, tam = _resolver_tam_pagina()

    if not q:
        return "<p class='text-muted text-center'>Escribe el nombre de un producto.</p>"

    _limpiar_busquedas_async()
    job_id = uuid.uuid4().hex
    contexto = _contexto_parafarmacia_vacio(q, tam_query, tam, pagina=pagina)
    with _BUSQUEDAS_ASYNC_LOCK:
        _BUSQUEDAS_ASYNC[job_id] = {
            "contexto": _crear_snapshot_busqueda(contexto),
            "created_at": time.time(),
            "updated_at": time.time(),
            "done": False,
            "error": "",
        }

    threading.Thread(
        target=_lanzar_busqueda_parafarmacia_async,
        args=(job_id, q, pagina, tam, tam_query),
        daemon=True,
    ).start()

    push_url = url_for("buscar_parafarmacia", q=q, pagina=pagina, tam=tam_query)
    response = make_response(
        render_template("_resultados_parafarmacia_progresivos.html", job_id=job_id, **contexto)
    )
    response.headers["HX-Push-Url"] = push_url
    return response


@app.route("/parafarmacia/buscar/progreso/<job_id>")
def progreso_busqueda_parafarmacia(job_id: str):
    _limpiar_busquedas_async()
    with _BUSQUEDAS_ASYNC_LOCK:
        trabajo = _BUSQUEDAS_ASYNC.get(job_id)

    if not trabajo:
        ctx = _contexto_parafarmacia_vacio("", "25", 25)
        ctx["busqueda_completa"] = True
        ctx["error_busqueda"] = "La búsqueda ha caducado. Vuelve a intentarlo."
        return render_template("_resultados_parafarmacia_progresivos.html", job_id=job_id, **ctx)

    return render_template("_resultados_parafarmacia_progresivos.html", job_id=job_id, **trabajo["contexto"])


@app.route("/parafarmacia/<object_id>")
def detalle_parafarmacia_legacy(object_id: str):
    return detalle_parafarmacia("dosfarma", object_id)


@app.route("/parafarmacia/<fuente>/<object_id>")
def detalle_parafarmacia(fuente: str, object_id: str):
    """Detalle de un producto de parafarmacia obtenido desde Dosfarma."""
    producto = farmacia.obtener_producto_con_comparativa(object_id, fuente=fuente)
    if producto is None:
        return render_template("404.html"), 404

    if _es_htmx():
        return render_template("_parafarmacia.html", prod=producto)

    return render_template("parafarmacia.html", prod=producto)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _es_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


# ---------------------------------------------------------------------------
# Manejadores de error
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def pagina_no_encontrada(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def error_interno(e):
    return render_template("404.html", error_500=True), 500


# ---------------------------------------------------------------------------
# CLI: importar nomenclátor
# ---------------------------------------------------------------------------

@app.cli.command("importar-nomenclator")
def cli_importar_nomenclator():
    """Descarga e importa el Nomenclátor del SNS."""
    from services.nomenclator import importar_nomenclator
    importar_nomenclator()


if __name__ == "__main__":
    app.run(debug=DEBUG, host="0.0.0.0", port=5000)

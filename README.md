# 💊 FarmaFLUSH — Verificador de precio de medicamentos en España

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![Framework: Flask](https://img.shields.io/badge/framework-Flask-lightgrey.svg)

Verificador de precios de medicamentos (PVP Oficial) y comparador de parafarmacia.

FarmaFLUSH es una aplicación web (PWA) diseñada para estar informados sobre "posibles" irregularidades en el cobro de medicamentos. La herramienta permite verificar en segundos si el precio cobrado coincide con el PVP máximo oficial regulado por el Ministerio de Sanidad (Nomenclátor).

**Propósito del proyecto**

Este proyecto nace de una necesidad real y personal: garantizar que el derecho a la salud no se vea afectado por "errores" en el redondeo o sobreprecios aplicados a medicamentos con precio regulado. FarmaFLUSH utiliza datos abiertos y técnicas de agregación de datos para ofrecer transparencia inmediata al ciudadano.

---

## Propuesta de valor

| Lo que NO hacemos | Lo que SÍ somos |
|---|---|
| ❌ Comparar y buscar "el más barato" | ✅ Verificador de precio correcto vs oficial |
| ❌ Agregador de ofertas | ✅ Referencia al PVP máximo intervenido por ley |
| ❌ Acusar a nadie | ✅ Educamos + alertamos con fuente oficial |

**Primaria:** ¿El precio que me han cobrado coincide con el PVP oficial del Nomenclátor SNS?  
**Secundaria:** Comparativa de precios de parafarmacia en farmacias online.

---

## Arquitectura de producto

FarmaFLUSH tiene **dos capas funcionalmente separadas** que nunca se mezclan en la misma pantalla:

| Capa | Rutas | Fuente de verdad | Tipo de precio |
|------|-------|------------------|----------------|
| 🏛️ **Verificador** | `/verificar-precio` · `/verificar-ticket` | Nomenclátor SNS (AEMPS) | Regulado (PVP máximo intervenido) |
| 🧴 **Comparador** | `/parafarmacia` | Farmacias online (scraping) | Libre (mercado) |

**Regla de oro:** el verificador nunca muestra precios de farmacias online en cuanto a medicamentos se refiere; el comparador nunca menciona el PVP oficial del SNS como referencia de comparación.

El puente entre ambas capas es un enlace discreto al pie del resultado del verificador: _"¿Buscas este producto en farmacias online? → Ver opciones de compra"_. Así el usuario accede al comparador si lo desea, pero los dos contextos (regulado vs libre) permanecen visualmente y conceptualmente separados.

---

## Stack

| Capa | Tecnología |
| ---- | ---------- |
| Backend | **Flask** (Python 3.10+) |
| Frontend | **HTMX** + **Pico CSS** (responsive, sin JS pesado) |
| Base de datos | **SQLite** (WAL mode) |
| Matching | **FTS5** + **rapidfuzz** (hybrid matcher) |
| Fuentes principales | CIMA (AEMPS) REST API · Nomenclátor SNS (PVP oficial) · Vademécum · BIFIMED (Ministerio de Sanidad) |
| Fuentes online | Dosfarma · Farmacia Tedin · Farmacias Direct · Castrofarma · Farmacia Barata · FarmaGalicia · OpenFarma · Farmacia Pontevea · Gomezulla |

### Exclusión voluntaria (Opt-out)

Este proyecto respeta la voluntad de los comercios analizados. 

Aunque la aplicación utiliza datos públicos y accesibles por cualquier usuario en la web, ofrecemos a los titulares de las farmacias la posibilidad de ser eliminados del motor de búsqueda. 

Para solicitar la retirada de su catálogo de nuestra base de datos, dirígase a la sección "**Aviso para titulares de farmacias**", situada al final de esta documentación.


## Configuración antes de usar

Copia `.env.example` a `.env` y ajusta los siguientes valores **antes** de arrancar la aplicación, especialmente en producción:

| Variable | Descripción | Acción requerida |
|----------|-------------|-----------------|
| `FLASK_SECRET_KEY` | Clave usada para firmar cookies de sesión de Flask. El valor por defecto es inseguro. | **Obligatorio cambiar.** Genera una cadena aleatoria segura, por ejemplo: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `FLASK_DEBUG` | Activa el modo debug y el Debugger PIN de Werkzeug. | **Debe ser `0` en producción.** Dejarlo en `1` expone la consola interactiva de depuración a cualquier visitante. |
| `DATABASE_PATH` | Ruta al archivo SQLite. Por defecto `data/pildora.db`. | Opcional. Cambia solo si quieres almacenar la BD en otra ubicación. |
| `NOMENCLATOR_CSV_URL` | URL del CSV del Nomenclátor SNS. | No es necesario cambiarla salvo que la URL oficial varíe. |
| `CIMA_API_BASE` | Base URL de la API REST de CIMA (AEMPS). | No es necesario cambiarla salvo que cambie el endpoint oficial. |

---

## Arranque rápido

```bash
# 1. Entorno virtual
python3 -m venv .venv && source .venv/bin/activate

# 2. Dependencias
pip install -r requirements.txt

# 3. Configuración
cp .env.example .env
# Edita .env: cambia FLASK_SECRET_KEY y establece FLASK_DEBUG=0

# 4. Arrancar (importa el Nomenclátor automáticamente en el primer arranque)
python app.py
# → http://127.0.0.1:5000
```

## Producción (VPS)

```bash
gunicorn app:app -b 0.0.0.0:5000 -w 2
```

Configura un reverse proxy (nginx/caddy) delante para HTTPS.

---

## Arquitectura

```text
FarmaFLUSH/
├── app.py                  # Aplicación Flask principal
├── config.py               # Configuración desde .env
├── database.py             # SQLite: esquema + helpers (incluye FTS5)
├── requirements.txt
├── services/
│   ├── matcher.py                   # Hybrid matcher: FTS5 + rapidfuzz + bonuses/penalties
│   ├── cima.py                      # Cliente API REST de CIMA (AEMPS)
│   ├── farmacia.py                  # Orquestador de fuentes de farmacias online
│   ├── farmacia_dosfarma.py         # Fuente: Dosfarma (Algolia)
│   ├── farmacia_tedin.py            # Fuente: Farmacia Tedin (Prestashop)
│   ├── farmacia_farmaciasdirect.py  # Fuente: Farmacias Direct (Shopify)
│   ├── farmacia_castrofarma.py      # Fuente: Castrofarma (Magento 2)
│   ├── farmacia_farmaciabarata.py   # Fuente: Farmacia Barata (Prestashop)
│   ├── farmacia_farmagalicia.py     # Fuente: FarmaGalicia (Magento 2)
│   ├── farmacia_openfarma.py        # Fuente: OpenFarma (Prestashop 1.6)
│   ├── farmacia_pontevea.py         # Fuente: Farmacia Pontevea (Prestashop)
│   ├── farmacia_gomezulla.py        # Fuente: Gomezulla en tu Piel (Prestashop)
│   ├── bifimed.py                   # Scraper situación de financiación (BIFIMED / Mº Sanidad)
│   ├── nomenclator.py               # Importador del Nomenclátor SNS
│   ├── precios.py                   # Motor de agregación de precios
│   └── vademecum.py                 # Complemento informativo
├── static/
│   └── img/
├── templates/
│   ├── base.html                        # Layout responsive (Pico CSS + HTMX)
│   ├── index.html                       # Página principal
│   ├── buscar.html                      # Resultados de búsqueda
│   ├── detalle.html                     # Detalle de medicamento
│   ├── parafarmacia.html                # Ficha de parafarmacia
│   ├── verificar.html                   # Verificador precio individual
│   ├── ticket.html                      # Verificador multi-producto (modo ticket)
│   ├── fuentes.html                     # Fuentes consultadas
│   ├── favoritos.html                   # Medicamentos favoritos del usuario
│   ├── _search_form.html                # Formulario de búsqueda (fragmento HTMX)
│   ├── _resultados.html                 # Tarjetas de resultados (fragmento HTMX)
│   ├── _resultados_progresivos.html     # Polling progresivo
│   ├── _detalle.html                    # Detalle (fragmento HTMX)
│   ├── _parafarmacia.html               # Parafarmacia (fragmento HTMX)
│   ├── _verificar_resultado.html        # Resultado verificador individual (fragmento HTMX)
│   ├── _ticket_resultado.html           # Resultado modo ticket (fragmento HTMX)
│   ├── _favoritos_tarjetas.html         # Tarjetas de favoritos (fragmento renderizado por API)
│   └── 404.html
├── data/                    # Base de datos SQLite
└── img/                     # Imágenes descargadas de CIMA
```

---

## 🗄️ Base de datos — `pildora.db`

`pildora.db` es una **caché local de datos oficiales**, no una base de datos propia de la aplicación. Todos los datos provienen de fuentes externas (Ministerio de Sanidad, AEMPS); la BD solo evita repetir las mismas peticiones HTTP en cada búsqueda del usuario.

### Tablas y su propósito

| Tabla | Origen de los datos | Para qué se usa |
|-------|---------------------|-----------------|
| `nomenclator_producto` | CSV del Nomenclátor SNS (sanidad.gob.es) | Índice de búsqueda de medicamentos por nombre/CN |
| `precio` (fuente `nomenclator`) | CSV del Nomenclátor SNS | PVP máximo intervenido por ley que muestra el verificador |
| `medicamento` | API REST CIMA (AEMPS) | Ficha del medicamento: laboratorio, forma, dosis, ficha técnica… |
| `presentacion` | API REST CIMA (AEMPS) | Relación CN ↔ nregistro, nombre de presentación |
| `precio` (otras fuentes) | Farmacias online (scrapers) | Precios de parafarmacia para comparativa |
| `medicamento_features` | Generada desde `nomenclator_producto` | Índice del matcher híbrido (FTS5 + rapidfuzz): nombre normalizado, dosis, unidades, principio activo |
| `busqueda_log` | Términos tecleados por usuarios | Sección "más buscados" de cada buscador (últimos 7 días); columna `tipo` distingue `medicamento` vs `parafarmacia` |
| `importacion` | Registro interno | Auditoría de importaciones del Nomenclátor (fecha, nº registros, errores) |

### Ciclo de vida de los datos

```
Primer arranque
  └─ tabla precio vacía? → descarga CSV (~20.000 filas) de sanidad.gob.es
                         → importa nomenclator_producto + precio(nomenclator)
                         → genera medicamento_features para el matcher

Búsqueda de medicamento
  └─ consulta SQLite (FTS5) → milisegundos, sin petición HTTP
  └─ si la ficha CIMA no está en BD o tiene > 24 h → petición a cima.aemps.es
                                                    → almacena en medicamento + presentacion

Renovar Nomenclátor (datos cambian mensualmente)
  └─ flask importar-nomenclator  (o reiniciar con tabla vacía)
```

### Por qué no se consulta el Ministerio en cada búsqueda

El CSV del Nomenclátor tiene ~20.000 filas y pesa varios MB. Descargarlo y parsearlo en cada petición implicaría:
- **Latencia de 2-10 s** por búsqueda (dependiendo de la conexión a sanidad.gob.es)
- **Dependencia total** de la disponibilidad del servidor externo
- **Carga innecesaria** sobre un servidor público

Con la caché en SQLite, cada búsqueda responde en milisegundos y la aplicación funciona aunque sanidad.gob.es esté temporalmente inaccesible.

> Los datos del Nomenclátor se actualizan mensualmente. Para renovar la caché: `flask importar-nomenclator` (o borra `data/pildora.db` y reinicia).

---

## 🎯 Funcionalidades implementadas

### 1. Verificador de precio individual (`/verificar-precio`)

El usuario introduce el nombre de un medicamento y el precio cobrado. El sistema:

1. Localiza el medicamento en el Nomenclátor SNS mediante el **hybrid matcher** (FTS5 + rapidfuzz)
2. Muestra el **PVP oficial** (precio máximo intervenido por ley)
3. Emite un veredicto con cuatro niveles:

| Nivel | Condición | Mensaje mostrado al usuario |
|-------|-----------|-------------------|
| ✅ OK | `\|diff\| ≤ 0,05 €` | **Has pagado el precio correcto** |
| ℹ️ Inferior | `diff < -0,05 €` | **Has pagado X € menos del PVP oficial** (posible copago o descuento) |
| ⚠️ Superior | `0,05 € < diff ≤ 10%` | **Pagaste X € más del precio oficial** |
| 💸 Diferencia elevada | `diff > 10% del PVP` | **Podrías haber pagado hasta X € de más** |

4. Muestra un **badge de confianza prominente** (ALTA / MEDIA / BAJA) en el resultado.
5. Si la confianza es `probable` (score 80-91) o `débil` (score < 80), el sistema **no muestra el veredicto directamente**: presenta los candidatos más cercanos para que el usuario confirme cuál corresponde antes de ver la comparativa de precios.

### 2. Modo ticket — auditoría multi-producto (`/verificar-ticket`)

El usuario introduce varios productos de su ticket:

```
Ibuprofeno 600mg 20 comp.   →  3,20 €   ✅ OK
Paracetamol 1g 20 comp.     →  2,80 €   ✅ OK
Nolotil 575mg 10 cáp.       →  4,10 €   🚨 +28%
```

- Formulario dinámico: añade/elimina filas sin recarga
- Resumen ejecutivo en formato legible: `✅ 2 correctos · ℹ️ 1 inferior al oficial · 🚨 1 con diferencia elevada`
- Tabla de detalle con fila explicativa expandida en cada alerta
- Lenguaje factual sin acusaciones: "diferencia elevada respecto al PVP oficial" en lugar de términos absolutos
- Procesado completo vía HTMX (sin recarga de página)

**Trazabilidad de la anomalía:**

El modo ticket permite distinguir entre un error puntual y un patrón sistemático. Si de 6 productos auditados 5 presentan diferencia elevada, el resumen lo refleja de forma visible. Esto es relevante porque un TPV mal configurado o una base de precios desactualizada afectará de forma consistente a todos los productos dispensados, no solo a uno. El sistema no identifica farmacias concretas ni emite juicios de intencionalidad: únicamente agrega y visualiza la comparativa para que el usuario pueda valorar si escalar la consulta.

### 3. Hybrid matcher (FTS5 + rapidfuzz)

Pipeline de identificación de medicamentos por texto libre:

```
Texto libre → normalizar (sin números, sin stopwords)
           → FTS5 LIMIT 60 (tokens alfabéticos, OR join)
           → rapidfuzz token_set_ratio sobre nombre_norm
           → bonuses: dosis (+15), unidades (+10), forma (+8), PA en query (+10)
           → penalty: PA conocido pero ausente en query (−20)
           → sort sin clamp → top result clamp [0,100]
           → confianza: seguro ≥92 · probable ≥80 · débil <80
```

**Comportamiento por nivel de confianza:**

---

## Frontend y rendimiento (actualización)

Se ha realizado una limpieza de arquitectura en frontend para mejorar mantenibilidad y rendimiento percibido:

### Separación de responsabilidades

- `templates/base.html` se mantiene como layout limpio (estructura + enlaces a assets)
- Estilos movidos a `static/css/style.css`
- Lógica cliente movida a `static/js/app.js`

### Beneficio de caché del navegador

Al externalizar CSS y JS, el navegador puede cachear estos archivos y reutilizarlos entre páginas, reduciendo tiempos de carga y mejorando la navegación interna.

### HTMX: indicador de carga unificado

La aplicación usa un único indicador de progreso global:

- En `base.html`: `hx-indicator="#global-progress"`
- Se eliminó el segundo sistema de progreso para evitar duplicidad visual y lógica

### Inicialización JS y rendimiento en scroll

- Toda la lógica DOM se inicializa bajo `DOMContentLoaded` en `app.js`
- Se aplica `requestAnimationFrame` throttle (`rafThrottle`) en listeners de scroll para evitar recalcular en cada píxel
- Este ajuste se usa en el botón "Subir arriba" y en el modo de cabecera compacta (`body.logo-compacto`)

### PWA (recordatorio)

La app incluye soporte PWA con:

- `static/manifest.json`
- `static/sw.js` (Service Worker)
- Iconos instalables en `static/img/icon-192.png` y `static/img/icon-512.png`

Además, en móvil se muestra un banner de instalación con botón de acción:

- Si el navegador expone `beforeinstallprompt` (Android/Chrome), el botón abre el prompt nativo de instalación.
- En iOS/Safari, el botón muestra instrucciones para "Añadir a pantalla de inicio".
- Si el usuario cierra el banner o rechaza el prompt, el aviso se pausa y vuelve a mostrarse a los 7 días.
- Si la app se instala (`appinstalled`), el banner deja de mostrarse.

Para instalación en móvil en entorno real, es necesario HTTPS en producción.

| Confianza | Score | Comportamiento |
|-----------|-------|----------------|
| `seguro` | ≥ 92 | Muestra veredicto directo con badge ALTA |
| `probable` | 80-91 | Muestra candidatos para confirmar (badge MEDIA) |
| `débil` | < 80 | Muestra candidatos para confirmar (badge BAJA) |

Resultados de prueba:
- `ibuprofeno 600mg 20 comprimidos` → IBUPROFENO ARISTO 600mg | score 100 seguro
- `paracetamol 1g 20 comprimidos` → PARACETAMOL KERN PHARMA 1G | score 100 seguro
- `nolotil 575mg 10 capsulas` → NOLOTIL 575MG 10 CAPSULAS | score 100 seguro
- `lorazepam 1mg 50 comprimidos` → LORAZEPAM CINFA 1mg | score 100 seguro

### 4. Situación de financiación del SNS (BIFIMED)

Cada ficha de medicamento muestra un **badge de financiación** con la situación oficial según el [BIFIMED](https://www.sanidad.gob.es/profesionales/medicamentos.do?metodo=buscarMedicamentos) del Ministerio de Sanidad. La consulta se lanza en paralelo con las demás fuentes al cargar la ficha.

| Badge | Color | Significado |
|-------|-------|-------------|
| **Financiado SNS** | Verde | Cubierto total o parcialmente por el SNS (`Sí` / `Sí para determinadas indicaciones/condiciones`) |
| **No financiado** | Naranja | No incluido en la financiación pública |
| **Excluido** | Rojo | Explícitamente retirado de la financiación por resolución |

La situación se obtiene scrapeando el formulario BIFIMED en dos pasos (GET de cookies + POST con el CN del medicamento) y se cachea en memoria durante 24 horas.

> **Nota sobre `precio_oficial`:** solo los medicamentos presentes en el Nomenclátor SNS tienen PVP máximo intervenido. El precio obtenido de Vademécum como fallback se trata exclusivamente como precio de referencia medio, nunca como precio oficial regulado.

### 5. Búsqueda de medicamentos y parafarmacia

- Consulta CIMA (AEMPS) en tiempo real + cruce con Nomenclátor SNS
- Filtrado por marca, forma farmacéutica, dosis, tipo de dispensación
- Búsqueda asíncrona con polling progresivo (sin bloqueo de UI)
- Caché de 12 horas por `consulta + página + tamaño`

### 5. Comparativa de precios online (parafarmacia)

- Búsqueda paralela en 9 farmacias online españolas
- El usuario puede o no introducir su precio de mostrador para comparar
- Transparencia de fuentes: indica explícitamente qué farmacias no tienen el producto
- Enlace directo a la ficha de cada producto en cada farmacia

### 6. Filtrado automático de medicamentos en parafarmacia

Los scrapers de farmacias online devuelven en ocasiones medicamentos regulados (comprimidos, geles con concentración, etc.) mezclados con productos de parafarmacia. El sistema aplica dos capas de filtrado en `services/farmacia.py`:

**Capa 1 — regex (O(1)):** detecta formas farmacéuticas exclusivas (comprimidos, cápsulas, sobres, colirio…), dosis en concentración (`50mg/g`, `10mg/ml`, `2% gel`) y sufijos EFG/ECG. Filtra ~95 % de los casos.

**Capa 2 — CN lookup (cacheado):** si la capa 1 no filtra, consulta el Nomenclátor SNS vía `match_producto`. Si la coincidencia es `seguro` (score ≥ 92), el producto se descarta como medicamento regulado. Resultado cacheado con `lru_cache(maxsize=512)` para minimizar accesos a BD.

### 7. UX — Navegación y consistencia visual

- Botones de navegación (`← Inicio`, acciones secundarias) con estilo Pico CSS `role="button"` consistente en todas las páginas del verificador
- **Quick-filters en los buscadores:** debajo del formulario de búsqueda aparecen dos botones — «🧾 Verificar ticket» (enlace directo) y «🔥 Más buscados» (despliega panel con los términos más buscados en los últimos 7 días). Los registros de "Más buscados" están separados por tipo: el buscador de medicamentos llama a `/mas-buscados` y el de parafarmacia llama a `/mas-buscados/parafarmacia`, de modo que cada sección muestra sus propias tendencias de búsqueda.
- Los resultados del buscador de medicamentos muestran siempre 10 resultados (fijo, sin selector).
- Badge de confianza como pill coloreada prominente en el encabezado del resultado (verde ALTA / amarillo MEDIA / rojo BAJA)
- Candidatos presentados como tarjetas clicables con el sugerido destacado
- **Banners de navegación cruzada:** en el buscador de medicamentos aparece un banner «💄 ¿Buscas parafarmacia?» y viceversa, para que el usuario pueda cambiar de sección sin perder el contexto de búsqueda
- **Header sticky con accesos directos:** al hacer scroll, el header compacto muestra los botones «💊 Medicamentos» y «💄 Parafarmacia» junto al mini-logo; el logo cambia contextualmente según la sección: `farmaflus100.png` en medicamentos/verificador, `farmaflusparafarmacia.png` en parafarmacia, logo genérico en el resto
- **Barra de progreso con texto descriptivo:** al navegar entre secciones o al enviar una búsqueda, aparece una barra en la parte superior con pasos cronometrados y una pastilla en la esquina superior derecha que muestra el porcentaje y el texto de la fase actual (ej: `58% · Procesando resultados…`); la barra llega al 100% / «¡Listo!» en el momento exacto en que el servidor termina la búsqueda (incluyendo el caso de sin resultados), y se desvanece automáticamente. Implementación: las navegaciones reales usan `sessionStorage`; las búsquedas HTMX se cierran mediante el evento personalizado `ff:cargaCompleta` emitido por `_resultados_progresivos.html`
- **Páginas de error personalizadas:** manejadores `@app.errorhandler(404)` y `@app.errorhandler(500)` registrados globalmente en Flask; el template 404 muestra icono, código de error, mensaje descriptivo diferenciado según el tipo de error, y botones de acción hacia los dos buscadores

### 9. Búsqueda con precios diferenciados por formato

Cuando hay varios formatos del mismo medicamento (ej. paracetamol 650 mg, paracetamol 1 g, paracetamol 100 mg), la consulta a farmacias se construye incluyendo la dosis específica del medicamento, no el término genérico tecleado por el usuario. Así cada tarjeta muestra el precio del formato exacto, no el mismo resultado para todos.

Prioridad de consulta:

1. `INN + dosis` del medicamento (ej. `PARACETAMOL 650 mg`)
2. `INN sin dosis` — fallback si no hay resultado con dosis exacta
3. `INN base sin sufijo de sal` + dosis (ej. `AMOXICILINA 500 mg` en lugar de `AMOXICILINA TRIHIDRATO 500 mg`)
4. Término original tecleado por el usuario — último recurso

### 10. Precio de referencia vs precio de farmacia

Las tarjetas de búsqueda distinguen claramente el origen del precio mostrado:

| Situación | Mensaje mostrado |
|-----------|------------------|
| Hay ofertas de farmacias online | "El precio más bajo encontrado es de: X.X€ (Farmacia)" con enlace a la oferta |
| Solo precio del Nomenclátor o Vademécum | "Precio de referencia: X.X€ (SNS / Vademécum)" sin enlace |
| Medicamento con receta sin precio | Enlace al Nomenclátor del SNS |
| Sin precio en ninguna fuente | "Precio no disponible en las fuentes consultadas" |

### 11. Favoritos — medicamentos guardados (`/favoritos`)

El usuario puede guardar medicamentos con el botón ☆ presente en cada tarjeta de búsqueda y en la ficha de detalle. Los favoritos se almacenan en el `localStorage` del navegador.

**Diseño técnico:**

- **Solo se guarda el `nregistro`** (número de registro) y la fecha. Nunca se persisten nombre, precio ni laboratorio.
- Al abrir `/favoritos`, el navegador envía los `nregistro` al servidor vía `POST /api/favoritos`, que devuelve tarjetas HTML con datos **siempre frescos** de CIMA + precio oficial del Nomenclátor.
- En `/favoritos`, los enlaces de "Ver ficha y precios" abren la ficha completa con navegación normal a `/medicamento/<nregistro>` para garantizar que la vista de detalle cargue siempre correctamente.
- **Precarga offline:** al marcar un favorito, se lanza silenciosamente un `fetch` a la ficha del medicamento para que quede en la caché HTTP del navegador y sea accesible sin conexión.
- Los datos de precio en la página de favoritos son solo de referencia (Nomenclátor/Vademécum); para ver precios de farmacias online hay que entrar en la ficha individual.
- **Aviso informativo** visible en la página: _"Esta lista es informativa de precios y fichas técnicas. No sustituye la pauta establecida por su médico ni el consejo de su farmacéutico."_
- El badge numérico en el header compacto muestra cuántos medicamentos hay guardados.

### 8. Circuit breaker en scrapers

Todos los scrapers con cliente HTTP persistente implementan un circuit breaker:
- Timeout de lectura reducido a 6 s (antes 10 s)
- Tras un `ReadTimeout`, la fuente se pausa automáticamente durante 60 s
- Log de un único mensaje `DEBUG` en lugar de traceback repetido

---

## ⚖️ Marco legal

Este verificador **informa, no acusa**.

**Lo que mostramos:**
- "El precio introducido supera el PVP oficial publicado por el SNS"
- "Podría no corresponder al PVP oficial — verifique formato y presentación"

**Lo que no hacemos:**
- Señalar establecimientos concretos
- Afirmar que una farmacia está cobrando ilegalmente

**Disclaimer visible en todas las páginas del verificador:**
> La información mostrada es orientativa y basada en fuentes oficiales (Nomenclátor del SNS — AEMPS / Ministerio de Sanidad). No constituye asesoramiento legal ni farmacéutico. Solo los medicamentos con financiación pública tienen PVP máximo intervenido. La parafarmacia y los OTC sin financiación tienen precio libre.

**Precaución con los OTC ("Efecto precio libre"):**

Algunos medicamentos que aparecen en el Nomenclátor son OTC de precio libre: el laboratorio fija un PVP orientativo, pero la farmacia puede aplicar el suyo propio. En estos casos el verificador muestra el PVP registrado en el Nomenclátor como referencia, pero **no puede concluir que exista una irregularidad** si el precio cobrado difiere. El sistema identifica estos casos e indica explícitamente: _"Este producto es de precio libre. El PVP del Nomenclátor es orientativo; cada establecimiento puede fijar el suyo."_

**FarmaFLUSH como herramienta de detección de errores de TPV:**

FarmaFLUSH ha sido diseñado para detectar errores de configuración en terminales de punto de venta (TPV) farmacéuticos donde el cargado de precios puede no estar sincronizado con el Nomenclátor oficial. Un sobreprecio aislado puede ser un error de introducción manual; un patrón repetido en varios productos de un mismo ticket apunta a una desincronización sistemática entre el TPV y la base de datos oficial de precios.

---

## 📊 Fuentes de datos

### Oficiales

| Fuente | Tipo | Qué se obtiene |
|--------|------|----------------|
| **CIMA** (AEMPS) | API REST pública | Nombre, laboratorio, forma, dosis, ficha técnica, prospecto, fotos |
| **Nomenclátor SNS** | Archivo oficial (sanidad.gob.es) | PVP máximo intervenido (base del verificador) |
| **Vademécum** | Consulta pública | Indicaciones, contraindicaciones, interacciones |

### Farmacias online

| Farmacia | Plataforma | Qué se obtiene |
|----------|-----------|----------------|
| **Dosfarma** | Algolia | Parafarmacia, vitaminas, complementos |
| **Farmacia Tedin** | Prestashop | Parafarmacia, OTC (Galicia) |
| **Farmacias Direct** | Shopify | Medicamentos OTC + parafarmacia |
| **Castrofarma** | Magento 2 | Parafarmacia, cosmética |
| **Farmacia Barata** | Prestashop | Parafarmacia, complementos |
| **FarmaGalicia** | Magento 2 | Parafarmacia, catálogo amplio |
| **OpenFarma** | Prestashop 1.6 | Parafarmacia, higiene, salud |
| **Farmacia Pontevea** | Prestashop | Dermocosmética (Galicia) |
| **Gomezulla en tu Piel** | Prestashop | Cosmética dermatológica (Galicia) |

> **Farmacias Direct** es la única fuente integrada que vende medicamentos OTC. El resto aportan parafarmacia.

### Farmacias exploradas pero no integradas

| Farmacia | Razón |
|----------|-------|
| Mifarma (atida.com) | WAF + captcha → 403 desde VPS |
| Promofarma | Solo parafarmacia, sin OTC |
| Labandeira | SearchAPI no configurada (404) |
| Farmacia Toca | DNS no resuelve desde VPS |
| Farmacia Loureiro | DNS no resuelve desde VPS |
| Farmacia San Mamed | Cloudflare anti-bot activo |
| Farmacia del Camino | Sin endpoint de búsqueda |

---

## 🛠️ Configuración

### Variables de entorno (.env)

```env
FLASK_ENV=production
FLASK_SECRET_KEY=tu-clave-secreta-aqui
DATABASE_PATH=data/pildora.db
CACHE_EXPIRY_HOURS=12
LOG_LEVEL=INFO
```

### SQLite WAL mode

```python
conn.execute('PRAGMA journal_mode=WAL')
```

---

## 🚀 Roadmap

- [x] Favoritos de medicamentos guardados en el navegador (localStorage + datos frescos vía API)
- [x] Precios diferenciados por formato/dosis de medicamento
- [x] Precio de referencia visible en tarjetas cuando no hay farmacias online
- [ ] Modo ticket: exportar resultado en PDF
- [ ] Historial de verificaciones (usuario registrado)
- [ ] API pública de validación de precios (B2B)
- [ ] Alertas de variación de PVP oficial
- [ ] Estadísticas de gasto por categoría terapéutica

---

## 📝 Licencia y aviso legal

Este proyecto es de uso personal y educativo para practicar con Flask y Python, sin ánimo de lucro.

### Datos oficiales

Los datos del Nomenclátor SNS y de la API CIMA (AEMPS) son de titularidad pública y se distribuyen bajo los términos de reutilización establecidos por el Ministerio de Sanidad y la Agencia Española de Medicamentos y Productos Sanitarios (AEMPS). Su uso en este proyecto es meramente informativo y no implica ninguna relación oficial con dichos organismos.

### Datos de farmacias online (scraping)

Los precios de farmacias online se obtienen mediante consulta pública de sus catálogos web, de la misma forma que lo haría cualquier usuario desde un navegador. Este proyecto:

- **No almacena** de forma persistente datos de productos o precios de terceros más allá de la caché temporal necesaria para el funcionamiento de la aplicación.
- **No reproduce** catálogos completos ni extrae datos de forma masiva o sistemática con fines comerciales.
- **No interfiere** con el funcionamiento de los sistemas de las farmacias consultadas.
- Muestra únicamente el **precio y nombre del producto** con enlace directo a la fuente original, respetando la autoría y redirigiendo al usuario al sitio web de cada farmacia.

El acceso a datos públicos de precios con fines comparativos e informativos está amparado por el principio de libre circulación de información y por la normativa de competencia leal en la UE (Directiva 2019/2161 sobre modernización de las normas de protección del consumidor), que reconoce el derecho de los consumidores a acceder a comparativas de precios.

Si eres titular de alguna de las farmacias referenciadas y deseas que tu establecimiento sea excluido, puedes solicitarlo a través de los datos de contacto del proyecto.

### Exención de responsabilidad

La información mostrada tiene carácter **orientativo**. Este proyecto no garantiza la exactitud, completitud ni actualidad de los precios mostrados. El PVP oficial del Nomenclátor SNS puede no coincidir con el precio vigente en el momento de la consulta si ha habido actualizaciones recientes. El autor no se hace responsable de decisiones tomadas en base a la información proporcionada por esta herramienta.

---

## Aviso para titulares de farmacias

***FarmaFLUSH*** **utiliza datos de acceso público** para fomentar la transparencia de precios. Si es usted titular de una farmacia indexada y desea que sus datos sean excluidos de futuras comparativas, puede solicitar la baja abriendo una incidencia en nuestro repositorio oficial:

[Solicitar exclusión vía GitHub Issues](https://github.com/sapoclay/farmaflush/issues)

Para solicitar que dejen de indexarse los datos de un establecimiento, abra una Nueva Issue con el título "Solicitud de Exclusión - [Nombre de la Farmacia]" e incluya:

    URL del dominio a excluir.

    Breve acreditación de la titularidad.

> **Nota:** Se requiere una cuenta de GitHub para garantizar la trazabilidad de la solicitud.




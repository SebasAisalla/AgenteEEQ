"""
Módulo de automatización para descarga de facturas (planillas) de la Empresa
Eléctrica Quito (EEQ) usando Playwright (navegador Chromium real).

Flujo general:
  1. Abre el portal EEQ y navega al formulario de consulta.
  2. Ingresa el número de cuenta contrato del cliente.
  3. Recorre todos los años disponibles y recopila los metadatos de las facturas.
  4. Ordena las facturas por fecha DESC y toma las primeras N (12 o 24).
  5. Por cada año involucrado, navega a ese año en el portal y descarga las facturas.
  6. Guarda cada factura en datos/{cuenta_contrato}/temp_pdfs/ (carpeta temporal).

Los PDFs en temp_pdfs/ son temporales: se usan para ofrecer el ZIP al usuario y
para la extracción de datos a JSON. Una nueva descarga limpia esa carpeta.

Manejo especial:
  - CAPTCHA: detecta reCAPTCHA/hCAPTCHA y pausa hasta que el usuario lo resuelva.
  - Pausas entre lotes: evita bloqueos temporales del sitio.
  - Reintentos: hasta MAX_REINTENTOS_DESCARGA por factura fallida.
  - Screenshots de depuración: se guardan en debug/ ante errores.
"""

import asyncio
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Error,
    Frame,
    Locator,
    Page,
    Response as PlaywrightResponse,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


URL_EEQ = "https://www.eeq.com.ec/consulte-su-factura"
DATOS_DIR = Path(__file__).parent / "datos"   # Ruta absoluta — independiente del CWD
DEBUG_DIR = Path(__file__).parent / "debug"   # Capturas de pantalla de errores
TIMEOUT = 30_000                   # Timeout general de Playwright en ms
TIMEOUT_MODAL_DESCARGA = 20_000    # Tiempo máximo para que el modal responda
PATRON_ANIO = re.compile(r"\b20\d{2}\b")
MAX_REINTENTOS_DESCARGA = 2
PAUSA_ENTRE_DESCARGAS = int(os.getenv("EEQ_PAUSA_DESCARGAS", "2"))
DESCARGAS_POR_LOTE = int(os.getenv("EEQ_DESCARGAS_POR_LOTE", "12"))
PAUSA_ENTRE_LOTES = int(os.getenv("EEQ_PAUSA_ENTRE_LOTES", "90"))

# Estado de módulo — inicializado por ejecutar() en cada invocación
_progreso_cb: Optional[Callable] = None
_captcha_ev: Optional[threading.Event] = None
_skip_pausa_ev: Optional[threading.Event] = None
_pausa_entre_descargas: int = PAUSA_ENTRE_DESCARGAS
_pausa_entre_lotes: int = PAUSA_ENTRE_LOTES
_descargas_por_lote: int = DESCARGAS_POR_LOTE
# Año que se está procesando actualmente (usado para recargas tras error)
_anio_actual_descarga: str = ""


class DescargaNoProducida(RuntimeError):
    """El portal volvió a un estado manejable, pero Playwright no recibió un PDF."""

    def __init__(self, base_nombre: str, anio: str, estado_portal: str, detalle: str = "") -> None:
        self.base_nombre = base_nombre
        self.anio = anio
        self.estado_portal = estado_portal
        self.detalle = detalle
        texto = (
            f"No se produjo evento de descarga para {base_nombre} ({anio or 'sin año'}). "
            f"Estado visual del portal: {estado_portal}"
        )
        if detalle:
            texto += f" - {detalle}"
        super().__init__(texto)


@dataclass
class DocumentoMeta:
    """
    Metadatos de un documento/factura leídos de la tabla del portal EEQ.
    No contiene referencia al DOM — se puede conservar entre cambios de año.
    """
    numero_documento: str
    fecha: str            # Fecha de emisión (texto del portal, ej. "05/01/2026")
    numero_factura: str
    fecha_vencimiento: str
    tipo_documento: str
    valor: str
    anio: str             # Año al que pertenece en el portal (selector del portal)
    indice: int
    fecha_dt: datetime = field(default_factory=lambda: datetime.min)


@dataclass
class Documento:
    """
    Documento con referencia a la fila DOM actual, listo para descargar.
    Se crea a partir de DocumentoMeta cuando el año correspondiente ya está
    seleccionado en el portal.
    """
    numero_documento: str
    fecha: str
    numero_factura: str
    fecha_vencimiento: str
    tipo_documento: str
    valor: str
    fila: Locator
    indice: int


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def _log(nivel: str, texto: str) -> None:
    prefijos = {
        "info": "INFO", "ok": "OK", "error": "ERROR",
        "skip": "SKIP", "aviso": "AVISO", "debug": "DEBUG", "resumen": "RESUMEN",
    }
    print(f"[{prefijos.get(nivel, nivel.upper())}] {texto}")
    if _progreso_cb:
        _progreso_cb({"tipo": "log", "nivel": nivel, "texto": texto})


def _emitir(evento: dict) -> None:
    if _progreso_cb:
        _progreso_cb(evento)


async def _esperar_con_conteo(segundos: int, razon: str) -> None:
    """Sleep que emite un countdown por SSE y puede ser interrumpido por _skip_pausa_ev."""
    _emitir({"tipo": "pausa_inicio", "segundos": segundos, "razon": razon})
    for i in range(segundos, 0, -1):
        _emitir({"tipo": "pausa", "restante": i, "total": segundos, "razon": razon})
        await asyncio.sleep(1)
        if _skip_pausa_ev is not None and _skip_pausa_ev.is_set():
            _skip_pausa_ev.clear()
            _log("info", f"Pausa omitida por el usuario ({razon})")
            break
    _emitir({"tipo": "pausa_fin", "razon": razon})


async def _esperar_filas_estables(
    filas: Locator,
    intentos_estable: int = 3,
    intervalo: float = 0.3,
    timeout_total: float = TIMEOUT / 1000,
) -> int:
    """
    Espera hasta que el número de filas de la tabla deje de cambiar.

    Los mat-table de Angular pueden renderizar filas de forma progresiva tras
    seleccionar un año; leer la tabla demasiado pronto captura solo un
    subconjunto (p. ej. los primeros meses) y descarta el resto en silencio.
    """
    deadline = asyncio.get_running_loop().time() + timeout_total
    anterior = -1
    estables = 0
    while True:
        actual = await filas.count()
        if actual == anterior:
            estables += 1
            if estables >= intentos_estable:
                return actual
        else:
            estables = 0
        anterior = actual
        if asyncio.get_running_loop().time() > deadline:
            return actual
        await asyncio.sleep(intervalo)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

async def guardar_screenshot_debug(page: Page, nombre: str) -> Path:
    DEBUG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta = DEBUG_DIR / f"{timestamp}_{limpiar_nombre_archivo(nombre)}.png"
    await page.screenshot(path=str(ruta), full_page=True)
    _log("debug", f"Screenshot guardado: {ruta}")
    return ruta


def limpiar_nombre_archivo(valor: str) -> str:
    valor = valor.strip() or "sin_nombre"
    valor = re.sub(r"[^\w.-]+", "_", valor, flags=re.UNICODE)
    return valor.strip("._") or "sin_nombre"


def texto_regex(texto: str) -> re.Pattern[str]:
    return re.compile(re.escape(texto), re.IGNORECASE)


def formatear_duracion(segundos: float) -> str:
    segundos_int = max(0, int(round(segundos)))
    minutos, seg = divmod(segundos_int, 60)
    horas, minutos = divmod(minutos, 60)
    if horas:
        return f"{horas}h {minutos}m {seg}s"
    if minutos:
        return f"{minutos}m {seg}s"
    return f"{seg}s"


def estimar_tiempo_total_descarga(
    cantidad: int,
    pausa_descargas: int,
    pausa_lotes: int,
    descargas_por_lote: int,
) -> int:
    """
    Estima el tiempo total en segundos para una cuenta.

    Incluye navegación inicial, interacción media por factura y pausas
    configuradas. Es una estimación conservadora para descargas exitosas.
    Los reintentos, CAPTCHA o bloqueos del portal pueden aumentarla.
    """
    cantidad = max(0, cantidad)
    if cantidad == 0:
        return 0

    navegacion_base = 60
    segundos_interaccion_por_factura = 10
    pausas_descarga = max(cantidad - 1, 0) * max(pausa_descargas, 0)
    lotes_completados_antes_del_final = max((cantidad - 1) // max(descargas_por_lote, 1), 0)
    pausas_lote = lotes_completados_antes_del_final * max(pausa_lotes, 0)
    return navegacion_base + cantidad * segundos_interaccion_por_factura + pausas_descarga + pausas_lote


def es_respuesta_pdf(response: PlaywrightResponse) -> bool:
    headers = {k.lower(): v.lower() for k, v in response.headers.items()}
    content_type = headers.get("content-type", "")
    content_disposition = headers.get("content-disposition", "")
    url = response.url.lower()
    return (
        "application/pdf" in content_type
        or "pdf" in content_disposition
        or url.endswith(".pdf")
        or ".pdf?" in url
    )


def crear_tarea_respuesta_pdf(page: Page, timeout: int) -> asyncio.Task[PlaywrightResponse]:
    loop = asyncio.get_running_loop()
    futuro: asyncio.Future[PlaywrightResponse] = loop.create_future()

    def _on_response(response: PlaywrightResponse) -> None:
        if futuro.done():
            return
        try:
            if es_respuesta_pdf(response):
                futuro.set_result(response)
        except Exception:
            pass

    async def _esperar() -> PlaywrightResponse:
        page.on("response", _on_response)
        try:
            return await asyncio.wait_for(futuro, timeout=timeout / 1000)
        except asyncio.TimeoutError as exc:
            raise PlaywrightTimeoutError("No se detectó respuesta HTTP PDF.") from exc
        finally:
            try:
                page.remove_listener("response", _on_response)
            except AttributeError:
                try:
                    page.off("response", _on_response)
                except AttributeError:
                    pass

    return asyncio.create_task(_esperar())


async def primer_visible(localizadores: list[Locator], timeout: int = 3_000) -> Optional[Locator]:
    if not localizadores:
        return None

    async def _intentar(loc: Locator) -> Optional[Locator]:
        try:
            await loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception:
            return None

    tasks = [asyncio.create_task(_intentar(loc)) for loc in localizadores]
    resultado: Optional[Locator] = None
    pending: set = set(tasks)

    # Retornar en cuanto cualquier locator resuelva; cancelar los demás.
    while pending and resultado is None:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            r = t.result()
            if r is not None and resultado is None:
                resultado = r

    for t in tasks:
        if not t.done():
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    return resultado


async def click_primer_visible(localizadores: list[Locator], timeout: int = 5_000) -> Locator:
    locator = await primer_visible(localizadores, timeout=timeout)
    if locator is None:
        raise PlaywrightTimeoutError("No se encontro ningun elemento visible para hacer click.")
    await locator.click()
    return locator


# ---------------------------------------------------------------------------
# Page interactions
# ---------------------------------------------------------------------------

async def abrir_pagina(page: Page) -> None:
    _log("info", f"Abriendo página: {URL_EEQ}")
    await page.goto(URL_EEQ, wait_until="domcontentloaded", timeout=TIMEOUT)
    # Intentar networkidle con timeout corto; si expira (analytics/tracking) continuar igual.
    # Esto da tiempo al portal Angular de renderizar el formulario sin esperar 30+ segundos.
    try:
        await page.wait_for_load_state("networkidle", timeout=3_000)
    except PlaywrightTimeoutError:
        pass
    await _detectar_bloqueo_ip(page)
    await aceptar_cookies(page)
    await detectar_captcha_y_pausar(page)


async def _detectar_bloqueo_ip(page: Page) -> None:
    """Detecta la página 'Web Page Blocked!' del WAF del portal EEQ y lanza RuntimeError."""
    try:
        blocked = page.get_by_text(re.compile(r"Web Page Blocked", re.IGNORECASE))
        if await blocked.count() > 0:
            await guardar_screenshot_debug(page, "ip_bloqueada_waf")
            raise RuntimeError(
                "IP bloqueada por el WAF del portal EEQ. "
                "Espera 30-60 minutos antes de reintentar."
            )
    except RuntimeError:
        raise
    except Exception:
        pass


async def aceptar_cookies(page: Page) -> None:
    boton = await primer_visible(
        [
            page.get_by_role("button", name=re.compile("Aceptar|Acepto", re.IGNORECASE)),
            page.locator("#cookies-banner button").filter(has_text=re.compile("Aceptar|Acepto", re.IGNORECASE)),
            page.locator(".cookies_eeq button").filter(has_text=re.compile("Aceptar|Acepto", re.IGNORECASE)),
        ],
        timeout=3_000,
    )
    if boton is None:
        return

    _log("info", "Cerrando banner de cookies")
    await boton.click()
    try:
        await page.locator("#cookies-banner, .cookies_eeq").first.wait_for(state="hidden", timeout=5_000)
    except PlaywrightTimeoutError:
        pass


async def buscar_contexto_consulta(page: Page) -> "Page | Frame":
    patrones = [
        re.compile("Consulta de planillas", re.IGNORECASE),
        re.compile("Consulta por Cuenta Contrato", re.IGNORECASE),
        re.compile("Criterio de busqueda|Criterio de búsqueda", re.IGNORECASE),
    ]

    candidatos: list["Page | Frame"] = [page, *page.frames]
    for contexto in candidatos:
        for patron in patrones:
            try:
                await contexto.get_by_text(patron).first.wait_for(state="visible", timeout=5_000)
                _log("info", f"Formulario encontrado en: {getattr(contexto, 'url', 'página principal')}")
                return contexto
            except PlaywrightTimeoutError:
                continue

    await guardar_screenshot_debug(page, "no_se_encontro_contexto_consulta")
    raise RuntimeError("No se encontró la sección 'Consulta de planillas' en la página ni en iframes.")


async def detectar_captcha_y_pausar(page: Page) -> None:
    captcha_visible = False
    detectores = [
        page.locator("iframe[src*='recaptcha' i], iframe[src*='hcaptcha' i]"),
        page.locator(".g-recaptcha, .h-captcha, [class*='captcha' i], [id*='captcha' i]"),
        page.get_by_text(re.compile("captcha|no soy un robot|robot", re.IGNORECASE)),
    ]

    for detector in detectores:
        try:
            if await detector.first.is_visible(timeout=1_000):
                captcha_visible = True
                break
        except (PlaywrightTimeoutError, Error):
            continue

    if not captcha_visible:
        return

    await guardar_screenshot_debug(page, "captcha_detectado")
    _log("aviso", "Se detectó un CAPTCHA o validación anti-robot.")

    if _captcha_ev is not None:
        _log("aviso", "Resuelve el CAPTCHA en el navegador y haz clic en 'Continuar' en la interfaz.")
        _emitir({"tipo": "captcha"})
        await asyncio.to_thread(_captcha_ev.wait)
        _captcha_ev.clear()
    else:
        _log("aviso", "Por seguridad no se intenta evadirlo. Resuélvelo manualmente en el navegador abierto.")
        await asyncio.to_thread(input, "Cuando termines el CAPTCHA, presiona Enter para continuar...")


async def seleccionar_cuenta_contrato(contexto: "Page | Frame") -> None:
    _log("info", "Seleccionando tipo de búsqueda: Consulta por Cuenta Contrato")
    opcion = "Consulta por Cuenta Contrato"
    locator = await primer_visible(
        [
            contexto.get_by_text(texto_regex(opcion)),
            contexto.locator("label").filter(has_text=texto_regex(opcion)),
            contexto.get_by_role("radio", name=texto_regex(opcion)),
            contexto.get_by_label(texto_regex(opcion)),
        ],
        timeout=TIMEOUT,
    )
    if locator is None:
        raise RuntimeError("No se encontró la opción 'Consulta por Cuenta Contrato'.")

    await locator.evaluate("(element) => element.click()")


async def ingresar_criterio(contexto: "Page | Frame", cuenta_contrato: str) -> None:
    _log("info", f"Ingresando criterio de búsqueda: {cuenta_contrato}")
    campo = await primer_visible(
        [
            contexto.get_by_label(re.compile("Criterio de busqueda|Criterio de búsqueda", re.IGNORECASE)),
            contexto.get_by_placeholder(re.compile("Criterio|Cuenta|Contrato", re.IGNORECASE)),
            contexto.locator("input[type='text']"),
            contexto.locator("input:not([type]), input[type='number'], input[type='search']"),
            contexto.locator("textarea"),
        ],
        timeout=TIMEOUT,
    )
    if campo is None:
        raise RuntimeError("No se encontró el campo 'Criterio de búsqueda'.")

    await campo.fill(cuenta_contrato)


async def consultar(contexto: "Page | Frame") -> None:
    _log("info", "Ejecutando consulta")
    boton = await primer_visible(
        [
            contexto.get_by_role("button", name=re.compile("Consultar", re.IGNORECASE)),
            contexto.locator("button").filter(has_text=re.compile("Consultar", re.IGNORECASE)),
            contexto.locator("input[type='submit'][value*='Consultar' i]"),
        ],
        timeout=TIMEOUT,
    )
    if boton is None:
        raise RuntimeError("No se encontró el botón 'Consultar'.")

    await boton.wait_for(state="visible", timeout=TIMEOUT)
    await esperar_habilitado(boton)
    await boton.click()

    await contexto.get_by_text(re.compile("Servicios|Lista de servicios", re.IGNORECASE)).first.wait_for(
        state="visible",
        timeout=TIMEOUT,
    )


async def esperar_habilitado(locator: Locator, timeout: int = TIMEOUT) -> None:
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while True:
        try:
            if await locator.is_enabled():
                return
        except Error:
            pass
        if asyncio.get_running_loop().time() > deadline:
            raise PlaywrightTimeoutError("El elemento no se habilitó dentro del tiempo esperado.")
        await asyncio.sleep(0.2)


async def seleccionar_servicio(contexto: "Page | Frame", cuenta_contrato: str) -> int:
    _log("info", "Buscando servicio y presionando lupa de acciones")
    await contexto.get_by_text(re.compile("Servicios|Lista de servicios", re.IGNORECASE)).first.wait_for(
        state="visible",
        timeout=TIMEOUT,
    )

    filas = contexto.locator("table tbody tr")
    total = await filas.count()
    if total == 0:
        filas = contexto.locator("tr").filter(has_text=re.compile(cuenta_contrato))
        total = await filas.count()

    fila_servicio = filas.filter(has_text=re.compile(cuenta_contrato)).first
    if await fila_servicio.count() == 0:
        fila_servicio = filas.first

    await fila_servicio.wait_for(state="visible", timeout=TIMEOUT)
    servicios_encontrados = max(total, 1)
    _log("info", f"Servicios encontrados: {servicios_encontrados}")

    acciones = [
        fila_servicio.get_by_role("button", name=re.compile("visualizar|consultar|detalle|lupa|ver", re.IGNORECASE)),
        fila_servicio.get_by_role("link", name=re.compile("visualizar|consultar|detalle|lupa|ver", re.IGNORECASE)),
        fila_servicio.locator("[title*='Visualizar' i], [title*='Consultar' i], [title*='Detalle' i]"),
        fila_servicio.locator("[aria-label*='Visualizar' i], [aria-label*='Consultar' i], [aria-label*='Detalle' i]"),
        fila_servicio.locator("button, a").first,
    ]
    await click_primer_visible(acciones, timeout=TIMEOUT)

    await contexto.get_by_text(re.compile("Documentos|Detalle de los documentos", re.IGNORECASE)).first.wait_for(
        state="visible",
        timeout=TIMEOUT,
    )
    return servicios_encontrados


async def seleccionar_anio_documentos(contexto: "Page | Frame", anio: str) -> list[str]:
    anio = anio.strip().lower()
    await contexto.get_by_text(re.compile("Buscar por año|Buscar por ano", re.IGNORECASE)).first.wait_for(
        state="visible",
        timeout=TIMEOUT,
    )

    if anio == "todos":
        anios = await obtener_anios_disponibles(contexto)
        if not anios:
            _log("aviso", "No se pudieron detectar años disponibles; se usará el año visible por defecto.")
            return [""]
        _log("info", f"Años disponibles detectados: {', '.join(anios)}")
        return anios

    await seleccionar_anio_individual(contexto, anio)
    return [anio]


async def obtener_anios_disponibles(contexto: "Page | Frame") -> list[str]:
    combo = await encontrar_combo_anio(contexto)
    if combo is None:
        return []
    await combo.evaluate("(element) => element.click()")
    opciones = contexto.locator("mat-option, [role='option'], .mat-option")
    try:
        await opciones.first.wait_for(state="visible", timeout=TIMEOUT)
        textos = [
            normalizar_espacios(await opciones.nth(i).inner_text())
            for i in range(await opciones.count())
        ]
    finally:
        await cerrar_overlay_con_escape(contexto)

    anios: list[str] = []
    for texto in textos:
        match = PATRON_ANIO.search(texto)
        if match and match.group(0) not in anios:
            anios.append(match.group(0))
    return anios


async def seleccionar_anio_individual(contexto: "Page | Frame", anio: str) -> None:
    if not anio:
        return

    _log("info", f"Seleccionando año: {anio}")
    combo = await encontrar_combo_anio(contexto, timeout=TIMEOUT)
    if combo is None:
        raise RuntimeError(f"No se encontró el combo de año para seleccionar {anio}.")
    await combo.evaluate("(element) => element.click()")

    opcion = await primer_visible(
        [
            contexto.get_by_role("option", name=re.compile(re.escape(anio), re.IGNORECASE)),
            contexto.locator("mat-option, [role='option'], .mat-option").filter(
                has_text=re.compile(re.escape(anio), re.IGNORECASE)
            ),
            contexto.get_by_text(re.compile(re.escape(anio), re.IGNORECASE)),
        ],
        timeout=TIMEOUT,
    )
    if opcion is None:
        await cerrar_overlay_con_escape(contexto)
        raise RuntimeError(f"No se encontró el año {anio} en el combo 'Buscar por año'.")

    await opcion.evaluate("(element) => element.click()")
    await esperar_combo_anio_seleccionado(contexto, anio, combo=combo)
    # Se omite la comparación de firmas DOM: ya esperamos filas con el año en el texto
    await esperar_actualizacion_documentos(contexto, None, anio)


async def encontrar_combo_anio(contexto: "Page | Frame", timeout: int = 5_000) -> Optional[Locator]:
    localizadores = [
        contexto.get_by_role("combobox").filter(has_text=PATRON_ANIO),
        contexto.locator("mat-select").filter(has_text=PATRON_ANIO),
        contexto.locator(".mat-select-trigger").filter(has_text=PATRON_ANIO),
        contexto.get_by_role("combobox").first,
        contexto.locator("mat-select").first,
        contexto.locator(".mat-select-trigger").first,
    ]
    return await primer_visible(localizadores, timeout=timeout)


async def esperar_combo_anio_seleccionado(
    contexto: "Page | Frame",
    anio: str,
    combo: Optional[Locator] = None,
) -> None:
    if combo is None:
        combo = await encontrar_combo_anio(contexto, timeout=TIMEOUT)
    if combo is None:
        return
    try:
        await combo.filter(has_text=re.compile(re.escape(anio))).wait_for(
            state="visible", timeout=TIMEOUT
        )
    except PlaywrightTimeoutError:
        pass


async def esperar_actualizacion_documentos(
    contexto: "Page | Frame",
    documentos_antes: Optional[set[str]] = None,
    anio: str = "",
) -> None:
    filas = contexto.locator("mat-table, table, .mat-table, [role='table'], [role='grid']").filter(
        has_text=re.compile("Documento", re.IGNORECASE)
    ).last.locator("tbody tr, tr.mat-row, mat-row, .mat-row, [role='row']").filter(
        has_text=re.compile(r"\d{6,}", re.IGNORECASE)
    )
    await filas.first.wait_for(state="visible", timeout=TIMEOUT)

    if anio:
        filas_del_anio = filas.filter(has_text=re.compile(re.escape(anio)))
        try:
            await filas_del_anio.first.wait_for(state="visible", timeout=TIMEOUT)
            return
        except PlaywrightTimeoutError:
            _log("aviso", f"No se detectaron filas visibles con el año {anio}; se leerá la tabla disponible.")

    if documentos_antes is None:
        return

    # Esperar a que la tabla cambie (incluyendo el caso de que quede vacía)
    deadline = asyncio.get_running_loop().time() + TIMEOUT / 1000
    while True:
        documentos_despues = await obtener_firmas_documentos(contexto)
        # Detecta tanto: tabla actualizada con nuevas filas, como tabla vacía (sin docs para ese año)
        if documentos_despues != documentos_antes:
            return

        if asyncio.get_running_loop().time() > deadline:
            _log("aviso", "El año fue seleccionado, pero la tabla no cambió dentro del tiempo esperado.")
            return

        await asyncio.sleep(0.3)


async def obtener_firmas_documentos(contexto: "Page | Frame") -> set[str]:
    try:
        contenedores = contexto.locator("mat-table, table, .mat-table, [role='table'], [role='grid']").filter(
            has_text=re.compile("Documento", re.IGNORECASE)
        )
        contenedor = contenedores.last if await contenedores.count() else contexto.locator("body")
        filas = contenedor.locator("tbody tr, tr.mat-row, mat-row, .mat-row, [role='row']").filter(
            has_text=re.compile(r"\d{6,}", re.IGNORECASE)
        )
        firmas: set[str] = set()
        for indice in range(await filas.count()):
            texto = normalizar_espacios(await filas.nth(indice).inner_text())
            if texto:
                firmas.add(texto)
        return firmas
    except Error:
        return set()


async def cerrar_overlay_con_escape(contexto: "Page | Frame") -> None:
    try:
        await contexto.locator("body").press("Escape", timeout=2_000)
    except (PlaywrightTimeoutError, Error):
        pass


async def obtener_documentos(contexto: "Page | Frame") -> list[Documento]:
    _log("info", "Leyendo tabla de documentos")
    await contexto.get_by_text(re.compile("Detalle de los documentos|Documentos", re.IGNORECASE)).first.wait_for(
        state="visible",
        timeout=TIMEOUT,
    )

    contenedores = contexto.locator("mat-table, table, .mat-table, [role='table'], [role='grid']").filter(
        has_text=re.compile("Documento", re.IGNORECASE)
    )
    contenedor = contenedores.last if await contenedores.count() else contexto.locator("body")
    filas = contenedor.locator("tbody tr, tr.mat-row, mat-row, .mat-row, [role='row']").filter(
        has_text=re.compile(r"\d{6,}", re.IGNORECASE)
    )
    await filas.first.wait_for(state="visible", timeout=TIMEOUT)
    total = await filas.count()

    documentos: list[Documento] = []
    for indice in range(total):
        fila = filas.nth(indice)
        textos = await obtener_textos_celdas(fila)
        texto_completo = " ".join(textos)
        if len(textos) < 2 or not any(textos) or not re.search(r"\d{6,}", texto_completo):
            continue

        documentos.append(
            Documento(
                numero_documento=obtener_columna(textos, 0),
                fecha=obtener_columna(textos, 1),
                numero_factura=obtener_columna(textos, 2),
                fecha_vencimiento=obtener_columna(textos, 3),
                tipo_documento=obtener_columna(textos, 4),
                valor=obtener_columna(textos, 5),
                fila=fila,
                indice=indice + 1,
            )
        )

    _log("info", f"Documentos encontrados: {len(documentos)}")
    _emitir({"tipo": "documentos", "total": len(documentos)})
    return documentos


async def obtener_textos_celdas(fila: Locator) -> list[str]:
    try:
        textos = await fila.evaluate(
            "el => Array.from(el.querySelectorAll('td, mat-cell, .mat-cell, [role=\"cell\"]'))"
            ".map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim())"
        )
        if textos:
            return textos
    except Error:
        pass

    texto_fila = normalizar_espacios(await fila.inner_text())
    return [parte for parte in re.split(r"\s{2,}|\n", texto_fila) if parte.strip()]


def obtener_columna(columnas: list[str], indice: int) -> str:
    return columnas[indice] if indice < len(columnas) else ""


def normalizar_espacios(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


async def obtener_documentos_meta(
    contexto: "Page | Frame",
    anio: str,
    ya_esperado: bool = False,
) -> list[DocumentoMeta]:
    """
    Lee la tabla de documentos del portal y retorna metadatos sin referencia DOM.

    A diferencia de obtener_documentos(), esta función no emite el evento SSE
    'documentos' y retorna DocumentoMeta (sin fila Locator), lo que permite
    conservar los metadatos mientras se navega entre años.

    Parámetros:
        contexto: Página o frame donde está la tabla de documentos.
        anio: Año seleccionado actualmente en el portal (para etiquetar los docs).
        ya_esperado: Si True, omite el wait_for inicial (ya lo hizo el caller).

    Retorna:
        Lista de DocumentoMeta con fecha_dt parseada para ordenamiento.
    """
    contenedores = contexto.locator("mat-table, table, .mat-table, [role='table'], [role='grid']").filter(
        has_text=re.compile("Documento", re.IGNORECASE)
    )
    contenedor = contenedores.last if await contenedores.count() else contexto.locator("body")
    filas = contenedor.locator("tbody tr, tr.mat-row, mat-row, .mat-row, [role='row']").filter(
        has_text=re.compile(r"\d{6,}", re.IGNORECASE)
    )
    if not ya_esperado:
        try:
            await filas.first.wait_for(state="visible", timeout=TIMEOUT)
        except PlaywrightTimeoutError:
            return []

    total = await _esperar_filas_estables(filas)
    documentos: list[DocumentoMeta] = []

    for indice in range(total):
        fila = filas.nth(indice)
        textos = await obtener_textos_celdas(fila)
        texto_completo = " ".join(textos)
        if len(textos) < 2 or not any(textos) or not re.search(r"\d{6,}", texto_completo):
            continue

        fecha_str = obtener_columna(textos, 1)
        fecha_dt = _parsear_fecha_documento(fecha_str)

        documentos.append(DocumentoMeta(
            numero_documento=obtener_columna(textos, 0),
            fecha=fecha_str,
            numero_factura=obtener_columna(textos, 2),
            fecha_vencimiento=obtener_columna(textos, 3),
            tipo_documento=obtener_columna(textos, 4),
            valor=obtener_columna(textos, 5),
            anio=anio,
            indice=indice + 1,
            fecha_dt=fecha_dt,
        ))

    return documentos


def _parsear_fecha_documento(fecha_str: str) -> datetime:
    """
    Parsea la fecha de un documento del portal a datetime.

    El portal puede mostrar fechas en varios formatos. Se prueban los más comunes
    del portal EEQ. Si ninguno funciona, retorna datetime.min para que los docs
    sin fecha queden al final al ordenar.

    Parámetros:
        fecha_str: Cadena de fecha leída del portal.

    Retorna:
        datetime parseado, o datetime.min si no se puede parsear.
    """
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(fecha_str.strip(), fmt)
        except ValueError:
            continue
    return datetime.min


async def recopilar_documentos_ordenados(
    contexto: "Page | Frame",
    cantidad: int,
) -> list[DocumentoMeta]:
    """
    Recorre todos los años disponibles en el portal, recopila los metadatos de
    todas las facturas y retorna las `cantidad` más recientes ordenadas por fecha.

    Si el total de facturas disponibles es menor a `cantidad`, retorna todas.
    Deja el portal posicionado en el primer año que se procesó (el más reciente).

    Parámetros:
        contexto: Página o frame donde está el selector de año y la tabla.
        cantidad: Número de facturas más recientes a retornar (12 o 24).

    Retorna:
        Lista de DocumentoMeta ordenada por fecha DESC, máximo `cantidad` elementos.
    """
    anios_disponibles = await obtener_anios_disponibles(contexto)
    if not anios_disponibles:
        _log("aviso", "No se detectaron años disponibles; leyendo tabla actual.")
        docs = await obtener_documentos_meta(contexto, "")
        return sorted(docs, key=lambda d: d.fecha_dt, reverse=True)[:cantidad]

    _log("info", f"Años disponibles: {', '.join(anios_disponibles)}")
    todos_los_docs: list[DocumentoMeta] = []

    for anio in anios_disponibles:
        try:
            await seleccionar_anio_individual(contexto, anio)
            docs_anio = await obtener_documentos_meta(contexto, anio, ya_esperado=True)
        except Exception as exc_anio:
            _log("aviso", f"No se pudo leer el año {anio}: {exc_anio}; continuando con el siguiente.")
            continue

        _log("info", f"Año {anio}: {len(docs_anio)} facturas encontradas")
        todos_los_docs.extend(docs_anio)

        # Si ya tenemos suficientes del extremo más reciente, podemos parar antes
        # (los años están en orden DESC: el primero es el más reciente)
        if len(todos_los_docs) >= cantidad * 2:
            _log("info", f"Suficientes documentos recopilados ({len(todos_los_docs)}); deteniendo búsqueda.")
            break

    # Deduplicar por número de documento antes de ordenar
    # (puede haber duplicados si la navegación de años re-muestra los mismos)
    vistos: set[str] = set()
    deduplicados: list[DocumentoMeta] = []
    for doc in todos_los_docs:
        clave = doc.numero_documento or doc.numero_factura
        if clave and clave not in vistos:
            vistos.add(clave)
            deduplicados.append(doc)
        elif not clave:
            deduplicados.append(doc)
    todos_los_docs = deduplicados

    # Ordenar por fecha DESC y tomar las primeras `cantidad`
    ordenados = sorted(todos_los_docs, key=lambda d: d.fecha_dt, reverse=True)
    seleccionados = ordenados[:cantidad]

    if len(seleccionados) < cantidad:
        _log("aviso", f"Solo se encontraron {len(seleccionados)} facturas disponibles (se solicitaron {cantidad}).")

    _log("info", f"Facturas a descargar: {len(seleccionados)}")
    _emitir({"tipo": "documentos", "total": len(seleccionados)})
    return seleccionados


async def descargar_documentos(
    page: Page,
    contexto: "Page | Frame",
    documentos: list[DocumentoMeta],
    cuenta_contrato: str,
) -> list[Path]:
    """
    Descarga las facturas de una lista de DocumentoMeta.

    Agrupa los documentos por año del portal, navega a cada año y descarga
    los documentos correspondientes. Maneja pausas entre descargas y entre
    lotes, reintentos ante errores y recargas del portal.

    Parámetros:
        page: Página principal de Playwright.
        contexto: Frame o página con el formulario de documentos.
        documentos: Lista de DocumentoMeta a descargar (ya filtrada y ordenada).
        cuenta_contrato: Número de cuenta (para la carpeta destino y recargas).

    Retorna:
        Lista de rutas de los PDFs descargados exitosamente.
    """
    global _anio_actual_descarga

    descargados: list[Path] = []
    descargas_desde_recarga = 0

    # Agrupar por año del portal para minimizar cambios de año en el selector
    por_anio: dict[str, list[DocumentoMeta]] = {}
    for doc in documentos:
        por_anio.setdefault(doc.anio, []).append(doc)

    for anio, docs_del_anio in por_anio.items():
        _anio_actual_descarga = anio

        # Navegar al año solo si el portal no está ya en ese año
        _log("info", f"Procesando {len(docs_del_anio)} facturas del año {anio or 'default'}")
        if anio:
            await seleccionar_anio_individual(contexto, anio)

        todos_docs = [doc_meta_a_documento(dm, contexto, i) for i, dm in enumerate(docs_del_anio)]

        for doc_idx, documento in enumerate(todos_docs):
            base_nombre = documento.numero_factura or documento.numero_documento or f"documento_{documento.indice}"
            base_nombre = limpiar_nombre_archivo(base_nombre)

            if descarga_existente(base_nombre, cuenta_contrato):
                _log("skip", f"Ya existe la factura {base_nombre}; omitiendo.")
                _emitir({"tipo": "saltado", "nombre": base_nombre, "anio": anio})
                continue

            _log("info", f"Descargando factura {base_nombre} ({anio})")

            for intento in range(1, MAX_REINTENTOS_DESCARGA + 1):
                # Verificar si ya fue guardada en un intento anterior
                if intento > 1 and descarga_existente(base_nombre, cuenta_contrato):
                    _log("skip", f"Factura {base_nombre} guardada en intento anterior; omitiendo reintento.")
                    break
                try:
                    ruta = await intentar_descargar_documento(
                        page, contexto, documento, base_nombre, cuenta_contrato, anio
                    )
                    descargados.append(ruta)
                    print(f"[OK] Factura descargada: {ruta}")
                    _emitir({"tipo": "descargado", "nombre": str(ruta), "anio": anio})
                    descargas_desde_recarga += 1
                    await _esperar_con_conteo(_pausa_entre_descargas, "pausa entre descargas")

                    es_ultimo_del_anio = (doc_idx == len(todos_docs) - 1)
                    if (not es_ultimo_del_anio
                            and descargas_desde_recarga >= _descargas_por_lote):
                        _log("info", f"Pausa de {_pausa_entre_lotes}s para evitar bloqueo del sitio.")
                        await _esperar_con_conteo(_pausa_entre_lotes, "pausa entre lotes")
                        _log("info", "Recargando consulta para continuar con el siguiente lote.")
                        contexto = await recargar_hasta_documentos(page, cuenta_contrato, anio)
                        descargas_desde_recarga = 0

                    break
                except Exception as exc:
                    _log("error", f"Intento {intento}/{MAX_REINTENTOS_DESCARGA} falló para {base_nombre}: {exc}")
                    await cerrar_modal_si_existe(contexto)
                    if isinstance(exc, DescargaNoProducida) and exc.estado_portal == "tabla":
                        _log("info", "El portal quedó operativo; reintentando sin recargar toda la consulta.")
                        if intento == MAX_REINTENTOS_DESCARGA:
                            await guardar_screenshot_debug(page, f"error_descarga_{base_nombre}")
                        else:
                            await _esperar_con_conteo(
                                max(_pausa_entre_descargas * intento, 1),
                                "pausa entre reintentos",
                            )
                    else:
                        _log("info", f"Esperando {_pausa_entre_lotes}s antes de reintentar.")
                        await _esperar_con_conteo(_pausa_entre_lotes, "pausa entre lotes")
                        contexto = await recargar_hasta_documentos(page, cuenta_contrato, anio)
                        descargas_desde_recarga = 0
                        if intento == MAX_REINTENTOS_DESCARGA:
                            await guardar_screenshot_debug(page, f"error_descarga_{base_nombre}")
                        else:
                            await _esperar_con_conteo(_pausa_entre_descargas * intento, "pausa entre reintentos")

    return descargados


def doc_meta_a_documento(dm: DocumentoMeta, contexto: "Page | Frame", nuevo_indice: int) -> Documento:
    """
    Crea un Documento (con referencia DOM) a partir de un DocumentoMeta.

    La referencia DOM (fila) se resuelve lazily por número de factura usando
    encontrar_fila_documento(), por lo que se usa un locator vacío aquí y se
    busca al momento de descargar.

    Parámetros:
        dm: Metadatos del documento.
        contexto: Página o frame actual (para crear el locator placeholder).
        nuevo_indice: Índice de visualización.

    Retorna:
        Documento con los campos del meta y un locator de la fila.
    """
    # El locator real se busca por número en encontrar_fila_documento()
    fila_placeholder = contexto.locator("body")
    return Documento(
        numero_documento=dm.numero_documento,
        fecha=dm.fecha,
        numero_factura=dm.numero_factura,
        fecha_vencimiento=dm.fecha_vencimiento,
        tipo_documento=dm.tipo_documento,
        valor=dm.valor,
        fila=fila_placeholder,
        indice=nuevo_indice + 1,
    )


def carpeta_temp_pdfs(cuenta_contrato: str) -> Path:
    """
    Retorna la ruta de la carpeta temporal de PDFs para una cuenta.

    Los PDFs se guardan aquí durante la sesión de descarga. Son temporales:
    se eliminan al iniciar una nueva descarga o cuando el usuario limpia el servidor.

    Parámetros:
        cuenta_contrato: Número de cuenta contrato.

    Retorna:
        Ruta: datos/{cuenta_contrato}/temp_pdfs/
    """
    return DATOS_DIR / limpiar_nombre_archivo(cuenta_contrato) / "temp_pdfs"


def descarga_existente(base_nombre: str, cuenta_contrato: str) -> bool:
    """
    Verifica si ya existe un PDF con ese nombre base en la carpeta temporal.

    Parámetros:
        base_nombre: Nombre sin extensión del archivo.
        cuenta_contrato: Número de cuenta contrato.

    Retorna:
        True si ya existe el archivo.
    """
    carpeta = carpeta_temp_pdfs(cuenta_contrato)
    if not carpeta.exists():
        return False
    return any(carpeta.glob(f"{base_nombre}*.pdf"))


async def encontrar_fila_documento(contexto: "Page | Frame", documento: Documento) -> Locator:
    contenedores = contexto.locator("mat-table, table, .mat-table, [role='table'], [role='grid']").filter(
        has_text=re.compile("Documento", re.IGNORECASE)
    )
    contenedor = contenedores.last if await contenedores.count() else contexto.locator("body")
    filas = contenedor.locator("tbody tr, tr.mat-row, mat-row, .mat-row, [role='row']")

    claves = [documento.numero_factura, documento.numero_documento]
    for clave in claves:
        if not clave:
            continue
        fila = filas.filter(has_text=re.compile(re.escape(clave))).first
        if await fila.count():
            await fila.wait_for(state="visible", timeout=TIMEOUT)
            return fila

    raise RuntimeError(
        f"No se pudo relocalizar la fila del documento {documento.numero_factura or documento.numero_documento}."
    )


async def recargar_hasta_documentos(page: Page, cuenta_contrato: str, anio: str) -> "Page | Frame":
    """
    Recarga el portal desde cero y navega hasta la tabla de documentos del año indicado.

    Se usa cuando hay un error en medio de una descarga y el estado del portal es
    incierto. Recupera el flujo completo: abrir página → consultar cuenta → servicio → año.

    Parámetros:
        page: Página principal de Playwright.
        cuenta_contrato: Número de cuenta para volver a consultar.
        anio: Año al que debe navegar tras la recarga.

    Retorna:
        Contexto (Frame o Page) con la tabla de documentos visible.
    """
    _log("info", "Restaurando pantalla de documentos...")
    await abrir_pagina(page)
    contexto = await buscar_contexto_consulta(page)
    await seleccionar_cuenta_contrato(contexto)
    await ingresar_criterio(contexto, cuenta_contrato)
    await consultar(contexto)
    await seleccionar_servicio(contexto, cuenta_contrato)
    if anio:
        await seleccionar_anio_individual(contexto, anio)
    return contexto


async def encontrar_boton_descarga(fila: Locator) -> Locator:
    por_atributo = [
        fila.locator("[title*='Descargar' i], [aria-label*='Descargar' i]"),
    ]
    for locator in por_atributo:
        if await locator.count():
            return locator.first

    botones = fila.locator("button, a")
    total_botones = await botones.count()
    if total_botones:
        return botones.nth(total_botones - 1)

    raise RuntimeError("No se encontró el icono/botón de descarga en la fila.")


async def manejar_modal_descarga(contexto: "Page | Frame") -> None:
    await responder_modal_descarga(contexto)
    estado, detalle = await esperar_resultado_modal_descarga(contexto, estricto=True)
    await cerrar_confirmacion_descarga(contexto)


async def responder_modal_descarga(contexto: "Page | Frame") -> None:
    modal_titulo = contexto.get_by_text(
        re.compile("Ingresa la informacion solicitada|Ingresa la información solicitada", re.IGNORECASE)
    ).first
    await modal_titulo.wait_for(
        state="visible",
        timeout=TIMEOUT,
    )

    no_titular = await encontrar_control_no_titular(contexto)
    if no_titular is None:
        raise RuntimeError("No se encontró la opción 'No soy titular' en el modal.")

    if await activar_no_titular(contexto, no_titular):
        return

    _log("debug", "Se activó 'No soy titular', pero el modal aún no cambió de estado.")


async def encontrar_control_no_titular(contexto: "Page | Frame") -> Optional[Locator]:
    patron = re.compile(r"^\s*No\s+soy\s+titular\s*$", re.IGNORECASE)
    candidatos = [
        contexto.get_by_role("button", name=patron),
        contexto.locator("button").filter(has_text=patron),
        contexto.locator("[role='button']").filter(has_text=patron),
        contexto.get_by_label(patron),
        contexto.get_by_role("radio", name=patron),
        contexto.locator("label").filter(has_text=patron),
        contexto.get_by_text(patron),
    ]
    return await primer_visible(candidatos, timeout=TIMEOUT)


async def activar_no_titular(contexto: "Page | Frame", control: Locator) -> bool:
    async def _respondio(timeout_ms: int = 2_500) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        modal_locator = contexto.get_by_text(
            re.compile("Ingresa la informacion solicitada|Ingresa la información solicitada", re.IGNORECASE)
        )
        exito_locator = contexto.get_by_text(re.compile("Descarga exitosa", re.IGNORECASE))
        error_locator = contexto.get_by_text(
            re.compile("Error|temporarily unavailable|page is temporarily unavailable|no disponible", re.IGNORECASE)
        )
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await exito_locator.first.is_visible() or await error_locator.first.is_visible():
                    return True
                if not await modal_locator.first.is_visible():
                    return True
            except Error:
                return True
            await asyncio.sleep(0.15)
        return False

    async def _click_js() -> None:
        await control.evaluate(
            """(element) => {
                const target = element.closest('button, [role="button"], label, mat-radio-button') || element;
                target.scrollIntoView({ block: 'center', inline: 'center' });
                for (const eventName of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    target.dispatchEvent(new MouseEvent(eventName, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        buttons: eventName.endsWith('down') ? 1 : 0
                    }));
                }
            }"""
        )

    try:
        await control.scroll_into_view_if_needed(timeout=5_000)
    except (PlaywrightTimeoutError, Error):
        pass

    estrategias = []

    async def click_normal() -> None:
        await esperar_habilitado(control, timeout=5_000)
        await control.click(timeout=TIMEOUT)

    async def click_forzado() -> None:
        await control.click(timeout=TIMEOUT, force=True)

    async def click_mouse() -> None:
        caja = await control.bounding_box(timeout=5_000)
        if caja is None:
            raise RuntimeError("No se pudo calcular la posición de 'No soy titular'.")
        pagina = control.page
        await pagina.mouse.move(caja["x"] + caja["width"] / 2, caja["y"] + caja["height"] / 2)
        await pagina.mouse.down()
        await asyncio.sleep(0.08)
        await pagina.mouse.up()

    async def enter_o_espacio() -> None:
        await control.focus(timeout=5_000)
        await control.press("Enter", timeout=5_000)

    estrategias.extend([click_normal, click_forzado, click_mouse, enter_o_espacio, _click_js])

    ultimo_error: Optional[Exception] = None
    for estrategia in estrategias:
        try:
            await estrategia()
            if await _respondio():
                return True
        except Exception as exc:
            ultimo_error = exc
            continue

    if ultimo_error is not None:
        _log("debug", f"Último intento de click 'No soy titular' falló: {ultimo_error}")

    try:
        await _click_js()
    except (PlaywrightTimeoutError, Error):
        pass
    return await _respondio(timeout_ms=1_500)


async def cerrar_confirmacion_descarga(contexto: "Page | Frame") -> None:
    aceptar = await primer_visible(
        [
            contexto.get_by_role("button", name=re.compile("Aceptar|OK", re.IGNORECASE)),
            contexto.locator("button").filter(has_text=re.compile("Aceptar|OK", re.IGNORECASE)),
        ],
        timeout=3_000,
    )
    if aceptar is not None:
        await aceptar.evaluate("(element) => element.click()")


async def esperar_resultado_modal_descarga(
    contexto: "Page | Frame",
    estricto: bool = True,
) -> tuple[str, str]:
    deadline = asyncio.get_running_loop().time() + TIMEOUT_MODAL_DESCARGA / 1000
    error_locator = contexto.get_by_text(
        re.compile("Error|temporarily unavailable|page is temporarily unavailable|no disponible", re.IGNORECASE)
    )
    exito_locator = contexto.get_by_text(re.compile("Descarga exitosa", re.IGNORECASE))
    modal_locator = contexto.get_by_text(
        re.compile("Ingresa la informacion solicitada|Ingresa la información solicitada", re.IGNORECASE)
    )
    documentos_locator = contexto.get_by_text(
        re.compile("Detalle de los documentos|Documentos", re.IGNORECASE)
    )

    while True:
        try:
            if await exito_locator.first.is_visible():
                return ("exito", "Descarga exitosa")
        except Error:
            pass

        try:
            if await error_locator.first.is_visible():
                texto_error = normalizar_espacios(await error_locator.first.inner_text())
                detalle = texto_error[:220]
                if estricto:
                    raise RuntimeError(f"El sitio mostró un error al descargar: {detalle}")
                return ("error", detalle)
        except RuntimeError:
            raise
        except Error:
            pass

        try:
            modal_visible = await modal_locator.first.is_visible()
            documentos_visible = await documentos_locator.first.is_visible()
            if not modal_visible and documentos_visible:
                return ("tabla", "El portal volvió a la tabla de documentos.")
        except Error:
            pass

        if asyncio.get_running_loop().time() > deadline:
            try:
                if await modal_locator.first.is_visible():
                    detalle = "El modal de descarga siguió abierto después de seleccionar No soy titular."
                else:
                    detalle = "No apareció confirmación visual de descarga exitosa."
            except Error:
                detalle = "No apareció confirmación visual de descarga exitosa."
            if estricto:
                raise PlaywrightTimeoutError(detalle)
            return ("modal_abierto" if "modal" in detalle else "timeout", detalle)

        await asyncio.sleep(0.3)


async def esperar_pdf_o_descarga(
    descarga_task: asyncio.Task[Download],
    respuesta_pdf_task: asyncio.Task[PlaywrightResponse],
) -> tuple[str, Download | PlaywrightResponse]:
    pendientes = {descarga_task, respuesta_pdf_task}
    ultimo_timeout: Optional[PlaywrightTimeoutError] = None

    while pendientes:
        done, pendientes = await asyncio.wait(pendientes, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                if task is descarga_task:
                    return ("download", task.result())
                return ("response", task.result())
            except PlaywrightTimeoutError as exc:
                ultimo_timeout = exc

    if ultimo_timeout is not None:
        raise ultimo_timeout
    raise PlaywrightTimeoutError("No se produjo descarga ni respuesta PDF.")


async def intentar_descargar_documento(
    page: Page,
    contexto: "Page | Frame",
    documento: Documento,
    base_nombre: str,
    cuenta_contrato: str,
    anio: str,
) -> Path:
    """
    Ejecuta la descarga de una factura y guarda el PDF.

    El evento real de descarga de Playwright es la señal principal de éxito.
    La confirmación visual del portal se usa como diagnóstico porque a veces
    el portal vuelve a la tabla o queda con overlay sin mostrar "Descarga exitosa".
    """
    await detectar_captcha_y_pausar(page)
    await cerrar_modal_si_existe(contexto)
    fila_actual = await encontrar_fila_documento(contexto, documento)
    boton_descarga = await encontrar_boton_descarga(fila_actual)

    estado_portal = "sin_confirmacion"
    detalle_portal = ""
    descarga_task: Optional[asyncio.Task[Download]] = None
    respuesta_pdf_task: Optional[asyncio.Task[PlaywrightResponse]] = None
    pdf_task: Optional[asyncio.Task[tuple[str, Download | PlaywrightResponse]]] = None
    estado_task: Optional[asyncio.Task[tuple[str, str]]] = None

    try:
        descarga_task = asyncio.create_task(page.wait_for_event("download", timeout=TIMEOUT * 4))
        respuesta_pdf_task = crear_tarea_respuesta_pdf(page, timeout=TIMEOUT * 4)
        pdf_task = asyncio.create_task(esperar_pdf_o_descarga(descarga_task, respuesta_pdf_task))

        await boton_descarga.evaluate("(element) => element.click()")
        await responder_modal_descarga(contexto)
        estado_task = asyncio.create_task(
            esperar_resultado_modal_descarga(contexto, estricto=False)
        )

        done, _pending = await asyncio.wait(
            {pdf_task, estado_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if pdf_task in done:
            try:
                tipo_pdf, resultado_pdf = pdf_task.result()
            except PlaywrightTimeoutError:
                if estado_task is not None and estado_task.done() and not estado_task.cancelled():
                    estado_portal, detalle_portal = estado_task.result()
                raise
        else:
            estado_portal, detalle_portal = estado_task.result()
            await cerrar_confirmacion_descarga(contexto)
            if estado_portal == "error":
                pdf_task.cancel()
                await asyncio.gather(pdf_task, return_exceptions=True)
                raise RuntimeError(
                    f"El sitio mostró un error al descargar {base_nombre}: {detalle_portal}"
                )
            if estado_portal == "modal_abierto":
                pdf_task.cancel()
                await asyncio.gather(pdf_task, return_exceptions=True)
                raise DescargaNoProducida(base_nombre, anio, estado_portal, detalle_portal)
            tipo_pdf, resultado_pdf = await pdf_task
    except PlaywrightTimeoutError as exc:
        raise DescargaNoProducida(base_nombre, anio, estado_portal, detalle_portal) from exc
    finally:
        if estado_task is not None and not estado_task.done():
            estado_task.cancel()
            await asyncio.gather(estado_task, return_exceptions=True)
        if pdf_task is not None and not pdf_task.done():
            pdf_task.cancel()
            await asyncio.gather(pdf_task, return_exceptions=True)
        if descarga_task is not None and not descarga_task.done():
            descarga_task.cancel()
            await asyncio.gather(descarga_task, return_exceptions=True)
        if respuesta_pdf_task is not None and not respuesta_pdf_task.done():
            respuesta_pdf_task.cancel()
            await asyncio.gather(respuesta_pdf_task, return_exceptions=True)

    if tipo_pdf == "download":
        ruta = await guardar_descarga(resultado_pdf, base_nombre, cuenta_contrato)
    else:
        ruta = await guardar_respuesta_pdf(resultado_pdf, base_nombre, cuenta_contrato)
        _log("debug", f"{base_nombre} ({anio}): PDF capturado desde respuesta HTTP.")

    if estado_portal == "error":
        _log(
            "aviso",
            f"{base_nombre} ({anio}): el portal mostró error visual, "
            "pero el PDF sí fue capturado por Playwright.",
        )
    elif estado_portal not in ("exito", "sin_confirmacion"):
        _log(
            "debug",
            f"{base_nombre} ({anio}): PDF capturado; confirmación visual: {estado_portal}.",
        )

    await cerrar_modal_si_existe(contexto)
    return ruta


async def cerrar_modal_si_existe(contexto: "Page | Frame") -> None:
    botones = [
        contexto.get_by_role("button", name=re.compile("Aceptar|OK|Cerrar|Close", re.IGNORECASE)),
        contexto.locator("button").filter(has_text=re.compile("Aceptar|OK|Cerrar|Close", re.IGNORECASE)),
        contexto.locator("button[aria-label*='close' i], button[aria-label*='cerrar' i]"),
    ]
    for boton_locator in botones:
        try:
            if await boton_locator.first.is_visible(timeout=200):
                await boton_locator.first.evaluate("(element) => element.click()")
                await asyncio.sleep(0.3)
                return
        except (PlaywrightTimeoutError, Error):
            continue

    await cerrar_overlay_con_escape(contexto)


async def guardar_descarga(descarga: Download, base_nombre: str, cuenta_contrato: str) -> Path:
    """
    Guarda el archivo descargado en la carpeta temporal de la cuenta.

    Parámetros:
        descarga: Objeto Download de Playwright.
        base_nombre: Nombre base del archivo (sin extensión).
        cuenta_contrato: Número de cuenta contrato.

    Retorna:
        Ruta del archivo guardado.
    """
    carpeta_destino = carpeta_temp_pdfs(cuenta_contrato)
    carpeta_destino.mkdir(parents=True, exist_ok=True)

    nombre_sugerido = descarga.suggested_filename or f"{base_nombre}.pdf"
    extension = Path(nombre_sugerido).suffix or ".pdf"
    destino = carpeta_destino / f"{base_nombre}{extension}"

    contador = 2
    while destino.exists():
        destino = carpeta_destino / f"{base_nombre}_{contador}{extension}"
        contador += 1

    await descarga.save_as(str(destino))
    return destino


async def guardar_respuesta_pdf(
    response: PlaywrightResponse,
    base_nombre: str,
    cuenta_contrato: str,
) -> Path:
    """
    Guarda una respuesta HTTP PDF cuando el portal no dispara evento download.
    """
    carpeta_destino = carpeta_temp_pdfs(cuenta_contrato)
    carpeta_destino.mkdir(parents=True, exist_ok=True)

    destino = carpeta_destino / f"{base_nombre}.pdf"
    contador = 2
    while destino.exists():
        destino = carpeta_destino / f"{base_nombre}_{contador}.pdf"
        contador += 1

    contenido = await response.body()
    if not contenido or not contenido.lstrip().startswith(b"%PDF"):
        raise RuntimeError(
            f"La respuesta capturada para {base_nombre} no parece ser un PDF válido."
        )

    destino.write_bytes(contenido)
    return destino


# ---------------------------------------------------------------------------
# Descarga progresiva (busca y descarga año por año sin fase separada)
# ---------------------------------------------------------------------------

async def obtener_anio_actual_combo(contexto: "Page | Frame") -> str:
    """
    Lee el año actualmente seleccionado en el combo SIN abrirlo.

    Estrategia en dos fases:
    1. Localizar el elemento combo rápido sin filtro de texto (~0ms cuando existe).
    2. Sondear inner_text() con loop hasta que Angular cargue el valor del año.

    La fase 1 retorna el combo vacío (Angular aún inicializando) en ~0ms.
    La fase 2 espera hasta TIMEOUT ms hasta que aparezca un año en el texto
    (en práctica Angular llena el valor en < 1s tras renderizar la vista).
    """
    try:
        combo = await encontrar_combo_anio(contexto, timeout=5_000)
        if combo is None:
            return ""
        deadline = asyncio.get_running_loop().time() + TIMEOUT / 1000
        while True:
            try:
                texto = normalizar_espacios(await combo.inner_text())
                m = PATRON_ANIO.search(texto)
                if m:
                    return m.group(0)
            except Exception:
                pass
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.1)
        return ""
    except Exception:
        return ""


async def descargar_por_anios_progresivo(
    page: Page,
    contexto: "Page | Frame",
    cantidad: int,
    cuenta_contrato: str,
) -> list[Path]:
    """
    Descarga las últimas `cantidad` facturas de forma progresiva y lazy.

    Flujo optimizado:
      1. Lee el año actual del combo SIN abrirlo (sin overhead de enumeración).
      2. Descarga inmediatamente las facturas del año visible.
      3. Solo si se necesitan más, abre el combo una vez para obtener los
         años restantes y los procesa en orden.

    Esto elimina la doble apertura del combo (enumerar + volver a seleccionar
    el mismo año) que hacía lento el arranque.
    """
    global _anio_actual_descarga

    todos_descargados: list[Path] = []
    descargas_desde_recarga = 0

    # Leer año actual sin abrir el combo
    anio_inicial = await obtener_anio_actual_combo(contexto)
    anios_pre_enumerados: list[str] = []
    anios_truncados = False
    if not anio_inicial:
        _log("aviso", "No se detectó año en el combo; usando los años más recientes disponibles.")
        anios_disponibles = await obtener_anios_disponibles(contexto)
        if not anios_disponibles:
            _log("aviso", "No se encontraron años en el combo; leyendo tabla actual.")
            docs = await obtener_documentos_meta(contexto, "")
            docs_ordenados = sorted(docs, key=lambda d: d.fecha_dt, reverse=True)[:cantidad]
            _emitir({"tipo": "documentos", "total": len(docs_ordenados)})
            return await descargar_documentos(page, contexto, docs_ordenados, cuenta_contrato)
        # anios_disponibles viene en orden DESC (más reciente primero); nos limitamos a
        # los últimos 3 para no alargar la búsqueda. Si hacen falta más, el fallback de
        # "años pendientes" más abajo amplía la lista.
        anios_pre_enumerados = anios_disponibles[:3]
        anios_truncados = len(anios_disponibles) > 3
        anio_inicial = anios_pre_enumerados[0]

    # Lista dinámica: empieza con el año visible; se amplía con los demás si se necesitan.
    # Si ya enumeramos arriba, partimos con los años más recientes para no abrir el combo dos veces.
    anios_a_procesar: list[str] = anios_pre_enumerados if anios_pre_enumerados else [anio_inicial]
    anios_enumerados: bool = bool(anios_pre_enumerados) and not anios_truncados

    anio_idx = 0
    while anio_idx < len(anios_a_procesar):
        if len(todos_descargados) >= cantidad:
            break

        anio = anios_a_procesar[anio_idx]
        anio_idx += 1
        _anio_actual_descarga = anio

        # Para el primer año NO se selecciona (ya está visible); para los demás sí
        anio_ya_seleccionado = False
        if anio != anio_inicial:
            faltan = cantidad - len(todos_descargados)
            _log(
                "info",
                f"Año {anio_inicial} agotado ({len(todos_descargados)} descargadas); "
                f"cambiando al año {anio} para completar las {faltan} facturas restantes.",
            )
            try:
                await seleccionar_anio_individual(contexto, anio)
                anio_ya_seleccionado = True
            except Exception as exc:
                _log("aviso", f"No se pudo navegar al año {anio}: {exc}; continuando.")
                continue

        try:
            # Si seleccionar_anio_individual ya esperó las filas, no esperar de nuevo
            docs_anio = await obtener_documentos_meta(contexto, anio, ya_esperado=anio_ya_seleccionado)
        except Exception as exc:
            _log("aviso", f"No se pudo leer el año {anio}: {exc}; continuando.")
            continue

        if not docs_anio:
            _log("info", f"Año {anio}: sin facturas")
        else:
            docs_ordenados = sorted(docs_anio, key=lambda d: d.fecha_dt, reverse=True)
            faltan = cantidad - len(todos_descargados)
            docs_a_descargar = docs_ordenados[:faltan]

            _log("info", f"Año {anio}: {len(docs_anio)} disponibles, descargando {len(docs_a_descargar)}")
            _emitir({"tipo": "documentos", "total": len(todos_descargados) + len(docs_a_descargar)})

            for doc_idx, doc_meta in enumerate(docs_a_descargar):
                base_nombre = limpiar_nombre_archivo(
                    doc_meta.numero_factura or doc_meta.numero_documento or f"doc_{doc_meta.indice}"
                )

                if descarga_existente(base_nombre, cuenta_contrato):
                    _log("skip", f"Ya existe {base_nombre}; omitiendo.")
                    _emitir({"tipo": "saltado", "nombre": base_nombre, "anio": anio})
                    continue

                _log("info", f"Descargando {base_nombre} ({anio})")
                documento = doc_meta_a_documento(doc_meta, contexto, doc_idx)

                for intento in range(1, MAX_REINTENTOS_DESCARGA + 1):
                    if intento > 1 and descarga_existente(base_nombre, cuenta_contrato):
                        break
                    try:
                        ruta = await intentar_descargar_documento(
                            page, contexto, documento, base_nombre, cuenta_contrato, anio
                        )
                        todos_descargados.append(ruta)
                        print(f"[OK] Descargada: {ruta.name}")
                        _emitir({"tipo": "descargado", "nombre": str(ruta), "anio": anio})
                        descargas_desde_recarga += 1

                        await _esperar_con_conteo(_pausa_entre_descargas, "pausa entre descargas")

                        es_ultimo = doc_idx == len(docs_a_descargar) - 1
                        if not es_ultimo and descargas_desde_recarga >= _descargas_por_lote:
                            _log("info", f"Pausa de {_pausa_entre_lotes}s entre lotes.")
                            await _esperar_con_conteo(_pausa_entre_lotes, "pausa entre lotes")
                            _log("info", "Recargando consulta para continuar.")
                            contexto = await recargar_hasta_documentos(page, cuenta_contrato, anio)
                            descargas_desde_recarga = 0

                        break
                    except Exception as exc:
                        _log("error", f"Intento {intento}/{MAX_REINTENTOS_DESCARGA} fallido para {base_nombre}: {exc}")
                        await cerrar_modal_si_existe(contexto)
                        if isinstance(exc, DescargaNoProducida) and exc.estado_portal == "tabla":
                            _log("info", "El portal quedó operativo; reintentando sin recargar toda la consulta.")
                            if intento == MAX_REINTENTOS_DESCARGA:
                                await guardar_screenshot_debug(page, f"error_descarga_{base_nombre}")
                            else:
                                await _esperar_con_conteo(
                                    max(_pausa_entre_descargas * intento, 1),
                                    "pausa reintentos",
                                )
                        else:
                            await _esperar_con_conteo(_pausa_entre_lotes, "pausa entre lotes")
                            contexto = await recargar_hasta_documentos(page, cuenta_contrato, anio)
                            descargas_desde_recarga = 0
                            if intento == MAX_REINTENTOS_DESCARGA:
                                await guardar_screenshot_debug(page, f"error_descarga_{base_nombre}")
                            else:
                                await _esperar_con_conteo(_pausa_entre_descargas * intento, "pausa reintentos")

        # Si aún faltan facturas y todavía no hemos enumerado los demás años, hacerlo ahora
        if len(todos_descargados) < cantidad and not anios_enumerados:
            anios_enumerados = True
            todos_anios = await obtener_anios_disponibles(contexto)
            anios_pendientes = [a for a in todos_anios if a not in anios_a_procesar]
            if anios_pendientes:
                _log("info", f"Años pendientes: {', '.join(anios_pendientes)}")
                anios_a_procesar.extend(anios_pendientes)

    return todos_descargados


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def ejecutar(
    cuenta_contrato: str,
    cantidad: int,
    tipo_cliente: str = "residencial",
    on_progreso: Optional[Callable] = None,
    captcha_evento: Optional[threading.Event] = None,
    pausa_descargas: int = PAUSA_ENTRE_DESCARGAS,
    pausa_lotes: int = PAUSA_ENTRE_LOTES,
    descargas_por_lote: int = DESCARGAS_POR_LOTE,
    skip_pausa_evento: Optional[threading.Event] = None,
) -> None:
    """
    Punto de entrada principal. Descarga las últimas `cantidad` facturas de
    la cuenta indicada desde el portal EEQ.

    Flujo:
      1. Abre el navegador y navega al portal EEQ.
      2. Consulta la cuenta contrato.
      3. Recopila metadatos de todos los años disponibles.
      4. Ordena por fecha DESC y selecciona las `cantidad` más recientes.
      5. Descarga esas facturas a datos/{cuenta}/temp_pdfs/.

    Parámetros:
        cuenta_contrato: Número de cuenta a consultar.
        cantidad: Número de facturas más recientes a descargar (12 o 24).
        tipo_cliente: 'residencial' o 'industrial' (se reporta en el evento fin).
        on_progreso: Callback para emitir eventos SSE al servidor Flask.
        captcha_evento: Event de threading para sincronizar la resolución de CAPTCHA.
        pausa_descargas: Segundos de espera entre descargas individuales.
        pausa_lotes: Segundos de espera entre lotes de descargas.
        descargas_por_lote: Número de descargas antes de una pausa larga.
        skip_pausa_evento: Event de threading para saltarse una pausa manualmente.
    """
    global _progreso_cb, _captcha_ev
    global _pausa_entre_descargas, _pausa_entre_lotes, _descargas_por_lote, _skip_pausa_ev
    _progreso_cb = on_progreso
    _captcha_ev = captcha_evento
    _pausa_entre_descargas = pausa_descargas
    _pausa_entre_lotes = pausa_lotes
    _descargas_por_lote = descargas_por_lote
    _skip_pausa_ev = skip_pausa_evento

    browser: Optional[Browser] = None
    contexto_browser: Optional[BrowserContext] = None

    async with async_playwright() as playwright:
        inicio_ejecucion = time.monotonic()
        try:
            tiempo_estimado = estimar_tiempo_total_descarga(
                cantidad,
                pausa_descargas,
                pausa_lotes,
                descargas_por_lote,
            )
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            contexto_browser = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1600, "height": 1200},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            )
            # Ocultar navigator.webdriver para evitar detección por WAF/bot-detection
            await contexto_browser.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await contexto_browser.new_page()
            page.set_default_timeout(TIMEOUT)

            _log("info", f"Cuenta: {cuenta_contrato} | Tipo: {tipo_cliente} | Últimas: {cantidad}")
            _emitir({"tipo": "planillas_objetivo", "cantidad": cantidad, "tipo_cliente": tipo_cliente})
            _log(
                "info",
                "Tiempo estimado por cuenta: "
                f"{formatear_duracion(tiempo_estimado)} "
                "(puede aumentar si hay CAPTCHA, reintentos o lentitud del portal).",
            )
            _emitir({
                "tipo": "tiempo_estimado",
                "segundos": tiempo_estimado,
                "texto": formatear_duracion(tiempo_estimado),
            })

            await abrir_pagina(page)
            contexto_consulta = await buscar_contexto_consulta(page)
            await seleccionar_cuenta_contrato(contexto_consulta)
            await ingresar_criterio(contexto_consulta, cuenta_contrato)
            await detectar_captcha_y_pausar(page)
            await consultar(contexto_consulta)
            await detectar_captcha_y_pausar(page)
            servicios = await seleccionar_servicio(contexto_consulta, cuenta_contrato)
            await detectar_captcha_y_pausar(page)

            # Descarga progresiva: busca y descarga año por año sin fase separada
            descargados = await descargar_por_anios_progresivo(
                page, contexto_consulta, cantidad, cuenta_contrato
            )

            if not descargados:
                await guardar_screenshot_debug(page, "sin_documentos_descargados")
                _log("aviso", "No se descargaron documentos para esta cuenta.")

            _log("resumen", f"Cuenta: {cuenta_contrato}")
            _log("resumen", f"Planillas solicitadas: últimas {cantidad}")
            _log("resumen", f"Facturas descargadas: {len(descargados)}")
            tiempo_real = time.monotonic() - inicio_ejecucion
            _log("resumen", f"Tiempo total real: {formatear_duracion(tiempo_real)}")
            for ruta in descargados:
                _log("resumen", f"- {ruta.name}")
            _emitir({
                "tipo": "fin",
                "cuenta": cuenta_contrato,
                "cantidad": cantidad,
                "tipo_cliente": tipo_cliente,
                "servicios": servicios,
                "documentos_total": len(descargados),
                "descargados": len(descargados),
                "tiempo_total_segundos": int(round(tiempo_real)),
                "tiempo_total": formatear_duracion(tiempo_real),
            })
        except Exception as exc:
            _log("error", f"Ocurrió un error: {exc}")
            if "page" in locals():
                await guardar_screenshot_debug(page, "error_general")
            _emitir({"tipo": "fin", "error": str(exc)})
        finally:
            if contexto_browser is not None:
                await contexto_browser.close()
            if browser is not None:
                await browser.close()


def main() -> None:
    """Punto de entrada de línea de comandos para pruebas manuales."""
    cuenta_contrato = pedir_cuenta_contrato()
    cantidad_str = input("Últimas cuántas planillas (12/24) [12]: ").strip() or "12"
    cantidad = int(cantidad_str) if cantidad_str.isdigit() else 12
    tipo = input("Tipo de cliente (residencial/industrial) [residencial]: ").strip() or "residencial"
    asyncio.run(ejecutar(cuenta_contrato, cantidad, tipo))


def pedir_cuenta_contrato() -> str:
    """Solicita el número de cuenta contrato por consola con validación básica."""
    while True:
        cuenta_contrato = input("Número de cuenta contrato: ").strip()
        if cuenta_contrato:
            return cuenta_contrato
        print("[ERROR] La cuenta contrato es obligatoria.")


if __name__ == "__main__":
    main()

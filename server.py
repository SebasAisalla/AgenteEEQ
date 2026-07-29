"""
Servidor Flask del Agente EEQ.

Orquesta la descarga de facturas, la extracción de datos y la generación de
Anexos C/D para el trámite SGDA (Sistema de Generación Distribuida para
Autoabastecimiento) ante la Empresa Eléctrica Quito.

Estructura de carpetas de datos (en BASE_DIR/datos/):
  datos/
    {cuenta_contrato}/
      temp_pdfs/       ← PDFs descargados durante la sesión (temporales)
      facturas.json    ← Datos extraídos de todas las facturas
      resultado.json   ← Resultado del último análisis (Anexo C/D)

Los PDFs en temp_pdfs/ se conservan mientras dura la sesión para ofrecer
la descarga en ZIP. Se eliminan al iniciar una nueva descarga o manualmente
via /api/limpiar-pdfs.

Rutas principales:
  GET  /                     → Interfaz web
  POST /api/iniciar          → Inicia descarga (cantidad + tipo_cliente)
  GET  /api/progreso         → Stream SSE de eventos en tiempo real
  POST /api/analizar-consumo → Calcula Anexo C/D
  GET  /api/exportar-excel   → Descarga Excel Anexo C o D
  POST /api/exportar-excel-custom → Excel con valores editados por el usuario
  GET  /api/descargar-zip    → ZIP de PDFs de la sesión actual
  POST /api/limpiar-pdfs     → Elimina temp_pdfs del servidor
  POST /api/captcha-resuelto → Continúa tras CAPTCHA manual
  POST /api/saltar-pausa     → Omite la pausa actual
  POST /api/cancelar         → Cancela la descarga en curso
  GET  /api/estado           → Estado JSON de la sesión actual
"""

import asyncio
import io
import json
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

# Windows, al correr como servicio (NSSM redirige stdout/stderr a un archivo),
# usa el codepage del sistema (cp1252) en vez de UTF-8 para stdout/stderr —
# cualquier print()/log con un carácter fuera de cp1252 (ej. "→") revienta el
# proceso con UnicodeEncodeError apenas arranca. Forzar UTF-8 aquí, con
# errors="replace" como red de seguridad, evita que un símbolo suelto tumbe
# todo el servicio en producción (ver incidente: server.py:845 con "→").
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

from eeq_descargar_facturas import ejecutar, carpeta_temp_pdfs
from consumo_calculator import analizar_global, analizar_global_desde_json
from anexos_generator import (
    generar_json,
    generar_excel_anexo_c,
    generar_excel_anexo_d,
    generar_excel_detalle_planilla,
    tabla_anexo_d,
)

BASE_DIR   = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"
ASSETS_DIR = BASE_DIR / "assets"
DATOS_DIR  = BASE_DIR / "datos"
PORT = 3002

app = Flask(__name__, static_folder=None)


class _StripPrefix:
    """
    Middleware WSGI que elimina el prefijo /eeq de las rutas entrantes.
    Permite montar la aplicación bajo /eeq en un reverse proxy (nginx/IIS).
    """
    _PREFIX = "/eeq"

    def __init__(self, inner):
        self._inner = inner

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self._PREFIX:
            start_response("301 Moved Permanently", [
                ("Location", self._PREFIX + "/"),
                ("Content-Length", "0"),
            ])
            return [b""]
        elif path.startswith(self._PREFIX + "/"):
            environ["PATH_INFO"] = path[len(self._PREFIX):] or "/"
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self._PREFIX
        return self._inner(environ, start_response)


app.wsgi_app = _StripPrefix(app.wsgi_app)

# ---------------------------------------------------------------------------
# Estado de sesión (compartido entre el hilo de descarga y el servidor Flask)
# ---------------------------------------------------------------------------

_sesion: dict = {}
_sesion_lock = threading.Lock()
_eventos: list[dict] = []
_eventos_lock = threading.Lock()
_hilo: threading.Thread | None = None
_captcha_ev = threading.Event()
_skip_pausa_ev = threading.Event()


def _hilo_descarga_vivo() -> bool:
    return _hilo is not None and _hilo.is_alive()


def _formatear_duracion(segundos: float) -> str:
    segundos_int = max(0, int(round(segundos)))
    minutos, seg = divmod(segundos_int, 60)
    horas, minutos = divmod(minutos, 60)
    if horas:
        return f"{horas}h {minutos}m {seg}s"
    if minutos:
        return f"{minutos}m {seg}s"
    return f"{seg}s"


def _reset() -> None:
    """Reinicia el estado de sesión al valor inicial (antes de una nueva descarga)."""
    global _eventos
    with _sesion_lock:
        _sesion.clear()
        _sesion.update({
            "estado": "idle",
            "cuentas": [],
            "cuentas_total": 0,
            "cuenta_actual": "",
            "cuenta_idx": 0,
            "cantidad": 12,
            "tipo_cliente": "residencial",
            "documentos_total": 0,
            "documentos_descargados": 0,
            "documentos_saltados": 0,
            "archivos": [],
        })
    with _eventos_lock:
        _eventos = []


def _on_progreso(evento: dict) -> None:
    """
    Recibe un evento del hilo de descarga y actualiza el estado de sesión.
    También encola el evento para ser enviado al frontend via SSE.
    """
    tipo = evento.get("tipo")
    with _sesion_lock:
        if tipo == "documentos":
            _sesion["documentos_total"] = evento.get("total", 0)
        elif tipo == "descargado":
            _sesion["documentos_descargados"] += 1
            ruta_p = Path(evento["nombre"])
            # Rutar relativa a DATOS_DIR para el frontend
            try:
                ruta_rel = str(ruta_p.relative_to(BASE_DIR / "datos"))
            except ValueError:
                ruta_rel = ruta_p.name
            _sesion["archivos"].append({
                "nombre": ruta_p.name,
                "anio": evento.get("anio", ""),
                "estado": "descargado",
                "ruta": ruta_rel,
                "cuenta": _sesion.get("cuenta_actual", "sin_cuenta"),
            })
            evento = {**evento, "ruta": ruta_rel}
        elif tipo == "saltado":
            _sesion["documentos_saltados"] += 1
            nombre_base = evento["nombre"]
            cuenta_act = _sesion.get("cuenta_actual", "sin_cuenta")
            ruta_sal = f"{cuenta_act}/temp_pdfs/{nombre_base}.pdf"
            _sesion["archivos"].append({
                "nombre": nombre_base,
                "anio": evento.get("anio", ""),
                "estado": "saltado",
                "ruta": ruta_sal,
                "cuenta": cuenta_act,
            })
            evento = {**evento, "ruta": ruta_sal}
        elif tipo == "captcha":
            _sesion["estado"] = "esperando_captcha"
        elif tipo == "fin":
            _sesion["estado"] = "completado"
        elif tipo == "cuenta_inicio":
            _sesion["cuenta_actual"] = evento.get("cuenta", "")
            _sesion["cuenta_idx"] = evento.get("idx", 0)
            _sesion["documentos_total"] = 0

    with _eventos_lock:
        _eventos.append(evento)


def _hacer_callback_cuenta(idx: int, total: int):
    """
    Genera un callback de progreso para una cuenta individual.
    Transforma el evento 'fin' de la cuenta en 'fin_cuenta' para que el frontend
    pueda distinguir el fin de una cuenta del fin de toda la sesión.
    """
    def callback(evento: dict) -> None:
        if evento.get("tipo") == "fin":
            nuevo = dict(evento)
            nuevo["tipo"] = "fin_cuenta"
            nuevo["idx"] = idx
            nuevo["total"] = total
            with _eventos_lock:
                _eventos.append(nuevo)
        else:
            _on_progreso(evento)
    return callback


def _extraer_y_guardar_json(cuenta: str, tipo_cliente: str) -> None:
    """
    Parsea los PDFs en temp_pdfs/ de la cuenta y guarda facturas.json.

    Se llama automáticamente al terminar cada cuenta en el hilo de descarga.
    Esto permite que el análisis posterior use el JSON y no dependa de los PDFs.

    Parámetros:
        cuenta: Número de cuenta contrato.
        tipo_cliente: 'residencial' o 'industrial'.
    """
    carpeta_pdfs = carpeta_temp_pdfs(cuenta)
    if not carpeta_pdfs.exists():
        return

    try:
        if tipo_cliente == "industrial":
            from eeq_pdf_parser import parse_factura_eeq_industrial as parser
        else:
            from eeq_pdf_parser import parse_factura_eeq as parser

        pdfs = sorted(carpeta_pdfs.glob("*.pdf"))
        facturas = [parser(p) for p in pdfs]
        ruta_json = carpeta_pdfs.parent / "facturas.json"
        ruta_json.parent.mkdir(parents=True, exist_ok=True)
        with open(ruta_json, "w", encoding="utf-8") as fp:
            json.dump(facturas, fp, ensure_ascii=False, indent=2, default=str)
        _on_progreso({
            "tipo": "log", "nivel": "ok",
            "texto": f"Datos extraídos: {len(facturas)} facturas → facturas.json"
        })
    except Exception as e:
        _on_progreso({"tipo": "log", "nivel": "error",
                      "texto": f"Error extrayendo datos de {cuenta}: {e}"})


def _ejecutar_en_hilo(cuentas: list, cantidad: int, tipo_cliente: str, config: dict) -> None:
    """
    Ejecuta la descarga de todas las cuentas en un hilo de asyncio separado.
    Al finalizar cada cuenta, extrae los datos a JSON automáticamente.

    Parámetros:
        cuentas: Lista de números de cuenta a procesar.
        cantidad: Número de facturas más recientes a descargar (12 o 24).
        tipo_cliente: 'residencial' o 'industrial'.
        config: Diccionario con pausa_descargas, pausa_lotes, descargas_por_lote.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    inicio_total = time.monotonic()
    global _hilo
    try:
        for idx, cuenta in enumerate(cuentas, 1):
            _captcha_ev.clear()
            _skip_pausa_ev.clear()
            _on_progreso({"tipo": "cuenta_inicio", "cuenta": cuenta,
                          "idx": idx, "total": len(cuentas)})
            try:
                # Limpiar temp_pdfs antes de descargar (nueva sesión = PDFs frescos)
                # Usar carpeta_temp_pdfs() para garantizar la misma ruta que usa el módulo de descarga
                carpeta_pdfs = carpeta_temp_pdfs(cuenta)
                if carpeta_pdfs.exists():
                    shutil.rmtree(carpeta_pdfs)

                loop.run_until_complete(
                    ejecutar(
                        cuenta, cantidad, tipo_cliente,
                        on_progreso=_hacer_callback_cuenta(idx, len(cuentas)),
                        captcha_evento=_captcha_ev,
                        pausa_descargas=config["pausa_descargas"],
                        pausa_lotes=config["pausa_lotes"],
                        descargas_por_lote=config["descargas_por_lote"],
                        skip_pausa_evento=_skip_pausa_ev,
                    )
                )
                # Extraer datos de PDFs a JSON tras completar esta cuenta
                _extraer_y_guardar_json(cuenta, tipo_cliente)

            except Exception as e:
                _on_progreso({"tipo": "log", "nivel": "error",
                              "texto": f"Error en cuenta {cuenta}: {e}"})

        with _sesion_lock:
            desc = _sesion["documentos_descargados"]
            salt = _sesion["documentos_saltados"]
        tiempo_total = time.monotonic() - inicio_total
        _on_progreso({"tipo": "fin", "cuentas": cuentas, "cantidad": cantidad,
                      "tipo_cliente": tipo_cliente,
                      "descargados": desc, "saltados": salt,
                      "tiempo_total_segundos": int(round(tiempo_total)),
                      "tiempo_total": _formatear_duracion(tiempo_total)})
    except Exception as e:
        _on_progreso({"tipo": "log", "nivel": "error", "texto": str(e)})
        _on_progreso({"tipo": "fin", "error": str(e)})
    finally:
        loop.close()
        with _sesion_lock:
            if _sesion.get("estado") not in ("completado",):
                _sesion["estado"] = "completado"
            _hilo = None
        with _eventos_lock:
            _eventos.append({"tipo": "fin_hilo"})


_reset()

# ---------------------------------------------------------------------------
# Rutas de la interfaz web estática
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Sirve la SPA (Single Page Application) con CSS y JS inlineados."""
    html = (PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC_DIR / "styles" / "app.css").read_text(encoding="utf-8")
    js_code = (PUBLIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    base = (request.script_root or "") + "/"
    html = html.replace("<head>", f'<head>\n  <base href="{base}">', 1)
    html = html.replace(
        '<link rel="stylesheet" href="styles/app.css" />',
        f'<style>\n{css}\n</style>',
    )
    html = html.replace(
        '<script src="js/app.js"></script>',
        f'<script>\n{js_code}\n</script>',
    )
    return Response(html, mimetype="text/html")


@app.route("/styles/<path:filename>")
def styles(filename):
    return send_from_directory(PUBLIC_DIR / "styles", filename)


@app.route("/js/<path:filename>")
def js_files(filename):
    return send_from_directory(PUBLIC_DIR / "js", filename)


@app.route("/assets/logos/<path:filename>")
def logos(filename):
    return send_from_directory(ASSETS_DIR / "logos", filename)


# ---------------------------------------------------------------------------
# API — Estado y control de sesión
# ---------------------------------------------------------------------------

@app.route("/api/estado")
def estado():
    """Retorna el estado actual de la sesión de descarga."""
    with _sesion_lock:
        estado_actual = dict(_sesion)
    estado_actual["hilo_descarga_vivo"] = _hilo_descarga_vivo()
    return jsonify(estado_actual)


@app.route("/api/iniciar", methods=["POST"])
def iniciar():
    """
    Inicia la descarga de facturas para una o más cuentas.

    Body JSON esperado:
        cuentas (list):     Lista de números de cuenta contrato.
        cantidad (int):     Número de planillas más recientes a descargar (12 o 24).
        tipo_cliente (str): 'residencial' o 'industrial'.
        config (dict):      Ajustes opcionales: pausa_descargas, pausa_lotes,
                            descargas_por_lote.
    """
    global _hilo
    data = request.get_json() or {}

    # Validar y normalizar cuentas
    cuentas_raw = data.get("cuentas")
    if cuentas_raw and isinstance(cuentas_raw, list):
        cuentas = list(dict.fromkeys(str(c).strip() for c in cuentas_raw if str(c).strip()))
    else:
        cuenta_unica = data.get("cuenta_contrato", "").strip()
        cuentas = [cuenta_unica] if cuenta_unica else []

    if not cuentas:
        return jsonify({"error": "Al menos una cuenta contrato es obligatoria"}), 400

    # Validar cantidad
    cantidad_raw = data.get("cantidad", 12)
    try:
        cantidad = int(cantidad_raw)
    except (ValueError, TypeError):
        cantidad = 12
    if cantidad not in (12, 24):
        cantidad = 12

    tipo_cliente = data.get("tipo_cliente", "residencial").strip().lower()
    if tipo_cliente not in ("residencial", "industrial"):
        tipo_cliente = "residencial"

    with _sesion_lock:
        if _sesion.get("estado") == "corriendo":
            if _hilo_descarga_vivo():
                return jsonify({"error": "Ya hay una descarga en curso"}), 409
            _sesion["estado"] = "completado"
            with _eventos_lock:
                _eventos.append({
                    "tipo": "log",
                    "nivel": "aviso",
                    "texto": "Se limpió una descarga anterior que ya no estaba activa.",
                })

    _reset()
    with _sesion_lock:
        _sesion["estado"] = "corriendo"
        _sesion["cuentas"] = cuentas
        _sesion["cuentas_total"] = len(cuentas)
        _sesion["cuenta_actual"] = cuentas[0]
        _sesion["cuenta_idx"] = 0
        _sesion["cantidad"] = cantidad
        _sesion["tipo_cliente"] = tipo_cliente

    cfg = data.get("config", {})
    config = {
        "pausa_descargas":    max(1, int(cfg.get("pausa_descargas", 5))),
        "pausa_lotes":        max(5, int(cfg.get("pausa_lotes", 90))),
        "descargas_por_lote": max(1, int(cfg.get("descargas_por_lote", 12))),
    }
    with _sesion_lock:
        _sesion["config"] = config

    _captcha_ev.clear()
    _skip_pausa_ev.clear()
    _hilo = threading.Thread(
        target=_ejecutar_en_hilo,
        args=(cuentas, cantidad, tipo_cliente, config),
        daemon=True,
    )
    _hilo.start()
    return jsonify({"ok": True})


@app.route("/api/captcha-resuelto", methods=["POST"])
def captcha_resuelto():
    """Notifica que el CAPTCHA fue resuelto manualmente. Reanuda la descarga."""
    with _sesion_lock:
        _sesion["estado"] = "corriendo"
    _captcha_ev.set()
    return jsonify({"ok": True})


@app.route("/api/saltar-pausa", methods=["POST"])
def saltar_pausa():
    """Omite la pausa actual entre lotes o entre descargas."""
    _skip_pausa_ev.set()
    return jsonify({"ok": True})


@app.route("/api/cancelar", methods=["POST"])
def cancelar():
    """Cancela la descarga en curso (el hilo se detendrá en su próxima iteración)."""
    with _sesion_lock:
        _sesion["estado"] = "completado"
    with _eventos_lock:
        _eventos.append({"tipo": "cancelado"})
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — Stream de progreso (SSE)
# ---------------------------------------------------------------------------

@app.route("/api/progreso")
def progreso():
    """
    Stream de Server-Sent Events (SSE) con los eventos de descarga en tiempo real.

    El cliente puede conectarse especificando el índice de inicio con ?desde=N
    para recuperar eventos perdidos al reconectarse.
    """
    desde = int(request.args.get("desde", "0"))

    def stream():
        idx = desde
        while True:
            with _eventos_lock:
                nuevos = _eventos[idx:]
            for ev in nuevos:
                yield f"data: {json.dumps(ev)}\n\n"
                idx += 1
            with _sesion_lock:
                estado_actual = _sesion.get("estado")
            if estado_actual == "completado" and idx >= len(_eventos):
                break
            time.sleep(0.25)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# API — Descarga de archivos
# ---------------------------------------------------------------------------

@app.route("/api/descargar-zip")
def descargar_zip():
    """
    Genera y retorna un ZIP con los PDFs de la sesión actual.

    Los PDFs se leen desde datos/{cuenta}/temp_pdfs/ para cada cuenta de la sesión.
    Si temp_pdfs no existe (ya fue limpiada), el ZIP estará vacío.
    """
    with _sesion_lock:
        cuentas = list(_sesion.get("cuentas", []))

    buf = io.BytesIO()
    archivos_incluidos = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for cuenta in cuentas:
            carpeta = carpeta_temp_pdfs(cuenta)
            if not carpeta.exists():
                continue
            for pdf in sorted(carpeta.glob("*.pdf")):
                zf.write(pdf, f"{cuenta}/{pdf.name}")
                archivos_incluidos += 1

    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=facturas_eeq.zip",
            "X-Archivos-Incluidos": str(archivos_incluidos),
        },
    )


@app.route("/api/limpiar-pdfs", methods=["POST"])
def limpiar_pdfs():
    """
    Elimina los PDFs temporales del servidor para las cuentas de la sesión actual.

    Los datos extraídos en facturas.json y resultado.json NO se eliminan.
    Esto libera espacio en disco después de que el usuario haya descargado el ZIP.
    """
    with _sesion_lock:
        cuentas = list(_sesion.get("cuentas", []))

    eliminados = 0
    for cuenta in cuentas:
        carpeta = carpeta_temp_pdfs(cuenta)
        if carpeta.exists():
            pdfs = list(carpeta.glob("*.pdf"))
            eliminados += len(pdfs)
            shutil.rmtree(carpeta)

    return jsonify({"ok": True, "eliminados": eliminados, "cuentas": cuentas})


# ---------------------------------------------------------------------------
# API — Análisis energético (Anexo C / D)
# ---------------------------------------------------------------------------

@app.route("/api/analizar-consumo", methods=["POST"])
def analizar_consumo():
    """
    Calcula el consumo anual y determina Anexo C o D para las cuentas indicadas.

    Si `cuentas` no se envía en el body, usa las cuentas de la sesión activa.
    Si `tipo_cliente` no se envía, usa el tipo de la sesión activa.

    Busca datos en este orden:
      1. PDFs en datos/{cuenta}/temp_pdfs/ (si existen, sesión activa).
      2. facturas.json en datos/{cuenta}/ (datos persistentes de sesión anterior).

    Body JSON opcional:
        cuentas (list):     Filtrar por cuentas específicas.
        tipo_cliente (str): Forzar tipo de cliente ('residencial' o 'industrial').
    """
    data = request.get_json() or {}

    # Determinar cuentas: prioridad body > sesión activa
    cuentas_body = data.get("cuentas")
    if cuentas_body and isinstance(cuentas_body, list):
        cuentas_filtro = [str(c).strip() for c in cuentas_body if str(c).strip()]
    else:
        with _sesion_lock:
            cuentas_filtro = list(_sesion.get("cuentas") or [])

    if not cuentas_filtro:
        return jsonify({"error": "Debes ingresar al menos un número de cuenta contrato para analizar."}), 400

    # Determinar tipo_cliente: prioridad body > sesión
    tipo_body = data.get("tipo_cliente", "").strip().lower()
    if tipo_body in ("residencial", "industrial"):
        tipo_cliente = tipo_body
    else:
        with _sesion_lock:
            tipo_cliente = _sesion.get("tipo_cliente", "residencial")

    resultados: dict = {}

    # Intentar análisis por cada cuenta
    for cuenta in cuentas_filtro:
        carpeta_pdfs = carpeta_temp_pdfs(cuenta)
        ruta_json    = carpeta_pdfs.parent / "facturas.json"

        if carpeta_pdfs.exists() and any(carpeta_pdfs.glob("*.pdf")):
            # Hay PDFs frescos: analizar desde carpeta (global — sin agrupar por año)
            try:
                res = analizar_global(
                    carpeta_pdfs,
                    cuentas_filtro=[cuenta],
                    tipo_cliente=tipo_cliente,
                    cantidad_max=12,
                )
                resultados.update(res)
            except Exception as e:
                return jsonify({"error": f"Error al analizar {cuenta}: {e}"}), 500
        elif ruta_json.exists():
            # No hay PDFs pero sí JSON previo: analizar desde JSON (global)
            try:
                res = analizar_global_desde_json(
                    ruta_json, tipo_cliente=tipo_cliente, cantidad_max=12
                )
                resultados.update(res)
            except Exception as e:
                return jsonify({"error": f"Error al leer datos de {cuenta}: {e}"}), 500

    if not resultados:
        return jsonify({"error": "No se encontraron facturas válidas para analizar"}), 404

    # Calcular tabla Anexo D para los resultados que lo necesiten
    for res in resultados.values():
        if res.get("tipo_anexo") == "D":
            res["tabla_anexo_d"] = tabla_anexo_d(res)

    # Guardar resultado.json por cuenta
    for res in resultados.values():
        cuenta_dir = res.get("cuenta_contrato", "sin_cuenta")
        dest = carpeta_temp_pdfs(cuenta_dir).parent / "resultado.json"
        try:
            generar_json(res, dest)
        except Exception:
            pass

    return jsonify({"ok": True, "resultados": resultados})


@app.route("/api/exportar-excel")
def exportar_excel_endpoint():
    """
    Genera y descarga un Excel Anexo C o D.

    Parámetros query:
        tipo  (str): 'C' o 'D'.
        cuenta (str): Número de cuenta contrato.
    """
    tipo   = request.args.get("tipo", "C").upper()
    cuenta = request.args.get("cuenta", "")

    with _sesion_lock:
        tipo_cliente = _sesion.get("tipo_cliente", "residencial")
        cuentas_sesion = list(_sesion.get("cuentas") or [])

    cuentas_filtro = [cuenta] if cuenta else cuentas_sesion or None
    if not cuentas_filtro:
        return jsonify({"error": "No hay cuentas en sesión. Especifica ?cuenta=XXXXXXXXX"}), 400

    resultados: dict = {}
    for c in cuentas_filtro:
        carpeta_pdfs = carpeta_temp_pdfs(c)
        ruta_json    = carpeta_pdfs.parent / "facturas.json"
        try:
            if carpeta_pdfs.exists() and any(carpeta_pdfs.glob("*.pdf")):
                res = analizar_global(
                    carpeta_pdfs, cuentas_filtro=[c],
                    tipo_cliente=tipo_cliente, cantidad_max=12,
                )
            elif ruta_json.exists():
                res = analizar_global_desde_json(
                    ruta_json, tipo_cliente=tipo_cliente, cantidad_max=12
                )
            else:
                continue
            resultados.update(res)
        except Exception:
            continue

    if not resultados:
        return jsonify({"error": "Sin datos para exportar"}), 404

    res = list(resultados.values())[0]
    return _generar_respuesta_excel(res, tipo)


def _generar_respuesta_excel(res: dict, tipo: str):
    """
    Genera el Excel del Anexo indicado y lo retorna como respuesta HTTP.

    Parámetros:
        res: Resultado del análisis de una cuenta/año.
        tipo: 'C' para histórico o 'D' para estimado por aparatos.

    Retorna:
        Response con el archivo Excel para descarga.
    """
    import os
    import tempfile

    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        if tipo == "C":
            generar_excel_anexo_c(res, tmp_path)
            filename = f"anexo_c_{res.get('anio', '0000')}.xlsx"
        else:
            filas_override = res.get("tabla_anexo_d") or None
            tmp_path, _ = generar_excel_anexo_d(res, tmp_path, filas_override=filas_override)
            filename = f"anexo_d_{res.get('anio', '0000')}.xlsx"

        data = tmp_path.read_bytes()
        if tmp_path.exists():
            os.unlink(tmp_path)

        return Response(
            data,
            mimetype=mime,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/exportar-excel-custom", methods=["POST"])
def exportar_excel_custom():
    """
    Genera un Excel con los valores editados enviados desde el frontend.

    Body JSON:
        resultado (dict): Resultado del análisis con valores de consumo modificados.
        tipo (str):       'C' o 'D'.
    """
    data = request.get_json() or {}
    resultado = data.get("resultado")
    tipo      = (data.get("tipo") or "C").upper()

    if not resultado:
        return jsonify({"error": "Falta el campo 'resultado'"}), 400

    return _generar_respuesta_excel(resultado, tipo)


@app.route("/api/exportar-excel-detalle", methods=["POST"])
def exportar_excel_detalle():
    """
    Genera y descarga un Excel con el detalle de planilla (franjas horarias,
    energía reactiva, demandas) de una factura industrial.

    Body JSON:
        filas (list):  Filas de 'detalle_planilla' de la factura (dicts con
                        'descripcion' y 'consumo_total', entre otros campos).
        archivo (str): Nombre de la factura, para el nombre del archivo descargado.
    """
    import os
    import tempfile

    data = request.get_json() or {}
    filas = data.get("filas")
    if not filas:
        return jsonify({"error": "Falta el campo 'filas'"}), 400

    nombre = Path(data.get("archivo") or "detalle_planilla").stem

    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        tmp_path = generar_excel_detalle_planilla(filas, tmp_path)
        contenido = tmp_path.read_bytes()
        if tmp_path.exists():
            os.unlink(tmp_path)
        return Response(
            contenido,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=detalle_{nombre}.xlsx"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/exportar-json-consumo")
def exportar_json_consumo():
    """
    Exporta el resultado del análisis como archivo JSON descargable.

    Parámetros query:
        cuenta (str): Número de cuenta contrato.
    """
    cuenta = request.args.get("cuenta", "")

    with _sesion_lock:
        tipo_cliente    = _sesion.get("tipo_cliente", "residencial")
        cuentas_sesion  = list(_sesion.get("cuentas") or [])

    cuentas_filtro = [cuenta] if cuenta else cuentas_sesion or None
    if not cuentas_filtro:
        return jsonify({"error": "Sin cuentas para exportar"}), 400

    resultados: dict = {}
    for c in cuentas_filtro:
        ruta_json = carpeta_temp_pdfs(c).parent / "facturas.json"
        if ruta_json.exists():
            res = analizar_global_desde_json(
                ruta_json, tipo_cliente=tipo_cliente, cantidad_max=12
            )
            resultados.update(res)

    if not resultados:
        return jsonify({"error": "Sin datos"}), 404

    res = list(resultados.values())[0]
    raw = json.dumps(res, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    filename = f"consumo_{res.get('cuenta_contrato', 'cuenta')}_{res.get('anio', '0000')}.json"
    return Response(
        raw,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    import webbrowser
    url = f"http://localhost:{PORT}"
    print(f"[AgenteEEQ] Servidor iniciado -> {url}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # Escucha solo en localhost: en producción (Lightsail) Caddy es el único
    # que debe llegar a este puerto (reverse_proxy 127.0.0.1:3002 en
    # C:\Caddy\Caddyfile, dominio eeq.airis.ec) — el puerto no se expone
    # directamente a internet, igual que la plataforma Next.js.
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)

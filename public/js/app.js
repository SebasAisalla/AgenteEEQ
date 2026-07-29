/* AgenteEEQ — Frontend SPA */

"use strict";

// ── Base URL (funciona en / y en /eeq/ vía reverse proxy) ─────────
const _BASE = (() => {
  const p = window.location.pathname;
  return p.endsWith("/") ? p : p.substring(0, p.lastIndexOf("/") + 1);
})();
const _url = (path) => _BASE + path;

// ── Estado global ─────────────────────────────────────────────────
let totalDocs = 0;
let descargados = 0;
let saltados = 0;
let descargadosCuenta = 0;
let saltadosCuenta = 0;
let eventoIdx = 0;
let archivos = [];
let sseSource = null;
let totalCuentas = 1;

// Estado de sesión (para el flujo sin re-ingreso de cuenta)
let _desde_descarga = false;   // true si venimos de una descarga exitosa
let _cuentasSesion  = [];      // cuentas de la sesión de descarga actual
let _tipoSesion     = "residencial";

// ── Elementos DOM ──────────────────────────────────────────────────
const screens = {
  selector:   document.getElementById("pantallaSelector"),
  progreso:   document.getElementById("pantallaProgreso"),
  resultados: document.getElementById("pantallaResultados"),
  analisis:   document.getElementById("pantallaAnalisis"),
};

const el = (id) => document.getElementById(id);

// ── Navegación entre pantallas ─────────────────────────────────────
function mostrarPantalla(nombre) {
  Object.values(screens).forEach((s) => s.classList.remove("pantalla--activa"));
  screens[nombre].classList.add("pantalla--activa");
}

// ── Inicio de descarga ─────────────────────────────────────────────
el("formSelector").addEventListener("submit", async (e) => {
  e.preventDefault();

  const cuentas = Array.from(document.querySelectorAll(".cuenta-input"))
    .map((i) => i.value.trim())
    .filter(Boolean);
  const cuentasUnicas = [...new Set(cuentas)];
  const cantidad      = parseInt(el("selectCantidad").value) || 12;
  const tipoCliente   = el("selectTipoCliente").value || "residencial";
  const errorBox      = el("selectorError");

  errorBox.style.display = "none";
  if (cuentasUnicas.length === 0) {
    errorBox.textContent = "Ingresa al menos un número de cuenta contrato.";
    errorBox.style.display = "block";
    return;
  }

  // Guardar en estado de sesión
  _cuentasSesion = cuentasUnicas;
  _tipoSesion    = tipoCliente;

  // Resetear contadores
  totalDocs           = 0;
  descargados         = 0;
  saltados            = 0;
  descargadosCuenta   = 0;
  saltadosCuenta      = 0;
  eventoIdx           = 0;
  archivos            = [];
  totalCuentas        = cuentasUnicas.length;

  // Resetear UI de progreso
  el("logContenido").innerHTML   = "";
  el("logContador").textContent  = "0 eventos";
  el("barraProgreso").style.width = "0%";
  el("progresoTexto").textContent = "Esperando documentos...";
  el("statDescargados").textContent = "0";
  el("statSaltados").textContent    = "0";

  el("fichaCuenta").textContent = cuentasUnicas.length === 1
    ? cuentasUnicas[0]
    : `${cuentasUnicas.length} cuentas`;

  if (cuentasUnicas.length > 1) {
    el("fichaProgresoCuentas").textContent = `0 de ${cuentasUnicas.length}`;
    el("fichaProgresoCuentas").style.display = "";
    el("dtProgresoCuentas").style.display    = "";
  } else {
    el("fichaProgresoCuentas").style.display = "none";
    el("dtProgresoCuentas").style.display    = "none";
  }

  el("fichaTipo").textContent  = tipoCliente === "industrial" ? "Industrial" : "Residencial";
  el("fichaAnio").textContent  = `Últimas ${cantidad}`;
  el("headerSubtitulo").textContent = "Descargando facturas...";
  el("pausaArea").style.display = "none";
  mostrarPantalla("progreso");

  const config = {
    pausa_descargas:    parseInt(el("cfgPausaDescargas").value)    || 8,
    pausa_lotes:        parseInt(el("cfgPausaLotes").value)        || 180,
    descargas_por_lote: parseInt(el("cfgDescargasPorLote").value)  || 12,
  };

  try {
    const res = await fetch(_url("api/iniciar"), {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ cuentas: cuentasUnicas, cantidad, tipo_cliente: tipoCliente, config }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      agregarLog("error", data.error || "Error al iniciar la descarga.");
      return;
    }
    iniciarSSE();
  } catch (err) {
    agregarLog("error", "No se pudo conectar con el servidor.");
  }
});

// ── SSE ────────────────────────────────────────────────────────────
function iniciarSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource(_url(`api/progreso?desde=${eventoIdx}`));

  sseSource.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    eventoIdx++;
    procesarEvento(ev);
  };

  sseSource.onerror = () => {
    if (sseSource.readyState === EventSource.CLOSED) return;
    sseSource.close();
    setTimeout(iniciarSSE, 2000);
  };
}

function procesarEvento(ev) {
  const tipo = ev.tipo;
  if (tipo === "ping") return;

  if (tipo === "log") {
    agregarLog(ev.nivel, ev.texto);

  } else if (tipo === "planillas_objetivo") {
    const cant = ev.cantidad || 0;
    const tipo_c = ev.tipo_cliente || "residencial";
    agregarLog("info", `Objetivo: ${cant} planillas más recientes (${tipo_c})`);

  } else if (tipo === "tiempo_estimado") {
    agregarLog("info", `Tiempo estimado: ${ev.texto || `${ev.segundos || 0}s`}`);

  } else if (tipo === "cuenta_inicio") {
    descargadosCuenta = 0;
    saltadosCuenta    = 0;
    totalDocs         = 0;
    if (totalCuentas > 1) {
      agregarSeparador(`Cuenta ${ev.idx} de ${ev.total}: ${ev.cuenta}`);
      el("fichaCuenta").textContent = ev.cuenta;
      el("fichaProgresoCuentas").textContent = `${ev.idx} de ${ev.total}`;
    }
    actualizarProgreso();

  } else if (tipo === "fin_cuenta") {
    if (totalCuentas > 1) {
      const d = ev.descargados ?? 0;
      agregarLog("resumen", `Cuenta ${ev.idx}/${ev.total} — ${d} descargada${d !== 1 ? "s" : ""}`);
    }

  } else if (tipo === "documentos") {
    totalDocs = ev.total;
    actualizarProgreso();
    agregarLog("info", `${ev.total} documento${ev.total !== 1 ? "s" : ""} encontrado${ev.total !== 1 ? "s" : ""}.`);

  } else if (tipo === "descargado") {
    descargados++;
    descargadosCuenta++;
    const nombre = (ev.nombre || "").split("/").pop().split("\\").pop();
    archivos.push({ nombre, anio: ev.anio, estado: "descargado", ruta: ev.ruta || nombre });
    actualizarProgreso();
    agregarLog("ok", `Descargada: ${nombre}`);

  } else if (tipo === "saltado") {
    saltados++;
    saltadosCuenta++;
    archivos.push({ nombre: ev.nombre, anio: ev.anio, estado: "saltado", ruta: ev.ruta || ev.nombre });
    actualizarProgreso();

  } else if (tipo === "pausa_inicio" || tipo === "pausa") {
    el("pausaArea").style.display = "";
    el("pausaTexto").textContent = ev.razon === "pausa entre lotes"
      ? "Pausa entre lotes:" : (ev.razon === "pausa entre reintentos" ? "Reintentando en:" : "Pausa:");
    el("pausaRestante").textContent = `${ev.restante ?? ev.segundos}s`;

  } else if (tipo === "pausa_fin") {
    el("pausaArea").style.display = "none";

  } else if (tipo === "captcha") {
    el("modalCaptcha").style.display = "";
    el("modalCaptcha").classList.add("modal-overlay--activo");

  } else if (tipo === "fin" || tipo === "fin_hilo" || tipo === "cancelado") {
    if (sseSource) sseSource.close();
    terminarDescarga(ev);
  }
}

function agregarSeparador(texto) {
  const contenido = el("logContenido");
  const div = document.createElement("div");
  div.className = "log-separador";
  div.textContent = texto;
  contenido.appendChild(div);
  contenido.scrollTop = contenido.scrollHeight;
}

function agregarLog(nivel, texto) {
  const contenido = el("logContenido");
  const div = document.createElement("div");
  div.className = `log-linea log-linea--${nivel}`;
  const prefijos = {
    info: "▸", ok: "✓", error: "✗", skip: "↷",
    aviso: "⚠", debug: "·", resumen: "■",
  };
  div.innerHTML = `<span class="log-prefijo">${prefijos[nivel] || "▸"}</span><span class="log-texto">${escHtml(texto)}</span>`;
  contenido.appendChild(div);
  contenido.scrollTop = contenido.scrollHeight;

  const total = contenido.querySelectorAll(".log-linea").length;
  el("logContador").textContent = `${total} evento${total !== 1 ? "s" : ""}`;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function actualizarProgreso() {
  const hechoCuenta = descargadosCuenta + saltadosCuenta;
  const pct = totalDocs > 0 ? Math.round((hechoCuenta / totalDocs) * 100) : 0;
  el("barraProgreso").style.width = `${pct}%`;
  el("progresoTexto").textContent  = totalDocs > 0
    ? `${hechoCuenta} de ${totalDocs} (${pct}%)`
    : "Procesando...";
  el("statDescargados").textContent = String(descargados);
  el("statSaltados").textContent    = String(saltados);
}

// ── Saltar pausa ───────────────────────────────────────────────────
el("btnSaltarPausa").addEventListener("click", () => {
  el("pausaArea").style.display = "none";
  fetch(_url("api/saltar-pausa"), { method: "POST" }).catch(() => {});
  agregarLog("info", "Pausa omitida manualmente.");
});

// ── Captcha modal ──────────────────────────────────────────────────
el("btnCaptchaResuelto").addEventListener("click", async () => {
  el("modalCaptcha").classList.remove("modal-overlay--activo");
  await fetch(_url("api/captcha-resuelto"), { method: "POST" });
  agregarLog("info", "CAPTCHA resuelto, continuando...");
});

// ── Cancelar ───────────────────────────────────────────────────────
el("btnCancelar").addEventListener("click", async () => {
  if (sseSource) sseSource.close();
  await fetch(_url("api/cancelar"), { method: "POST" }).catch(() => {});
  terminarDescarga({ cancelado: true });
});

// ── Fin de descarga → resultados ───────────────────────────────────
function terminarDescarga(ev) {
  el("headerSubtitulo").textContent = "Proceso finalizado";
  _desde_descarga = true;   // habilita flujo sin re-ingreso de cuenta
  mostrarPantalla("resultados");

  const icono  = el("resultadoIcono");
  const titulo = el("resultadosTitulo");
  const resumen = el("resultadosResumen");

  if (ev && ev.error) {
    icono.className = "resultado-icono resultado-icono--error";
    icono.innerHTML = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 18L18 6M6 6l12 12"/></svg>`;
    titulo.textContent = "Error en la descarga";
    resumen.textContent = ev.error;
  } else if (ev && ev.cancelado) {
    titulo.textContent = "Descarga cancelada";
    resumen.textContent = `Se procesaron ${descargados + saltados} factura${descargados + saltados !== 1 ? "s" : ""} antes de cancelar.`;
  } else {
    titulo.textContent = descargados > 0 ? "Descarga completada" : "Sin nuevas descargas";
    const cuentasInfo = totalCuentas > 1 ? `(${totalCuentas} cuentas) ` : "";
    const tiempoInfo = ev && ev.tiempo_total ? ` Tiempo total: ${ev.tiempo_total}.` : "";
    if (descargados > 0 && saltados > 0) {
      resumen.textContent = `${cuentasInfo}Se descargaron ${descargados} factura${descargados !== 1 ? "s" : ""} nuevas. ${saltados} ya existían.${tiempoInfo}`;
    } else if (descargados > 0) {
      resumen.textContent = `${cuentasInfo}Se descargaron ${descargados} factura${descargados !== 1 ? "s" : ""} correctamente.${tiempoInfo}`;
    } else {
      resumen.textContent = saltados > 0
        ? `${cuentasInfo}Todas las facturas (${saltados}) ya estaban descargadas.${tiempoInfo}`
        : `${cuentasInfo}No se encontraron facturas para descargar.${tiempoInfo}`;
    }
  }

  // Tabla de archivos descargados
  const tbody = el("archivosTablaBody");
  tbody.innerHTML = "";
  if (archivos.length === 0) {
    el("archivosSeccion").style.display = "none";
  } else {
    el("archivosSeccion").style.display = "block";
    archivos.forEach(({ nombre, anio, estado }) => {
      const tr = document.createElement("tr");
      const badgeClass = estado === "descargado" ? "estado-badge--descargado" : "estado-badge--saltado";
      const badgeTexto = estado === "descargado" ? "✓ Descargado" : "↷ Ya existía";
      tr.innerHTML = `
        <td class="archivo-nombre">${escHtml(nombre)}</td>
        <td>${escHtml(anio || "—")}</td>
        <td><span class="estado-badge ${badgeClass}">${badgeTexto}</span></td>
      `;
      tbody.appendChild(tr);
    });
  }

  // Descarga automática del ZIP si hay nuevas planillas
  if (descargados > 0) {
    setTimeout(() => {
      const a = document.createElement("a");
      a.href     = _url("api/descargar-zip");
      a.download = "facturas_eeq.zip";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }, 800);
  }
}

// ── Lista dinámica de cuentas ──────────────────────────────────────
function actualizarBotonesQuitar() {
  const filas = document.querySelectorAll(".cuenta-fila");
  filas.forEach((fila) => {
    const btn = fila.querySelector(".btn-quitar-cuenta");
    if (btn) btn.style.display = filas.length > 1 ? "" : "none";
  });
}

function agregarCuentaFila() {
  const lista = el("listaCuentas");
  const div   = document.createElement("div");
  div.className = "cuenta-fila";
  div.innerHTML = `
    <input type="text" class="campo__input cuenta-input" placeholder="Ej. 1234567890" autocomplete="off" />
    <button type="button" class="btn-quitar-cuenta" aria-label="Quitar cuenta">×</button>
  `;
  div.querySelector(".btn-quitar-cuenta").addEventListener("click", () => {
    div.remove();
    actualizarBotonesQuitar();
  });
  lista.appendChild(div);
  actualizarBotonesQuitar();
  div.querySelector(".cuenta-input").focus();
}

el("btnAgregarCuenta").addEventListener("click", agregarCuentaFila);

el("listaCuentas").addEventListener("click", (e) => {
  if (e.target.classList.contains("btn-quitar-cuenta")) {
    e.target.closest(".cuenta-fila").remove();
    actualizarBotonesQuitar();
  }
});

// ── Acciones de resultados ─────────────────────────────────────────
el("btnNuevaDescarga").addEventListener("click", () => {
  _desde_descarga = false;
  el("headerSubtitulo").textContent = "Descarga de Facturas";
  mostrarPantalla("selector");
});

el("btnAbrirCarpeta").addEventListener("click", () => {
  window.location.href = _url("api/descargar-zip");
});

// ── Navegación a la pantalla de análisis ──────────────────────────

function irAAnalisisDesdeSesion() {
  // Flujo desde resultados de descarga: auto-usa cuentas de la sesión
  _desde_descarga = true;
  el("headerSubtitulo").textContent = "Análisis Energético";
  el("analisisResultados").innerHTML = "";
  el("analisisError").style.display  = "none";
  el("analisisCargando").style.display = "none";

  // Mostrar chips de cuentas de sesión
  el("analisisSesionInfo").style.display    = "";
  el("analisisInputsStandalone").style.display = "none";
  el("btnVolverResultados").style.display   = "";

  const tipoLabel = _tipoSesion === "industrial" ? "Industrial" : "Residencial";
  el("analisisSesionCuentas").innerHTML = _cuentasSesion
    .map((c) => `<span class="chip-cuenta">${escHtml(c)}</span>`)
    .join(" ");
  el("analisisTipoChip").textContent = tipoLabel;
  el("analisisTipoChip").className   =
    `badge-tipo badge-tipo--${_tipoSesion}`;

  mostrarPantalla("analisis");
}

function irAAnalisisStandalone() {
  // Flujo desde "Analizar facturas existentes": ingreso manual de cuentas
  _desde_descarga = false;
  el("headerSubtitulo").textContent = "Análisis Energético";
  el("analisisResultados").innerHTML = "";
  el("analisisError").style.display  = "none";
  el("analisisCargando").style.display = "none";

  // Prellenar cuentas del formulario de descarga si las hay
  const cuentasForm = Array.from(document.querySelectorAll(".cuenta-input"))
    .map((i) => i.value.trim()).filter(Boolean);
  el("analisisCuentas").value = cuentasForm.join(", ");

  el("analisisSesionInfo").style.display    = "none";
  el("analisisInputsStandalone").style.display = "";
  el("btnVolverResultados").style.display   = "none";

  mostrarPantalla("analisis");
}

el("btnAnalizarExistentes").addEventListener("click", irAAnalisisStandalone);
el("btnVerAnalisis").addEventListener("click", irAAnalisisDesdeSesion);
el("btnVolverResultados").addEventListener("click", () => {
  mostrarPantalla("resultados");
  el("headerSubtitulo").textContent = "Proceso finalizado";
});

// ── Calcular consumo ───────────────────────────────────────────────
el("btnCalcular").addEventListener("click", async () => {
  let body = {};

  if (!_desde_descarga) {
    // Modo standalone: se necesita ingresar las cuentas
    const cuentasStr = el("analisisCuentas").value.trim();
    const cuentas = [...new Set(
      cuentasStr.split(/[,\s]+/).map((c) => c.trim()).filter(Boolean)
    )];
    if (cuentas.length === 0) {
      el("analisisError").textContent = "Ingresa al menos un número de cuenta contrato antes de calcular.";
      el("analisisError").style.display = "";
      return;
    }
    body = {
      cuentas,
      tipo_cliente: el("analisisTipoCliente").value || "residencial",
    };
  }
  // Si _desde_descarga = true: body vacío → el servidor usa la sesión activa

  el("analisisCargando").style.display  = "";
  el("analisisError").style.display     = "none";
  el("analisisResultados").innerHTML    = "";

  try {
    const res  = await fetch(_url("api/analizar-consumo"), {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Error al analizar");
    renderizarAnalisis(data.resultados);
  } catch (err) {
    el("analisisError").textContent = err.message;
    el("analisisError").style.display = "";
  } finally {
    el("analisisCargando").style.display = "none";
  }
});

// ── Delegación de clics para exportación y toggle ─────────────────

// Valores editados por el usuario en Anexo C: { "cuenta_anio": { indice: kwh } }
const _editedC = {};
// Resultado original de Anexo C por clave (para export sin corrupción de HTML attributes)
const _resultadosC = {};
// Filas originales de Anexo D por clave (para export)
const _filasD = {};
// Detalle de planilla (franjas/reactiva/demandas) por factura, para export a Excel
const _detallePlanilla = {};
// Edits del usuario en Anexo D: { "cuenta_anio": { idx: { cantidad, horas, energia } } }
const _editedD = {};

el("analisisResultados").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-accion]");
  if (!btn) return;
  const { accion, anio, cuenta, clave: claveBtn } = btn.dataset;

  if (accion === "exportar-excel-c") {
    const clave     = claveBtn || `${cuenta}_${anio}`;
    const resultado = _resultadosC[clave] ? JSON.parse(JSON.stringify(_resultadosC[clave])) : null;
    if (!resultado) return;

    // Aplicar overrides si existen
    const overrides = _editedC[clave] || {};
    if (Object.keys(overrides).length > 0) {
      resultado.facturas = resultado.facturas.map((f, i) => ({
        ...f,
        consumo_total_kwh: overrides[i] !== undefined ? overrides[i] : f.consumo_total_kwh,
      }));
    }

    btn.disabled    = true;
    btn.textContent = "Generando...";
    try {
      const res = await fetch(_url("api/exportar-excel-custom"), {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ resultado, tipo: "C" }),
      });
      if (!res.ok) throw new Error("Error al generar Excel");
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = `anexo_c_${anio}.xlsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled    = false;
      btn.textContent = "Excel Anexo C";
    }
  }

  if (accion === "exportar-excel-d") {
    const clave     = claveBtn || `${cuenta}_${anio}`;
    const filasBase = _filasD[clave] || [];
    const overrides = _editedD[clave] || {};

    const filas = filasBase.map((f, i) => {
      const edit = overrides[i];
      if (!edit) return f;
      return {
        ...f,
        cantidad:           edit.cantidad,
        pot_total_kw:       edit.pot_total,
        carga_instalada_kw: edit.carga,
        demanda_max_kw:     edit.demanda,
        horas_anio:         edit.horas,
        energia_kwh:        edit.energia,
      };
    });

    btn.disabled    = true;
    btn.textContent = "Generando...";
    try {
      const res = await fetch(_url("api/exportar-excel-custom"), {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ resultado: { tabla_anexo_d: filas, anio }, tipo: "D" }),
      });
      if (!res.ok) throw new Error("Error al generar Excel");
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = `anexo_d_${anio}.xlsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled    = false;
      btn.textContent = "Excel Anexo D";
    }
  }

  if (accion === "exportar-excel-detalle") {
    const clave = claveBtn;
    const datos = _detallePlanilla[clave];
    if (!datos) return;

    btn.disabled    = true;
    btn.textContent = "Generando...";
    try {
      const res = await fetch(_url("api/exportar-excel-detalle"), {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ filas: datos.filas, archivo: datos.archivo }),
      });
      if (!res.ok) throw new Error("Error al generar Excel");
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = `detalle_${datos.archivo}.xlsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled    = false;
      btn.textContent = "Excel";
    }
  }

  if (accion === "toggle-facturas") {
    const targetId = btn.dataset.target;
    const section  = document.getElementById(targetId);
    if (!section) return;
    const visible = section.style.display !== "none";
    section.style.display = visible ? "none" : "";
    btn.textContent = visible ? "Ver facturas" : "Ocultar facturas";
  }
});

// ── Renderizado de resultados de análisis ──────────────────────────

function renderizarAnalisis(resultados) {
  const cont = el("analisisResultados");
  cont.innerHTML = "";

  const entries = Object.entries(resultados);
  if (entries.length === 0) {
    cont.innerHTML = '<p style="color:#888;padding:24px 0">No se encontraron facturas válidas.</p>';
    return;
  }

  for (const [clave, res] of entries) {
    const esC         = res.tipo_anexo === "C";
    const consumo     = esC ? res.consumo_anual_kwh : res.consumo_anual_estimado_kwh;
    const tipoLabel   = esC ? "Anexo C — Histórico real" : "Anexo D — Estimado";
    const tipoClass   = esC ? "badge--anexo-c" : "badge--anexo-d";
    const cuenta      = res.cuenta_contrato || "";
    const cuentaLabel = cuenta || "Sin cuenta";
    const facturasId  = `facturas-${clave}`;

    const card = document.createElement("div");
    card.className = "analisis-card";

    const advertHtml = res.advertencias?.length
      ? `<div class="analisis-advertencias">${res.advertencias.map(
          (a) => `<p>⚠ ${escHtml(a)}</p>`
        ).join("")}</div>`
      : "";

    const invalidasHtml = res.invalidas?.length
      ? `<div class="analisis-invalidas">
           <p class="analisis-invalidas__titulo">⚠ ${res.invalidas.length} factura(s) descargada(s) no se usaron en el cálculo:</p>
           ${res.invalidas.map(
             (inv) => `<p>· ${escHtml(inv.archivo || "archivo desconocido")}: ${escHtml(inv.error || "motivo no especificado")}</p>`
           ).join("")}
         </div>`
      : "";

    // Guardar datos originales para export (sin pasar por atributos HTML)
    if (esC) {
      _resultadosC[clave] = res;
    } else {
      _filasD[clave] = res.tabla_anexo_d || [];
    }

    const tablaAnexoHtml = esC
      ? renderizarTablaAnexoC(res, clave)
      : renderizarTablaAnexoD(res.tabla_anexo_d || [], clave);

    const tablaFacturasHtml = `
      <div id="${escHtml(facturasId)}" style="display:none">
        ${renderizarTablaFacturas(res.facturas)}
      </div>
    `;

    const detallePlanillaHtml = renderizarTablasDetallePlanilla(res.facturas, clave);

    const botonExcel = esC
      ? `<button class="boton boton--primario boton--sm"
           data-accion="exportar-excel-c"
           data-clave="${escHtml(clave)}"
           data-anio="${escHtml(String(res.anio))}"
           data-cuenta="${escHtml(cuenta)}">
           Excel Anexo C
         </button>`
      : `<button class="boton boton--primario boton--sm"
           data-accion="exportar-excel-d"
           data-clave="${escHtml(clave)}"
           data-anio="${escHtml(String(res.anio))}"
           data-cuenta="${escHtml(cuenta)}">
           Excel Anexo D
         </button>`;

    card.innerHTML = `
      <div class="analisis-card__header">
        <div>
          <h3 class="analisis-card__titulo">Año ${res.anio}</h3>
          <span class="analisis-cuenta">Cuenta: ${escHtml(cuentaLabel)}</span>
        </div>
        <span class="badge-anexo ${escHtml(tipoClass)}">${escHtml(tipoLabel)}</span>
      </div>
      <div class="analisis-stats">
        <div class="stat">
          <span class="stat__num">${res.facturas_validas}</span>
          <span class="stat__label">Facturas válidas</span>
        </div>
        <div class="stat">
          <span class="stat__num" id="stat-consumo-${escHtml(clave)}">${consumo != null ? consumo.toFixed(2) : "—"}</span>
          <span class="stat__label">kWh ${esC ? "reales" : "estimados"}</span>
        </div>
        <div class="stat">
          <span class="stat__num">${res.promedio_mensual_kwh != null ? res.promedio_mensual_kwh.toFixed(2) : "—"}</span>
          <span class="stat__label">kWh/mes promedio</span>
        </div>
        <div class="stat stat--gris">
          <span class="stat__num">${res.potencia_minima_kwp != null ? res.potencia_minima_kwp.toFixed(2) : "—"}</span>
          <span class="stat__label">kWp mínimo</span>
        </div>
        <div class="stat">
          <span class="stat__num">${res.valor_total_planillas_usd != null ? `$${res.valor_total_planillas_usd.toFixed(2)}` : "—"}</span>
          <span class="stat__label">Total facturado</span>
        </div>
      </div>
      ${advertHtml}
      ${invalidasHtml}
      ${tablaAnexoHtml}
      ${tablaFacturasHtml}
      ${detallePlanillaHtml}
      <div class="analisis-acciones">
        ${botonExcel}
        <button class="boton boton--secundario boton--sm"
          data-accion="toggle-facturas"
          data-target="${escHtml(facturasId)}">
          Ver facturas
        </button>
      </div>
    `;
    cont.appendChild(card);

    if (esC) {
      _registrarEdicionAnexoC(card, res, clave);
    } else {
      _registrarEdicionAnexoD(card, clave);
    }
  }
}

function renderizarTablaAnexoC(res, clave) {
  const facturas = res.facturas || [];
  if (facturas.length === 0) {
    return "<p style='color:#888;font-size:13px;margin-top:12px'>Sin facturas válidas.</p>";
  }
  const filas = facturas.map((f, i) => {
    const mesAnio = f.fecha_hasta ? f.fecha_hasta.substring(3) : "—";
    return `
      <tr>
        <td style="text-align:center">${i + 1}</td>
        <td>${escHtml(mesAnio)}</td>
        <td>
          <input
            class="anexo-input"
            type="number"
            step="0.01"
            min="0"
            data-idx="${i}"
            data-clave="${escHtml(clave)}"
            value="${f.consumo_total_kwh != null ? f.consumo_total_kwh.toFixed(2) : 0}"
          />
        </td>
      </tr>
    `;
  }).join("");

  const total = facturas.reduce((s, f) => s + (f.consumo_total_kwh || 0), 0);
  return `
    <div class="anexo-tabla-wrap">
      <p class="anexo-tabla-titulo">Registro histórico de consumos</p>
      <table class="anexo-tabla anexo-tabla--c">
        <thead>
          <tr><th>#</th><th>Mes/Año</th><th>Consumo (kWh)</th></tr>
        </thead>
        <tbody>${filas}</tbody>
        <tfoot>
          <tr class="anexo-total">
            <td colspan="2">Total</td>
            <td id="total-c-${escHtml(clave)}">${total.toFixed(2)}</td>
          </tr>
        </tfoot>
      </table>
      <p class="anexo-hint">Puedes editar los valores antes de descargar el Excel.</p>
    </div>
  `;
}

function _registrarEdicionAnexoC(card, res, clave) {
  card.querySelectorAll(`.anexo-input[data-clave="${clave}"]`).forEach((input) => {
    input.addEventListener("input", () => {
      if (!_editedC[clave]) _editedC[clave] = {};
      const idx = parseInt(input.dataset.idx, 10);
      _editedC[clave][idx] = parseFloat(input.value) || 0;

      const total = (res.facturas || []).reduce((s, f, i) => {
        const v = _editedC[clave]?.[i] !== undefined ? _editedC[clave][i] : (f.consumo_total_kwh || 0);
        return s + v;
      }, 0);
      const totalEl = document.getElementById(`total-c-${clave}`);
      if (totalEl) totalEl.textContent = total.toFixed(2);
    });
  });
}

function renderizarTablaAnexoD(filas, clave) {
  if (!filas || filas.length === 0) {
    return "<p style='color:#888;font-size:13px;margin-top:12px'>Sin datos de aparatos disponibles.</p>";
  }
  const totalE = filas.reduce((s, f) => s + (f.energia_kwh || 0), 0);
  const n = (v, dec) => v != null ? (+v).toFixed(dec) : "—";
  const rows = filas.map((f, idx) => `
    <tr data-idx="${idx}"
        data-pot="${f.potencia_kw != null ? f.potencia_kw : 0}"
        data-freq="${f.factor_frecuencia != null ? f.factor_frecuencia : 0.8}"
        data-simul="${f.factor_simul != null ? f.factor_simul : 1}">
      <td class="col-c">${f.item}</td>
      <td class="col-nombre">${escHtml(f.nombre)}</td>
      <td class="col-c">
        <span class="d-val">${f.cantidad != null ? f.cantidad : "—"}</span>
        <input class="d-inp anexo-input" type="number" min="0" step="1"
               value="${f.cantidad != null ? f.cantidad : 0}" data-field="cant">
      </td>
      <td class="col-r">${n(f.potencia_kw, 3)}</td>
      <td class="col-r"><span class="d-pot-total">${n(f.pot_total_kw, 3)}</span></td>
      <td class="col-r">${n(f.factor_frecuencia, 2)}</td>
      <td class="col-r"><span class="d-carga">${n(f.carga_instalada_kw, 3)}</span></td>
      <td class="col-r">${n(f.factor_simul, 2)}</td>
      <td class="col-r"><span class="d-demanda">${n(f.demanda_max_kw, 3)}</span></td>
      <td class="col-r">
        <span class="d-val">${f.horas_anio != null ? f.horas_anio : "—"}</span>
        <input class="d-inp anexo-input" type="number" min="0" max="8760" step="1"
               value="${f.horas_anio != null ? f.horas_anio : 0}" data-field="hrs">
      </td>
      <td class="col-r col-bold">
        <span class="d-energia">${f.energia_kwh != null ? f.energia_kwh.toFixed(2) : "—"}</span>
      </td>
    </tr>
  `).join("");

  return `
    <div class="anexo-tabla-wrap">
      <div class="anexo-edit-bar">
        <p class="anexo-tabla-titulo">Estimación de consumo por aparatos eléctricos</p>
        <button class="boton boton--secundario boton--sm" id="btn-edit-d-${escHtml(clave)}">Editar</button>
      </div>
      <div class="anexo-tabla-overflow">
        <table class="anexo-tabla" id="tabla-d-${escHtml(clave)}">
          <thead>
            <tr>
              <th rowspan="2" class="col-c th-item">ITEM</th>
              <th colspan="3" class="col-c th-grupo">APARATOS ELÉCTRICOS Y DE ALUMBRADO</th>
              <th rowspan="2" class="col-r">Potencia Total<br>Instalada (kW)</th>
              <th rowspan="2" class="col-r">Factor de<br>Frecuencia de Uso</th>
              <th rowspan="2" class="col-r">Carga Instalada<br>del Consumidor (kW)</th>
              <th rowspan="2" class="col-r">Factor de<br>Simultaneidad</th>
              <th rowspan="2" class="col-r">Demanda Máxima<br>Unitaria (kW)</th>
              <th rowspan="2" class="col-r">Horas de Uso<br>Anual (h/año)</th>
              <th rowspan="2" class="col-r">Consumo<br>Anual (kWh)</th>
            </tr>
            <tr>
              <th>DESCRIPCIÓN</th>
              <th class="col-c">CANTIDAD</th>
              <th class="col-r">Potencia<br>nominal (kW)</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
          <tfoot>
            <tr class="anexo-total">
              <td colspan="10">CONSUMO ANUAL TOTAL (kWh/año)</td>
              <td class="col-r" id="total-d-${escHtml(clave)}">${totalE.toFixed(2)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p class="anexo-hint">Edita Cant. o Horas/año; las columnas derivadas se recalculan automáticamente.</p>
    </div>
  `;
}

function _registrarEdicionAnexoD(card, clave) {
  const btn   = card.querySelector(`#btn-edit-d-${clave}`);
  const tabla = card.querySelector(`#tabla-d-${clave}`);
  if (!btn || !tabla) return;

  btn.addEventListener("click", () => {
    const editing = tabla.classList.toggle("modo-edicion");
    btn.textContent = editing ? "Cerrar edición" : "Editar";
  });

  tabla.querySelectorAll("tbody tr[data-idx]").forEach((tr) => {
    const potNominal = parseFloat(tr.dataset.pot)   || 0;
    const freq       = parseFloat(tr.dataset.freq)  || 0.8;
    const simul      = parseFloat(tr.dataset.simul) || 1;
    const cantInp    = tr.querySelector('[data-field="cant"]');
    const hrsInp     = tr.querySelector('[data-field="hrs"]');
    const potTotalEl = tr.querySelector(".d-pot-total");
    const cargaEl    = tr.querySelector(".d-carga");
    const demandaEl  = tr.querySelector(".d-demanda");
    const energiaEl  = tr.querySelector(".d-energia");

    const recalc = () => {
      const cant     = parseFloat(cantInp?.value) || 0;
      const hrs      = parseFloat(hrsInp?.value)  || 0;
      const potTotal = Math.round(cant * potNominal * 1000) / 1000;
      const carga    = Math.round(potTotal * freq  * 1000) / 1000;
      const demanda  = Math.round(potTotal * simul * 1000) / 1000;
      const energia  = Math.round(carga * simul * hrs * 100) / 100;

      if (potTotalEl) potTotalEl.textContent = potTotal.toFixed(3);
      if (cargaEl)    cargaEl.textContent    = carga.toFixed(3);
      if (demandaEl)  demandaEl.textContent  = demanda.toFixed(3);
      if (energiaEl)  energiaEl.textContent  = energia.toFixed(2);

      if (!_editedD[clave]) _editedD[clave] = {};
      _editedD[clave][parseInt(tr.dataset.idx, 10)] = {
        cantidad: cant, horas: hrs,
        pot_total: potTotal, carga, demanda, energia,
      };

      let total = 0;
      tabla.querySelectorAll("tbody .d-energia").forEach((s) => {
        total += parseFloat(s.textContent) || 0;
      });
      const totalEl = card.querySelector(`#total-d-${clave}`);
      if (totalEl) totalEl.textContent = total.toFixed(2);
    };

    cantInp?.addEventListener("input", recalc);
    hrsInp?.addEventListener("input", recalc);
  });
}

function renderizarTablaFacturas(facturas) {
  if (!facturas || facturas.length === 0) {
    return "<p style='color:#888;font-size:13px;margin-top:8px'>Sin facturas válidas.</p>";
  }
  const tieneDemanda = facturas.some((f) => f.valor_demanda != null && f.valor_demanda > 0);
  const fmt = (v) => v != null ? (+v).toLocaleString("es-EC", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";

  const filas = facturas.map((f) => {
    const mesAnio = f.fecha_hasta ? f.fecha_hasta.substring(3) : "—";
    return `
      <tr>
        <td>${escHtml(mesAnio)}</td>
        <td class="archivo-nombre">${escHtml(f.factura || "—")}</td>
        <td>${escHtml(f.fecha_desde || "—")} → ${escHtml(f.fecha_hasta || "—")}</td>
        <td style="text-align:center">${f.dias_facturados || "—"}</td>
        <td style="font-weight:600;text-align:right">${
          f.consumo_total_kwh != null ? f.consumo_total_kwh.toFixed(2) : "—"
        }</td>
        <td style="text-align:right">$ ${fmt(f.valor_total_planilla)}</td>
        ${tieneDemanda ? `<td style="text-align:right">$ ${fmt(f.valor_demanda)}</td>` : ""}
      </tr>
    `;
  }).join("");

  return `
    <div class="archivos-tabla-wrap" style="margin-top:8px">
      <table class="archivos-tabla">
        <thead>
          <tr>
            <th>Mes/Año</th>
            <th>N° Factura</th>
            <th>Período</th>
            <th>Días</th>
            <th>kWh</th>
            <th>Valor Planilla</th>
            ${tieneDemanda ? "<th>Valor Demanda</th>" : ""}
          </tr>
        </thead>
        <tbody>${filas}</tbody>
      </table>
    </div>
  `;
}

// Tabla aparte (fuera del Anexo C/D) con el detalle de planilla — franjas
// horarias, energía reactiva y demandas — de cada factura industrial del año.
function renderizarTablasDetallePlanilla(facturas, claveAnio) {
  if (!facturas || facturas.length === 0) return "";
  const fmt = (v) => v != null ? (+v).toLocaleString("es-EC", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";

  return facturas
    .filter((f) => f.detalle_planilla && f.detalle_planilla.length > 0)
    .map((f, i) => {
      const claveFactura = `${claveAnio}_detalle_${i}`;
      _detallePlanilla[claveFactura] = { filas: f.detalle_planilla, archivo: f.factura || f.archivo || claveFactura };

      const filasHtml = f.detalle_planilla.map((d) => `
        <tr>
          <td>${escHtml(d.descripcion || "—")}</td>
          <td>${escHtml(d.fecha_hasta || "—")}</td>
          <td style="text-align:right">${fmt(d.lectura_actual)}</td>
          <td style="text-align:right">${fmt(d.lectura_anterior)}</td>
          <td style="text-align:right">${fmt(d.consumo_subtotal)}</td>
          <td style="text-align:right">${fmt(d.consumo_interno_transformador)}</td>
          <td style="font-weight:600;text-align:right">${fmt(d.consumo_total)}</td>
          <td style="text-align:center">${escHtml(d.unidad || "—")}</td>
          <td style="text-align:right">$ ${fmt(d.monto)}</td>
        </tr>
      `).join("");

      return `
        <div class="analisis-detalle-planilla">
          <div class="analisis-detalle-planilla__header">
            <h4>Detalle de planilla — Factura ${escHtml(f.factura || "—")} (${escHtml(f.fecha_hasta || "—")})</h4>
            <button class="boton boton--secundario boton--sm"
              data-accion="exportar-excel-detalle"
              data-clave="${escHtml(claveFactura)}">
              Excel
            </button>
          </div>
          <div class="archivos-tabla-wrap">
            <table class="archivos-tabla">
              <thead>
                <tr>
                  <th>Descripción</th>
                  <th>Fecha Hasta</th>
                  <th>Lectura Actual</th>
                  <th>Lectura Anterior</th>
                  <th>Consumo Subtotal</th>
                  <th>Consumo interno Transformador</th>
                  <th>Consumo Total</th>
                  <th>Unidad</th>
                  <th>Monto ($)</th>
                </tr>
              </thead>
              <tbody>${filasHtml}</tbody>
            </table>
          </div>
        </div>
      `;
    })
    .join("");
}

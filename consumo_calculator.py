"""
Módulo de análisis de consumo energético a partir de facturas PDF de la EEQ.

Funcionalidades principales:
  - Escanea una carpeta de PDFs y extrae los datos de consumo de cada factura.
  - Soporta clientes residenciales (consumo único por período) e industriales
    (4 franjas horarias A, B, C, D).
  - Agrupa facturas por (cuenta_contrato, año) usando la fecha_hasta de cada
    factura para determinar el año de facturación.
  - Deduplica por número de factura y, dentro de un mismo mes, conserva la
    factura más reciente.
  - Decide automáticamente si corresponde Anexo C (histórico real, ≥12 facturas
    válidas) o Anexo D (estimado por extrapolación de días, <12 facturas).
  - Para facturas industriales, incluye el desglose por franjas en el resultado.
"""

import json
from datetime import datetime
from pathlib import Path

from eeq_pdf_parser import parse_factura_eeq, parse_factura_eeq_industrial

CONSUMO_BASE_ANEXO_D = 14747.28  # kWh/año del template AO_Macro.xlsm


def _mes_anio(fecha: str) -> tuple[int, int] | None:
    """
    Extrae el mes y año de una fecha en formato DD-MM-YYYY.

    Parámetros:
        fecha: Cadena de fecha en formato 'DD-MM-YYYY'.

    Retorna:
        Tupla (mes, año) o None si la fecha es inválida.
    """
    try:
        d = datetime.strptime(fecha, "%d-%m-%Y")
        return (d.month, d.year)
    except (ValueError, TypeError):
        return None


def analizar_carpeta(
    carpeta: Path,
    anio_filtro: int | None = None,
    cuentas_filtro: list | None = None,
    tipo_cliente: str = "residencial",
    cantidad_max: int | None = None,
) -> dict:
    """
    Lee todos los PDFs en `carpeta` (y subcarpetas) y calcula el consumo energético.

    Agrupa los resultados por (cuenta_contrato, año) usando la fecha_hasta de cada
    factura. Retorna un diccionario con clave "{cuenta}_{año}".

    Parámetros:
        carpeta: Directorio raíz donde buscar PDFs (se escanea recursivamente).
        anio_filtro: Si se especifica, solo incluye facturas de ese año.
        cuentas_filtro: Si se especifica, solo procesa las cuentas de la lista.
        tipo_cliente: 'residencial' o 'industrial'. Determina el parser a usar.
        cantidad_max: Número máximo de facturas a considerar (las más recientes).
                      Si es None, se usan todas. Útil para 'últimas 12' o 'últimas 24'.

    Retorna:
        Diccionario donde cada clave es "{cuenta}_{año}" y el valor es el
        resultado del análisis para ese par cuenta/año.
    """
    # Seleccionar el parser según el tipo de cliente
    parser = (
        parse_factura_eeq_industrial
        if tipo_cliente == "industrial"
        else parse_factura_eeq
    )

    pdfs = sorted(carpeta.rglob("*.pdf"))
    facturas_raw = [parser(p) for p in pdfs]

    invalidas = []
    validas = []

    for f in facturas_raw:
        if f.get("error") or "consumo_total_kwh" not in f or not f.get("fecha_hasta"):
            invalidas.append(f)
            continue

        ma = _mes_anio(f["fecha_hasta"])
        if ma is None:
            f["error"] = f"Fecha_hasta inválida: {f.get('fecha_hasta')}"
            invalidas.append(f)
            continue

        validas.append(f)

    # Limitar a las N facturas más recientes si se especifica cantidad_max
    if cantidad_max and len(validas) > cantidad_max:
        validas_ordenadas = sorted(
            validas,
            key=lambda x: datetime.strptime(x["fecha_hasta"], "%d-%m-%Y"),
            reverse=True,
        )
        validas = validas_ordenadas[:cantidad_max]

    # Agrupar por (cuenta, año)
    por_cuenta_anio: dict[tuple, list] = {}
    for f in validas:
        _, anio = _mes_anio(f["fecha_hasta"])

        if anio_filtro and anio != anio_filtro:
            continue

        cuenta = f.get("cuenta_contrato", "sin_cuenta")
        if cuentas_filtro and cuenta not in cuentas_filtro:
            continue

        por_cuenta_anio.setdefault((cuenta, anio), []).append(f)

    resultados = {}
    for (cuenta, anio), facturas in por_cuenta_anio.items():
        key = f"{cuenta}_{anio}"
        resultados[key] = _calcular_anio(facturas, anio, invalidas, cuenta, tipo_cliente)

    return resultados


def analizar_desde_json(
    ruta_json: Path,
    tipo_cliente: str = "residencial",
) -> dict:
    """
    Analiza el consumo leyendo datos desde un archivo facturas.json previamente
    guardado, en lugar de escanear PDFs.

    Útil cuando los PDFs ya han sido eliminados del servidor y solo queda el JSON
    con los datos extraídos.

    Parámetros:
        ruta_json: Ruta al archivo facturas.json de la cuenta.
        tipo_cliente: 'residencial' o 'industrial'.

    Retorna:
        Diccionario de resultados por "{cuenta}_{año}", igual que analizar_carpeta.
    """
    if not ruta_json.exists():
        return {}

    with open(ruta_json, encoding="utf-8") as fp:
        facturas_raw: list[dict] = json.load(fp)

    invalidas = []
    por_cuenta_anio: dict[tuple, list] = {}

    for f in facturas_raw:
        if f.get("error") or "consumo_total_kwh" not in f or not f.get("fecha_hasta"):
            invalidas.append(f)
            continue

        ma = _mes_anio(f["fecha_hasta"])
        if ma is None:
            invalidas.append(f)
            continue

        _, anio = ma
        cuenta = f.get("cuenta_contrato", "sin_cuenta")
        por_cuenta_anio.setdefault((cuenta, anio), []).append(f)

    resultados = {}
    for (cuenta, anio), facturas in por_cuenta_anio.items():
        key = f"{cuenta}_{anio}"
        resultados[key] = _calcular_anio(facturas, anio, invalidas, cuenta, tipo_cliente)

    return resultados


def _calcular_anio(
    facturas: list,
    anio: int,
    invalidas: list,
    cuenta: str = "",
    tipo_cliente: str = "residencial",
) -> dict:
    """
    Calcula el consumo anual y determina el tipo de Anexo (C o D) para un
    conjunto de facturas del mismo año y cuenta.

    Aplica deduplicación por número de factura y por mes, luego decide:
      - Anexo C (histórico real): si hay ≥12 facturas válidas.
      - Anexo D (estimado): si hay <12 facturas, extrapola a 365 días.

    Parámetros:
        facturas: Lista de facturas del mismo año y cuenta.
        anio: Año de facturación.
        invalidas: Lista acumulada de facturas inválidas (se agrega aquí si hay nuevas).
        cuenta: Número de cuenta contrato.
        tipo_cliente: 'residencial' o 'industrial'.

    Retorna:
        Diccionario con el resultado del análisis incluyendo tipo_anexo, consumos,
        facturas válidas, advertencias y (para industrial) franjas_mensuales.
    """
    advertencias: list[str] = []

    # ── 1. Deduplicar por número de factura ────────────────────────────────
    seen_factura: dict[str, dict] = {}
    for f in facturas:
        key = f.get("factura") or f["archivo"]
        if key in seen_factura:
            advertencias.append(f"Factura duplicada ignorada: {key}")
        else:
            seen_factura[key] = f
    facturas = list(seen_factura.values())

    # ── 2. Agrupar por mes; conservar la más reciente si hay duplicados ────
    por_mes: dict[int, dict] = {}
    for f in facturas:
        ma = _mes_anio(f["fecha_hasta"])
        if ma is None:
            continue
        mes, _ = ma
        if mes not in por_mes:
            por_mes[mes] = f
        else:
            nueva = f.get("fecha_emision", "01-01-1900")
            exist = por_mes[mes].get("fecha_emision", "01-01-1900")
            if nueva > exist:
                advertencias.append(
                    f"Mes {mes}/{anio}: se usa la factura más reciente "
                    f"({f.get('factura', f['archivo'])})"
                )
                por_mes[mes] = f
            else:
                advertencias.append(
                    f"Mes {mes}/{anio}: factura duplicada ignorada "
                    f"({f.get('factura', f['archivo'])})"
                )

    # ── 3. Ordenar por fecha_hasta ─────────────────────────────────────────
    facturas_validas = sorted(
        por_mes.values(),
        key=lambda x: datetime.strptime(x["fecha_hasta"], "%d-%m-%Y"),
    )
    n = len(facturas_validas)
    consumo_anual = round(sum(f["consumo_total_kwh"] for f in facturas_validas), 2)
    promedio = round(consumo_anual / n, 2) if n > 0 else 0.0
    valor_total_planillas = round(sum(f.get("valor_total_planilla", 0) for f in facturas_validas), 2)

    # ── 4. Decisión Anexo C / D ────────────────────────────────────────────
    if n >= 12:
        tipo        = "C"
        modo        = "historico_real"
        consumo_est = None
    else:
        tipo = "D"
        modo = "estimado"
        advertencias.append(
            f"Solo {n} factura(s) válida(s) para {anio}. "
            f"Se usa Anexo D (estimado por facturas insuficientes)."
        )
        total_dias = sum(f.get("dias_facturados", 30) for f in facturas_validas)
        consumo_est = round(consumo_anual / total_dias * 365, 2) if total_dias > 0 else consumo_anual

    # ── 5. Dimensionamiento solar ──────────────────────────────────────────
    hsp = 1200
    pr  = 0.8
    consumo_solar = consumo_est if tipo == "D" else consumo_anual
    pot_min = round(consumo_solar / (hsp * pr), 2) if consumo_solar else 0.0

    razon_social = next(
        (f.get("razon_social", "") for f in facturas_validas if f.get("razon_social")),
        "",
    )

    resultado = {
        "cuenta_contrato":            cuenta,
        "razon_social":               razon_social,
        "tipo_cliente":               tipo_cliente,
        "anio":                       anio,
        "tipo_anexo":                 tipo,
        "modo_calculo":               modo,
        "facturas_encontradas":       len(facturas),
        "facturas_validas":           n,
        "meses_cubiertos":            sorted(por_mes.keys()),
        "consumo_anual_kwh":          consumo_anual,
        "consumo_anual_estimado_kwh": consumo_est,
        "promedio_mensual_kwh":       promedio,
        "valor_total_planillas_usd":  valor_total_planillas,
        "hsp":                        hsp,
        "pr":                         pr,
        "potencia_minima_kwp":        pot_min,
        "facturas":                   [_serializar(f) for f in facturas_validas],
        "invalidas": [
            {"archivo": f.get("archivo", ""), "error": f.get("error")}
            for f in invalidas
        ],
        "advertencias": advertencias,
    }

    # ── 6. Desglose por franjas (solo industrial) ──────────────────────────
    if tipo_cliente == "industrial":
        resultado["franjas_mensuales"] = _calcular_franjas_mensuales(facturas_validas)

    return resultado


def analizar_global(
    carpeta: Path,
    cuentas_filtro: list | None = None,
    tipo_cliente: str = "residencial",
    cantidad_max: int | None = None,
) -> dict:
    """
    Analiza todas las facturas de una carpeta como un conjunto global, sin agrupar por año.

    A diferencia de analizar_carpeta(), que agrupa por (cuenta, año) y decide el tipo
    de Anexo por separado para cada año, esta función trata todas las facturas como un
    único bloque. Así, facturas de 2024 + 2025 + 2026 se cuentan juntas para decidir
    si hay suficientes meses para el Anexo C.

    Retorna un dict con clave "{cuenta}_global" por cada cuenta encontrada.

    Parámetros:
        carpeta: Directorio con los PDFs (se escanea recursivamente).
        cuentas_filtro: Si se especifica, solo procesa esas cuentas.
        tipo_cliente: 'residencial' o 'industrial'.
        cantidad_max: Máximo de facturas más recientes a considerar.
    """
    parser = (
        parse_factura_eeq_industrial
        if tipo_cliente == "industrial"
        else parse_factura_eeq
    )

    pdfs = sorted(carpeta.rglob("*.pdf"))
    facturas_raw = [parser(p) for p in pdfs]

    invalidas = []
    validas = []

    for f in facturas_raw:
        if f.get("error") or "consumo_total_kwh" not in f or not f.get("fecha_hasta"):
            invalidas.append(f)
            continue

        ma = _mes_anio(f["fecha_hasta"])
        if ma is None:
            f["error"] = f"Fecha_hasta inválida: {f.get('fecha_hasta')}"
            invalidas.append(f)
            continue

        cuenta = f.get("cuenta_contrato", "sin_cuenta")
        if cuentas_filtro and cuenta not in cuentas_filtro:
            continue

        validas.append(f)

    if not validas:
        return {}

    # Dedup por (mes, año) ANTES de aplicar cantidad_max para que los duplicados
    # no consuman slots y siempre se obtengan N meses únicos.
    seen_ma: dict[tuple, dict] = {}
    for f in sorted(validas, key=lambda x: datetime.strptime(x["fecha_hasta"], "%d-%m-%Y"), reverse=True):
        ma = _mes_anio(f["fecha_hasta"])
        if ma not in seen_ma:
            seen_ma[ma] = f
    validas_deduped = sorted(seen_ma.values(), key=lambda x: datetime.strptime(x["fecha_hasta"], "%d-%m-%Y"), reverse=True)
    if cantidad_max:
        validas_deduped = validas_deduped[:cantidad_max]

    por_cuenta: dict[str, list] = {}
    for f in validas_deduped:
        cuenta = f.get("cuenta_contrato", "sin_cuenta")
        por_cuenta.setdefault(cuenta, []).append(f)

    resultados = {}
    for cuenta, facturas in por_cuenta.items():
        resultados[f"{cuenta}_global"] = _calcular_global(facturas, invalidas, cuenta, tipo_cliente)

    return resultados


def analizar_global_desde_json(
    ruta_json: Path,
    tipo_cliente: str = "residencial",
    cantidad_max: int | None = None,
) -> dict:
    """
    Igual que analizar_global() pero leyendo datos desde un facturas.json previamente
    guardado, en lugar de escanear PDFs directamente.

    Parámetros:
        ruta_json: Ruta al archivo facturas.json de la cuenta.
        tipo_cliente: 'residencial' o 'industrial'.
        cantidad_max: Máximo de facturas más recientes a considerar.
    """
    if not ruta_json.exists():
        return {}

    with open(ruta_json, encoding="utf-8") as fp:
        facturas_raw: list[dict] = json.load(fp)

    invalidas = []
    validas = []

    for f in facturas_raw:
        if f.get("error") or "consumo_total_kwh" not in f or not f.get("fecha_hasta"):
            invalidas.append(f)
            continue

        ma = _mes_anio(f["fecha_hasta"])
        if ma is None:
            invalidas.append(f)
            continue

        validas.append(f)

    if not validas:
        return {}

    # Dedup por (mes, año) ANTES de aplicar cantidad_max para que los duplicados
    # no consuman slots y siempre se obtengan N meses únicos.
    seen_ma: dict[tuple, dict] = {}
    for f in sorted(validas, key=lambda x: datetime.strptime(x["fecha_hasta"], "%d-%m-%Y"), reverse=True):
        ma = _mes_anio(f["fecha_hasta"])
        if ma not in seen_ma:
            seen_ma[ma] = f
    validas_deduped = sorted(seen_ma.values(), key=lambda x: datetime.strptime(x["fecha_hasta"], "%d-%m-%Y"), reverse=True)
    if cantidad_max:
        validas_deduped = validas_deduped[:cantidad_max]

    por_cuenta: dict[str, list] = {}
    for f in validas_deduped:
        cuenta = f.get("cuenta_contrato", "sin_cuenta")
        por_cuenta.setdefault(cuenta, []).append(f)

    resultados = {}
    for cuenta, facturas in por_cuenta.items():
        resultados[f"{cuenta}_global"] = _calcular_global(facturas, invalidas, cuenta, tipo_cliente)

    return resultados


def _calcular_global(
    facturas: list,
    invalidas: list,
    cuenta: str,
    tipo_cliente: str = "residencial",
) -> dict:
    """
    Calcula el consumo global de un conjunto de facturas que pueden abarcar múltiples años.

    No agrupa por año — trata todas las facturas como un único bloque. La decisión
    Anexo C / D se basa en el número total de meses únicos cubiertos.

    Parámetros:
        facturas: Todas las facturas de la cuenta (pueden ser de distintos años).
        invalidas: Lista acumulada de facturas inválidas.
        cuenta: Número de cuenta contrato.
        tipo_cliente: 'residencial' o 'industrial'.
    """
    advertencias: list[str] = []

    # 1. Deduplicar por número de factura
    seen_factura: dict[str, dict] = {}
    for f in facturas:
        key = f.get("factura") or f["archivo"]
        if key in seen_factura:
            advertencias.append(f"Factura duplicada ignorada: {key}")
        else:
            seen_factura[key] = f
    facturas = list(seen_factura.values())

    # 2. Deduplicar por (mes, año): conservar la más reciente en cada período
    por_mes_anio: dict[tuple, dict] = {}
    for f in facturas:
        ma = _mes_anio(f["fecha_hasta"])
        if ma is None:
            continue
        mes, anio = ma
        clave = (mes, anio)
        if clave not in por_mes_anio:
            por_mes_anio[clave] = f
        else:
            nueva = f.get("fecha_emision", "01-01-1900")
            exist = por_mes_anio[clave].get("fecha_emision", "01-01-1900")
            if nueva > exist:
                advertencias.append(
                    f"Mes {mes}/{anio}: se usa la factura más reciente "
                    f"({f.get('factura', f['archivo'])})"
                )
                por_mes_anio[clave] = f
            else:
                advertencias.append(
                    f"Mes {mes}/{anio}: factura duplicada ignorada "
                    f"({f.get('factura', f['archivo'])})"
                )

    # 3. Ordenar cronológicamente (ASC) para el Anexo C
    facturas_validas = sorted(
        por_mes_anio.values(),
        key=lambda x: datetime.strptime(x["fecha_hasta"], "%d-%m-%Y"),
    )
    n = len(facturas_validas)
    consumo_anual = round(sum(f["consumo_total_kwh"] for f in facturas_validas), 2)
    promedio = round(consumo_anual / n, 2) if n > 0 else 0.0
    valor_total_planillas = round(sum(f.get("valor_total_planilla", 0) for f in facturas_validas), 2)

    # 4. Decisión Anexo C / D según total de meses únicos
    if n >= 12:
        tipo = "C"
        modo = "historico_real"
        consumo_est = None
    else:
        tipo = "D"
        modo = "estimado"
        advertencias.append(
            f"Solo {n} mes(es) único(s) disponibles. "
            f"Se usa Anexo D (estimado por facturas insuficientes)."
        )
        total_dias = sum(f.get("dias_facturados", 30) for f in facturas_validas)
        consumo_est = round(consumo_anual / total_dias * 365, 2) if total_dias > 0 else consumo_anual

    hsp = 1200
    pr = 0.8
    consumo_solar = consumo_est if tipo == "D" else consumo_anual
    pot_min = round(consumo_solar / (hsp * pr), 2) if consumo_solar else 0.0

    razon_social = next(
        (f.get("razon_social", "") for f in facturas_validas if f.get("razon_social")),
        "",
    )

    # Año de referencia = el más reciente del conjunto (para nombre de archivo, etc.)
    anio_ref = (
        _mes_anio(facturas_validas[-1]["fecha_hasta"])[1]
        if facturas_validas else datetime.now().year
    )

    resultado = {
        "cuenta_contrato":            cuenta,
        "razon_social":               razon_social,
        "tipo_cliente":               tipo_cliente,
        "anio":                       anio_ref,
        "tipo_anexo":                 tipo,
        "modo_calculo":               modo,
        "facturas_encontradas":       len(facturas),
        "facturas_validas":           n,
        "meses_cubiertos":            sorted(set(k[0] for k in por_mes_anio.keys())),
        "consumo_anual_kwh":          consumo_anual,
        "consumo_anual_estimado_kwh": consumo_est,
        "promedio_mensual_kwh":       promedio,
        "valor_total_planillas_usd":  valor_total_planillas,
        "hsp":                        hsp,
        "pr":                         pr,
        "potencia_minima_kwp":        pot_min,
        "facturas":                   [_serializar(f) for f in facturas_validas],
        "invalidas": [
            {"archivo": f.get("archivo", ""), "error": f.get("error")}
            for f in invalidas
        ],
        "advertencias": advertencias,
    }

    if tipo_cliente == "industrial":
        resultado["franjas_mensuales"] = _calcular_franjas_mensuales(facturas_validas)

    return resultado


def _calcular_franjas_mensuales(facturas_validas: list) -> list[dict]:
    """
    Construye el resumen mensual con desglose por franjas horarias A/B/C/D
    para facturas industriales.

    Parámetros:
        facturas_validas: Lista de facturas del año ya deduplicadas y ordenadas.

    Retorna:
        Lista de dicts con {mes, anio, franja_a, franja_b, franja_c, franja_d, total}.
    """
    franjas = []
    for f in facturas_validas:
        ma = _mes_anio(f.get("fecha_hasta", ""))
        if ma is None:
            continue
        mes, anio = ma
        franjas.append({
            "mes":      mes,
            "anio":     anio,
            "franja_a": f.get("energia_hor_a_kwh", 0.0),
            "franja_b": f.get("energia_hor_b_kwh", 0.0),
            "franja_c": f.get("energia_hor_c_kwh", 0.0),
            "franja_d": f.get("energia_hor_d_kwh", 0.0),
            "total":    f.get("consumo_total_kwh", 0.0),
        })
    return franjas


def _serializar(f: dict) -> dict:
    """
    Serializa una factura a un diccionario con solo los campos necesarios
    para los Anexos C/D y para almacenar en facturas.json.

    Parámetros:
        f: Diccionario de factura con todos los campos extraídos del PDF.

    Retorna:
        Diccionario reducido con campos relevantes para el análisis.
    """
    base = {
        "factura":                      f.get("factura") or Path(f.get("archivo", "")).stem,
        "cuenta_contrato":              f.get("cuenta_contrato", ""),
        "razon_social":                 f.get("razon_social", ""),
        "fecha_emision":                f.get("fecha_emision", ""),
        "fecha_desde":                  f.get("fecha_desde", ""),
        "fecha_hasta":                  f.get("fecha_hasta", ""),
        "dias_facturados":              f.get("dias_facturados", 0),
        "lectura_actual":               f.get("lectura_actual", 0),
        "lectura_anterior":             f.get("lectura_anterior", 0),
        "diferencia":                   f.get("diferencia", 0),
        "consumo_interno_transformador": f.get("consumo_interno_transformador", 0),
        "consumo_total_kwh":            f.get("consumo_total_kwh", 0),
        "monto_energia":                f.get("monto_energia", 0),
        "valor_total_planilla":         f.get("valor_total_planilla", 0),
        "archivo":                      f.get("archivo", ""),
    }
    # Preservar franjas horarias y valor demanda si existen (facturas industriales)
    for campo in ("energia_hor_a_kwh", "energia_hor_b_kwh",
                  "energia_hor_c_kwh", "energia_hor_d_kwh", "valor_demanda"):
        if campo in f:
            base[campo] = f[campo]
    # Desglose completo de la planilla (franjas, reactiva, demandas) — solo
    # está presente en facturas con tarifa horaria diferenciada
    if f.get("detalle_planilla"):
        base["detalle_planilla"] = f["detalle_planilla"]
    return base

"""
Módulo de extracción de datos de facturas PDF de la Empresa Eléctrica Quito (EEQ).

Soporta dos tipos de cliente:
  - Residencial: una sola fila de 'Energía activa total' con el consumo del período.
  - Industrial: cuatro franjas horarias (A, B, C, D) definidas por la tarifa MTCGCD32
    (MT Industrial con Demanda Horaria Diferenciada). El consumo total es la suma
    de las cuatro franjas.

La extracción usa PyMuPDF (fitz) como método principal y pdftotext (poppler) como
fallback si fitz no está disponible.
"""

import re
import subprocess
from pathlib import Path

try:
    import fitz
    _FITZ_OK = True
except ImportError:
    _FITZ_OK = False


# ---------------------------------------------------------------------------
# Utilidades comunes
# ---------------------------------------------------------------------------

def _extraer_valor_total(lineas: list) -> float:
    """
    Extrae el VALOR TOTAL de la planilla (Sector Eléctrico + Bomberos + Basura).

    PyMuPDF extrae primero el monto que aparece en la esquina superior del
    PDF (antes de la etiqueta "VALOR TOTAL:"), así que casi siempre es la
    línea 0 del texto. Se usa como fuente principal porque el bloque
    "RESUMEN DE VALORES" al final del documento imprime varias etiquetas
    seguidas (Total Sector Eléctrico, Total Recaudación de Terceros, VALOR
    TOTAL) y luego sus valores en el mismo orden — tomar "el primer número
    después de la etiqueta VALOR TOTAL" agarra un subtotal previo, no el total.
    """
    if lineas:
        primera = lineas[0].strip()
        if re.match(r'^\d[\d.,]*$', primera):
            try:
                val = _parse_num(primera)
                if val > 0:
                    return val
            except ValueError:
                pass

    # Fallback si la línea 0 no es numérica: buscar tras la última etiqueta.
    ultimo_idx = None
    for i, linea in enumerate(lineas):
        if re.search(r'VALOR\s+TOTAL', linea, re.IGNORECASE):
            ultimo_idx = i
    if ultimo_idx is None:
        return 0.0
    for j in range(ultimo_idx, min(ultimo_idx + 4, len(lineas))):
        nums = re.findall(r'\d[\d.,]*', lineas[j])
        for n in reversed(nums):
            try:
                val = _parse_num(n)
                if val > 0:
                    return val
            except ValueError:
                continue
    return 0.0


def _extraer_valor_demanda(lineas: list) -> float:
    """
    Extrae el 'Valor Demanda' del cuadro gris junto al gráfico (solo facturas industriales).
    Retorna 0.0 si no se encuentra.
    """
    for i, linea in enumerate(lineas):
        if re.search(r'Valor\s+Demanda', linea, re.IGNORECASE):
            nums = re.findall(r'\d[\d.,]*', linea)
            for n in reversed(nums):
                try:
                    val = _parse_num(n)
                    if val > 0:
                        return val
                except ValueError:
                    continue
            for j in range(i + 1, min(i + 4, len(lineas))):
                tok = lineas[j].strip()
                if not tok:
                    continue
                try:
                    return _parse_num(tok)
                except ValueError:
                    break
    return 0.0


def _extraer_texto(pdf_path: Path) -> str:
    """
    Lee el contenido de texto de un PDF.

    Intenta primero con PyMuPDF; si no está disponible, usa pdftotext (poppler).

    Parámetros:
        pdf_path: Ruta al archivo PDF.

    Retorna:
        Texto completo del PDF como string.
    """
    if _FITZ_OK:
        doc = fitz.open(str(pdf_path))
        partes = [page.get_text("text") for page in doc]
        doc.close()
        return "\n".join(partes)
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext falló: {result.stderr.strip()}")
    return result.stdout


def _parse_num(s: str) -> float:
    """
    Convierte un número en formato europeo (1.234,56 o 1234,56) a float.

    Parámetros:
        s: Cadena numérica con separadores europeos.

    Retorna:
        Valor numérico como float.
    """
    return float(s.replace(".", "").replace(",", "."))


def _extraer_cabecera(texto: str) -> dict:
    """
    Extrae los campos de cabecera comunes a facturas residenciales e industriales:
    número de factura, cuenta contrato, fecha de emisión, fecha desde y días facturados.

    Parámetros:
        texto: Texto completo extraído del PDF.

    Retorna:
        Diccionario con los campos encontrados.
    """
    c = {}
    lineas = texto.splitlines()

    m = re.search(r'Fecha desde\s+(\d{2}-\d{2}-\d{4})', texto)
    if m:
        c["fecha_desde"] = m.group(1).strip()

    m = re.search(r'D[ií]as facturados\s+(\d+)', texto)
    if m:
        c["dias_facturados"] = int(m.group(1))

    _IDX = {
        "factura":         re.compile(r'^Nro\.\s*factura$', re.IGNORECASE),
        "cuenta_contrato": re.compile(r'^CUENTA CONTRATO$'),
        "fecha_emision":   re.compile(r'^Fecha de emisi[oó]n$', re.IGNORECASE),
        "razon_social":    re.compile(r'^NOMBRE DEL CLIENTE$', re.IGNORECASE),
    }
    _VAL = {
        "factura":         re.compile(r'^\d{3}-\d{3}-\d+$'),
        "cuenta_contrato": re.compile(r'^\d{6,}$'),
        "fecha_emision":   re.compile(r'^\d{2}-\d{2}-\d{4}$'),
        # Nombre del cliente: cualquier texto 3-80 chars que no sea fecha, número largo
        # ni línea de etiqueta conocida
        "razon_social":    re.compile(
            r'^(?!\d{2}-\d{2}-\d{4}$)(?!\d{5,}$)'
            r'(?!(?:Nro\.|FECHA|CUENTA|DIREC|TELEF|CORREO|RUC\b|NOMBRE|SERVICIO))'
            r'[\w\s\.,\-\/&()À-ÿ]{3,80}$',
            re.IGNORECASE,
        ),
    }
    for i, linea in enumerate(lineas):
        stripped = linea.strip()
        for campo, pat in _IDX.items():
            if campo in c:
                continue
            if pat.match(stripped):
                for j in range(i + 1, min(i + 20, len(lineas))):
                    candidato = lineas[j].strip()
                    if _VAL[campo].match(candidato):
                        c[campo] = candidato
                        break

    return c


# ---------------------------------------------------------------------------
# Parser residencial
# ---------------------------------------------------------------------------

def _parsear_linea_energia(linea: str) -> dict | None:
    """
    Parsea la fila 'Energía activa total' de una factura residencial.

    Columnas esperadas (header del PDF):
      Descripción | Fecha Hasta | Lectura Actual | Lectura Anterior |
      Diferencia Consumo (puede estar vacía) | Consumo Subtotal |
      Consumo interno Transformador | Consumo Total | Unidad | Monto ($)

    Estrategia: split por whitespace del texto después de 'activa total'.
      - Desde el inicio: fecha (DD-MM-YYYY), lect_actual, lect_anterior
      - Desde el final:  monto, 'kWh', consumo_total, consumo_interno, consumo_subtotal
      - Diferencia: si hay 9+ tokens entre inicio y final, es el token[3]

    Parámetros:
        linea: Línea de texto que contiene 'Energía activa total'.

    Retorna:
        Diccionario con los campos energéticos o None si no se puede parsear.
    """
    m = re.split(r'activa\s+total', linea, maxsplit=1, flags=re.IGNORECASE)
    if len(m) < 2:
        return None

    tokens = m[1].strip().split()

    # Cuando el consumo del período es 0, el PDF no imprime la celda de monto
    # (kWh queda como último token). Se asume monto 0,00 en ese caso.
    if tokens and tokens[-1].lower() == "kwh":
        tokens = tokens + ["0,00"]

    if len(tokens) < 7:
        return None

    if tokens[-2].lower() != "kwh":
        return None

    try:
        monto         = _parse_num(tokens[-1])
        consumo_total = _parse_num(tokens[-3])
        consumo_int   = _parse_num(tokens[-4])
        consumo_sub   = _parse_num(tokens[-5])

        fecha_hasta    = tokens[0]
        lectura_actual = _parse_num(tokens[1])
        lectura_ant    = _parse_num(tokens[2])
    except (ValueError, IndexError):
        return None

    if not re.match(r'^\d{2}-\d{2}-\d{4}$', fecha_hasta):
        return None

    # La diferencia aparece como token extra cuando hay 9+ tokens
    diferencia = _parse_num(tokens[3]) if len(tokens) >= 9 else consumo_sub

    return {
        "fecha_hasta": fecha_hasta,
        "lectura_actual": lectura_actual,
        "lectura_anterior": lectura_ant,
        "diferencia": diferencia,
        "consumo_interno_transformador": consumo_int,
        "consumo_total_kwh": consumo_total,
        "monto_energia": monto,
    }


def parse_factura_eeq(pdf_path, _es_fallback: bool = False) -> dict:
    """
    Extrae datos de una factura PDF residencial de la EEQ.

    Busca la fila 'Energía activa total' para obtener el consumo del período,
    junto con lecturas de medidor, fechas y monto.

    Parámetros:
        pdf_path: Ruta al archivo PDF (str o Path).
        _es_fallback: Uso interno — evita el reintento cruzado con el parser
            industrial cuando esta llamada YA es ese reintento (previene
            recursión infinita si un PDF no coincide con ningún formato).

    Retorna:
        Diccionario con los campos extraídos. Si algo falla, el campo
        'error' describe el problema.
    """
    path = Path(pdf_path)
    resultado: dict = {"archivo": str(path), "error": None}

    try:
        texto = _extraer_texto(path)
    except Exception as e:
        resultado["error"] = f"No se pudo leer el PDF: {e}"
        return resultado

    try:
        cabecera = _extraer_cabecera(texto)
        resultado.update(cabecera)

        lineas = texto.splitlines()
        for i, linea in enumerate(lineas):
            if re.search(r'energ[íi]a\s+activa\s+total', linea, re.IGNORECASE):
                datos = _parsear_linea_energia(linea)
                if datos is None:
                    # PyMuPDF a veces emite un campo por línea; recolectar tokens
                    following: list[str] = []
                    for j in range(i + 1, min(i + 14, len(lineas))):
                        tok = lineas[j].strip()
                        if not tok:
                            continue
                        following.append(tok)
                        if tok.lower() == "kwh":
                            # El monto (si existe) es el siguiente token numérico.
                            # Cuando el monto es 0 el PDF no imprime esa celda, así
                            # que no hay que arrastrar texto de la sección siguiente.
                            for k in range(j + 1, min(j + 4, len(lineas))):
                                extra = lineas[k].strip()
                                if not extra:
                                    continue
                                if re.match(r'^-?\d[\d.,]*-?\s*$', extra):
                                    following.append(extra)
                                break
                            break
                    combined = linea + " " + " ".join(following)
                    datos = _parsear_linea_energia(combined)
                if datos:
                    resultado.update(datos)
                    resultado["fecha_hasta"] = datos["fecha_hasta"]
                    break

        if "consumo_total_kwh" not in resultado:
            # No tiene la fila "Energía activa total": puede ser una factura con
            # tarifa horaria diferenciada (industrial) analizada erróneamente
            # como residencial. Se reintenta con ese parser antes de darla
            # por inválida — evita que una cuenta industrial quede sin ninguna
            # factura válida solo por haber elegido mal el tipo de cliente.
            if not _es_fallback:
                alterno = parse_factura_eeq_industrial(path, _es_fallback=True)
                if not alterno.get("error"):
                    alterno["tipo_detectado"] = "industrial"
                    return alterno
            resultado["error"] = (
                "No se encontró la fila 'Energía activa total' "
                "o el consumo no pudo extraerse"
            )
        else:
            resultado["valor_total_planilla"] = _extraer_valor_total(lineas)
    except Exception as e:
        resultado["error"] = str(e)

    return resultado


# ---------------------------------------------------------------------------
# Detalle completo de planilla (todas las filas: franjas, reactiva, demandas)
# ---------------------------------------------------------------------------

_UNIDAD_FILA = re.compile(r'^(kWh|kW|kVarh)$', re.IGNORECASE)

_PAT_FILA_DETALLE = re.compile(
    r'(energ[íi]a\s+act\.\s+hor\.\s+[ABCD]\s*\([^)]*\)'
    r'|energ[íi]a\s+reactiva\s+total'
    r'|demanda\s+m[áa]x\.\s+hor\.\s+[ABCD]\s*\([^)]*\)'
    r'|demanda\s+facturable)',
    re.IGNORECASE,
)


def _parsear_fila_detalle(lineas: list[str], idx_inicio: int, descripcion: str) -> dict | None:
    """
    Extrae los datos de una fila de detalle de planilla (franja de energía,
    demanda, etc.) a partir del índice de su línea descriptora.

    PyMuPDF emite cada celda en su propia línea. La estructura varía según
    si la fila tiene monto o no: cuando el monto es 0 el PDF no imprime esa
    celda, así que solo se toma si el token inmediato tras la unidad
    (kWh/kW/kVarh) es numérico.

    El 'Consumo Total' es siempre el último número antes de la unidad —
    regla verificada contra facturas reales (franjas en cero, franjas con
    consumo y la fila de demanda facturable, que sí trae monto).

    Parámetros:
        lineas: Líneas del texto completo del PDF.
        idx_inicio: Índice de la línea descriptora de la fila.
        descripcion: Texto descriptivo de la fila (para el resultado).

    Retorna:
        Diccionario con los campos de la fila, o None si no se pudo extraer.
    """
    fecha_hasta: str | None = None
    numeros: list[float] = []
    unidad: str | None = None
    monto = 0.0

    limite = min(idx_inicio + 14, len(lineas))
    j = idx_inicio + 1
    while j < limite:
        tok = lineas[j].strip()
        j += 1
        if not tok:
            continue
        if fecha_hasta is None and re.match(r'^\d{2}-\d{2}-\d{4}$', tok):
            fecha_hasta = tok
            continue
        m_uni = _UNIDAD_FILA.match(tok)
        if m_uni:
            unidad = m_uni.group(1)
            if j < limite:
                siguiente = lineas[j].strip()
                if re.match(r'^-?\d[\d.,]*-?\s*$', siguiente):
                    try:
                        monto = _parse_num(siguiente)
                    except ValueError:
                        pass
            break
        try:
            numeros.append(_parse_num(tok))
        except ValueError:
            break  # llegamos a la siguiente fila/sección sin encontrar unidad

    if unidad is None or fecha_hasta is None:
        return None

    consumo_total = numeros[-1] if numeros else 0.0
    return {
        "descripcion": descripcion,
        "fecha_hasta": fecha_hasta,
        "lectura_actual": numeros[0] if len(numeros) >= 1 else None,
        "lectura_anterior": numeros[1] if len(numeros) >= 2 else None,
        "consumo_subtotal": numeros[-3] if len(numeros) >= 3 else None,
        "consumo_interno_transformador": numeros[-2] if len(numeros) >= 2 else None,
        "consumo_total": round(consumo_total, 2),
        "unidad": unidad,
        "monto": round(monto, 2),
    }


def extraer_detalle_planilla(texto: str) -> list[dict]:
    """
    Extrae el desglose completo de una factura con tarifa horaria
    diferenciada (franjas de energía activa A-D, energía reactiva total,
    demandas máximas A-D y demanda facturable), fila por fila, tal como
    aparecen impresas en el PDF.

    A diferencia de 'parse_factura_eeq_industrial' (que solo suma las 4
    franjas de energía activa para obtener consumo_total_kwh), esta función
    devuelve TODAS las filas de detalle para mostrarlas en una tabla aparte.
    En facturas residenciales (que no tienen estas filas) devuelve una lista
    vacía.

    Parámetros:
        texto: Texto completo extraído del PDF.

    Retorna:
        Lista de dicts (uno por fila encontrada), vacía si no aplica.
    """
    lineas = texto.splitlines()
    filas: list[dict] = []
    for i, linea in enumerate(lineas):
        m = _PAT_FILA_DETALLE.search(linea)
        if not m:
            continue
        descripcion = re.sub(r'\s+', ' ', m.group(1)).strip()
        fila = _parsear_fila_detalle(lineas, i, descripcion)
        if fila:
            filas.append(fila)
    return filas


# ---------------------------------------------------------------------------
# Parser industrial (tarifa MTCGCD32 — 4 franjas horarias)
# ---------------------------------------------------------------------------

# Descripción de cada franja según la tarifa MTCGCD32
_FRANJAS_INFO = {
    "A": "L-V 08h00-18h00",
    "B": "L-V 18h00-22h00",
    "C": "L-V 22h00-08h00 y S,D,F 22h00-18h00",
    "D": "S,D,F 18h00-22h00",
}


def _parsear_franja_industrial(lineas: list[str], idx_inicio: int) -> float | None:
    """
    Extrae el consumo en kWh de una franja horaria industrial dado el índice
    de la línea descriptora.

    PyMuPDF emite cada celda en una línea separada. La estructura esperada es:
      +0: "Energía act. hor. A (L-V 08h00-18h00)"   ← línea de inicio
      +1: fecha_hasta (DD-MM-YYYY)
      +2: lectura_actual
      +3: lectura_anterior
      +4: consumo_bruto kWh
      +5: reducción (generalmente 0,00)
      +6: consumo_neto kWh   ← valor a retornar
      +7: "kWh"
      +8: monto $

    Se busca el último valor numérico antes de la línea "kWh" dentro de las
    siguientes 12 líneas no vacías.

    Parámetros:
        lineas: Lista de líneas del texto completo del PDF.
        idx_inicio: Índice de la línea descriptora de la franja.

    Retorna:
        Consumo en kWh como float, o None si no se puede determinar.
    """
    ultimo_numero: float | None = None
    for j in range(idx_inicio + 1, min(idx_inicio + 14, len(lineas))):
        tok = lineas[j].strip()
        if not tok:
            continue
        if tok.lower() == "kwh":
            # El último número registrado antes de este token es el consumo neto
            return ultimo_numero
        try:
            ultimo_numero = _parse_num(tok)
        except ValueError:
            pass
    return None


def parse_factura_eeq_industrial(pdf_path, _es_fallback: bool = False) -> dict:
    """
    Extrae datos de una factura PDF industrial de la EEQ (tarifa MTCGCD32).

    Las facturas industriales tienen 4 franjas horarias de consumo:
      - Franja A: Lunes a Viernes 08h00-18h00 (tarifa más alta)
      - Franja B: Lunes a Viernes 18h00-22h00
      - Franja C: Lunes a Viernes 22h00-08h00 y Sáb., Dom., Feriados 22h00-18h00
      - Franja D: Sábados, Domingos, Feriados 18h00-22h00

    PyMuPDF emite cada celda en su propia línea, por lo que el valor kWh de
    cada franja se obtiene escaneando las líneas siguientes hasta encontrar
    la unidad 'kWh'.

    El campo 'consumo_total_kwh' es la suma de las cuatro franjas.

    Parámetros:
        pdf_path: Ruta al archivo PDF (str o Path).

    Retorna:
        Diccionario con los campos extraídos, incluyendo 'energia_hor_a_kwh',
        'energia_hor_b_kwh', 'energia_hor_c_kwh', 'energia_hor_d_kwh' y
        'consumo_total_kwh'. Si algo falla, el campo 'error' describe el problema.
    """
    path = Path(pdf_path)
    resultado: dict = {"archivo": str(path), "error": None}

    try:
        texto = _extraer_texto(path)
    except Exception as e:
        resultado["error"] = f"No se pudo leer el PDF: {e}"
        return resultado

    try:
        cabecera = _extraer_cabecera(texto)
        resultado.update(cabecera)

        lineas = texto.splitlines()

        # Patrón para detectar la línea descriptora de cada franja
        _PAT_FRANJA = re.compile(
            r'energ[íi]a\s+act\.\s+hor\.\s+([ABCD])\s*[\(\[]',
            re.IGNORECASE
        )

        franjas_encontradas: dict[str, float] = {}
        fecha_hasta: str | None = None

        for i, linea in enumerate(lineas):
            m = _PAT_FRANJA.search(linea)
            if m:
                letra = m.group(1).upper()
                valor = _parsear_franja_industrial(lineas, i)
                if valor is not None:
                    franjas_encontradas[letra] = valor

                    # La línea inmediata siguiente al descriptor contiene la fecha_hasta
                    if fecha_hasta is None:
                        for j in range(i + 1, min(i + 5, len(lineas))):
                            tok = lineas[j].strip()
                            if re.match(r'^\d{2}-\d{2}-\d{4}$', tok):
                                fecha_hasta = tok
                                break

        if not franjas_encontradas:
            # Puede ser una factura residencial (una sola fila "Energía activa
            # total") analizada erróneamente como industrial.
            if not _es_fallback:
                alterno = parse_factura_eeq(path, _es_fallback=True)
                if not alterno.get("error"):
                    alterno["tipo_detectado"] = "residencial"
                    return alterno
            resultado["error"] = (
                "No se encontraron franjas horarias industriales (A/B/C/D). "
                "Verifica que sea una factura tarifa MTCGCD32."
            )
            return resultado

        resultado["energia_hor_a_kwh"] = franjas_encontradas.get("A", 0.0)
        resultado["energia_hor_b_kwh"] = franjas_encontradas.get("B", 0.0)
        resultado["energia_hor_c_kwh"] = franjas_encontradas.get("C", 0.0)
        resultado["energia_hor_d_kwh"] = franjas_encontradas.get("D", 0.0)
        resultado["consumo_total_kwh"]  = round(sum(franjas_encontradas.values()), 2)

        if fecha_hasta:
            resultado["fecha_hasta"] = fecha_hasta
        else:
            # Fallback: buscar en texto completo "Fecha hasta DD-MM-YYYY"
            m2 = re.search(r'Fecha\s+hasta\s+(\d{2}-\d{2}-\d{4})', texto, re.IGNORECASE)
            if m2:
                resultado["fecha_hasta"] = m2.group(1)
            else:
                resultado["error"] = (
                    "No se pudo determinar la fecha_hasta de la factura industrial."
                )

        resultado["valor_total_planilla"] = _extraer_valor_total(lineas)
        resultado["valor_demanda"] = _extraer_valor_demanda(lineas)
        resultado["detalle_planilla"] = extraer_detalle_planilla(texto)

    except Exception as e:
        resultado["error"] = str(e)

    return resultado

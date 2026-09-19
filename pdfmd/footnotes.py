# -*- coding: utf-8 -*-
"""
pdfmd.footnotes — Extracción de notas al pie y su conversión a sintaxis Markdown nativa.

Se ejecuta ANTES de `transform_pages`: reclama las líneas de la franja inferior para
que las reglas de cabeceras/pies y de numeración de página no las borren.

Cada nota recibe una numeración global correlativa a lo largo de todo el documento
(los PDFs suelen reiniciar la suya en cada página) y, si se localiza la llamada
volada dentro del cuerpo, ésta se sustituye por `[^N]`.
"""

import re
from collections import Counter
from typing import Callable, List, Optional, Tuple

from pdfmd.models import Block, Footnote, Line, Options, PageText, Span
from pdfmd.transform import find_repetitive_headers_footers, is_page_number_line

# Dígitos volados Unicode que algunos PDFs usan como llamada o como marcador
SUPERINDICES = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
                "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}

# Marcador al principio de una nota: "1 ", "1.", "(1)", "[1]", "¹"
_RE_MARCADOR = re.compile(
    r"^[\(\[]?\s*(?P<num>\d{1,3}|[" + "".join(SUPERINDICES) + r"]{1,3})\s*[\)\]\.\-–]?\s+(?P<texto>\S.*)$"
)

# Proporción del alto de página donde empieza a buscarse si no hay raya separadora
ZONA_INFERIOR = 0.82

# Una nota se compone en cuerpo menor; se admite hasta este factor del tamaño base
FACTOR_TAMANO_NOTA = 0.92

# La llamada volada es bastante más pequeña que el texto que la rodea
FACTOR_TAMANO_LLAMADA = 0.85


def normalizar_marcador(texto: str) -> str:
    """Convierte dígitos volados a dígitos normales: '¹²' -> '12'."""
    return "".join(SUPERINDICES.get(c, c) for c in texto)


def _y0(linea: Line) -> float:
    return linea.bbox[1]


def _tam(linea: Line) -> float:
    return linea.avg_size


def _inicio_de_zona(page: PageText) -> float:
    """
    Límite superior de la franja de notas. La raya separadora, cuando existe,
    da un corte exacto; si no, se recurre a la proporción de página.
    """
    if page.rule_y:
        return page.rule_y
    return (page.height or 842.0) * ZONA_INFERIOR


def tamano_de_cuerpo(pages: List[PageText]) -> float:
    """
    Tamano de fuente del cuerpo calculado IGNORANDO la franja de notas.

    Si se midiera sobre la pagina entera, en documentos cortos el texto de las
    notas —mas largo que el propio cuerpo— podria imponerse y falsear la
    referencia, con lo que ninguna llamada volada se reconoceria.
    """
    tamanos = Counter()
    for page in pages:
        limite = _inicio_de_zona(page)
        for block in page.blocks:
            if block.block_type != "text":
                continue
            for linea in block.lines:
                if _y0(linea) >= limite:
                    continue
                for span in linea.spans:
                    contenido = span.text.strip()
                    if contenido:
                        tamanos[round(span.size, 1)] += len(contenido)
    if not tamanos:
        return 11.0
    return float(tamanos.most_common(1)[0][0])


def _es_inicio_de_nota(linea: Line, body_size: float, hay_raya: bool) -> Optional[Tuple[str, str]]:
    """
    Decide si la línea abre una nota. Exige el marcador y, además, una segunda
    señal: o bien viene por debajo de la raya separadora, o bien está en cuerpo menor.
    """
    m = _RE_MARCADOR.match(linea.text().strip())
    if not m:
        return None
    if not hay_raya and _tam(linea) > body_size * FACTOR_TAMANO_NOTA:
        return None
    return normalizar_marcador(m.group("num")), m.group("texto").strip()


def _lineas_de_zona(page: PageText) -> List[Tuple[Block, Line]]:
    """Líneas de texto situadas en la franja inferior, ordenadas de arriba a abajo."""
    limite = _inicio_de_zona(page)
    encontradas = []
    for block in page.blocks:
        if block.block_type != "text":
            continue
        for linea in block.lines:
            if not linea.text().strip():
                continue
            y0 = _y0(linea)
            if y0 == 0.0 and linea.bbox[3] == 0.0:
                continue  # Sin geometría fiable (p. ej. tras OCR): no se puede ubicar
            if y0 >= limite:
                encontradas.append((block, linea))
    encontradas.sort(key=lambda par: _y0(par[1]))
    return encontradas


def _anclar_llamada(page: PageText, marcador: str, numero_global: int,
                    body_size: float, lineas_nota: set) -> bool:
    """
    Busca en el cuerpo de la página el dígito volado que llama a la nota y lo
    sustituye por `[^N]`. Devuelve False si no se encuentra (modo degradado).
    """
    for block in page.blocks:
        if block.block_type in ("image", "table"):
            continue
        for linea in block.lines:
            if id(linea) in lineas_nota:
                continue  # Es la propia nota, no su llamada
            for span in linea.spans:
                bruto = span.text.strip()
                if not bruto or normalizar_marcador(bruto) != marcador:
                    continue
                # La llamada va en cuerpo menor; si no, es un número corriente del texto
                if span.size and span.size > body_size * FACTOR_TAMANO_LLAMADA:
                    continue
                referencia = f"[^{numero_global}]"
                span.text = span.text.replace(bruto, referencia, 1)
                # El volado suele venir marcado como cursiva o negrita; envolver la
                # referencia en *...* romperia la sintaxis de nota al pie.
                if span.text.strip() == referencia:
                    span.bold = span.italic = span.monospace = False
                return True
    return False


def process_footnotes(
    pages: List[PageText],
    options: Options,
    body_size: Optional[float] = None,
    log_cb: Optional[Callable[[str], None]] = None
) -> List[Footnote]:
    """
    Extrae las notas al pie de todas las páginas y las retira del flujo de texto.
    Devuelve la lista completa, ya numerada de corrido en orden de lectura.
    """
    if not options.detect_footnotes:
        return []

    if body_size is None:
        body_size = tamano_de_cuerpo(pages)

    # Los pies recurrentes comparten franja con las notas: hay que reconocerlos
    # aqui o acabarian pegados al final del texto de la ultima nota.
    recurrentes = find_repetitive_headers_footers(pages) if options.remove_headers_footers else set()

    todas: List[Footnote] = []
    contador = 0

    for page in pages:
        candidatas = _lineas_de_zona(page)
        if not candidatas:
            continue

        hay_raya = bool(page.rule_y)
        notas_pagina: List[Footnote] = []
        consumidas = set()
        actual: Optional[Footnote] = None

        for block, linea in candidatas:
            if linea.text().strip() in recurrentes:
                continue
            apertura = _es_inicio_de_nota(linea, body_size, hay_raya)
            if apertura:
                marcador, texto = apertura
                contador += 1
                actual = Footnote(numero=contador, texto=texto, page_num=page.page_num,
                                  marcador_original=marcador)
                notas_pagina.append(actual)
                consumidas.add(id(linea))
            elif actual is not None:
                continuacion = linea.text().strip()
                # El número de página vive en esta misma franja: si se absorbiera,
                # acabaría pegado al final del texto de la nota.
                if is_page_number_line(continuacion) or continuacion in recurrentes:
                    continue
                # Continuación de la nota anterior: se une con un espacio
                if continuacion.endswith("-"):
                    actual.texto += continuacion[:-1]
                else:
                    actual.texto = f"{actual.texto} {continuacion}".strip()
                consumidas.add(id(linea))
            # Si aún no ha empezado ninguna nota, la línea se respeta tal cual

        if not notas_pagina:
            continue

        # Anclar cada nota a su llamada dentro del cuerpo de esta misma página
        for nota in notas_pagina:
            nota.anclada = _anclar_llamada(page, nota.marcador_original, nota.numero,
                                           body_size, consumidas)

        # Retirar del documento las líneas que ya son notas
        bloques_restantes = []
        for block in page.blocks:
            if block.block_type == "text":
                block.lines = [l for l in block.lines if id(l) not in consumidas]
                if not block.lines:
                    continue
            bloques_restantes.append(block)
        page.blocks = bloques_restantes

        page.footnotes = notas_pagina
        todas.extend(notas_pagina)

    if todas and log_cb:
        ancladas = sum(1 for n in todas if n.anclada)
        log_cb(f"Notas al pie extraídas: {len(todas)} ({ancladas} enlazadas a su llamada, "
               f"{len(todas) - ancladas} sin ancla)")

    return todas

# -*- coding: utf-8 -*-
"""
pdfmd.transform — Limpieza, jerarquía de títulos, unión de líneas huérfanas, bloques de código y headers/footers.
"""

import hashlib
import re
from collections import Counter
from dataclasses import replace
from typing import Callable, List, Optional, Set
from pdfmd.models import Block, DocumentMetadata, Line, Options, PageText, Span

# Marcadores de viñeta habituales en PDFs que no son sintaxis Markdown válida
# Marcadores de viñeta habituales en PDFs que no son sintaxis Markdown válida
BULLET_CHARS = "•▪▫◦‣·∙●○–—"
_RE_BULLET = re.compile(r"^([" + BULLET_CHARS + r"\-\*])\s*")

# Numeración de página aislada: "12", "- 12 -", "Página 3 de 40", "Page 3/40"
_RE_PAGE_NUMBER = re.compile(
    r"^(?:[\-–—|\s]*\d{1,4}[\-–—|\s]*"
    r"|(?:p[áa]g(?:ina)?\.?|page|pag\.?)\s*\d{1,4}(?:\s*(?:de|of|/)\s*\d{1,4})?"
    r"|\d{1,4}\s*(?:de|of|/)\s*\d{1,4})$",
    re.IGNORECASE
)


def detect_body_size(pages: List[PageText]) -> float:
    """Calcula el tamaño de fuente predominante (cuerpo) en el documento."""
    sizes = []
    for page in pages:
        for block in page.blocks:
            if block.block_type == "text":
                for line in block.lines:
                    for span in line.spans:
                        if span.text.strip():
                            sizes.extend([round(span.size, 1)] * len(span.text.strip()))
    if not sizes:
        return 11.0
    counter = Counter(sizes)
    most_common, _ = counter.most_common(1)[0]
    return float(most_common)


def clave_de_repeticion(texto: str, con_numeros: bool = False) -> str:
    """
    Forma comparable de una cabecera: sin espacios ni puntuacion
    ("Q U A R T E R L Y" = "QUARTERLY") y, si es larga, sin numeros (el folio
    cambia en cada pagina). En una corta los numeros se quedan: "Capitulo 1"
    y "Capitulo 2" no son la misma cabecera.
    """
    if con_numeros:
        return re.sub(r"[\W_]+", "", texto.lower())
    sin_numeros = re.sub(r"[\W\d_]+", "", texto.lower())
    if len(sin_numeros) >= 15:
        return sin_numeros
    return re.sub(r"[\W_]+", "", texto.lower())


def find_repetitive_headers_footers(pages: List[PageText], con_numeros: bool = False) -> Set[str]:
    """
    Claves (ver `clave_de_repeticion`) de las lineas que se repiten en muchas
    paginas: en los margenes (arriba, abajo o el lateral, donde va el sello
    "Downloaded By..." en vertical) en >= 30 % de las paginas.
    """
    if len(pages) < 2:
        return set()

    en_margen: Counter = Counter()

    for page in pages:
        page_h = page.height or 842.0
        page_w = page.width or 595.0
        vistas_margen = set()
        for block in page.blocks:
            if block.block_type != "text":
                continue
            for line in block.lines:
                clave = clave_de_repeticion(line.text(), con_numeros)
                if len(clave) < 6:   # "Ibid." o una sigla no son cabeceras
                    continue
                x0, y0, x1, y1 = line.bbox
                if (y1 < page_h * 0.12 or y0 > page_h * 0.88
                        or x1 < page_w * 0.08 or x0 > page_w * 0.92):
                    vistas_margen.add(clave)
        en_margen.update(vistas_margen)

    return {c for c, k in en_margen.items() if k >= max(2, int(len(pages) * 0.3))}


def find_repetitive_header_footer_images(pages: List[PageText]) -> Set[str]:
    """Identifica hashes de imágenes que se repiten en la franja superior o inferior de múltiples páginas."""
    if len(pages) < 2:
        return set()

    margin_image_digests = []
    for page in pages:
        page_h = page.height or 842.0
        for block in page.blocks:
            if block.block_type != "image" or not block.image_bytes:
                continue
            bx0, by0, bx1, by1 = block.bbox
            if by1 < page_h * 0.15 or by0 > page_h * 0.85:
                digest = hashlib.sha1(block.image_bytes).hexdigest()
                margin_image_digests.append(digest)

    counts = Counter(margin_image_digests)
    repetitive = set()
    for digest, count in counts.items():
        if count >= max(2, int(len(pages) * 0.3)):
            repetitive.add(digest)
    return repetitive


def is_page_number_line(text: str) -> bool:
    """Detecta numeraciones de página aisladas (que varían en cada página)."""
    stripped = text.strip()
    if not stripped or len(stripped) > 24:
        return False
    return bool(_RE_PAGE_NUMBER.match(stripped))


def in_margin_zone(line: Line, page_height: float) -> bool:
    """Indica si la línea está en la franja de cabecera o de pie de página."""
    page_h = page_height or 842.0
    _, y0, _, y1 = line.bbox
    if y0 == 0 and y1 == 0:
        return False
    return y1 < page_h * 0.12 or y0 > page_h * 0.88


def looks_tabular(text: str) -> bool:
    """Heurística de línea con estructura de columnas: no debe fusionarse con la siguiente."""
    stripped = text.strip()
    if stripped.count("|") >= 2:
        return True
    return len(re.findall(r"\S {2,}\S", stripped)) >= 2


def is_list_item(text: str) -> bool:
    """Detecta si una línea empieza con viñeta, número o checklist."""
    stripped = text.strip()
    patterns = [
        r"^[" + BULLET_CHARS + r"\-\*]\s*",
        r"^\-\s+\[[ xX]\]\s*",
        r"^\d+[\.\)]\s*",
        r"^[a-zA-Z][\.\)]\s+",
        r"^\([a-zA-Z0-9]+\)\s*"
    ]
    return any(re.match(p, stripped) for p in patterns)


def is_quote_block(text: str) -> bool:
    """Detecta si una línea es una cita o bloque indentado."""
    return text.strip().startswith("> ") or text.strip().startswith("» ")


def normalize_bullet(line: Line) -> None:
    """Reescribe viñetas tipográficas (•, ▪, –, -) como guiones Markdown con espacio."""
    for span in line.spans:
        if not span.text.strip():
            continue
        leading_ws = span.text[:len(span.text) - len(span.text.lstrip())]
        new_text = _RE_BULLET.sub("- ", span.text.lstrip())
        if new_text != span.text.lstrip():
            span.text = leading_ws + new_text
        return


def _all_lines_are_list_items(block: Block) -> bool:
    return bool(block.lines) and all(is_list_item(l.text()) for l in block.lines)


def merge_list_blocks(blocks: List[Block]) -> List[Block]:
    """Une bloques consecutivos formados solo por elementos de lista en una única lista."""
    merged: List[Block] = []
    for block in blocks:
        if (merged and block.block_type == "text" and merged[-1].block_type == "text"
                and _all_lines_are_list_items(block) and _all_lines_are_list_items(merged[-1])):
            merged[-1].lines.extend(block.lines)
            continue
        merged.append(block)
    return merged


def merge_heading_blocks(blocks: List[Block]) -> List[Block]:
    """Une bloques de encabezado consecutivos del mismo nivel (# Titulo 1 + # Titulo 2 -> # Titulo 1 Titulo 2)."""
    merged: List[Block] = []
    for b in blocks:
        if (merged and b.block_type == "heading" and merged[-1].block_type == "heading"
                and b.heading_level == merged[-1].heading_level):
            prev = merged[-1]
            if prev.lines and b.lines:
                prev.lines[-1].spans.append(Span(text=" "))
                prev.lines[-1].spans.extend(b.lines[0].spans)
                if len(b.lines) > 1:
                    prev.lines.extend(b.lines[1:])
            elif b.lines:
                prev.lines.extend(b.lines)
        else:
            merged.append(b)
    return merged


def merge_broken_sentence_blocks(blocks: List[Block], options: Options) -> List[Block]:
    """
    Une bloques de texto consecutivos cuando el primero termina a mitad de frase
    (sin punto final ni dos puntos) y el siguiente continúa la misma frase.
    Evita que un párrafo quede partido en dos con un doble salto de línea artificial.
    """
    merged: List[Block] = []
    for b in blocks:
        if not merged:
            merged.append(b)
            continue
        prev = merged[-1]
        if (prev.block_type == "text" and b.block_type == "text"
                and prev.lines and b.lines):
            prev_txt = prev.text().rstrip()
            next_txt = b.text().lstrip()
            if (prev_txt and not prev_txt.endswith((".", "!", "?", ":", ";", "---", "…"))
                    and not is_list_item(next_txt)
                    and not is_quote_block(next_txt)):
                dy = b.bbox[1] - prev.bbox[3] if (b.bbox and prev.bbox) else 0
                if dy < 120.0:  # Dentro del flujo de la página
                    prev.lines.extend(b.lines)
                    prev.lines = unir_palabras_partidas(prev.lines, options)
                    if prev.bbox and b.bbox:
                        prev.bbox = (
                            min(prev.bbox[0], b.bbox[0]),
                            prev.bbox[1],
                            max(prev.bbox[2], b.bbox[2]),
                            b.bbox[3]
                        )
                    continue
        merged.append(b)
    return merged


def _classify_heading(line: Line, body_size: float, options: Options) -> int:
    """Devuelve el nivel de encabezado (1-3) o 0 si la línea no es un título."""
    text = line.text().strip()
    if not text or sum(1 for c in text if c.isalpha()) < 2:
        return 0
    line_size = line.avg_size

    if line_size >= body_size * options.heading_size_ratio and len(text) < 160:
        if line_size >= body_size * 1.55:
            return 1
        if line_size >= body_size * 1.25:
            return 2
        return 3

    if options.caps_to_headings and text.isupper() and 4 <= len(text) < 100:
        if not any(c.isdigit() for c in text[:2]):
            return 2 if line.is_bold else 3

    return 0


# Un titulo no termina en articulo, preposicion o conjuncion: "Y JOURNAL OF" es
# un trozo de cabecera de revista, no un titulo.
_PALABRAS_DE_ENLACE = {
    "of", "the", "and", "or", "a", "an", "in", "on", "for", "to", "with", "by", "at",
    "de", "del", "la", "el", "los", "las", "y", "o", "e", "en", "con", "por", "para",
    "du", "des", "le", "les", "et", "au", "aux", "di", "della", "il", "und", "der", "die",
}


def _titulo_cortado(text: str) -> bool:
    palabras = re.findall(r"[^\W\d_]+", text.lower())
    if not palabras:
        return True
    return palabras[-1] in _PALABRAS_DE_ENLACE or text.rstrip().endswith((",", "-"))


_RE_SINTAXIS_CODIGO = re.compile(
    r"[{};]\s*$"                                        # fin de sentencia o bloque
    r"|^\s*(def|class|import|function|var|let|const|elif|#include|#define)\s+\w"
    r"|==|!=|=>|->|::|\+=|\$\(|</?[a-z]+[ >]"           # operadores, etiquetas
    r"|\w\(.*\)\s*[:{;]?\s*$"                           # llamada sin espacio: f(x)
)


def parece_codigo(lines: List[Line]) -> bool:
    """
    Una fuente monoespaciada no basta: avisos legales, portadas mecanografiadas,
    cuestionarios y transcripciones tambien la usan. Hace falta sintaxis de
    programa en al menos dos lineas y en el 30 % de ellas.
    """
    textos = [l.text() for l in lines if l.text().strip()]
    con_sintaxis = sum(1 for t in textos if _RE_SINTAXIS_CODIGO.search(t))
    return con_sintaxis >= 2 and con_sintaxis / len(textos) >= 0.3


# Palabras que aparecen como mitad de compuesto legitimo con guion interno
# (long-term, well-known, left-right, high-level). Si ambas mitades
# estan aqui, el guion de final de linea es del autor y no un corte tipografico.
_MITADES_DE_COMPUESTO = {
    # ingles
    "one", "two", "three", "half", "full", "long", "short", "term", "time",
    "left", "right", "wing", "party", "state", "post", "pre", "anti", "pro",
    "non", "self", "well", "known", "based", "led", "made", "wide", "high",
    "low", "old", "new", "far", "near", "cross", "multi", "inter", "intra",
    "sub", "super", "counter", "over", "under", "out", "off", "back", "front",
    "progressive", "conservative", "liberal", "socialist", "democratic",
    "government", "east", "west", "north", "south", "central", "middle",
    "first", "second", "third", "fourth", "public", "private", "civil",
    "military", "legal", "human", "rights", "media", "social", "political",
    "economic", "national", "regional", "local", "global", "world", "war",
    "line", "level", "scale", "term", "year", "day", "week", "month", "hour",
    "risk", "free", "hard", "soft", "open", "closed", "read", "write", "data",
    "driven", "making", "taking", "seeking", "building", "related", "linked",
    # espanol
    "socio", "politico", "juridico", "medico", "tecnico", "teorico", "practico",
    "norte", "sur", "este", "oeste", "post", "pre", "anti", "pro", "auto",
    "franco", "hispano", "latino", "afro", "euro", "ibero", "arabe", "ruso",
    "corto", "largo", "medio", "plazo", "alto", "bajo", "nuevo", "viejo",
}

_RE_CORTE = re.compile(r"[-‐‑]$")


def _mitad_izquierda(texto: str) -> str:
    """Ultima palabra antes del guion de corte."""
    trozos = re.findall(r"[^\W\d_]+", texto[:-1], re.UNICODE)
    return trozos[-1].lower() if trozos else ""


def _mitad_derecha(texto: str) -> str:
    """Primera palabra de la linea siguiente."""
    m = re.match(r"[^\W\d_]+", texto, re.UNICODE)
    return m.group(0).lower() if m else ""


def es_guion_de_corte(izquierda: str, derecha: str) -> bool:
    """
    Decide si el guion al final de la linea parte una palabra (hay que unirla) o
    es un guion legitimo del autor (hay que conservarlo).

    `grep "documentation"` no encuentra `docu-mentation`, y la busqueda por termino
    es la forma principal de trabajar un corpus; pero unir `long-term` en `longterm`
    destruiria el termino igual de bien.
    """
    izq = _mitad_izquierda(izquierda)
    der = _mitad_derecha(derecha)
    if not izq or not der:
        return False

    # Mayuscula al otro lado: casi siempre nombre propio compuesto (Anglo-American)
    if der[:1].isupper() or derecha[:1].isupper():
        if not izquierda[:-1].isupper():
            return False

    # Compuesto legitimo: ambas mitades funcionan como palabra por si solas
    if izq in _MITADES_DE_COMPUESTO and der in _MITADES_DE_COMPUESTO:
        return False

    return True


def unir_palabras_partidas(lines: List[Line], options: Options) -> List[Line]:
    """
    Une las lineas que quedan cortadas por un guion de fin de linea.

    Tras esta pasada ninguna linea termina en guion: o la palabra se recompone
    (`docu-` + `mentation` -> `documentation`) o el guion se conserva pero pegado a su
    otra mitad en la misma linea (`long-` + `term` -> `long-term`).
    """
    if not options.join_hyphens or len(lines) < 2:
        return lines

    salida: List[Line] = []
    pendiente: Optional[Line] = None

    for line in lines:
        if pendiente is None:
            pendiente = Line(spans=list(line.spans), bbox=line.bbox)
            continue

        texto_izq = pendiente.text().rstrip()
        texto_der = line.text().lstrip()
        if not (_RE_CORTE.search(texto_izq) and texto_der and not looks_tabular(texto_izq)):
            salida.append(pendiente)
            pendiente = Line(spans=list(line.spans), bbox=line.bbox)
            continue

        corta = es_guion_de_corte(texto_izq, texto_der)
        spans = list(pendiente.spans)
        # El guion vive en el ultimo span con texto; se quita solo si parte palabra
        if corta:
            for i in range(len(spans) - 1, -1, -1):
                recortado = spans[i].text.rstrip()
                if not recortado:
                    continue
                if _RE_CORTE.search(recortado):
                    spans[i] = replace(spans[i], text=recortado[:-1])
                break
        else:
            for i in range(len(spans) - 1, -1, -1):
                if spans[i].text.strip():
                    spans[i] = replace(spans[i], text=spans[i].text.rstrip())
                    break

        # Los spans de la linea siguiente se conservan enteros: llevan el enlace,
        # la negrita y el tamano que se usan mas adelante.
        primeros = list(line.spans)
        for i, sp in enumerate(primeros):
            if sp.text.strip():
                primeros[i] = replace(sp, text=sp.text.lstrip())
                break
        pendiente = Line(spans=spans + primeros, bbox=pendiente.bbox)

    if pendiente is not None:
        salida.append(pendiente)
    return salida


def _defragment(lines: List[Line], options: Options) -> List[Line]:
    """Une líneas huérfanas cortas respetando listas, tablas y guiones de corte."""
    fused_lines: List[Line] = []
    acc_text = ""
    acc_spans: List[Span] = []

    for l in lines:
        l_text = l.text().strip()
        if not acc_text:
            acc_text = l_text
            acc_spans = list(l.spans)
            continue

        # Nunca fusionar filas con aspecto tabular: destruiría las columnas
        if looks_tabular(acc_text) or looks_tabular(l_text):
            fused_lines.append(Line(spans=acc_spans))
            acc_text = l_text
            acc_spans = list(l.spans)
        # Hifenación rota al final de línea
        elif acc_text.endswith("-") and len(acc_text) > 1 and acc_text[-2].isalpha():
            acc_text = acc_text[:-1] + l_text
            acc_spans.extend(l.spans)
        elif len(acc_text) < options.orphan_max_len and not is_list_item(l_text):
            acc_text += " " + l_text
            # Un separador propio evita tocar el primer span, que puede llevar
            # el enlace o la negrita de la linea siguiente.
            acc_spans.append(Span(text=" "))
            acc_spans.extend(l.spans)
        else:
            fused_lines.append(Line(spans=acc_spans))
            acc_text = l_text
            acc_spans = list(l.spans)

    if acc_spans:
        fused_lines.append(Line(spans=acc_spans))
    return fused_lines


def unir_cortes_entre_bloques(pages: List[PageText], options: Options) -> None:
    """
    Recompone las palabras partidas cuando el corte cae entre dos bloques, o
    entre dos paginas.

    El guion de fin de linea no respeta la maquetacion: una palabra puede
    empezar al final de un bloque y terminar en el siguiente, o cruzar el salto
    de pagina. Si no se cose aqui, `grep` sigue sin encontrar el termino.
    """
    if not options.join_hyphens:
        return

    # Lista plana de bloques de texto en orden de lectura, con su pagina
    candidatos = [
        b for page in pages for b in page.blocks
        if b.block_type in ("text", "list_item") and b.lines
    ]

    # `actual` avanza solo cuando el bloque conserva lineas: al coser dos bloques
    # el segundo puede quedarse vacio y el corte siguiente seguiria pendiente.
    actual = None
    for siguiente in candidatos:
        if actual is None:
            if siguiente.lines:
                actual = siguiente
            continue
        if not actual.lines:
            actual = siguiente if siguiente.lines else None
            continue
        if not siguiente.lines:
            continue
        ultima = actual.lines[-1]
        primera = siguiente.lines[0]
        izq = ultima.text().rstrip()
        der = primera.text().lstrip()
        if not (_RE_CORTE.search(izq) and der) or looks_tabular(izq):
            actual = siguiente
            continue

        corta = es_guion_de_corte(izq, der)
        spans = list(ultima.spans)
        for i in range(len(spans) - 1, -1, -1):
            recortado = spans[i].text.rstrip()
            if not recortado:
                continue
            spans[i] = replace(spans[i], text=recortado[:-1] if corta else recortado)
            break

        primeros = list(primera.spans)
        for i, sp in enumerate(primeros):
            if sp.text.strip():
                primeros[i] = replace(sp, text=sp.text.lstrip())
                break

        actual.lines[-1] = Line(spans=spans + primeros, bbox=ultima.bbox)
        siguiente.lines = siguiente.lines[1:]
        # El bloque cosido sigue siendo el que puede acabar en guion
        if siguiente.lines:
            actual = siguiente

    # Un bloque que se ha quedado sin lineas ya no pinta nada
    for page in pages:
        page.blocks = [b for b in page.blocks
                       if b.block_type not in ("text", "list_item") or b.lines]


def reordenar_bloques_multicolumna(page: PageText) -> List[Block]:
    """
    Detecta si una página presenta maquetación en múltiples columnas (habitual en papers
    académicos y artículos científicos) y reordena los bloques para respetar el orden natural
    de lectura: primero la columna izquierda completa de arriba a abajo, luego la derecha,
    respetando los elementos de ancho completo (títulos, resúmenes o figuras que cruzan la página).
    """
    if len(page.blocks) < 3:
        return page.blocks

    page_w = page.width or (max(b.bbox[2] for b in page.blocks) if page.blocks else 600.0)
    if page_w <= 0:
        return page.blocks

    mitad = page_w * 0.5
    umbral_ancho_completo = page_w * 0.60

    col_izq = []
    col_der = []

    for b in page.blocks:
        x0, y0, x1, y1 = b.bbox
        ancho = x1 - x0
        if ancho >= umbral_ancho_completo:
            continue
        if x1 <= mitad + (page_w * 0.05):
            col_izq.append(b)
        elif x0 >= mitad - (page_w * 0.05):
            col_der.append(b)

    # Si no hay bloques en ambas columnas, no es una maquetación multi-columna
    if not col_izq or not col_der:
        return page.blocks

    # Comprobar si hay solapamiento vertical real entre bloques de la izquierda y la derecha
    hay_solapamiento = False
    for b_izq in col_izq:
        _, iy0, _, iy1 = b_izq.bbox
        for b_der in col_der:
            _, dy0, _, dy1 = b_der.bbox
            if max(iy0, dy0) < min(iy1, dy1) - 4:
                hay_solapamiento = True
                break
        if hay_solapamiento:
            break

    if not hay_solapamiento:
        return page.blocks

    # Dividir los bloques en franjas verticales delimitadas por bloques divisores (ancho completo)
    bloques_ordenados_y = sorted(page.blocks, key=lambda blk: (blk.bbox[1], blk.bbox[0]))

    franjas = []
    franja_actual = []

    for b in bloques_ordenados_y:
        x0, y0, x1, y1 = b.bbox
        es_divisor = (x1 - x0 >= umbral_ancho_completo) or (x0 < mitad * 0.75 and x1 > mitad * 1.25)
        if es_divisor:
            if franja_actual:
                franjas.append(("columnas", franja_actual))
                franja_actual = []
            franjas.append(("divisor", [b]))
        else:
            franja_actual.append(b)

    if franja_actual:
        franjas.append(("columnas", franja_actual))

    resultado = []
    for tipo, grupo in franjas:
        if tipo == "divisor":
            resultado.extend(grupo)
        else:
            izq = [b for b in grupo if b.bbox[0] < mitad]
            der = [b for b in grupo if b.bbox[0] >= mitad]
            izq_ordenado = sorted(izq, key=lambda b: (b.bbox[1], b.bbox[0]))
            der_ordenado = sorted(der, key=lambda b: (b.bbox[1], b.bbox[0]))
            resultado.extend(izq_ordenado)
            resultado.extend(der_ordenado)

    return resultado


def transform_pages(
    pages: List[PageText],
    options: Options,
    log_cb: Optional[Callable[[str], None]] = None,
    metadata: Optional["DocumentMetadata"] = None
) -> List[PageText]:
    """Aplica transformaciones de estructuración y limpieza semántica sobre las páginas."""
    if not pages:
        return []

    body_size = detect_body_size(pages)
    if log_cb:
        log_cb(f"Tamaño de fuente de cuerpo detectado: {body_size:.1f} pt")

    rep_headers = set()
    rep_images = set()
    if options.remove_headers_footers:
        rep_headers = find_repetitive_headers_footers(pages)
        rep_images = find_repetitive_header_footer_images(pages)
        if rep_headers and log_cb:
            log_cb(f"Encabezados/pies recurrentes detectados para suprimir: {len(rep_headers)}")
        if rep_images and log_cb:
            log_cb(f"Imágenes de cabecera/pie recurrentes detectadas para suprimir: {len(rep_images)}")

    dropped_page_numbers = 0
    suprimidas: Set[str] = set()

    for page in pages:
        if options.detect_columns:
            page.blocks = reordenar_bloques_multicolumna(page)
        new_blocks = []
        for block in page.blocks:
            if block.block_type == "table":
                new_blocks.append(block)
                continue

            if block.block_type == "image":
                if options.remove_headers_footers and block.image_bytes:
                    digest = hashlib.sha1(block.image_bytes).hexdigest()
                    if digest in rep_images:
                        continue
                    # Si la imagen está en el margen extremo superior o inferior cuando hay múltiples páginas
                    page_h = page.height or 842.0
                    bx0, by0, bx1, by1 = block.bbox
                    if (by1 < page_h * 0.10 or by0 > page_h * 0.90) and len(pages) >= 2:
                        continue
                new_blocks.append(block)
                continue

            filtered_lines = []
            for line in block.lines:
                txt = line.text().strip()
                if not txt:
                    continue
                if options.remove_headers_footers and clave_de_repeticion(txt) in rep_headers:
                    suprimidas.add(txt)
                    continue
                if (options.drop_page_numbers and is_page_number_line(txt)
                        and in_margin_zone(line, page.height)):
                    dropped_page_numbers += 1
                    continue
                filtered_lines.append(line)

            if not filtered_lines:
                continue

            # 1. Detección de bloques de código: monospace Y aspecto de programa
            if all(l.is_monospace for l in filtered_lines):
                if options.detect_code_blocks and parece_codigo(filtered_lines):
                    new_blocks.append(Block(
                        lines=filtered_lines,
                        block_type="code",
                        bbox=block.bbox,
                        page_num=page.page_num
                    ))
                    continue
                # Prosa en fuente de maquina de escribir: sin `comillas` de codigo
                for l in filtered_lines:
                    l.spans = [replace(sp, monospace=False) for sp in l.spans]

            # 2. Detección de citas (quote)
            first_text = filtered_lines[0].text().strip()
            if is_quote_block(first_text):
                new_blocks.append(Block(
                    lines=filtered_lines,
                    block_type="quote",
                    bbox=block.bbox,
                    page_num=page.page_num
                ))
                continue

            # 3. Analizar si la primera línea del bloque es un encabezado
            first_line = filtered_lines[0]
            heading_level = _classify_heading(first_line, body_size, options)
            body_lines = filtered_lines[1:]

            if heading_level:
                # Un título seguido de cuerpo en el mismo bloque se separa en dos bloques
                if body_lines and _classify_heading(body_lines[0], body_size, options) == heading_level:
                    heading_level = 0
                else:
                    new_blocks.append(Block(
                        lines=[first_line],
                        block_type="heading",
                        heading_level=heading_level,
                        bbox=block.bbox,
                        page_num=page.page_num
                    ))
                    filtered_lines = body_lines
                    if not filtered_lines:
                        continue

            # 4. Normalización de viñetas tipográficas a sintaxis Markdown
            if options.normalize_lists:
                for l in filtered_lines:
                    normalize_bullet(l)

            # 5. Recomposición de palabras partidas por el guión de fin de línea.
            #    Va antes de defragmentar: después las líneas ya no existen como tales.
            filtered_lines = unir_palabras_partidas(filtered_lines, options)

            # 6. Texto normal con defragmentación de líneas huérfanas
            if options.defragment_short:
                filtered_lines = _defragment(filtered_lines, options)

            new_blocks.append(Block(
                lines=filtered_lines,
                block_type="text",
                bbox=block.bbox,
                page_num=page.page_num
            ))

        blks = merge_list_blocks(new_blocks) if options.normalize_lists else new_blocks
        blks = merge_heading_blocks(blks)
        # Tras unir los titulos partidos en dos lineas: el que sigue acabando en
        # "of"/"de" es un trozo (cabecera de revista), no un titulo.
        for b in blks:
            if b.block_type == "heading" and _titulo_cortado(b.text()):
                b.block_type, b.heading_level = "text", 0
        blks = merge_broken_sentence_blocks(blks, options)
        page.blocks = blks

    # Queda constancia en el frontmatter de que se ha borrado del cuerpo
    if suprimidas and metadata is not None:
        metadata.lineas_suprimidas = sorted(suprimidas)

    # Ningun titulo real se repite en el 30 % de las paginas (o >= 3 paginas): si ocurre, es cabecera
    if len(pages) >= 3:
        conteo_titulos: Counter = Counter()
        for page in pages:
            vistos_pag = {
                clave_de_repeticion(b.text())
                for b in page.blocks
                if b.block_type == "heading" and len(clave_de_repeticion(b.text())) >= 4
            }
            conteo_titulos.update(vistos_pag)
        umbral_tit = max(3, int(len(pages) * 0.3))
        titulos_repetidos = {k for k, v in conteo_titulos.items() if v >= umbral_tit}
        if titulos_repetidos:
            for page in pages:
                nuevos_b = []
                for b in page.blocks:
                    if b.block_type == "heading" and clave_de_repeticion(b.text()) in titulos_repetidos:
                        if options.remove_headers_footers:
                            continue
                        b.block_type, b.heading_level = "text", 0
                    nuevos_b.append(b)
                page.blocks = nuevos_b

    # Si el documento no tiene titulos (o casi ninguno) pero el PDF trae esquema de marcadores (outline), usarlo
    if metadata is not None and getattr(metadata, "outline", None):
        num_headings = sum(1 for p in pages for b in p.blocks if b.block_type == "heading")
        if num_headings == 0 or (len(pages) >= 8 and num_headings < max(2, len(pages) // 10)):
            aplicar_outline_pdf(pages, metadata.outline)

    # Los cortes que cruzan bloque o pagina solo pueden coserse cuando ya estan
    # todos los bloques en su orden definitivo.
    unir_cortes_entre_bloques(pages, options)

    if dropped_page_numbers and log_cb:
        log_cb(f"Numeraciones de página suprimidas: {dropped_page_numbers}")

    return pages


def aplicar_outline_pdf(pages: List[PageText], outline: List) -> int:
    """
    Promueve a encabezado los bloques cuyo texto coincide con el esquema de marcadores
    (outline / TOC) del PDF o inserta el marcador al inicio de la página correspondiente
    cuando el documento carece de títulos detectados por tamaño de fuente.
    """
    if not pages or not outline:
        return 0
    por_pagina = {}
    for item in outline:
        if len(item) < 3:
            continue
        nivel, titulo, pno = item[0], str(item[1] or "").strip(), int(item[2] or 0)
        if not titulo or pno < 1:
            continue
        por_pagina.setdefault(pno, []).append((max(1, min(4, int(nivel or 1))), titulo))

    aplicados = 0
    total_headings_previos = sum(1 for p in pages for b in p.blocks if b.block_type == "heading")

    for page in pages:
        entradas = por_pagina.get(page.page_num, [])
        if not entradas:
            continue
        for nivel, titulo in entradas:
            clave_toc = clave_de_repeticion(titulo, con_numeros=True)
            if not clave_toc:
                continue
            encontrado = False
            for b in page.blocks:
                if b.block_type == "heading":
                    if clave_de_repeticion(b.text(), con_numeros=True) == clave_toc:
                        encontrado = True
                        break
                    continue
                if b.block_type != "text" or not b.lines:
                    continue
                primera_clave = clave_de_repeticion(b.lines[0].text(), con_numeros=True)
                bloque_clave = clave_de_repeticion(b.text(), con_numeros=True)
                if bloque_clave == clave_toc:
                    b.block_type = "heading"
                    b.heading_level = nivel
                    aplicados += 1
                    encontrado = True
                    break
                if primera_clave == clave_toc and len(b.lines) > 1:
                    encabezado = Block(
                        lines=[b.lines[0]],
                        block_type="heading",
                        heading_level=nivel,
                        bbox=b.bbox,
                        page_num=page.page_num,
                    )
                    b.lines = b.lines[1:]
                    idx_b = page.blocks.index(b)
                    page.blocks.insert(idx_b, encabezado)
                    aplicados += 1
                    encontrado = True
                    break
            if not encontrado and total_headings_previos == 0:
                encabezado = Block(
                    lines=[Line(spans=[Span(text=titulo)])],
                    block_type="heading",
                    heading_level=nivel,
                    page_num=page.page_num,
                )
                page.blocks.insert(0, encabezado)
                aplicados += 1
    return aplicados


# Terminos propios de la pagina de creditos / copyright de un libro o informe
_TERMINOS_CREDITOS = (
    "isbn", "deposito legal", "depósito legal", "d.l.", "©", "copyright",
    "todos los derechos reservados", "reservados todos los derechos",
    "primera edicion", "primera edición", "edita:", "editorial",
    "impreso en", "printed in", "all rights reserved", "maquetacion", "maquetación",
)

# Solo se examina el arranque del documento: la portada nunca esta a mitad de libro
_MAX_PAGINAS_PORTADA = 8


def marcar_fin_de_portada(pages: List[PageText], options: Options,
                          log_cb: Optional[Callable[[str], None]] = None) -> List[PageText]:
    """
    Marca la primera pagina de cuerpo real tras el bloque de creditos.

    Deliberadamente conservador: si no encuentra terminos de creditos, o si no hay
    despues una pagina con cuerpo de verdad, no marca nada. Vale mas no escribir
    la marca que ponerla en el sitio equivocado.
    """
    if not options.mark_cover_end or len(pages) < 2:
        return pages

    limite = min(_MAX_PAGINAS_PORTADA, len(pages) - 1)
    ultima_con_creditos = -1

    for idx in range(limite):
        texto = " ".join(b.text() for b in pages[idx].blocks).lower()
        if any(t in texto for t in _TERMINOS_CREDITOS):
            ultima_con_creditos = idx

    if ultima_con_creditos < 0:
        return pages

    # La primera pagina posterior con cuerpo sustancial es el comienzo real
    for idx in range(ultima_con_creditos + 1, len(pages)):
        pagina = pages[idx]
        texto = " ".join(b.text() for b in pagina.blocks).strip()
        if any(t in texto.lower() for t in _TERMINOS_CREDITOS):
            continue
        tiene_titulo = any(b.block_type == "heading" for b in pagina.blocks)
        if len(texto) >= 400 or (tiene_titulo and len(texto) >= 200):
            pagina.fin_de_portada = True
            if log_cb:
                log_cb(f"Fin de portada detectado: el cuerpo empieza en la página {pagina.page_num}")
            return pages

    return pages

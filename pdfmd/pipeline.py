# -*- coding: utf-8 -*-
"""
pdfmd.pipeline — Orquestador principal del pipeline de conversión de PDF a Markdown.
"""

import os
import re
import threading
from typing import Callable, Optional

from pdfmd.equations import process_equations
from pdfmd.extract import extract_pages
from pdfmd.footnotes import process_footnotes
from pdfmd.models import Options
from pdfmd.render import render_document
from pdfmd.tables import process_tables
from pdfmd.transform import marcar_fin_de_portada, transform_pages


_RE_TITULO_BASURA = re.compile(
    r"^(microsoft (word|powerpoint)|untitled|sin t[ií]tulo|document\d*|title)\b|\.(docx?|pptx?|indd|pdf)$",
    re.IGNORECASE)


def titulo_util(titulo: str, filename: str) -> bool:
    """Un título de metadatos que es el nombre del fichero o un resto de Word no sirve."""
    t = (titulo or "").strip()
    if len(t) < 4 or _RE_TITULO_BASURA.search(t):
        return False
    return t.lower() != os.path.splitext(filename or "")[0].lower()


def titulo_del_cuerpo(pages) -> str:
    """
    El título más destacado de las primeras páginas: el de menor nivel y, entre
    ellos, el de letra más grande (la cabecera de la revista suele ir antes y
    más pequeña que el título del artículo). A igualdad, el primero.
    """
    candidatos = [b for p in pages[:3] for b in p.blocks
                  if b.block_type == "heading" and b.text().strip()]
    if not candidatos:
        return ""
    def _peso(b):
        tam = max((l.avg_size for l in b.lines), default=0.0)
        return (-(b.heading_level or 9), round(tam, 1))
    mejor = max(candidatos, key=_peso)
    return " ".join(mejor.text().split())[:200]


def _check_cancel(cancel_event: Optional[threading.Event], mensaje: str) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError(mensaje)


def pdf_to_markdown(
    input_pdf: str,
    output_md: str,
    options: Optional[Options] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    pdf_password: Optional[str] = None,
    debug_tables: bool = False,
    cancel_event: Optional[threading.Event] = None,
    url_origen: str = ""
) -> str:
    """
    Convierte un archivo PDF a Markdown estructurado aplicando el pipeline completo.
    Devuelve el contenido Markdown generado.
    """
    opts = options or Options()

    _check_cancel(cancel_event, "Operación cancelada antes de iniciar.")

    if log_cb:
        log_cb(f"Iniciando conversión de: {os.path.basename(input_pdf)}")

    # 1. Extracción (Páginas + Metadatos del documento)
    pages, metadata = extract_pages(
        input_pdf=input_pdf,
        options=opts,
        pdf_password=pdf_password,
        log_cb=log_cb,
        progress_cb=progress_cb,
        cancel_event=cancel_event
    )

    if url_origen and metadata:
        metadata.url_origen = url_origen

    _check_cancel(cancel_event, "Operación cancelada por el usuario.")

    if not pages:
        if log_cb:
            log_cb("No se pudo extraer contenido del PDF.")
        return ""

    # 2. Notas al pie. Va PRIMERO a propósito: retira las líneas de la franja
    #    inferior antes de que la limpieza de cabeceras/pies y la supresión de
    #    numeración de página (paso 4) puedan confundirlas con ruido y borrarlas.
    if opts.detect_footnotes:
        process_footnotes(pages, options=opts, log_cb=log_cb)

    _check_cancel(cancel_event, "Operación cancelada por el usuario.")

    # 3. Tablas heurísticas sobre las líneas originales.
    #    Debe ejecutarse ANTES de transformar: la defragmentación de líneas
    #    huérfanas fusionaría las filas y destruiría las columnas.
    if opts.detect_tables:
        pages = process_tables(pages, options=opts, log_cb=log_cb)

    _check_cancel(cancel_event, "Operación cancelada por el usuario.")

    # 4. Transformación semántica (encabezados, listas, defragmentación, headers/footers, código)
    if log_cb:
        log_cb("Estructurando jerarquía de títulos, párrafos y bloques...")
    pages = transform_pages(pages, options=opts, log_cb=log_cb, metadata=metadata)

    # El nombre del fichero ya va en source_file: como título vale más el del cuerpo
    if metadata is not None and not titulo_util(metadata.title, metadata.filename):
        metadata.title = titulo_del_cuerpo(pages) or ""

    # Marca de separación entre créditos y cuerpo (tras conocer los títulos)
    if opts.mark_cover_end:
        pages = marcar_fin_de_portada(pages, options=opts, log_cb=log_cb)

    _check_cancel(cancel_event, "Operación cancelada por el usuario.")

    # 5. Ecuaciones LaTeX
    if opts.convert_equations:
        pages = process_equations(pages, options=opts, log_cb=log_cb)

    _check_cancel(cancel_event, "Operación cancelada por el usuario.")

    # 6. Renderizado final
    if log_cb:
        log_cb("Generando documento Markdown estructurado...")

    out_dir = os.path.dirname(os.path.abspath(output_md))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    md_content = render_document(
        pages,
        output_md_path=output_md,
        options=opts,
        metadata=metadata,
        log_cb=log_cb
    )

    # 7. Guardar en disco
    with open(output_md, "w", encoding="utf-8", newline="\n") as f:
        f.write(md_content)

    if log_cb:
        log_cb(f"Conversión completada con éxito -> {os.path.basename(output_md)}")

    return md_content

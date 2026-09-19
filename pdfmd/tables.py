# -*- coding: utf-8 -*-
"""
pdfmd.tables — Detección y reconstrucción de tablas para formato Markdown pipe-table.
"""

import re
from typing import Callable, List, Optional
from pdfmd.models import Block, Line, Options, PageText, Span


def detect_pipe_table_lines(lines: List[Line]) -> Optional[List[List[str]]]:
    """Detecta tablas que ya contienen delimitadores de barra (|)."""
    rows = []
    for l in lines:
        raw = l.text().strip()
        if "|" in raw:
            cols = [c.strip() for c in raw.split("|")]
            if cols and cols[0] == "":
                cols = cols[1:]
            if cols and cols[-1] == "":
                cols = cols[:-1]
            if len(cols) >= 2:
                # Omitir lineas separadoras tipo |---|---|
                if all(re.match(r"^:?-+:?$", c) for c in cols if c):
                    continue
                rows.append(cols)
    if len(rows) >= 2:
        return rows
    return None


# Las celdas de una tabla alineada por espacios son cortas por construcción:
# por encima de este ancho lo que hay es un párrafo con un doble espacio suelto.
# Ante la duda conviene rechazar: un falso positivo destroza el texto, mientras
# que una tabla no detectada se conserva legible tal cual.
MAX_CELL_LEN = 40


def detect_spaced_table_lines(lines: List[Line]) -> Optional[List[List[str]]]:
    """Detecta tablas con columnas separadas por 2 o más espacios o tabulaciones."""
    rows = []
    col_counts = []
    for l in lines:
        raw = l.text().strip()
        cols = [c.strip() for c in re.split(r"\s{2,}|\t+", raw) if c.strip()]
        if len(cols) >= 2:
            # Una celda muy larga delata un párrafo con doble espacio, no una tabla
            if any(len(c) > MAX_CELL_LEN for c in cols):
                return None
            rows.append(cols)
            col_counts.append(len(cols))
        else:
            return None

    if len(rows) >= 2:
        # Verificar que el número de columnas sea consistente (mismo conteo en la mayoría)
        most_common_cols = max(set(col_counts), key=col_counts.count)
        if most_common_cols >= 2 and col_counts.count(most_common_cols) >= len(rows) * 0.7:
            # Normalizar ancho de filas
            normalized = []
            for r in rows:
                if len(r) < most_common_cols:
                    r = r + [""] * (most_common_cols - len(r))
                elif len(r) > most_common_cols:
                    r = r[:most_common_cols - 1] + [" ".join(r[most_common_cols - 1:])]
                normalized.append(r)
            return normalized
    return None


def process_tables(
    pages: List[PageText],
    options: Options,
    log_cb: Optional[Callable[[str], None]] = None
) -> List[PageText]:
    """Escanea los bloques de las páginas para detectar regiones tabulares."""
    if not options.detect_tables:
        return pages

    tables_found = 0
    for page in pages:
        new_blocks = []
        for block in page.blocks:
            if block.block_type != "text" or len(block.lines) < 2:
                new_blocks.append(block)
                continue

            # 1. Probar delimitadores pipe
            pipe_rows = detect_pipe_table_lines(block.lines)
            if pipe_rows:
                tables_found += 1
                new_blocks.append(Block(
                    block_type="table",
                    table_data=pipe_rows,
                    bbox=block.bbox,
                    page_num=page.page_num
                ))
                continue

            # 2. Probar columnas separadas por espacios múltiples
            spaced_rows = detect_spaced_table_lines(block.lines)
            if spaced_rows:
                tables_found += 1
                new_blocks.append(Block(
                    block_type="table",
                    table_data=spaced_rows,
                    bbox=block.bbox,
                    page_num=page.page_num
                ))
                continue

            new_blocks.append(block)
        page.blocks = new_blocks

    if tables_found and log_cb:
        log_cb(f"Tablas detectadas y estructuradas: {tables_found}")

    return pages

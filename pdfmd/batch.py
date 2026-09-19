# -*- coding: utf-8 -*-
"""
pdfmd.batch — Conversion por lotes reanudable y manifiesto de control.

Con cientos de PDFs por delante hacen falta dos cosas que una conversion suelta
no necesita: saber de cuales fiarse sin abrirlos uno a uno (el manifiesto) y que
un fallo en el numero 150 no obligue a empezar por el primero (la reanudacion).
"""

import os
import re
import threading
from typing import Callable, Dict, List, Optional

from pdfmd.models import Options
from pdfmd.pipeline import pdf_to_markdown
from pdfmd.utils import sha256_fichero, slugify

COLUMNAS = ("archivo", "paginas", "chars", "chars_pag", "capa_texto", "enlaces", "avisos")

_RE_CAMPO = re.compile(r'^([a-z0-9_]+):\s*(.*)$')


def leer_frontmatter(texto: str) -> Dict[str, str]:
    """Lee el bloque YAML inicial de un Markdown generado por KIWI."""
    if not texto.startswith("---"):
        return {}
    cierre = texto.find("\n---", 3)
    if cierre == -1:
        return {}
    campos: Dict[str, str] = {}
    for linea in texto[3:cierre].splitlines():
        m = _RE_CAMPO.match(linea.strip())
        if not m:
            continue
        valor = m.group(2).strip()
        if len(valor) >= 2 and valor[0] == '"' and valor[-1] == '"':
            valor = valor[1:-1]
        campos[m.group(1)] = valor
    return campos


def ya_convertido(md_path: str, sha256_pdf: str) -> bool:
    """
    True si ya existe un .md generado a partir de ESE mismo PDF.

    La comparacion es por hash y no por fecha o tamano: si el PDF se ha
    reemplazado por otra version, hay que reconvertirlo aunque se llame igual.
    """
    if not sha256_pdf or not os.path.exists(md_path):
        return False
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            cabecera = f.read(4096)
    except OSError:
        return False
    return leer_frontmatter(cabecera).get("sha256_pdf", "") == sha256_pdf


def ruta_de_salida(pdf_path: str, output_dir: str, usados: set, usar_slug: bool = True) -> str:
    """Nombre de salida en ASCII, sin colisiones dentro del mismo lote."""
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    if usar_slug:
        base = slugify(base, max_len=80)
    candidato = os.path.join(output_dir, f"{base}.md")
    n = 2
    while candidato.lower() in usados:
        candidato = os.path.join(output_dir, f"{base}_{n}.md")
        n += 1
    usados.add(candidato.lower())
    return candidato


def fila_de_manifiesto(nombre: str, md_texto: str) -> Dict[str, str]:
    fm = leer_frontmatter(md_texto)
    avisos = fm.get("avisos", "")
    return {
        "archivo": nombre,
        "paginas": fm.get("pages", "0"),
        "chars": fm.get("chars_extraidos", "0"),
        "chars_pag": fm.get("chars_por_pagina", "0"),
        "capa_texto": fm.get("capa_texto", ""),
        "enlaces": fm.get("enlaces", "0"),
        "avisos": "si" if avisos and avisos != "[]" else "",
    }


def escribir_manifiesto(filas: List[Dict[str, str]], ruta: str) -> str:
    """
    Vuelca el manifiesto TSV ordenado por caracteres por pagina ascendente:
    las primeras filas son exactamente los documentos que hay que revisar a mano.
    """
    def _clave(f):
        try:
            return int(f.get("chars_pag") or 0)
        except ValueError:
            return 0

    ordenadas = sorted(filas, key=_clave)
    with open(ruta, "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(COLUMNAS) + "\n")
        for fila in ordenadas:
            f.write("\t".join(str(fila.get(c, "")) for c in COLUMNAS) + "\n")
    return ruta


def convertir_lote(
    pdfs: List[str],
    output_dir: str,
    options: Optional[Options] = None,
    reanudar: bool = True,
    manifiesto: Optional[str] = "manifiesto_conversion.tsv",
    usar_slug: bool = True,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[Dict[str, str]]:
    """
    Convierte una lista de PDFs y devuelve las filas del manifiesto.

    Con `reanudar` activo se saltan los que ya tienen un .md cuyo `sha256_pdf`
    coincide, de modo que relanzar el lote continua donde se quedo.
    """
    opts = options or Options()
    os.makedirs(output_dir, exist_ok=True)
    usados: set = set()
    filas: List[Dict[str, str]] = []
    saltados = 0
    fallidos = 0
    total = len(pdfs)

    for indice, pdf in enumerate(pdfs, start=1):
        if cancel_event is not None and cancel_event.is_set():
            if log_cb:
                log_cb("Lote cancelado por el usuario.")
            break

        salida = ruta_de_salida(pdf, output_dir, usados, usar_slug=usar_slug)
        nombre = os.path.basename(salida)

        if reanudar and opts.compute_hash and ya_convertido(salida, sha256_fichero(pdf)):
            saltados += 1
            try:
                with open(salida, "r", encoding="utf-8") as f:
                    filas.append(fila_de_manifiesto(nombre, f.read(8192)))
            except OSError:
                pass
            if log_cb:
                log_cb(f"[{indice}/{total}] Ya convertido, se omite: {nombre}")
            if progress_cb:
                progress_cb(indice, total)
            continue

        try:
            md = pdf_to_markdown(pdf, salida, options=opts, log_cb=None,
                                 cancel_event=cancel_event)
            filas.append(fila_de_manifiesto(nombre, md))
            if log_cb:
                log_cb(f"[{indice}/{total}] Convertido: {nombre}")
        except InterruptedError:
            if log_cb:
                log_cb("Lote cancelado por el usuario.")
            break
        except Exception as ex:
            fallidos += 1
            filas.append({"archivo": nombre, "paginas": "0", "chars": "0",
                          "chars_pag": "0", "capa_texto": "error",
                          "enlaces": "0", "avisos": str(ex)[:120]})
            if log_cb:
                log_cb(f"[{indice}/{total}] ERROR en {os.path.basename(pdf)}: {ex}")

        if progress_cb:
            progress_cb(indice, total)

    if manifiesto:
        ruta = manifiesto if os.path.isabs(manifiesto) else os.path.join(output_dir, manifiesto)
        escribir_manifiesto(filas, ruta)
        if log_cb:
            log_cb(f"Manifiesto escrito en {os.path.basename(ruta)}")

    if log_cb:
        log_cb(f"Lote terminado: {len(filas) - saltados - fallidos} convertidos, "
               f"{saltados} omitidos, {fallidos} con error.")
    return filas

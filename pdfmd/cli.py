# -*- coding: utf-8 -*-
"""
pdfmd.cli — Interfaz de línea de comandos para pdfmd.
"""

import argparse
import getpass
import os
import sys

from pdfmd.batch import convertir_lote
from pdfmd.models import Options
from pdfmd.pipeline import pdf_to_markdown


def main():
    parser = argparse.ArgumentParser(description="pdfmd — Conversor de PDF a Markdown")
    parser.add_argument("input_pdf", help="Ruta al archivo PDF de entrada")
    parser.add_argument("-o", "--output", dest="output_md", help="Ruta del archivo Markdown de salida")
    parser.add_argument("--ocr-mode", choices=["off", "auto", "tesseract", "ocrmypdf"], default="off", help="Modo de OCR")
    parser.add_argument("--ocr-lang", default="eng", help="Idioma para OCR (ej: eng, spa, eng+spa)")
    parser.add_argument("--preview-only", action="store_true", help="Procesa solo las primeras 3 páginas")
    parser.add_argument("--export-images", action="store_true", help="Extrae imágenes a una carpeta _assets")
    parser.add_argument("--insert-page-breaks", action="store_true", help="Inserta --- entre páginas")
    parser.add_argument("--no-tables", action="store_true", help="Desactiva detección de tablas")
    parser.add_argument("--no-equations", action="store_true", help="Desactiva conversión de ecuaciones a LaTeX")
    parser.add_argument("--no-headers-footers", action="store_true",
                        help="Conserva cabeceras y pies de página recurrentes")
    parser.add_argument("--no-frontmatter", action="store_true", help="Omite el bloque YAML de metadatos")
    parser.add_argument("--no-code-blocks", action="store_true", help="Desactiva la detección de bloques de código")
    parser.add_argument("--no-footnotes", action="store_true", help="No recolecta las notas al pie")
    parser.add_argument("--no-page-markers", action="store_true", help="Omite los comentarios <!-- p.N -->")
    parser.add_argument("--no-cover-mark", action="store_true", help="No marca el fin de la portada")
    parser.add_argument("--no-hash", action="store_true", help="Omite el SHA-256 del PDF")
    parser.add_argument("--url-origen", default="", help="URL de procedencia del documento")
    parser.add_argument("--fecha-captura", default="", help="Fecha de descarga (AAAA-MM-DD)")
    parser.add_argument("--password", help="Contraseña para PDFs cifrados")
    parser.add_argument("--no-links", action="store_true",
                        help="No extrae los hipervínculos del PDF")
    parser.add_argument("--no-join-hyphens", action="store_true",
                        help="Conserva las palabras partidas por el guión de fin de línea")
    parser.add_argument("--no-lang", action="store_true",
                        help="No detecta el idioma del documento")
    parser.add_argument("--min-chars-pagina", type=int, default=800,
                        help="Umbral por debajo del cual se marca capa_texto: ausente")
    parser.add_argument("--lote", metavar="CARPETA_SALIDA",
                        help="Convierte en lote: input_pdf pasa a ser una carpeta de PDFs")
    parser.add_argument("--no-reanudar", action="store_true",
                        help="En modo lote, reconvierte también los que ya tienen .md al día")
    parser.add_argument("--manifiesto", default="manifiesto_conversion.tsv",
                        help="Nombre del TSV de control del lote")

    args = parser.parse_args()

    input_pdf = os.path.abspath(args.input_pdf)
    if not os.path.exists(input_pdf):
        print(f"Error: No existe el archivo {input_pdf}", file=sys.stderr)
        sys.exit(1)

    if args.output_md:
        output_md = os.path.abspath(args.output_md)
    else:
        base, _ = os.path.splitext(input_pdf)
        output_md = base + ".md"

    options = Options(
        ocr_mode=args.ocr_mode,
        ocr_lang=args.ocr_lang,
        preview_only=args.preview_only,
        export_images=args.export_images,
        insert_page_breaks=args.insert_page_breaks,
        detect_tables=not args.no_tables,
        convert_equations=not args.no_equations,
        remove_headers_footers=not args.no_headers_footers,
        include_frontmatter=not args.no_frontmatter,
        detect_code_blocks=not args.no_code_blocks,
        detect_footnotes=not args.no_footnotes,
        page_markers=not args.no_page_markers,
        mark_cover_end=not args.no_cover_mark,
        compute_hash=not args.no_hash,
        url_origen=args.url_origen,
        fecha_captura=args.fecha_captura,
        extract_links=not args.no_links,
        join_hyphens=not args.no_join_hyphens,
        detect_language=not args.no_lang,
        min_chars_por_pagina=args.min_chars_pagina
    )

    if args.lote:
        if not os.path.isdir(input_pdf):
            print(f"Error: en modo lote {input_pdf} debe ser una carpeta", file=sys.stderr)
            sys.exit(1)
        pdfs = sorted(
            os.path.join(raiz, f)
            for raiz, _dirs, ficheros in os.walk(input_pdf)
            for f in ficheros if f.lower().endswith(".pdf")
        )
        if not pdfs:
            print(f"No se encontró ningún PDF en {input_pdf}", file=sys.stderr)
            sys.exit(1)
        print(f"[pdfmd] {len(pdfs)} PDF(s) encontrados.")
        convertir_lote(
            pdfs,
            output_dir=os.path.abspath(args.lote),
            options=options,
            reanudar=not args.no_reanudar,
            manifiesto=args.manifiesto,
            usar_slug=True,
            log_cb=lambda m: print(f"[pdfmd] {m}")
        )
        return

    def log_cb(msg):
        print(f"[pdfmd] {msg}")

    def prog_cb(done, total):
        print(f"[pdfmd] Progreso: {done}/{total} páginas", end="\r", flush=True)

    pwd = args.password
    try:
        pdf_to_markdown(
            input_pdf=input_pdf,
            output_md=output_md,
            options=options,
            progress_cb=prog_cb,
            log_cb=log_cb,
            pdf_password=pwd
        )
        print(f"\n[OK] Generado: {output_md}")
    except PermissionError:
        print("\nEl documento está protegido.")
        pwd = getpass.getpass("Introduce la contraseña del PDF: ")
        try:
            pdf_to_markdown(
                input_pdf=input_pdf,
                output_md=output_md,
                options=options,
                progress_cb=prog_cb,
                log_cb=log_cb,
                pdf_password=pwd
            )
            print(f"\n[OK] Generado: {output_md}")
        except Exception as e:
            print(f"\nError: {e}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

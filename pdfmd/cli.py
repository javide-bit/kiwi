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
from pdfmd.utils import diagnosticar_entorno_ocr, is_ocrmypdf_available


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="pdfmd — Conversor de PDF a Markdown")
    parser.add_argument("input_pdf", nargs="?", help="Ruta al archivo PDF de entrada (o carpeta con --lote)")
    parser.add_argument("-o", "--output", dest="output_md", help="Ruta del archivo Markdown de salida")
    parser.add_argument("--ocr", action="store_true",
                        help="Activa y fuerza el OCR sobre el documento o lote (usa ocrmypdf o tesseract)")
    parser.add_argument("--ocr-mode", choices=["off", "auto", "tesseract", "ocrmypdf"], default="auto",
                        help="Modo de OCR (auto: páginas sin texto y reintento si el PDF sale sin capa)")
    parser.add_argument("-l", "--ocr-lang", default="spa+eng",
                        help="Idioma para OCR (ej: eng, spa, spa+eng, ita+eng, nld+eng)")
    parser.add_argument("--check-ocr", action="store_true",
                        help="Diagnostica el entorno OCR (PyMuPDF, Tesseract, Ghostscript, ocrmypdf, TESSDATA_PREFIX)")
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
    parser.add_argument("--fecha-captura", default="",
                        help="Fecha de descarga declarada (AAAA-MM-DD); la fecha del fichero va en fecha_archivo")
    parser.add_argument("--password", help="Contraseña para PDFs cifrados")
    parser.add_argument("--no-links", action="store_true",
                        help="No extrae los hipervínculos del PDF")
    parser.add_argument("--no-join-hyphens", action="store_true",
                        help="Conserva las palabras partidas por el guión de fin de línea")
    parser.add_argument("--no-lang", action="store_true",
                        help="No detecta el idioma del documento")
    parser.add_argument("--modo-limpio", "--clean", dest="clean_mode", action="store_true",
                        help="Genera Markdown visualmente limpio (sin comentarios <!-- p.N -->, sin saltos ---, frontmatter minimalista y párrafos unidos)")
    parser.add_argument("--no-columns", action="store_true",
                        help="Desactiva la detección y reordenación de maquetación multi-columna")
    parser.add_argument("--obsidian-callouts", action="store_true",
                        help="Renderiza notas y avisos como callouts de Obsidian (> [!NOTE])")
    parser.add_argument("--obsidian-wikilinks", action="store_true",
                        help="Convierte enlaces relativos e internos en wikilinks de Obsidian ([[doc]])")
    parser.add_argument("--min-chars-pagina", type=int, default=100,
                        help="Media de caracteres/página por debajo de la cual se marca capa_texto: ausente")
    parser.add_argument("--conservar-nombre", action="store_true",
                        help="En modo lote, el .md se llama como el PDF (sin pasar '_' a '-')")
    parser.add_argument("--lote", metavar="CARPETA_SALIDA",
                        help="Convierte en lote: input_pdf pasa a ser una carpeta de PDFs (por defecto <carpeta>/markdown_export)")
    parser.add_argument("--no-reanudar", action="store_true",
                        help="En modo lote, reconvierte también los que ya tienen .md al día")
    parser.add_argument("--workers", type=int, default=1,
                        help="Número de hilos concurrentes para procesar el lote")
    parser.add_argument("--manifiesto", default="manifiesto_conversion.tsv",
                        help="Nombre del TSV de control del lote")
    return parser


def main(argv=None):
    parser = construir_parser()
    args = parser.parse_args(argv)

    if args.check_ocr:
        diag = diagnosticar_entorno_ocr()
        print("=== Diagnóstico de entorno OCR (KIWI pdfmd) ===")
        print(f"  PyMuPDF:         {diag['pymupdf_version'] or 'NO instalado'}")
        print(f"  Tesseract:       {diag['tesseract_version'] or 'NO encontrado'} ({diag['tesseract_bin'] or 'sin ruta'})")
        print(f"  Ghostscript:     {diag['ghostscript_bin'] or 'NO encontrado'}")
        print(f"  ocrmypdf:        {diag['ocrmypdf_version'] or 'NO instalado'} ({diag['ocrmypdf_bin'] or 'sin ruta'})")
        print(f"  TESSDATA_PREFIX: {diag['tessdata_prefix'] or 'sin definir'}")
        print(f"  configs/hocr:    {'OK' if diag['has_hocr'] else 'FALTA'}")
        print(f"  Idiomas ({len(diag['idiomas'])}):    {', '.join(diag['idiomas']) or 'ninguno'}")
        if diag["avisos"]:
            print("\nAvisos detectados:")
            for av in diag["avisos"]:
                print(f"  - {av}")
            sys.exit(1)
        print("\n[OK] Entorno OCR completo y operativo.")
        return

    if not args.input_pdf:
        parser.error("Se requiere 'input_pdf' (o usa --check-ocr para verificar el entorno).")

    input_pdf = os.path.abspath(args.input_pdf)
    if not os.path.exists(input_pdf):
        print(f"Error: No existe el archivo {input_pdf}", file=sys.stderr)
        sys.exit(1)

    if args.output_md:
        output_md = os.path.abspath(args.output_md)
    else:
        base, _ = os.path.splitext(input_pdf)
        output_md = base + ".md"

    ocr_mode = args.ocr_mode
    if args.ocr and ocr_mode in ("auto", "off"):
        ocr_mode = "ocrmypdf" if is_ocrmypdf_available() else "tesseract"

    options = Options(
        ocr_mode=ocr_mode,
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
        min_chars_por_pagina=args.min_chars_pagina,
        clean_mode=args.clean_mode,
        detect_columns=not args.no_columns,
        obsidian_callouts=args.obsidian_callouts,
        obsidian_wikilinks=args.obsidian_wikilinks
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
            usar_slug=not args.conservar_nombre,
            log_cb=lambda m: print(f"[pdfmd] {m}"),
            max_workers=args.workers
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

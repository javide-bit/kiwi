# -*- coding: utf-8 -*-
"""
htmlmd.cli — Interfaz de linea de comandos del conversor de web a Markdown.

    python -m htmlmd.cli https://example.com/articulo -o articulo.md
    python -m htmlmd.cli pagina_guardada.html -o pagina.md --url https://origen/real
    python -m htmlmd.cli carpeta_html/ -o markdown_web/ --conservar-nombre
    python -m htmlmd.cli --lista urls.txt -o markdown_web/ --export-images
"""

import argparse
import os
import sys

from htmlmd.batch import ItemLista, convertir_lote, leer_lista_estructurada
from htmlmd.fetch import ErrorDeDescarga, es_url
from htmlmd.models import WebOptions
from htmlmd.pipeline import html_to_markdown, nombre_de_salida


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="htmlmd",
        description="KIWI — convierte paginas web (URL, .html local o carpeta de .html) a Markdown estructurado.",
    )
    p.add_argument("origen", nargs="?",
                   help="URL, ruta a un fichero .html o carpeta de ficheros .html. Omitir si se usa --lista.")
    p.add_argument("-o", "--output",
                   help="Fichero .md de salida (o carpeta destino con --lista / carpeta, por defecto markdown_web).")
    p.add_argument("--lista", metavar="FICHERO",
                   help="Fichero de texto, CSV (url;doc_id / ruta;url) o lista .md para conversion por lotes.")
    p.add_argument("--url", default="", metavar="URL",
                   help="Procedencia real de un .html local, para el frontmatter.")

    lote = p.add_argument_group("lote y listas estructuradas")
    lote.add_argument("--sin-subcarpetas", action="store_true",
                      help="No clasificar los ficheros en subcarpetas por sección.")
    lote.add_argument("--no-index", action="store_true",
                      help="No generar el fichero de índice (INDEX.md / _INDEX.md) al finalizar el lote.")
    lote.add_argument("--index-name", default="INDEX.md",
                      help="Nombre del archivo de índice en lote (por defecto INDEX.md, o _INDEX.md).")
    lote.add_argument("--workers", type=int, default=1,
                      help="Número de hilos concurrentes para procesar el lote.")

    estructura = p.add_argument_group("estructura")
    estructura.add_argument("--modo-limpio", "--clean", dest="clean_mode", action="store_true",
                            help="Genera Markdown visualmente limpio (frontmatter minimalista y sin avisos).")
    estructura.add_argument("--obsidian-callouts", action="store_true",
                            help="Renderiza notas y advertencias como callouts de Obsidian (> [!NOTE]).")
    estructura.add_argument("--obsidian-wikilinks", action="store_true",
                            help="Convierte enlaces relativos e internos en wikilinks de Obsidian ([[doc]]).")
    estructura.add_argument("--no-frontmatter", action="store_true",
                            help="No emitir el bloque YAML de metadatos.")
    estructura.add_argument("--no-tables", action="store_true",
                            help="No convertir <table> en tablas Markdown.")
    estructura.add_argument("--no-links", action="store_true",
                            help="Emitir el texto de los enlaces sin su destino.")
    estructura.add_argument("--no-code-blocks", action="store_true",
                            help="No emitir <pre> como bloque de codigo.")
    estructura.add_argument("--no-slug", "--conservar-nombre", dest="no_slug", action="store_true",
                            help="No normalizar el nombre del fichero a ASCII (conserva el nombre original).")
    estructura.add_argument("--pagina-completa", action="store_true",
                            help="No podar el cromo ni buscar el cuerpo: convierte todo.")

    imagenes = p.add_argument_group("imagenes")
    imagenes.add_argument("--export-images", action="store_true",
                          help="Descargar las imagenes a <nombre>_assets/.")
    imagenes.add_argument("--no-alt", action="store_true",
                          help="No conservar el texto alternativo de las imagenes.")

    red = p.add_argument_group("red")
    red.add_argument("--timeout", type=float, default=20.0, metavar="SEG")
    red.add_argument("--user-agent", default=WebOptions().user_agent)
    red.add_argument("--insecure", action="store_true",
                     help="No verificar el certificado TLS. Usar solo con motivo.")
    red.add_argument("--pausa", type=float, default=1.0, metavar="SEG",
                     help="Espera entre peticiones en modo lote (por defecto 1 s).")
    red.add_argument("--refrescar", action="store_true",
                     help="En modo lote, reconvertir tambien las URLs ya presentes.")

    p.add_argument("-q", "--quiet", action="store_true", help="Silenciar el progreso.")
    return p


def opciones_desde_args(args) -> WebOptions:
    return WebOptions(
        incluir_frontmatter=not args.no_frontmatter,
        clean_mode=args.clean_mode,
        obsidian_callouts=args.obsidian_callouts,
        obsidian_wikilinks=args.obsidian_wikilinks,
        detectar_tablas=not args.no_tables,
        detectar_codigo=not args.no_code_blocks,
        conservar_enlaces=not args.no_links,
        exportar_imagenes=args.export_images,
        conservar_alt=not args.no_alt,
        quitar_cromo=not args.pagina_completa,
        solo_cuerpo=not args.pagina_completa,
        timeout=args.timeout,
        user_agent=args.user_agent,
        verificar_tls=not args.insecure,
    )


def main(argv=None) -> int:
    args = construir_parser().parse_args(argv)
    opts = opciones_desde_args(args)
    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))

    if not args.origen and not args.lista:
        print("Hay que indicar una URL/fichero o bien --lista.", file=sys.stderr)
        return 2

    # ---- Lote desde carpeta de .html -----------------------------------
    if args.origen and not es_url(args.origen) and os.path.isdir(args.origen):
        carpeta = os.path.abspath(args.origen)
        destino = args.output or os.path.join(carpeta, "markdown_web")
        origenes = [
            ItemLista(origen=os.path.join(raiz, f), tipo="local", etiqueta=f)
            for raiz, _dirs, ficheros in os.walk(carpeta)
            for f in sorted(ficheros)
            if f.lower().endswith((".html", ".htm"))
        ]
        if not origenes:
            print(f"No se encontró ningún archivo .html en {carpeta}.", file=sys.stderr)
            return 1
        resumen = convertir_lote(
            origenes, destino, opts, log_cb=log,
            refrescar=args.refrescar, pausa=0,
            organizar_por_secciones=not args.sin_subcarpetas,
            generar_index=not args.no_index,
            max_workers=args.workers,
            conservar_nombre=args.no_slug,
            nombre_index=args.index_name,
        )
        return 1 if resumen["fallidas"] else 0

    # ---- Lote desde fichero de lista -----------------------------------
    if args.lista:
        destino = args.output or "markdown_web"
        try:
            origenes = leer_lista_estructurada(args.lista)
        except OSError as ex:
            print(f"No se pudo leer la lista {args.lista}: {ex}", file=sys.stderr)
            return 1
        if not origenes:
            print(f"La lista {args.lista} no contiene ninguna URL ni captura local.", file=sys.stderr)
            return 1

        resumen = convertir_lote(origenes, destino, opts, log_cb=log,
                                 refrescar=args.refrescar, pausa=args.pausa,
                                 organizar_por_secciones=not args.sin_subcarpetas,
                                 generar_index=not args.no_index,
                                 max_workers=args.workers,
                                 conservar_nombre=args.no_slug,
                                 nombre_index=args.index_name)
        # Un lote con fallos no puede devolver 0: en CI o en un .bat nadie
        # leeria el resumen y las capturas perdidas pasarian inadvertidas.
        return 1 if resumen["fallidas"] else 0

    # ---- Documento suelto ----------------------------------------------
    salida = args.output
    if not salida:
        salida = nombre_de_salida(args.origen, usar_slug=not args.no_slug)
    elif os.path.isdir(salida):
        salida = os.path.join(salida, nombre_de_salida(args.origen,
                                                       usar_slug=not args.no_slug))

    try:
        _, meta = html_to_markdown(args.origen, salida, opts,
                                   log_cb=log, url_origen=args.url)
    except ErrorDeDescarga as ex:
        print(str(ex), file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"{os.path.abspath(salida)}  "
              f"[{meta.chars_extraidos} chars, {meta.enlaces} enlaces, "
              f"contenido: {meta.contenido}]")
    # El contenido ausente es un fallo de conversion, no un exito con matices
    return 1 if meta.contenido == "ausente" else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
Pruebas de mejoras para conversión masiva de corpus (PDF, OCR y HTML local): OCR por
defecto, umbral de capa de texto, listas htmlmd "ruta ; url", frontmatter
emparejable (sha256, fechas ISO, título) y nombres originales.

    python test_corpus.py
"""

import os
import shutil
import tempfile

import pymupdf as fitz

from htmlmd.batch import convertir_lote as convertir_lote_web
from htmlmd.batch import leer_lista_estructurada
from htmlmd.models import WebOptions
from htmlmd.pipeline import html_to_markdown
from pdfmd.batch import escribir_manifiesto, fila_de_manifiesto, leer_frontmatter, ya_convertido
from pdfmd.extract import diagnosticar_capa_texto
from pdfmd.models import Block, DocumentMetadata, Line, Options, PageText, Span
from pdfmd.pipeline import pdf_to_markdown, titulo_util
from pdfmd.render import join_clean_text_lines
from pdfmd.transform import _titulo_cortado, aplicar_outline_pdf, clave_de_repeticion, parece_codigo
from pdfmd.utils import (
    diagnosticar_entorno_ocr,
    fecha_pdf_iso,
    inferir_idioma_ocr_documento,
    version_motor_ocr,
)


def test_opciones_por_defecto():
    o = Options()
    assert o.ocr_mode == "auto" and o.ocr_lang == "spa+eng"
    assert o.min_chars_por_pagina == 100
    print("  -> PASSED: OCR auto y spa+eng por defecto")


def test_fecha_pdf_iso():
    assert fecha_pdf_iso("D:20250507134205+02'00'") == "2025-05-07T13:42:05+02:00"
    assert fecha_pdf_iso("D:20080210075913-05'00'") == "2008-02-10T07:59:13-05:00"
    assert fecha_pdf_iso("D:20080210075913Z") == "2008-02-10T07:59:13Z"
    assert fecha_pdf_iso("D:2008") == "2008-01-01"
    assert fecha_pdf_iso("mayo de 2020") == "mayo de 2020"
    print("  -> PASSED: creation_date en ISO 8601")


def _capa(chars_por_pagina):
    pages = [PageText(char_count=c) for c in chars_por_pagina]
    meta = DocumentMetadata()
    diagnosticar_capa_texto(pages, meta, Options(detect_language=False))
    return meta.capa_texto


def test_umbral_capa_texto():
    assert _capa([3000, 3500]) == "nativa"
    assert _capa([500, 520, 510]) == "escasa"           # informe con graficos: texto real
    assert _capa([700, 0, 650]) == "escasa"             # una diapositiva vacia no basta
    assert _capa([40, 60, 30]) == "ausente"
    assert _capa([2000, 0, 0, 0, 10]) == "ausente"      # mayoria de paginas sin texto
    print("  -> PASSED: ausente (sin texto) frente a escasa (poco texto real)")


def test_titulo_y_fechas_pdf():
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "1_2_informe_x.pdf")
        doc = fitz.open()
        pg = doc.new_page(width=595, height=842)
        pg.insert_htmlbox(fitz.Rect(50, 60, 545, 700),
                          "<h1>Medidas activas rusas</h1><p>" +
                          ("El informe analiza las operaciones de influencia. " * 40) + "</p>")
        doc.set_metadata({"creationDate": "D:20250507134205+02'00'"})
        doc.save(pdf)
        doc.close()
        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"), options=Options(ocr_mode="off"))
        fm = leer_frontmatter(md)
        assert fm["title"] == "Medidas activas rusas", fm["title"]
        assert fm["source_file"] == "1_2_informe_x.pdf"
        assert fm["creation_date"] == "2025-05-07T13:42:05+02:00"
        assert "fecha_captura" not in fm, "La mtime no es la fecha de captura"
        assert fm.get("fecha_archivo"), "La mtime va en fecha_archivo"
        assert fm.get("ocr_modo") == "off"
        print("  -> PASSED: título del cuerpo, fecha ISO, fecha_archivo")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_titulo_util():
    assert not titulo_util("Microsoft Word - informe.docx", "x.pdf")
    assert not titulo_util("1_2_informe", "1_2_informe.pdf")
    assert titulo_util("Russian Active Measures", "us_hpsci.pdf")
    print("  -> PASSED: títulos de metadatos inservibles")


def test_reanudar_rehace_ausentes_sin_ocr():
    d = tempfile.mkdtemp()
    try:
        md = os.path.join(d, "a.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write('---\nsha256_pdf: "abc"\ncapa_texto: "ausente"\n---\n')
        assert ya_convertido(md, "abc")
        assert not ya_convertido(md, "abc", rehacer_ausentes=True), "Sin OCR previo hay que rehacerlo"
        with open(md, "w", encoding="utf-8") as f:
            f.write('---\nsha256_pdf: "abc"\ncapa_texto: "ausente"\nocr_modo: "auto"\n---\n')
        assert ya_convertido(md, "abc", rehacer_ausentes=True), "Con OCR ya intentado no se repite"
        print("  -> PASSED: el lote rehace los 'ausente' convertidos sin OCR")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_manifiesto_con_entrada():
    d = tempfile.mkdtemp()
    try:
        fila = fila_de_manifiesto("a.md", '---\ncapa_texto: "nativa"\n---\n', r"C:\pdf\a_b.pdf")
        ruta = escribir_manifiesto([fila], os.path.join(d, "m.tsv"))
        cab, linea = open(ruta, encoding="utf-8").read().splitlines()
        assert cab.split("\t")[1] == "entrada" and linea.split("\t")[1] == r"C:\pdf\a_b.pdf"
        print("  -> PASSED: columna entrada en manifiesto_conversion.tsv")
    finally:
        shutil.rmtree(d, ignore_errors=True)


HTML = ("<html><head><title>Informe DSN</title></head><body><article><h1>Informe DSN</h1><p>"
        + "Texto del comunicado sobre la operacion de desinformacion. " * 20
        + "</p></article></body></html>")


def test_lista_ruta_url_y_frontmatter_web():
    d = tempfile.mkdtemp()
    try:
        for n in ("at_dsn_2025.html", "at_gadmo.html", "suelto_x.html"):
            with open(os.path.join(d, n), "w", encoding="utf-8") as f:
                f.write(HTML)
        lista = os.path.join(d, "rutas.txt")
        with open(lista, "w", encoding="utf-8") as f:
            f.write("at_dsn_2025.html ; https://www.disinforesilience.eu/press/\n"
                    "https://gadmo.eu/ | at_gadmo.html\n"
                    "suelto_x.html ; \n")      # URL vacia: cuenta como ruta sola
        items = leer_lista_estructurada(lista)
        assert [i.tipo for i in items] == ["local"] * 3
        assert items[0].url_declarada == "https://www.disinforesilience.eu/press/"
        assert items[1].url_declarada == "https://gadmo.eu/"
        assert items[2].url_declarada == "", "Una ruta sola no es una URL"

        salida = os.path.join(d, "md")
        convertir_lote_web(items, salida, WebOptions(), pausa=0, max_workers=1,
                           generar_index=False, conservar_nombre=True)
        assert sorted(os.listdir(salida)) == ["at_dsn_2025.md", "at_gadmo.md",
                                               "manifiesto.tsv", "suelto_x.md"]
        fm = leer_frontmatter(open(os.path.join(salida, "at_dsn_2025.md"), encoding="utf-8").read())
        assert fm["url_origen"] == "https://www.disinforesilience.eu/press/"
        assert len(fm["sha256"]) == 64 and fm["sha256"] == fm["sha256_html"]
        assert fm["source_file"] == "at_dsn_2025.html"
        assert "fecha_captura" not in fm and fm.get("fecha_archivo")
        fm3 = leer_frontmatter(open(os.path.join(salida, "suelto_x.md"), encoding="utf-8").read())
        assert fm3["url_origen"] == "null"

        # Relanzar el lote: los locales se reconocen por su ruta y se omiten
        res = convertir_lote_web(items, salida, WebOptions(), pausa=0, max_workers=1,
                                 generar_index=False, conservar_nombre=True)
        assert res["omitidas"] == 3, res
        print("  -> PASSED: listas 'ruta ; url', sha256 y nombre original en htmlmd")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_web_local_suelto():
    d = tempfile.mkdtemp()
    try:
        ruta = os.path.join(d, "p.html")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(HTML)
        md, meta = html_to_markdown(ruta, os.path.join(d, "p.md"),
                                    url_origen="https://ejemplo.org/p")
        fm = leer_frontmatter(md)
        assert fm["url_origen"] == "https://ejemplo.org/p" and "url_solicitada" not in fm
        print("  -> PASSED: .html local con URL declarada")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _lineas(*textos):
    return [Line(spans=[Span(text=t, monospace=True)]) for t in textos]


def test_calidad_markdown():
    # Monoespaciado no es codigo: aviso legal mecanografiado frente a programa
    assert not parece_codigo(_lineas("SELECT COMMITTEE ON INTELLIGENCE",
                                     "if it wasn't even going to be used as a finding.",
                                     "the House and Senate intelligence committees."))
    assert parece_codigo(_lineas("def suma(a, b):", "    return a + b", "print(suma(1, 2))"))
    # La misma cabecera con distinto folio o espaciada letra a letra
    assert (clave_de_repeticion("36 A. Furnham, H.C. Boo / The Journal of Socio-Economics 40")
            == clave_de_repeticion("A. Furnham, H.C. Boo / The Journal of Socio-Economics 40 37"))
    assert clave_de_repeticion("Q U A R T E R L Y JOURNAL") == clave_de_repeticion("QUARTERLY JOURNAL")
    assert clave_de_repeticion("Capitulo 1") != clave_de_repeticion("Capitulo 2")
    assert _titulo_cortado("Y JOURNAL OF") and not _titulo_cortado("A CONCEPTUAL TASK")
    print("  -> PASSED: vallas solo para codigo, cabeceras repetidas, titulos cortados")


def test_cabecera_lateral_suprimida():
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "x.pdf")
        doc = fitz.open()
        for i in range(4):
            pg = doc.new_page(width=595, height=842)
            pg.insert_text((10, 600), "Downloaded By: [Stanford University] At: 18:18 30 October 2009",
                           fontsize=7, rotate=90)
            pg.insert_htmlbox(fitz.Rect(60, 100, 540, 700),
                              f"<p>Pagina {i + 1}. " + "Texto del cuerpo del articulo. " * 30 + "</p>")
        doc.save(pdf)
        doc.close()
        md = pdf_to_markdown(pdf, os.path.join(d, "x.md"), options=Options(ocr_mode="off"))
        assert "Downloaded By" not in md, "El sello lateral repetido debe suprimirse"
        print("  -> PASSED: sello lateral 'Downloaded By' suprimido")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_entorno_ocr_e_idioma_por_documento():
    diag = diagnosticar_entorno_ocr()
    assert "tessdata_prefix" in diag and "has_hocr_config" in diag
    assert inferir_idioma_ocr_documento("it_copasir_2022_relazione.pdf", "spa+eng") in ("ita+eng", "spa+eng")
    assert inferir_idioma_ocr_documento("nl_aivd_2023_jaarverslag.pdf", "spa+eng") in ("nld+eng", "spa+eng")
    assert inferir_idioma_ocr_documento("pl_gov_2024_raport.pdf", "spa+eng") in ("pol+eng", "spa+eng")
    assert inferir_idioma_ocr_documento("es_dsn_2024.pdf", "spa+eng").startswith("spa")
    ver = version_motor_ocr("tesseract")
    assert ver.startswith("tesseract")
    print("  -> PASSED: diagnóstico OCR, versión de motor e idioma por documento")


def test_union_lineas_listas_y_outline_pdf():
    # Las listas numeradas partidas en varias líneas no deben dejar saltos a mitad de frase
    lineas_lista = [
        "1. We assess Russian President Vladimir Putin",
        "ordered an influence campaign in 2016 aimed at the",
        "US presidential election.",
        "2. Moscow also sought to undermine public faith",
        "in the democratic process.",
    ]
    unidas = join_clean_text_lines(lineas_lista).splitlines()
    assert len(unidas) == 2, f"Se esperaban 2 ítems unidos y salieron {len(unidas)}: {unidas}"
    assert "Putin ordered an influence" in unidas[0]
    assert "faith in the democratic" in unidas[1]

    # Esquema de marcadores (outline) del PDF promueve encabezados cuando no hay títulos detectados
    p1 = PageText(
        page_num=1,
        blocks=[
            Block(lines=[Line(spans=[Span(text="Introducción General")])], block_type="text", page_num=1),
            Block(lines=[Line(spans=[Span(text="Texto del capítulo inicial.")])], block_type="text", page_num=1),
        ],
    )
    toc = [(1, "Introducción General", 1), (2, "Anexo Metodológico", 1)]
    n_prom = aplicar_outline_pdf([p1], toc)
    assert n_prom >= 1
    assert any(b.block_type == "heading" and "Introducción General" in b.text() for b in p1.blocks)
    print("  -> PASSED: unión de líneas en listas numeradas y encabezados desde outline PDF")


def test_url_con_parentesis_y_csv_doc_id():
    d = tempfile.mkdtemp()
    try:
        for fn in ("cap_wiki.html", "cap_dsn.html"):
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                f.write(HTML)
        lista = os.path.join(d, "lote.csv")
        with open(lista, "w", encoding="utf-8") as f:
            f.write(
                "ruta;url;doc_id\n"
                "https://en.wikipedia.org/wiki/Noname057(16);wiki_noname057\n"
                "cap_wiki.html;https://en.wikipedia.org/wiki/Noname057(16);local_noname057\n"
                "cap_dsn.html;doc_dsn_personalizado\n"
            )
        items = leer_lista_estructurada(lista)
        assert len(items) == 3, f"La cabecera CSV debía ignorarse: {items}"
        assert items[0].origen == "https://en.wikipedia.org/wiki/Noname057(16)"
        assert items[0].nombre_salida == "wiki_noname057"
        assert items[1].url_declarada == "https://en.wikipedia.org/wiki/Noname057(16)"
        assert items[1].nombre_salida == "local_noname057"
        assert items[2].tipo == "local" and items[2].nombre_salida == "doc_dsn_personalizado"

        salida = os.path.join(d, "out")
        convertir_lote_web(items[1:], salida, WebOptions(), pausa=0, generar_index=False)
        generados = sorted(os.listdir(salida))
        assert "local_noname057.md" in generados and "doc_dsn_personalizado.md" in generados
        fm = leer_frontmatter(open(os.path.join(salida, "local_noname057.md"), encoding="utf-8").read())
        assert fm["url_origen"] == "https://en.wikipedia.org/wiki/Noname057(16)"
        print("  -> PASSED: URL con paréntesis 'Noname057(16)' y columnas CSV con doc_id")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_url_guardada_motivo_escaso_y_proteccion_md():
    d = tempfile.mkdtemp()
    try:
        # 1. URL extraída de comentario "saved from url" o meta og:url
        html_saved = os.path.join(d, "guardado.html")
        with open(html_saved, "w", encoding="utf-8") as f:
            f.write(
                "<!-- saved from url=(0045)https://www. state.gov/report-2024 -->\n"
                '<html><head><meta property="og:url" content="https://www.state.gov/report-2024">'
                "<title>State Report</title></head><body><article><h1>State Report</h1><p>"
                + ("Contenido analítico verificable del informe oficial. " * 20)
                + "</p></article></body></html>"
            )
        md_saved, meta_saved = html_to_markdown(html_saved, os.path.join(d, "guardado.md"))
        fm_s = leer_frontmatter(md_saved)
        assert fm_s["url_origen"] == "https://www.state.gov/report-2024"

        # 2. Motivo específico de contenido escaso (cookie wall) y protección de .md previo completo
        md_dest = os.path.join(d, "protegido.md")
        with open(md_dest, "w", encoding="utf-8") as f:
            f.write(md_saved)
        html_cookie = os.path.join(d, "cookie.html")
        with open(html_cookie, "w", encoding="utf-8") as f:
            f.write(
                "<html><head><title>Consent</title></head><body>"
                "<p>We use cookies. Accept all cookies to continue reading.</p>"
                "</body></html>"
            )
        _, meta_cookie = html_to_markdown(html_cookie, md_dest)
        assert meta_cookie.contenido in ("escaso", "ausente")
        assert "cookie" in meta_cookie.motivo_escaso.lower()
        # El archivo md_dest en disco NO debe haberse sobrescrito con la captura escasa
        en_disco = open(md_dest, encoding="utf-8").read()
        assert "Contenido analítico verificable del informe oficial." in en_disco

        # 3. En lote web, una descarga fallida deja 'archivo' y 'estado: fallo' en el manifiesto
        salida_lote = os.path.join(d, "lote_fallo")
        res = convertir_lote_web(
            [os.path.join(d, "inexistente_123.html")],
            salida_lote, WebOptions(), pausa=0, generar_index=False, conservar_nombre=True
        )
        assert res["fallidas"] == 1
        fila_f = res["filas"][0]
        assert fila_f["archivo"] == "inexistente_123.md"
        assert fila_f["estado"] == "fallo"
        print("  -> PASSED: URL guardada en HTML, motivo de escasez, protección de .md y manifiesto en fallos")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_all():
    print("--- PRUEBAS DE CONVERSIÓN DE CORPUS Y LOTES ---")
    test_opciones_por_defecto()
    test_fecha_pdf_iso()
    test_umbral_capa_texto()
    test_titulo_y_fechas_pdf()
    test_titulo_util()
    test_reanudar_rehace_ausentes_sin_ocr()
    test_manifiesto_con_entrada()
    test_lista_ruta_url_y_frontmatter_web()
    test_web_local_suelto()
    test_calidad_markdown()
    test_cabecera_lateral_suprimida()
    test_entorno_ocr_e_idioma_por_documento()
    test_union_lineas_listas_y_outline_pdf()
    test_url_con_parentesis_y_csv_doc_id()
    test_url_guardada_motivo_escaso_y_proteccion_md()
    print("[OK] TODAS LAS PRUEBAS DE CORPUS PASARON CON EXITO")


if __name__ == "__main__":
    run_all()

# -*- coding: utf-8 -*-
"""
test_mejoras.py — Pruebas de las mejoras de conversion:
hipervinculos, capa de texto, palabras partidas, manifiesto y lote reanudable.
"""

import os
import re
import shutil
import tempfile

import pymupdf as fitz

from pdfmd.batch import (
    convertir_lote,
    escribir_manifiesto,
    leer_frontmatter,
    ya_convertido,
)
from pdfmd.models import Options
from pdfmd.pipeline import pdf_to_markdown
from pdfmd.transform import es_guion_de_corte
from pdfmd.utils import slugify


def cuerpo(md):
    """Markdown sin el frontmatter."""
    if md.startswith("---"):
        partes = md.split("---", 2)
        if len(partes) == 3:
            return partes[2]
    return md


def _pdf_con_enlaces(ruta, n_paginas=2):
    doc = fitz.open()
    for i in range(n_paginas):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((50, 100), "Guia de recursos en linea", fontsize=14)
        pg.insert_text((50, 140), "El portal recursos-ejemplo.info se actualizo en junio.", fontsize=11)
        # El rectangulo cubre "recursos-ejemplo.info" dentro de la linea insertada
        destino = "https://ejemplo.org/archivo/pagina-" + str(i + 1)
        rects = pg.search_for("recursos-ejemplo.info")
        if rects:
            pg.insert_link({"kind": fitz.LINK_URI, "from": rects[0], "uri": destino})
    doc.save(ruta)
    doc.close()


def test_hipervinculos_conservados():
    """Los enlaces del PDF deben llegar al Markdown."""
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "informe.pdf")
        _pdf_con_enlaces(pdf, n_paginas=3)
        md = pdf_to_markdown(pdf, os.path.join(d, "informe.md"), options=Options())

        assert "](http" in md, "No se emitio ningun enlace Markdown"
        assert md.count("](http") >= 3, "Deben conservarse los enlaces de las tres paginas"
        assert "recursos-ejemplo.info](https://ejemplo.org/archivo/pagina-1)" in md, \
            "El ancla debe conservar el texto y apuntar a su destino real"
        assert "enlaces: 3" in md, "El recuento de enlaces debe constar en el frontmatter"
        print("  -> PASSED: hipervinculos conservados")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_enlaces_desactivables():
    """Sin la opcion activa no debe aparecer ningun enlace."""
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "informe.pdf")
        _pdf_con_enlaces(pdf)
        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"),
                             options=Options(extract_links=False))
        assert "](http" not in cuerpo(md), "Con extract_links=False no debe haber enlaces"
        print("  -> PASSED: extraccion de enlaces desactivable")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_capa_de_texto_ausente():
    """Un PDF sin capa de texto util debe avisar, no fallar en silencio."""
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "escaneado.pdf")
        doc = fitz.open()
        for _ in range(3):
            pg = doc.new_page(width=595, height=842)
            pg.insert_text((50, 100), "1", fontsize=9)   # practicamente sin texto
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "escaneado.md"), options=Options())
        campos = leer_frontmatter(md)

        assert campos.get("capa_texto") == "ausente", \
            "Debio marcarse la capa como ausente, no " + repr(campos.get("capa_texto"))
        assert "chars_por_pagina" in campos, "Falta chars_por_pagina en el frontmatter"
        assert "AVISO DE CONVERSION" in md, "El aviso debe verse en el cuerpo del documento"
        print("  -> PASSED: deteccion de PDF sin capa de texto")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_capa_de_texto_nativa():
    """Un PDF con texto de sobra no debe llevar aviso ni marcarse como ausente."""
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "denso.pdf")
        doc = fitz.open()
        parrafo = ("El equipo editorial reviso cada uno de los capitulos del "
                   "manual antes de la publicacion de la version final. ")
        for _ in range(2):
            pg = doc.new_page(width=595, height=842)
            y = 60
            for _ in range(30):
                pg.insert_text((40, y), parrafo, fontsize=9)
                y += 20
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "denso.md"), options=Options())
        campos = leer_frontmatter(md)

        assert campos.get("capa_texto") == "nativa", "La capa de texto es nativa"
        assert int(campos.get("chars_por_pagina", "0")) > 800, "Debe contar los caracteres reales"
        assert "AVISO DE CONVERSION" not in md, "No debe avisar de un PDF que si tiene texto"
        assert campos.get("idioma") == "es", "Idioma detectado: " + repr(campos.get("idioma"))
        print("  -> PASSED: capa de texto nativa e idioma")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_palabras_partidas():
    """Unir el corte tipografico sin destruir los compuestos legitimos."""
    assert es_guion_de_corte("docu-", "mentation"), "docu-mentation es un corte de linea"
    assert es_guion_de_corte("INFORMA-", "TION"), "INFORMA-TION es un corte de linea"
    assert es_guion_de_corte("biblio-", "teca"), "biblio-teca es un corte de linea"
    for izq, der in (("long-", "term"), ("left-", "right"), ("well-", "known"),
                     ("high-", "level"), ("Anglo-", "American")):
        assert not es_guion_de_corte(izq, der), izq + der + " es un compuesto legitimo"
    print("  -> PASSED: heuristica de palabras partidas")


def test_ninguna_linea_acaba_en_guion():
    """Comprobacion: grep -c '[a-z]-$' debe dar 0."""
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "cortes.pdf")
        doc = fitz.open()
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((50, 100), "La guia explica como preparar la docu-", fontsize=11)
        pg.insert_text((50, 118), "mentation del proyecto y planificar a long-", fontsize=11)
        pg.insert_text((50, 136), "term las tareas del equipo de trabajo.", fontsize=11)
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "cortes.md"), options=Options())
        texto = cuerpo(md)

        assert not re.search(r"[a-z]-$", texto, re.MULTILINE), \
            "Ninguna linea puede quedar acabada en guion"
        assert "documentation" in texto, "El corte docu-/mentation debia recomponerse"
        assert "long-term" in texto, "El compuesto long-term debia conservar su guion"
        print("  -> PASSED: ninguna linea acaba en guion de corte")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_nombre_de_salida_ascii():
    """Apostrofo tipografico y doble espacio no pueden llegar al nombre."""
    origen = "The Chef’s Guide to Seasonal  Cooking - Kitchen Lab"
    slug = slugify(origen)
    esperado = "the-chefs-guide-to-seasonal-cooking-kitchen-lab"
    assert slug == esperado, slug
    assert len(slug) <= 80, "El nombre no debe pasar de 80 caracteres"
    assert re.fullmatch(r"[a-z0-9-]+", slug), "Solo ASCII en minusculas y guiones"
    largo = slugify("Informe " * 40)
    assert len(largo) <= 80 and not largo.endswith("-"), "El recorte no debe dejar guion suelto"
    print("  -> PASSED: nombre de salida en ASCII")


def test_manifiesto_ordenado():
    """El manifiesto ordena por chars_pag ascendente."""
    d = tempfile.mkdtemp()
    try:
        filas = [
            {"archivo": "bueno.md", "paginas": "10", "chars": "35000",
             "chars_pag": "3500", "capa_texto": "nativa", "enlaces": "12", "avisos": ""},
            {"archivo": "dudoso.md", "paginas": "8", "chars": "400",
             "chars_pag": "50", "capa_texto": "ausente", "enlaces": "0", "avisos": "si"},
            {"archivo": "medio.md", "paginas": "5", "chars": "6000",
             "chars_pag": "1200", "capa_texto": "nativa", "enlaces": "3", "avisos": ""},
        ]
        ruta = escribir_manifiesto(filas, os.path.join(d, "manifiesto.tsv"))
        lineas = open(ruta, encoding="utf-8").read().strip().split("\n")

        assert lineas[0].split("\t")[0] == "archivo", "Falta la cabecera del TSV"
        assert lineas[1].startswith("dudoso.md"), \
            "El documento con menos texto por pagina va primero: es el que hay que revisar"
        assert lineas[3].startswith("bueno.md"), "El mas denso va el ultimo"
        assert len(lineas[0].split("\t")) == 7, "El TSV tiene siete columnas"
        print("  -> PASSED: manifiesto ordenado por densidad de texto")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_lote_reanudable():
    """Un lote relanzado no rehace lo ya convertido."""
    d = tempfile.mkdtemp()
    try:
        entrada = os.path.join(d, "pdfs")
        salida = os.path.join(d, "md")
        os.makedirs(entrada)
        for i in range(3):
            _pdf_con_enlaces(os.path.join(entrada, "doc " + str(i + 1) + ".pdf"), n_paginas=1)

        pdfs = sorted(os.path.join(entrada, f) for f in os.listdir(entrada))
        registro = []
        filas = convertir_lote(pdfs, salida, options=Options(), log_cb=registro.append)

        assert len(filas) == 3, "Deben convertirse los tres documentos"
        assert os.path.exists(os.path.join(salida, "manifiesto_conversion.tsv")), \
            "El lote debe dejar su manifiesto"
        assert os.path.exists(os.path.join(salida, "doc-1.md")), \
            "El nombre de salida debe ir en slug ASCII"
        assert not any("se omite" in m for m in registro), "En la primera pasada no se omite nada"

        registro_2 = []
        convertir_lote(pdfs, salida, options=Options(), log_cb=registro_2.append)
        omitidos = [m for m in registro_2 if "se omite" in m]
        assert len(omitidos) == 3, "La segunda pasada debe omitir los tres, omitio " + str(len(omitidos))

        # Si el PDF cambia, el hash deja de coincidir y hay que reconvertir
        _pdf_con_enlaces(pdfs[0], n_paginas=2)
        registro_3 = []
        convertir_lote(pdfs, salida, options=Options(), log_cb=registro_3.append)
        assert len([m for m in registro_3 if "se omite" in m]) == 2, \
            "El documento modificado debe reconvertirse"
        print("  -> PASSED: lote reanudable por hash")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ya_convertido_exige_hash():
    """Sin hash no hay reanudacion: mas vale reconvertir que dar por bueno otro fichero."""
    d = tempfile.mkdtemp()
    try:
        md = os.path.join(d, "x.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write('---\ntitle: "x"\nsha256_pdf: "abc123"\n---\n\nCuerpo\n')
        assert ya_convertido(md, "abc123"), "Mismo hash: ya esta convertido"
        assert not ya_convertido(md, "otro"), "Hash distinto: hay que reconvertir"
        assert not ya_convertido(md, ""), "Sin hash no se puede afirmar nada"
        assert not ya_convertido(os.path.join(d, "no-existe.md"), "abc123")
        print("  -> PASSED: la reanudacion se apoya en el hash")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_marcadores_de_pagina_por_pagina():
    """Un <!-- p.N --> por cada pagina del PDF."""
    d = tempfile.mkdtemp()
    try:
        pdf = os.path.join(d, "paginado.pdf")
        _pdf_con_enlaces(pdf, n_paginas=5)
        md = pdf_to_markdown(pdf, os.path.join(d, "paginado.md"), options=Options())
        assert len(re.findall(r"<!-- p\.\d+ -->", md)) == 5, \
            "Debe haber tantos marcadores como paginas"
        assert leer_frontmatter(md).get("pages") == "5", "El frontmatter cuenta las paginas"
        print("  -> PASSED: un marcador por pagina")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_all():
    print("--- PRUEBAS DE LAS MEJORAS DE CONVERSION ---")
    test_hipervinculos_conservados()
    test_enlaces_desactivables()
    test_capa_de_texto_ausente()
    test_capa_de_texto_nativa()
    test_palabras_partidas()
    test_ninguna_linea_acaba_en_guion()
    test_nombre_de_salida_ascii()
    test_manifiesto_ordenado()
    test_lote_reanudable()
    test_ya_convertido_exige_hash()
    test_marcadores_de_pagina_por_pagina()
    print("[OK] TODAS LAS PRUEBAS DE MEJORAS PASARON CON EXITO")


if __name__ == "__main__":
    run_all()

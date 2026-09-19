# -*- coding: utf-8 -*-
"""
Pruebas de las funciones de citabilidad y trazabilidad:
notas al pie, marcadores de página, frontmatter de procedencia,
slug ASCII y marca de fin de portada.
"""

import os
import re
import shutil
import tempfile

import pymupdf as fitz

from pdfmd import Options, pdf_to_markdown
from pdfmd.footnotes import normalizar_marcador
from pdfmd.utils import sha256_fichero, slugify


def cuerpo(md: str) -> str:
    """Devuelve el Markdown sin el frontmatter, que ahora audita lo suprimido."""
    if md.startswith("---"):
        partes = md.split("---", 2)
        if len(partes) == 3:
            return partes[2]
    return md


def _pagina_con_notas(doc, titulo, notas, con_raya=True, marca_volada=True):
    """Crea una página con cuerpo, llamadas voladas y notas al pie."""
    pg = doc.new_page(width=595, height=842)
    if marca_volada:
        llamadas = "".join(
            f"El punto {i+1} lo trata el autor<sup>{i+1}</sup> con detalle suficiente. "
            for i in range(len(notas))
        )
    else:
        llamadas = "".join(
            f"El punto {i+1} lo trata el autor con detalle suficiente. "
            for i in range(len(notas))
        )
    pg.insert_htmlbox(fitz.Rect(50, 60, 545, 500),
                      f"<h2>{titulo}</h2><p>Texto corriente que da cuerpo al documento "
                      f"y ocupa varias lineas seguidas. {llamadas}</p>")
    if con_raya:
        pg.draw_line(fitz.Point(50, 740), fitz.Point(250, 740))
    cuerpo_notas = "<br>".join(f"{i+1} {t}" for i, t in enumerate(notas))
    pg.insert_htmlbox(fitz.Rect(50, 750, 545, 835),
                      f"<p style='font-size:8px'>{cuerpo_notas}</p>")
    return pg


def test_notas_basicas_y_renumeracion():
    """Dos páginas que reinician su numeración deben salir correlativas 1..4."""
    d = tempfile.mkdtemp(prefix="kiwi_t_notas_")
    try:
        pdf = os.path.join(d, "doc.pdf")
        doc = fitz.open()
        _pagina_con_notas(doc, "Capitulo 1", ["Primera nota del documento.", "Segunda nota."])
        _pagina_con_notas(doc, "Capitulo 2", ["Tercera nota.", "Cuarta y ultima nota."])
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"), options=Options())

        for n in (1, 2, 3, 4):
            assert f"[^{n}]" in md, f"Falta la llamada [^{n}]"
            assert f"[^{n}]:" in md, f"Falta la definicion [^{n}]:"
        assert "[^5]" not in md, "Numeracion de mas"
        # La numeración es global: la nota 1 de la página 2 pasa a ser la 3
        assert "[^3]: Tercera nota." in md, md
        # Las definiciones van al final, después del cuerpo
        assert md.index("[^1]:") > md.index("Capitulo 2"), "Las notas deben ir al final"
        # El texto de la nota ya no aparece suelto en el cuerpo
        assert md.count("Primera nota del documento.") == 1
        print("  -> PASSED: notas al pie y renumeracion global")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_nota_multilinea():
    """Una nota que ocupa dos líneas debe unirse en una sola definición."""
    d = tempfile.mkdtemp(prefix="kiwi_t_multi_")
    try:
        pdf = os.path.join(d, "doc.pdf")
        doc = fitz.open()
        larga = ("Esta nota es deliberadamente larga para que el maquetador la reparta "
                 "en dos lineas completas dentro del pie de la pagina impresa.")
        _pagina_con_notas(doc, "Capitulo", [larga])
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"), options=Options())
        definiciones = [l for l in md.splitlines() if l.startswith("[^1]:")]
        assert len(definiciones) == 1, f"La nota debe quedar en una sola linea: {definiciones}"
        assert "dos lineas completas" in definiciones[0], definiciones[0]
        print("  -> PASSED: nota de varias lineas unida")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_modo_degradado_sin_llamada():
    """Sin llamada volada localizable, la nota va al apartado sin ancla."""
    d = tempfile.mkdtemp(prefix="kiwi_t_degr_")
    try:
        pdf = os.path.join(d, "doc.pdf")
        doc = fitz.open()
        _pagina_con_notas(doc, "Capitulo", ["Nota huerfana sin llamada."], marca_volada=False)
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"), options=Options())
        assert "## Notas sin ancla" in md, "Debe crearse el apartado de notas sin ancla"
        assert "Nota huerfana sin llamada." in md, "La nota no puede perderse"
        assert "[^1]:" not in md, "Sin llamada no debe emitirse definicion huerfana"
        print("  -> PASSED: modo degradado sin llamada")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_limpieza_no_se_come_las_notas():
    """Regresión: la supresión de pies y de numeración no debe borrar las notas."""
    d = tempfile.mkdtemp(prefix="kiwi_t_reg_")
    try:
        pdf = os.path.join(d, "doc.pdf")
        doc = fitz.open()
        for i in range(3):
            pg = _pagina_con_notas(doc, f"Capitulo {i+1}", [f"Nota especifica numero {i+1}."])
            # Pie repetitivo y número de página, justo donde viven las notas
            pg.insert_text((50, 828), "Empresa S.A. - Confidencial", fontsize=7)
            pg.insert_text((300, 828), str(i + 1), fontsize=7)
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"), options=Options())
        for i in range(3):
            assert f"Nota especifica numero {i+1}." in md, f"Se perdio la nota {i+1}"
        assert "Confidencial" not in cuerpo(md), "El pie repetitivo si debe eliminarse"
        # El número de página no puede acabar pegado al texto de una nota
        assert not re.search(r"Nota especifica numero \d\.\s+\d", md), \
            "La numeracion de pagina se ha colado dentro de una nota"
        print("  -> PASSED: la limpieza de pies respeta las notas")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_marcadores_de_pagina():
    """Un marcador por página, correlativo, incluidas las páginas vacías."""
    d = tempfile.mkdtemp(prefix="kiwi_t_marc_")
    try:
        pdf = os.path.join(d, "doc.pdf")
        doc = fitz.open()
        for i in range(4):
            pg = doc.new_page(width=595, height=842)
            if i != 2:  # La página 3 se deja en blanco a propósito
                pg.insert_text((50, 100), f"Contenido de la pagina {i+1}.", fontsize=11)
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"), options=Options())
        marcadores = re.findall(r"<!-- p\.(\d+) -->", md)
        assert marcadores == ["1", "2", "3", "4"], f"Marcadores incorrectos: {marcadores}"

        sin = pdf_to_markdown(pdf, os.path.join(d, "o2.md"), options=Options(page_markers=False))
        assert "<!-- p." not in sin, "Con la opcion desactivada no debe emitirse ninguno"
        print("  -> PASSED: marcadores de pagina correlativos")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_frontmatter_procedencia():
    """El frontmatter debe llevar hash real, fechas y la URL cuando se aporta."""
    d = tempfile.mkdtemp(prefix="kiwi_t_fm_")
    try:
        pdf = os.path.join(d, "doc.pdf")
        doc = fitz.open()
        pg = doc.new_page()
        pg.insert_text((50, 100), "Contenido de prueba para el hash.", fontsize=11)
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"),
                             options=Options(url_origen="https://ejemplo.org/doc.pdf",
                                             fecha_captura="2026-01-15"))
        assert 'url_origen: "https://ejemplo.org/doc.pdf"' in md
        assert 'fecha_captura: "2026-01-15"' in md
        assert "fecha_conversion:" in md

        esperado = sha256_fichero(pdf)
        assert f'sha256_pdf: "{esperado}"' in md, "El hash del frontmatter no coincide"
        assert len(esperado) == 64

        sin = pdf_to_markdown(pdf, os.path.join(d, "o2.md"), options=Options(compute_hash=False))
        assert "sha256_pdf:" not in sin
        print("  -> PASSED: frontmatter de procedencia")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_slug():
    assert slugify("Informe Anual: Situación 2026's") == "informe-anual-situacion-2026s"
    assert slugify("Análisis   de  Datos") == "analisis-de-datos"
    assert slugify("Ñandú & Co.") == "nandu-co"
    assert slugify("¿¡!?") == "documento"          # Sin nada aprovechable
    assert len(slugify("Á" * 300)) <= 120           # Se recorta
    assert slugify("2026's") == "2026s"             # El apóstrofo no deja hueco
    print("  -> PASSED: slug ASCII")


def test_marcadores_superindice():
    assert normalizar_marcador("¹") == "1"
    assert normalizar_marcador("¹²") == "12"
    assert normalizar_marcador("7") == "7"
    print("  -> PASSED: normalizacion de digitos volados")


def test_fin_de_portada():
    """Se marca tras los créditos; si no hay créditos, no se marca nada."""
    d = tempfile.mkdtemp(prefix="kiwi_t_port_")
    try:
        def construir(con_creditos):
            pdf = os.path.join(d, f"doc_{con_creditos}.pdf")
            doc = fitz.open()
            pg = doc.new_page(width=595, height=842)
            if con_creditos:
                pg.insert_text((50, 300), "ISBN: 978-84-000-0000-0", fontsize=9)
                pg.insert_text((50, 320), "Deposito legal: M-1234-2026", fontsize=9)
            else:
                pg.insert_text((50, 300), "Indice general del documento", fontsize=11)
            pg2 = doc.new_page(width=595, height=842)
            pg2.insert_htmlbox(fitz.Rect(50, 60, 545, 500),
                               "<h2>Capitulo primero</h2><p>" +
                               ("Cuerpo real del documento con extension suficiente. " * 12) + "</p>")
            doc.save(pdf)
            doc.close()
            return pdf

        con = pdf_to_markdown(construir(True), os.path.join(d, "a.md"), options=Options())
        assert "<!-- fin-de-portada -->" in con, "Deberia detectarse tras los creditos"
        assert con.index("<!-- fin-de-portada -->") < con.index("Capitulo primero")

        sin = pdf_to_markdown(construir(False), os.path.join(d, "b.md"), options=Options())
        assert "<!-- fin-de-portada -->" not in sin, "Sin creditos no debe marcar nada"
        print("  -> PASSED: fin de portada (y prudencia cuando no hay creditos)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ligaduras():
    """Las ligaduras del PDF deben deshacerse o el Markdown no sería buscable."""
    from pdfmd.utils import normalizar_texto_pdf
    assert normalizar_texto_pdf("aﬁrmacion") == "afirmacion"
    assert normalizar_texto_pdf("reﬂexion") == "reflexion"
    assert normalizar_texto_pdf("eﬃcaz") == "efficaz"   # ﬃ es la ligadura "ffi"
    assert normalizar_texto_pdf("dato suelto") == "dato suelto"
    # Los dígitos volados NO deben tocarse: son las llamadas de nota al pie
    assert normalizar_texto_pdf("autor¹") == "autor¹"

    d = tempfile.mkdtemp(prefix="kiwi_t_lig_")
    try:
        pdf = os.path.join(d, "doc.pdf")
        doc = fitz.open()
        pg = doc.new_page(width=595, height=842)
        pg.insert_htmlbox(fitz.Rect(50, 60, 545, 300),
                          "<p>La afirmacion final refleja una clasificacion eficaz del asunto.</p>")
        doc.save(pdf)
        doc.close()

        md = pdf_to_markdown(pdf, os.path.join(d, "o.md"), options=Options())
        assert "afirmacion" in md, "La ligadura fi ha sobrevivido al Markdown"
        assert "refleja" in md, "La ligadura fl ha sobrevivido al Markdown"
        assert "ﬁ" not in md and "ﬂ" not in md, "Quedan ligaduras sin deshacer"
        print("  -> PASSED: ligaduras tipograficas deshechas")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_all():
    print("--- PRUEBAS DE NOTAS AL PIE Y CITABILIDAD ---")
    test_notas_basicas_y_renumeracion()
    test_nota_multilinea()
    test_modo_degradado_sin_llamada()
    test_limpieza_no_se_come_las_notas()
    test_marcadores_de_pagina()
    test_frontmatter_procedencia()
    test_slug()
    test_marcadores_superindice()
    test_fin_de_portada()
    test_ligaduras()
    print("[OK] TODAS LAS PRUEBAS DE NOTAS Y CITABILIDAD PASARON CON EXITO")


if __name__ == "__main__":
    run_all()

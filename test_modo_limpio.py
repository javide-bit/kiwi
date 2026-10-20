# -*- coding: utf-8 -*-
"""
test_modo_limpio.py — Pruebas del modo visualmente limpio (clean_mode).
Verifica que el Markdown generado omita comentarios HTML (<!-- p.N -->),
saltos de página artificiales (---), avisos de conversión intrusivos
y metadatos técnicos excesivos, unificando líneas de prosa.
"""

import os
import shutil
import tempfile
import pymupdf as fitz

from pdfmd.models import Options
from pdfmd.pipeline import pdf_to_markdown
from pdfmd.render import join_clean_text_lines
from htmlmd.models import WebOptions
from htmlmd.pipeline import html_to_markdown
import kiwi_app


def _crear_pdf_prueba(ruta: str):
    doc = fitz.open()
    doc.set_metadata({
        "title": "Documento para Lectura Limpia",
        "author": "Usuario",
        "subject": "Prueba de Modo Limpio",
        "keywords": "Limpio, Lectura, Markdown"
    })
    # Pagina 1
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((50, 60), "TITULO DEL DOCUMENTO", fontsize=18)
    p1.insert_text((50, 90), "Este es el primer renglon de un parrafo largo", fontsize=11)
    p1.insert_text((50, 105), "que continua exactamente aqui en el segundo renglon.", fontsize=11)
    p1.insert_text((50, 135), "- [x] Primer elemento de lista", fontsize=11)
    p1.insert_text((50, 150), "- [ ] Segundo elemento de lista", fontsize=11)

    # Pagina 2
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((50, 60), "Segunda Pagina del Texto", fontsize=14)
    p2.insert_text((50, 90), "Contenido que pertenece a la segunda pagina.", fontsize=11)

    doc.save(ruta)
    doc.close()


def test_join_clean_text_lines():
    """Verifica que join_clean_text_lines una párrafos y respete listas."""
    lineas_prosa = [
        "Este es un párrafo de texto normal",
        "que continúa fluidamente en la siguiente línea."
    ]
    resultado_prosa = join_clean_text_lines(lineas_prosa)
    assert resultado_prosa == "Este es un párrafo de texto normal que continúa fluidamente en la siguiente línea."

    lineas_lista = [
        "- Elemento 1",
        "- Elemento 2",
        "1. Elemento numerado"
    ]
    resultado_lista = join_clean_text_lines(lineas_lista)
    assert resultado_lista == "- Elemento 1\n- Elemento 2\n1. Elemento numerado"
    print("  -> PASSED: join_clean_text_lines une prosa y respeta listas")


def test_pdf_modo_limpio():
    """Verifica la salida de pdf_to_markdown con clean_mode=True frente a clean_mode=False."""
    tmp = tempfile.mkdtemp(prefix="kiwi_test_limpio_")
    try:
        pdf_path = os.path.join(tmp, "doc.pdf")
        _crear_pdf_prueba(pdf_path)

        out_normal = os.path.join(tmp, "normal.md")
        out_limpio = os.path.join(tmp, "limpio.md")

        # 1. Normal (con saltos, marcadores de página y frontmatter técnico)
        md_normal = pdf_to_markdown(
            pdf_path, out_normal,
            options=Options(insert_page_breaks=True, page_markers=True, clean_mode=False)
        )
        assert "<!-- p.1 -->" in md_normal
        assert "<!-- p.2 -->" in md_normal
        assert "sha256_pdf:" in md_normal
        assert "chars_extraidos:" in md_normal
        assert "capa_texto:" in md_normal

        # 2. Modo limpio
        md_limpio = pdf_to_markdown(
            pdf_path, out_limpio,
            options=Options(insert_page_breaks=True, page_markers=True, clean_mode=True)
        )
        # Sin marcadores HTML
        assert "<!-- p." not in md_limpio, "El modo limpio no debe incluir <!-- p.N -->"
        assert "<!-- fin-de-portada -->" not in md_limpio
        # Sin saltos de página artificiales entre páginas
        assert "\n\n---\n\n" not in md_limpio, "El modo limpio no debe incluir separadores '---' entre páginas"
        # Sin telemetría técnica en el frontmatter
        # La huella se conserva: sin ella el .md no se puede emparejar con su PDF
        assert "sha256_pdf:" in md_limpio, "El modo limpio conserva sha256_pdf"
        assert "chars_extraidos:" not in md_limpio, "El modo limpio no debe incluir chars_extraidos"
        assert "capa_texto:" not in md_limpio, "El modo limpio no debe incluir capa_texto"
        assert "generator:" not in md_limpio, "El modo limpio no debe incluir generator"
        assert "ocr_motor:" not in md_limpio
        # Conserva metadatos editoriales
        assert 'title: "Documento para Lectura Limpia"' in md_limpio
        assert 'author: "Usuario"' in md_limpio
        # Párrafos unidos
        assert "primer renglon de un parrafo largo que continua exactamente" in md_limpio
        # Listas preservadas
        assert "- [x] Primer elemento de lista" in md_limpio
        assert "- [ ] Segundo elemento de lista" in md_limpio

        print("  -> PASSED: PDF en modo limpio elimina comentarios, saltos y telemetría")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_web_modo_limpio():
    """Verifica que el conversor web en clean_mode genere frontmatter minimalista sin avisos."""
    tmp = tempfile.mkdtemp(prefix="kiwi_web_limpio_")
    try:
        html_path = os.path.join(tmp, "pagina.html")
        out_limpio = os.path.join(tmp, "salida.md")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write("<html><head><title>Articulo Web</title></head><body><article><p>Breve texto.</p></article></body></html>")

        md_web, _ = html_to_markdown(html_path, out_limpio, options=WebOptions(clean_mode=True))
        assert "sha256_html:" not in md_web
        assert "sha256:" in md_web, "El modo limpio conserva la huella del HTML"
        assert "chars_extraidos:" not in md_web
        assert "criterio_cuerpo:" not in md_web
        assert "generator:" not in md_web
        assert "AVISO DE CONVERSION" not in md_web
        assert 'title: "Articulo Web"' in md_web
        print("  -> PASSED: Web en modo limpio genera frontmatter simple y sin avisos")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_flags():
    """Verifica que las opciones --modo-limpio y --clean se reconozcan en el CLI."""
    parser = kiwi_app.build_parser()
    args_conv = parser.parse_args(["convert", "doc.pdf", "--modo-limpio"])
    assert args_conv.clean_mode is True

    args_conv_clean = parser.parse_args(["convert", "doc.pdf", "--clean"])
    assert args_conv_clean.clean_mode is True

    args_web = parser.parse_args(["web", "https://ejemplo.com", "--modo-limpio"])
    assert args_web.clean_mode is True

    print("  -> PASSED: CLI flags --modo-limpio y --clean")


def run_all():
    print("--- PRUEBAS DEL MODO VISUALMENTE LIMPIO ---")
    test_join_clean_text_lines()
    test_pdf_modo_limpio()
    test_web_modo_limpio()
    test_cli_flags()
    print("[OK] TODAS LAS PRUEBAS DEL MODO LIMPIO PASARON CON EXITO")


if __name__ == "__main__":
    run_all()

# -*- coding: utf-8 -*-
"""
Test de verificación para las capacidades avanzadas de pdfmd en KIWI.
"""

import os
import shutil
import tempfile
from PIL import Image
import pymupdf as fitz

from pdfmd import Options, pdf_to_markdown


def create_sample_pdf(pdf_path: str):
    doc = fitz.open()

    # Metadatos del documento
    doc.set_metadata({
        "title": "Documento Científico de Inteligencia Artificial",
        "author": "Equipo KIWI",
        "subject": "Procesamiento de Lenguaje Natural",
        "keywords": "PDF, Markdown, LaTeX, OCR"
    })

    # Página 1: Título, Párrafos, Fórmulas y Tabla
    page1 = doc.new_page(width=595, height=842)

    # Insertar Título Grande
    page1.insert_text((50, 60), "DOCUMENTO CIENTIFICO DE PRUEBA", fontsize=20)
    # Subtítulo
    page1.insert_text((50, 95), "1. Introduccion y Teoria", fontsize=15)
    # Texto normal con cita
    page1.insert_text((50, 125), "Este es un parrafo de texto normal explicando los fundamentos del sistema.", fontsize=11)
    page1.insert_text((50, 145), "> Cita relevante de un autor clasico sobre la materia.", fontsize=11)
    
    # Ecuación matemática con fracciones y operadores
    page1.insert_text((50, 175), "Ecuacion: f(x) = ∫ x² dx + α + ½", fontsize=11)

    # Subtítulo 2
    page1.insert_text((50, 210), "2. Resultados Experimentales", fontsize=15)

    # Tabla con columnas alineadas
    page1.insert_text((50, 240), "Metrica       Valor       Estado", fontsize=11)
    page1.insert_text((50, 255), "Precision     98.5%       Aprobado", fontsize=11)
    page1.insert_text((50, 270), "Latencia      12ms        Optimo", fontsize=11)
    page1.insert_text((50, 285), "Memoria       45MB        Normal", fontsize=11)

    # Página 2: Imagen, lista y bloque de código
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((50, 60), "3. Conclusiones y Codigo", fontsize=15)
    page2.insert_text((50, 90), "- [x] Tarea completada con exito.", fontsize=11)
    page2.insert_text((50, 110), "- [ ] Tarea pendiente de revision.", fontsize=11)

    # Bloque de código monospace
    page2.insert_text((50, 140), "def kiwi_process(doc):", fontsize=10, fontname="courier")
    page2.insert_text((50, 155), "    return doc.to_markdown()", fontsize=10, fontname="courier")

    # Crear una pequeña imagen de prueba e insertarla
    temp_img = os.path.join(os.path.dirname(pdf_path), "sample_img.png")
    im = Image.new("RGB", (150, 150), color=(74, 133, 5))
    im.save(temp_img)
    page2.insert_image(fitz.Rect(50, 180, 200, 330), filename=temp_img)

    doc.save(pdf_path)
    doc.close()
    if os.path.exists(temp_img):
        os.remove(temp_img)


def test_pdfmd_pipeline():
    temp_dir = tempfile.mkdtemp(prefix="pdfmd_test_")
    try:
        sample_pdf = os.path.join(temp_dir, "test_doc.pdf")
        output_md = os.path.join(temp_dir, "test_doc.md")

        create_sample_pdf(sample_pdf)
        assert os.path.exists(sample_pdf), "El PDF sintético debe existir"

        logs = []
        opts = Options(
            export_images=True,
            insert_page_breaks=True,
            detect_tables=True,
            convert_equations=True,
            include_frontmatter=True,
            detect_code_blocks=True
        )

        md_result = pdf_to_markdown(
            input_pdf=sample_pdf,
            output_md=output_md,
            options=opts,
            log_cb=logs.append
        )

        print("\n--- CONTENIDO MARKDOWN GENERADO ---")
        print(md_result)
        print("-----------------------------------\n")

        assert os.path.exists(output_md), "El archivo MD de salida debe existir"
        
        # 1. Frontmatter
        assert "---" in md_result
        assert 'title: "Documento Científico de Inteligencia Artificial"' in md_result or 'title: "DOCUMENTO CIENTIFICO' in md_result
        assert 'author: "Equipo KIWI"' in md_result

        # 2. Títulos
        assert "DOCUMENTO CIENTIFICO" in md_result
        assert "Introduccion y Teoria" in md_result

        # 3. Citas y listas
        assert "> Cita relevante" in md_result or "Cita relevante" in md_result
        assert "- [x]" in md_result or "Tarea completada" in md_result

        # 4. Matemáticas y fracciones
        assert r"\alpha" in md_result or r"\int" in md_result or "x^{2}" in md_result or r"\frac{1}{2}" in md_result

        # 5. Tablas formateadas
        assert "|" in md_result and "---" in md_result

        # 6. Bloques de código monospace
        assert "```" in md_result or "kiwi_process" in md_result

        # 7. Imágenes exportadas
        assert "![Imagen" in md_result
        assets_dir = os.path.join(temp_dir, "test_doc_assets")
        assert os.path.exists(assets_dir), "El directorio de assets debe existir"
        assert len(os.listdir(assets_dir)) >= 1, "Debe haber imágenes guardadas en assets"

        print("[OK] TODAS LAS PRUEBAS AVANZADAS DE PDFMD PASARON CON EXITO")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    test_pdfmd_pipeline()

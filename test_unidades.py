# -*- coding: utf-8 -*-
"""
Pruebas unitarias de las heurísticas de pdfmd y de las utilidades de KIWI.

Complementan a test_pdfmd.py (pipeline completo sobre un PDF sintético) y a
test_kiwi.py (motor de recolección), cubriendo los casos concretos que
motivaron cada corrección.
"""

import os
import shutil
import tempfile

import pymupdf as fitz
from PIL import Image

from pdfmd import Options, pdf_to_markdown
from pdfmd.equations import convert_line_text
from pdfmd.models import Line, Span
from pdfmd.render import yaml_quote
from pdfmd.tables import detect_spaced_table_lines
from pdfmd.transform import _defragment, is_page_number_line, normalize_bullet

from kiwi_app import KiwiConverterRunner, ruta_visible


def _linea(texto):
    return Line(spans=[Span(text=texto)])


def cuerpo(md: str) -> str:
    """Devuelve el Markdown sin el frontmatter, que ahora audita lo suprimido."""
    if md.startswith("---"):
        partes = md.split("---", 2)
        if len(partes) == 3:
            return partes[2]
    return md


def test_ecuaciones():
    # Fórmula pura -> bloque display
    assert convert_line_text("E = mc²") == "$$E = mc^{2}$$"
    # Etiqueta + fórmula -> la prosa queda fuera del modo matemático
    assert convert_line_text("Ecuacion: α + β = γ") == r"Ecuacion: $\alpha + \beta = \gamma$"
    # Prosa con símbolo duro -> solo el símbolo se envuelve
    assert convert_line_text("El coeficiente α mide la dispersion") == \
        r"El coeficiente $\alpha$ mide la dispersion"
    # Los símbolos legibles en texto plano no se tocan dentro de prosa
    assert convert_line_text("La temperatura sube 25° cada hora") == \
        "La temperatura sube 25° cada hora"
    # Una línea ya en LaTeX no se vuelve a envolver
    assert convert_line_text("$x^{2}$") == "$x^{2}$"
    print("  -> PASSED: conversión de ecuaciones")


def test_numeros_de_pagina():
    for txt in ("12", "- 7 -", "Página 3 de 40", "Page 3/40", "| 5 |"):
        assert is_page_number_line(txt), f"Debería detectarse como paginación: {txt}"
    for txt in ("2024 fue un año clave", "Capitulo 3", "3.1 Metodologia"):
        assert not is_page_number_line(txt), f"No es paginación: {txt}"
    print("  -> PASSED: detección de numeración de página")


def test_defragmentacion_respeta_tablas():
    """La fusión de líneas huérfanas no debe destruir las columnas de una tabla."""
    filas = [_linea("Metrica       Valor       Estado"),
             _linea("Precision     98.5%       Aprobado"),
             _linea("Latencia      12ms        Optimo")]
    fusionadas = _defragment(filas, Options())
    assert len(fusionadas) == 3, f"Las filas no deben fusionarse, quedaron {len(fusionadas)}"

    # En cambio, una línea corta de prosa sí se une con la siguiente
    prosa = [_linea("Texto corto"), _linea("que continua en la linea siguiente.")]
    assert len(_defragment(prosa, Options())) == 1
    print("  -> PASSED: defragmentación respeta tablas")


def test_tabla_espaciada_rechaza_prosa():
    prosa = [_linea("Este parrafo tiene  un doble espacio accidental y es muy largo de verdad"),
             _linea("Y esta segunda linea  tambien lo tiene, pero sigue siendo prosa corriente")]
    assert detect_spaced_table_lines(prosa) is None
    print("  -> PASSED: la detección de tablas rechaza prosa")


def test_normalizacion_de_vinetas():
    linea = _linea("• Primer elemento de la lista")
    normalize_bullet(linea)
    assert linea.text() == "- Primer elemento de la lista", linea.text()
    print("  -> PASSED: normalización de viñetas")


def test_yaml_escapado():
    assert yaml_quote('Informe "final" del año') == '"Informe \\"final\\" del año"'
    assert yaml_quote("Titulo\ncon salto") == '"Titulo con salto"'
    print("  -> PASSED: escapado YAML del frontmatter")


def test_ruta_visible():
    assert ruta_visible("\\\\?\\C:\\datos\\a.pdf") == "C:\\datos\\a.pdf"
    assert ruta_visible("\\\\?\\UNC\\servidor\\share\\a.pdf") == "\\\\servidor\\share\\a.pdf"
    assert ruta_visible("C:\\datos\\a.pdf") == "C:\\datos\\a.pdf"
    print("  -> PASSED: normalización de rutas largas")


def test_nombres_de_salida_unicos():
    """Dos PDFs homónimos en subcarpetas distintas no deben pisarse al convertir."""
    runner = KiwiConverterRunner(items=[], output_dir="/salida")
    usados = set()
    a = runner._ruta_salida_unica(os.path.join("carpeta_a", "informe.pdf"), usados)
    b = runner._ruta_salida_unica(os.path.join("carpeta_b", "informe.pdf"), usados)
    assert a != b, "Los dos informes homónimos comparten fichero de salida"
    assert os.path.basename(b) == "informe_2.md", os.path.basename(b)
    print("  -> PASSED: nombres de salida únicos en lote")


def test_deduplicacion_de_imagenes():
    """Un mismo logotipo repetido en varias páginas se guarda una sola vez."""
    temp_dir = tempfile.mkdtemp(prefix="pdfmd_img_")
    try:
        img_path = os.path.join(temp_dir, "logo.png")
        Image.new("RGB", (120, 120), color=(74, 133, 5)).save(img_path)

        pdf_path = os.path.join(temp_dir, "repetida.pdf")
        doc = fitz.open()
        for _ in range(3):
            page = doc.new_page()
            page.insert_text((50, 50), "Pagina con logotipo corporativo repetido.", fontsize=11)
            page.insert_image(fitz.Rect(50, 80, 170, 200), filename=img_path)
        doc.save(pdf_path)
        doc.close()

        out_md = os.path.join(temp_dir, "repetida.md")
        md = pdf_to_markdown(pdf_path, out_md, options=Options(export_images=True))

        assets = os.listdir(os.path.join(temp_dir, "repetida_assets"))
        assert len(assets) == 1, f"El logotipo debe guardarse una sola vez, hay {assets}"
        assert md.count("![Imagen") == 3, "Las tres apariciones deben seguir enlazadas"
        print("  -> PASSED: deduplicación de imágenes repetidas")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_limpieza_de_cabeceras_y_pies():
    """Un informe con cabecera, pie y paginación debe salir limpio de ruido."""
    temp_dir = tempfile.mkdtemp(prefix="pdfmd_hf_")
    try:
        pdf_path = os.path.join(temp_dir, "informe.pdf")
        doc = fitz.open()
        for i in range(1, 5):
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 30), "INFORME ANUAL - CONFIDENCIAL", fontsize=8)
            page.insert_text((50, 120), f"Seccion {i}", fontsize=16)
            page.insert_text((50, 150), "Contenido del apartado con texto corriente.", fontsize=11)
            page.insert_text((50, 170), "• Primer punto de la lista", fontsize=11)
            page.insert_text((50, 190), "• Segundo punto de la lista", fontsize=11)
            page.insert_text((290, 810), str(i), fontsize=9)
            page.insert_text((50, 810), "Empresa S.A.", fontsize=8)
        doc.save(pdf_path)
        doc.close()

        md = pdf_to_markdown(pdf_path, os.path.join(temp_dir, "informe.md"), options=Options())

        # Por defecto, la cabecera desaparece del cuerpo y NO ensucia el frontmatter
        assert "CONFIDENCIAL" not in cuerpo(md), "La cabecera recurrente debe suprimirse"
        assert "Empresa S.A." not in cuerpo(md), "El pie recurrente debe suprimirse"
        assert "lineas_suprimidas:" not in md, "Por defecto no debe incluir lineas_suprimidas en el Markdown para evitar ruido"
        assert not any(l.strip() in ("1", "2", "3", "4") for l in cuerpo(md).splitlines()), \
            "La numeración de página debe suprimirse"
        assert md.count("## Seccion") == 4, "Deben conservarse los cuatro títulos"
        assert md.count("- Primer punto de la lista") == 4, "Las viñetas deben normalizarse a Markdown"

        # Con auditoría explícita (include_suppressed_lines=True), queda anotada en el frontmatter
        md_audit = pdf_to_markdown(pdf_path, os.path.join(temp_dir, "informe_audit.md"),
                                   options=Options(include_suppressed_lines=True))
        assert "lineas_suprimidas:" in md_audit, "Debe constar que lineas se han suprimido cuando se solicita"
        assert "CONFIDENCIAL" in md_audit, "La cabecera suprimida debe quedar registrada en modo auditoría"

        print("  -> PASSED: limpieza de cabeceras, pies y paginación")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_filtrado_imagenes_ruido_y_cabeceras():
    """Descarta líneas decorativas, artefactos e imágenes de cabecera repetitivas."""
    temp_dir = tempfile.mkdtemp(prefix="pdfmd_noise_")
    try:
        # 1. Imagen de cabecera (logotipo de 60x60)
        img_header = os.path.join(temp_dir, "header_logo.png")
        Image.new("RGB", (60, 60), color=(20, 40, 80)).save(img_header)

        # 2. Barra divisoria horizontal (400x3 px)
        img_bar = os.path.join(temp_dir, "barra.png")
        Image.new("RGB", (400, 3), color=(100, 100, 100)).save(img_bar)

        # 3. Imagen real de contenido en el cuerpo (120x120 px)
        img_content = os.path.join(temp_dir, "content.png")
        Image.new("RGB", (120, 120), color=(200, 50, 50)).save(img_content)

        pdf_path = os.path.join(temp_dir, "documento_con_ruido.pdf")
        doc = fitz.open()
        for i in range(1, 4):
            page = doc.new_page(width=595, height=842)
            # Cabecera repetitiva: logo y texto
            page.insert_image(fitz.Rect(50, 20, 90, 60), filename=img_header)
            page.insert_text((100, 45), "EMPRESA EJEMPLO - CABECERA CORPORATIVA", fontsize=9)
            # Cuerpo
            page.insert_text((50, 120), f"Capitulo {i}: Analisis del informe", fontsize=14)
            # Línea decorativa
            page.insert_image(fitz.Rect(50, 140, 450, 143), filename=img_bar)
            # Imagen de contenido solo en la página 2
            if i == 2:
                page.insert_image(fitz.Rect(50, 180, 170, 300), filename=img_content)
        doc.save(pdf_path)
        doc.close()

        out_md = os.path.join(temp_dir, "documento_con_ruido.md")
        md = pdf_to_markdown(pdf_path, out_md, options=Options(export_images=True, remove_headers_footers=True))

        # 1. No debe haber lineas_suprimidas en el frontmatter
        assert "lineas_suprimidas:" not in md, "El frontmatter no debe contener lineas_suprimidas"

        # 2. La barra horizontal con aspect ratio extremo no debe haberse insertado
        # 3. La imagen de cabecera repetitiva en el margen no debe haberse insertado en el cuerpo
        # 4. Solo la imagen de contenido real de la página 2 debe estar presente
        assert md.count("![Imagen") == 1, f"Debe extraerse solo 1 imagen (la de contenido), se encontraron {md.count('![Imagen')}"
        assert "(pág. 2)" in md, "La imagen conservada debe ser la del cuerpo de la página 2"

        print("  -> PASSED: filtrado de imágenes inservibles y logotipos de cabecera")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_all():
    print("--- PRUEBAS UNITARIAS DE KIWI / pdfmd ---")
    test_ecuaciones()
    test_numeros_de_pagina()
    test_defragmentacion_respeta_tablas()
    test_tabla_espaciada_rechaza_prosa()
    test_normalizacion_de_vinetas()
    test_yaml_escapado()
    test_ruta_visible()
    test_nombres_de_salida_unicos()
    test_deduplicacion_de_imagenes()
    test_limpieza_de_cabeceras_y_pies()
    test_filtrado_imagenes_ruido_y_cabeceras()
    print("[OK] TODAS LAS PRUEBAS UNITARIAS PASARON CON EXITO")


if __name__ == "__main__":
    run_all()

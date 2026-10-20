# -*- coding: utf-8 -*-
"""
Batería de pruebas unitarias para las 6 nuevas mejoras de KIWI:
1. Concurrencia multihilo en lotes (max_workers en pdfmd y htmlmd).
2. Detección y reordenación de maquetación multi-columna (papers científicos).
3. Soporte para Obsidian: Callouts (> [!NOTE]) y Wikilinks ([[doc|texto]]).
4. Visor integrado de Markdown con formateo de estilos (format_markdown_into_text).
5. Cola interactiva de tareas Treeview y lógica de Drag & Drop.
6. Paleta y conmutación de Modo Oscuro + Tarjetas KPI de métricas.
"""

import os
import shutil
import tempfile
import unittest

import fitz  # PyMuPDF

from pdfmd.models import Options as PdfmdOptions, PageText, Block, Line, Span
from pdfmd.transform import reordenar_bloques_multicolumna, transform_pages
from pdfmd.render import to_obsidian_callout, render_link, render_document
from pdfmd.batch import convertir_lote as convertir_lote_pdf
from htmlmd.models import WebOptions
from htmlmd.batch import convertir_lote as convertir_lote_html
from kiwi_app import format_markdown_into_text


def _crear_pdf_prueba(ruta, lineas):
    doc = fitz.open()
    pagina = doc.new_page(width=595, height=842)
    y = 100
    for l in lineas:
        pagina.insert_text((50, y), l, fontsize=12)
        y += 30
    doc.save(ruta)
    doc.close()


class TestMejorasV2(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kiwi_test_v2_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # 1. Concurrencia Multihilo
    # ------------------------------------------------------------------
    def test_01_concurrencia_pdfmd_batch(self):
        """Verifica que convertir_lote en pdfmd procesa varios PDFs en paralelo con max_workers=2."""
        pdf_dir = os.path.join(self.tmpdir, "pdfs")
        out_dir = os.path.join(self.tmpdir, "out_pdf_batch")
        os.makedirs(pdf_dir, exist_ok=True)

        rutas_pdf = []
        for i in range(1, 4):
            p = os.path.join(pdf_dir, f"doc_{i}.pdf")
            _crear_pdf_prueba(p, [f"# Documento {i}", f"Contenido del doc {i} para prueba concurrente."])
            rutas_pdf.append(p)

        opts = PdfmdOptions(include_frontmatter=True, compute_hash=True)
        resumen = convertir_lote_pdf(rutas_pdf, out_dir, options=opts, max_workers=2, manifiesto="manifiesto.tsv")

        self.assertEqual(len(resumen), 3)

        md_files = [f for f in os.listdir(out_dir) if f.endswith(".md")]
        self.assertEqual(len(md_files), 3)

        man_path = os.path.join(out_dir, "manifiesto.tsv")
        self.assertTrue(os.path.exists(man_path))

    def test_02_concurrencia_htmlmd_batch(self):
        """Verifica que convertir_lote en htmlmd procesa capturas en paralelo con max_workers=2."""
        html_dir = os.path.join(self.tmpdir, "htmls")
        out_dir = os.path.join(self.tmpdir, "out_web_batch")
        os.makedirs(html_dir, exist_ok=True)

        rutas_html = []
        for i in range(1, 4):
            p = os.path.join(html_dir, f"pagina_{i}.html")
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"<!DOCTYPE html><html><head><title>Web {i}</title></head>"
                        f"<body><main><h1>Artículo {i}</h1><p>Texto del artículo {i} para lote.</p></main></body></html>")
            rutas_html.append(p)

        opts = WebOptions(incluir_frontmatter=True)
        resumen = convertir_lote_html(rutas_html, out_dir, options=opts, max_workers=2, pausa=0.0,
                                      organizar_por_secciones=False, generar_index=True)

        self.assertEqual(resumen["convertidas"], 3)
        self.assertEqual(resumen["fallidas"], 0)

        md_files = [f for f in os.listdir(out_dir) if f.endswith(".md") and f != "INDEX.md"]
        self.assertEqual(len(md_files), 3)

        index_path = os.path.join(out_dir, "INDEX.md")
        self.assertTrue(os.path.exists(index_path))

    # ------------------------------------------------------------------
    # 2. Detección y Reordenación Multi-Columna
    # ------------------------------------------------------------------
    def test_03_deteccion_multicolumna_dos_columnas(self):
        """Verifica que bloques distribuidos en dos columnas se lean Columna 1 luego Columna 2."""
        def make_block(text, x0, y0, x1, y1):
            span = Span(text=text, font="Times", size=10.0, bbox=(x0, y0, x1, y1))
            line = Line(spans=[span], bbox=(x0, y0, x1, y1))
            return Block(lines=[line], bbox=(x0, y0, x1, y1))

        # En orden ingenuo (barrido horizontal): Col1-P1 (y=100), Col2-P1 (y=100), Col1-P2 (y=200), Col2-P2 (y=200)
        b_col1_p1 = make_block("Columna 1 Parrafo 1", 50, 100, 250, 150)
        b_col2_p1 = make_block("Columna 2 Parrafo 1", 350, 100, 550, 150)
        b_col1_p2 = make_block("Columna 1 Parrafo 2", 50, 200, 250, 250)
        b_col2_p2 = make_block("Columna 2 Parrafo 2", 350, 200, 550, 250)

        page = PageText(
            page_num=1,
            width=600,
            height=800,
            blocks=[b_col1_p1, b_col2_p1, b_col1_p2, b_col2_p2]
        )

        reordenados = reordenar_bloques_multicolumna(page)
        textos = [b.lines[0].spans[0].text for b in reordenados]

        # Debe reordenar: Columna 1 completa (P1, P2) y luego Columna 2 completa (P1, P2)
        esperado = [
            "Columna 1 Parrafo 1",
            "Columna 1 Parrafo 2",
            "Columna 2 Parrafo 1",
            "Columna 2 Parrafo 2"
        ]
        self.assertEqual(textos, esperado)

    def test_04_multicolumna_con_titulo_completo(self):
        """Verifica que un título a ancho completo se mantenga arriba y luego se ordenen las columnas."""
        def make_block(text, x0, y0, x1, y1):
            span = Span(text=text, font="Times", size=10.0, bbox=(x0, y0, x1, y1))
            line = Line(spans=[span], bbox=(x0, y0, x1, y1))
            return Block(lines=[line], bbox=(x0, y0, x1, y1))

        b_titulo = make_block("Título del Paper Científico", 50, 30, 550, 70)
        b_col1_p1 = make_block("C1 Intro", 50, 100, 250, 150)
        b_col2_p1 = make_block("C2 Métodos", 350, 100, 550, 150)
        b_col1_p2 = make_block("C1 Datos", 50, 200, 250, 250)
        b_col2_p2 = make_block("C2 Conclusión", 350, 200, 550, 250)

        page = PageText(
            page_num=1,
            width=600,
            height=800,
            blocks=[b_titulo, b_col1_p1, b_col2_p1, b_col1_p2, b_col2_p2]
        )

        reordenados = reordenar_bloques_multicolumna(page)
        textos = [b.lines[0].spans[0].text for b in reordenados]

        self.assertEqual(textos[0], "Título del Paper Científico")
        self.assertEqual(textos[1], "C1 Intro")
        self.assertEqual(textos[2], "C1 Datos")
        self.assertEqual(textos[3], "C2 Métodos")
        self.assertEqual(textos[4], "C2 Conclusión")

    # ------------------------------------------------------------------
    # 3. Obsidian Callouts y Wikilinks
    # ------------------------------------------------------------------
    def test_05_obsidian_callout_mapping(self):
        """Verifica la conversión de notas y avisos a callouts de Obsidian."""
        c_nota = "\n".join(to_obsidian_callout(["Nota: Este es un mensaje importante."]))
        self.assertTrue(c_nota.startswith("> [!NOTE]"))
        self.assertIn("Este es un mensaje importante.", c_nota)

        c_warn = "\n".join(to_obsidian_callout(["ADVERTENCIA: Procedimiento de alto riesgo."]))
        self.assertTrue(c_warn.startswith("> [!WARNING]"))
        self.assertIn("Procedimiento de alto riesgo.", c_warn)

        c_tip = "\n".join(to_obsidian_callout(["Tip: Puedes pulsar Ctrl+Enter para iniciar."]))
        self.assertTrue(c_tip.startswith("> [!TIP]"))
        self.assertIn("Puedes pulsar Ctrl+Enter", c_tip)

        c_danger = "\n".join(to_obsidian_callout(["Peligro: Se borrarán los datos."]))
        self.assertTrue(c_danger.startswith("> [!DANGER]") or c_danger.startswith("> [!ERROR]"))

    def test_06_obsidian_wikilinks(self):
        """Verifica que enlaces relativos internos se conviertan a [[destino|texto]] con obsidian_wikilinks."""
        # Enlace relativo interno a otro documento
        link_rel = render_link("Manual de Usuario", "manual_usuario.md", obsidian_wikilinks=True)
        self.assertEqual(link_rel, "[[manual_usuario|Manual de Usuario]]")

        # Enlace sin texto o mismo nombre
        link_rel_mismo = render_link("capitulo_1", "capitulo_1.pdf", obsidian_wikilinks=True)
        self.assertEqual(link_rel_mismo, "[[capitulo_1]]")

        # Enlace externo HTTP/HTTPS debe seguir siendo enlace Markdown normal
        link_ext = render_link("Sitio Web", "https://ejemplo.com/doc", obsidian_wikilinks=True)
        self.assertEqual(link_ext, "[Sitio Web](https://ejemplo.com/doc)")

        # Enlace mailto debe seguir siendo enlace Markdown normal
        link_mail = render_link("Contacto", "mailto:contacto@kiwi.org", obsidian_wikilinks=True)
        self.assertEqual(link_mail, "[Contacto](mailto:contacto@kiwi.org)")

    # ------------------------------------------------------------------
    # 4. Formateo de Texto Markdown en GUI
    # ------------------------------------------------------------------
    def test_07_markdown_formatting_logic(self):
        """Verifica que el parser de formato reconozca encabezados, tablas, enlaces y callouts."""
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            txt = tk.Text(root)

            md_sample = (
                "# Título Principal\n"
                "## Sección Secundaria\n"
                "> [!NOTE]\n"
                "> Esto es una nota en callout.\n"
                "| Cabecera 1 | Cabecera 2 |\n"
                "| --- | --- |\n"
                "| Dato A | Dato B |\n"
                "```python\n"
                "print('hola mundo')\n"
                "```\n"
                "[[otro_documento|Enlace a otro]]\n"
            )

            # Prueba en modo claro
            format_markdown_into_text(txt, md_sample, dark_mode=False)
            contenido = txt.get("1.0", tk.END)
            self.assertIn("Título Principal", contenido)
            self.assertIn("Sección Secundaria", contenido)
            self.assertIn("callout", contenido)

            # Prueba en modo oscuro
            format_markdown_into_text(txt, md_sample, dark_mode=True)
            contenido_dark = txt.get("1.0", tk.END)
            self.assertIn("Título Principal", contenido_dark)

            root.destroy()
        except tk.TclError:
            # Entornos sin servidor X o display activo
            pass

    # ------------------------------------------------------------------
    # 5. Modo Oscuro, Paleta y KPI Cards
    # ------------------------------------------------------------------
    def test_08_palette_and_kpi_logic(self):
        """Verifica la definición de colores de Modo Oscuro y Modo Claro."""
        from kiwi_app import KiwiAppGUI
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            app = KiwiAppGUI(root)

            # Estado inicial: Modo Claro
            app.dark_mode = False
            pal_light = app._palette()
            self.assertEqual(pal_light["bg"], "#f5f9f5")
            self.assertEqual(pal_light["card"], "#ffffff")

            # Toggle a Modo Oscuro
            app.dark_mode = False
            app._toggle_theme()
            self.assertTrue(app.dark_mode)
            pal_dark = app._palette()
            self.assertEqual(pal_dark["bg"], "#191d21")
            self.assertEqual(pal_dark["card"], "#22272b")

            # Métricas KPI
            app._update_kpis(docs_delta=5, pages_delta=42, elem_delta=10, time_delta=3.5)
            self.assertEqual(app.kpi_docs, 5)
            self.assertEqual(app.kpi_pages, 42)
            self.assertEqual(app.kpi_elem, 10)
            self.assertAlmostEqual(app.kpi_time, 3.5, places=1)

            # Arrastrar y soltar: archivo PDF
            app._handle_dropped_path("documento_prueba.pdf")
            self.assertEqual(app.conv_mode_var.get(), "file")
            self.assertIn("documento_prueba.pdf", app.conv_txt_in.get())

            # Conmutación de vista rápida de tareas / log
            self.assertTrue(hasattr(app, "btn_view_tree"))
            self.assertTrue(hasattr(app, "btn_view_log"))
            app._switch_prog_view(1)
            self.assertEqual(app.prog_sub_nb.index(app.prog_sub_nb.select()), 1)
            app._switch_prog_view(0)
            self.assertEqual(app.prog_sub_nb.index(app.prog_sub_nb.select()), 0)

            root.destroy()
        except tk.TclError:
            pass

    def test_09_merge_adjacent_markdown_links(self):
        """Verifica la fusión de enlaces idénticos adyacentes generados por saltos de línea."""
        from pdfmd.render import merge_adjacent_markdown_links
        texto = "[A large part](https://example.com) [of society](https://example.com)"
        resultado = merge_adjacent_markdown_links(texto)
        self.assertEqual(resultado, "[A large part of society](https://example.com)")

        # Tres enlaces seguidos
        texto_triple = "[The](https://test.org) [Disinformation](https://test.org) [Landscape](https://test.org)"
        res_triple = merge_adjacent_markdown_links(texto_triple)
        self.assertEqual(res_triple, "[The Disinformation Landscape](https://test.org)")

        # Enlaces distintos no deben fusionarse
        distintos = "[Uno](https://uno.com) [Dos](https://dos.com)"
        self.assertEqual(merge_adjacent_markdown_links(distintos), distintos)

    def test_10_merge_heading_blocks(self):
        """Verifica la fusión de bloques de encabezado consecutivos del mismo nivel."""
        from pdfmd.transform import merge_heading_blocks
        b1 = Block(lines=[Line(spans=[Span(text="DISINFORMATION")])], block_type="heading", heading_level=1)
        b2 = Block(lines=[Line(spans=[Span(text="LANDSCAPE IN")])], block_type="heading", heading_level=1)
        b3 = Block(lines=[Line(spans=[Span(text="AUSTRIA")])], block_type="heading", heading_level=1)
        merged = merge_heading_blocks([b1, b2, b3])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].block_type, "heading")
        self.assertEqual(merged[0].heading_level, 1)
        self.assertEqual(merged[0].text(), "DISINFORMATION LANDSCAPE IN AUSTRIA")

    def test_11_htmlmd_pdf_auto_detection(self):
        """Verifica que html_to_markdown detecte y procese ficheros PDF de forma transparente."""
        from htmlmd.pipeline import html_to_markdown
        pdf_path = os.path.join(self.tmpdir, "informe.pdf")
        _crear_pdf_prueba(pdf_path, ["# Título del Informe", "Texto del reporte descargado en formato PDF."])
        out_md = os.path.join(self.tmpdir, "informe_salida.md")

        md_res, meta = html_to_markdown(pdf_path, out_md)
        self.assertTrue(os.path.exists(out_md))
        self.assertIn("Título del Informe", md_res)
        self.assertEqual(meta.criterio_cuerpo, "pdfmd")
        self.assertEqual(meta.contenido, "completo")


def run_all():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestMejorasV2)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        raise RuntimeError("Fallaron pruebas en test_mejoras_v2.py")


if __name__ == "__main__":
    run_all()

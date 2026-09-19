# -*- coding: utf-8 -*-
"""
Pruebas de integración del conversor web dentro de la aplicación KIWI.

Las de `test_htmlmd.py` comprueban el motor; estas comprueban que el motor está
realmente enchufado a las tres puertas de entrada de KIWI —la interfaz gráfica,
la CLI de `kiwi_app.py` y el ejecutable—, que es lo que separa «existe el
módulo» de «la aplicación hace las dos cosas».

Sin red: todo se ejecuta contra un .html escrito en un directorio temporal.
"""

import os
import shutil
import sys
import tempfile

import kiwi_app
from htmlmd.models import WebOptions


def afirmar(condicion, mensaje):
    if not condicion:
        raise AssertionError(mensaje)


PAGINA = (
    '<!doctype html><html lang="es"><head><title>Pagina de prueba</title></head><body>'
    '<nav class="site-nav"><a href="/inicio">Menu</a></nav>'
    '<article><h1>Titulo</h1><p>Parrafo con <a href="/doc.pdf">un enlace</a>. '
    + "Texto suficiente para que la conversion no se marque como escasa. " * 12
    + '</p></article><footer class="site-footer">Pie del sitio</footer></body></html>'
)


def _con_pagina(func):
    """Ejecuta `func(tmp, ruta_html)` sobre un directorio temporal desechable."""
    tmp = tempfile.mkdtemp(prefix="kiwi_int_")
    try:
        ruta = os.path.join(tmp, "pagina.html")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(PAGINA)
        return func(tmp, ruta)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# El motor está importado en la aplicación (y por tanto entra en el .exe)
# ---------------------------------------------------------------------------

def test_htmlmd_importado_en_la_aplicacion():
    """
    PyInstaller resuelve dependencias de forma estática: si `kiwi_app` no
    importa htmlmd, el paquete no entra en el ejecutable por mucho que exista
    en el repositorio, y KIWI.exe se queda sin conversor web.
    """
    for nombre in ("WebOptions", "html_to_markdown", "nombre_de_salida",
                   "convertir_lote_web", "leer_lista", "es_url", "ErrorDeDescarga"):
        afirmar(hasattr(kiwi_app, nombre), f"kiwi_app no importa {nombre}")
    afirmar("htmlmd" in sys.modules, "htmlmd no quedo cargado al importar kiwi_app")
    print("  -> PASSED: htmlmd importado por la aplicacion (entra en el .exe)")


# ---------------------------------------------------------------------------
# Runner: mismos eventos que el motor de PDF
# ---------------------------------------------------------------------------

def test_runner_emite_los_eventos_de_la_interfaz():
    """El runner web tiene que hablar el mismo idioma que el de PDF."""
    def cuerpo(tmp, ruta):
        eventos = []
        salida = os.path.join(tmp, "salida.md")
        runner = kiwi_app.KiwiWebRunner(
            [ruta], tmp, WebOptions(),
            progress_cb=lambda e, d: eventos.append((e, d)),
            salida_fija=salida)
        runner.run()

        tipos = [e for e, _ in eventos]
        afirmar("web_finished" in tipos, f"no se emitio web_finished: {tipos}")
        stats = [d for e, d in eventos if e == "web_finished"][0]
        afirmar(stats["converted"] == 1, f"no se convirtio: {stats}")
        afirmar(stats["escasas"] == 0, f"falso positivo de escasez: {stats}")
        afirmar(os.path.exists(salida), "no se escribio el .md")
        # La interfaz necesita estas claves para habilitar sus botones
        for clave in ("output_dir", "last_md", "total", "errors", "skipped"):
            afirmar(clave in stats, f"falta {clave!r} en las estadisticas")
    _con_pagina(cuerpo)
    print("  -> PASSED: el runner web emite los eventos de la interfaz")


def test_runner_declara_la_procedencia_de_un_html_local():
    def cuerpo(tmp, ruta):
        salida = os.path.join(tmp, "salida.md")
        kiwi_app.KiwiWebRunner([ruta], tmp, WebOptions(), salida_fija=salida,
                               url_origen="https://ejemplo.com/real").run()
        with open(salida, encoding="utf-8") as f:
            md = f.read()
        afirmar('url_origen: "https://ejemplo.com/real"' in md,
                f"no se conservo la procedencia declarada:\n{md[:400]}")
    _con_pagina(cuerpo)
    print("  -> PASSED: procedencia declarada de un .html local")


def test_runner_en_lote_no_se_detiene_ante_un_fallo():
    def cuerpo(tmp, ruta):
        eventos = []
        salida_dir = os.path.join(tmp, "md")
        kiwi_app.KiwiWebRunner(
            [os.path.join(tmp, "no_existe.html"), ruta], salida_dir, WebOptions(),
            progress_cb=lambda e, d: eventos.append((e, d)), pausa=0).run()

        stats = [d for e, d in eventos if e == "web_finished"]
        afirmar(stats, f"el lote no termino: {[e for e, _ in eventos]}")
        afirmar(stats[0]["errors"] == 1, f"no se registro el fallo: {stats[0]}")
        afirmar(stats[0]["converted"] == 1, f"el fallo detuvo el lote: {stats[0]}")
        afirmar(any(e == "progress_copy" for e, _ in eventos),
                "no se emitio progreso: la barra no avanzaria")
    _con_pagina(cuerpo)
    print("  -> PASSED: el lote web no se detiene ante un fallo")


# ---------------------------------------------------------------------------
# CLI de kiwi_app.py
# ---------------------------------------------------------------------------

def test_cli_web_documento_suelto():
    def cuerpo(tmp, ruta):
        salida = os.path.join(tmp, "cli.md")
        codigo = kiwi_app.main(["web", ruta, "-o", salida, "--url",
                                "https://ejemplo.com/real"])
        afirmar(codigo == 0, f"la CLI devolvio {codigo}")
        afirmar(os.path.exists(salida), "la CLI no escribio el .md")
        with open(salida, encoding="utf-8") as f:
            md = f.read()
        afirmar('generator: "KIWI (htmlmd)"' in md, "el .md no lo genero htmlmd")
        afirmar("[un enlace](https://ejemplo.com/doc.pdf)" in md,
                f"la CLI perdio el enlace:\n{md[:500]}")
    _con_pagina(cuerpo)
    print("  -> PASSED: CLI 'web' con un documento suelto")


def test_cli_web_en_lote():
    def cuerpo(tmp, ruta):
        lista = os.path.join(tmp, "urls.txt")
        with open(lista, "w", encoding="utf-8") as f:
            f.write(f"# enlaces pendientes\n{ruta}\n")
        destino = os.path.join(tmp, "salida")
        codigo = kiwi_app.main(["web", "--lista", lista, "-o", destino, "--pausa", "0"])
        afirmar(codigo == 0, f"la CLI en lote devolvio {codigo}")
        afirmar(os.path.exists(os.path.join(destino, "manifiesto.tsv")),
                "el lote no dejo manifiesto")
        afirmar(os.path.exists(os.path.join(destino, "INDEX.md")),
                "el lote no dejo INDEX.md")
        generados = [f for f in os.listdir(destino) if f.endswith(".md") and f.upper() != "INDEX.MD"]
        afirmar(len(generados) == 1, f"ficheros generados inesperados: {generados}")
    _con_pagina(cuerpo)
    print("  -> PASSED: CLI 'web' en modo lote")


def test_cli_conserva_los_subcomandos_anteriores():
    """Añadir 'web' no puede romper el atajo histórico ni los otros comandos."""
    parser = kiwi_app.build_parser()
    args = parser.parse_args(["web", "https://ejemplo.com/x"])
    afirmar(args.command == "web", f"no se reconocio el subcomando: {args.command}")

    # `kiwi_app.py ORIGEN DESTINO` seguia significando 'collect'
    normalizado = kiwi_app._normalizar_argv(["C:/origen", "C:/destino"])
    afirmar(normalizado[0] == "collect", f"se rompio el atajo historico: {normalizado}")
    # ...y 'web' no debe reinterpretarse como una carpeta de origen
    afirmar(kiwi_app._normalizar_argv(["web", "http://x"])[0] == "web",
            "el subcomando web se convirtio en collect")
    print("  -> PASSED: los subcomandos anteriores siguen funcionando")


# ---------------------------------------------------------------------------
# Interfaz gráfica
# ---------------------------------------------------------------------------

def test_gui_tiene_la_pestana_web_cableada():
    """
    Construye la ventana de verdad (oculta). Si no hay entorno gráfico la
    prueba se salta: es una limitación del entorno, no un fallo del código.
    """
    try:
        import tkinter as tk
    except ImportError:
        print("  -> OMITIDA: tkinter no disponible")
        return

    try:
        root = tk.Tk()
    except tk.TclError:
        print("  -> OMITIDA: sin entorno grafico")
        return

    try:
        root.withdraw()
        app = kiwi_app.KiwiAppGUI(root)

        pestanas = [app.notebook.tab(i, "text").strip()
                    for i in range(app.notebook.index("end"))]
        afirmar(len(pestanas) == 4, f"se esperaban 4 pestanas: {pestanas}")
        afirmar("htmlmd" in pestanas[3], f"la cuarta pestana no es la web: {pestanas[3]}")

        for attr in ("web_txt_in", "web_txt_out", "web_txt_url", "web_txt_pausa",
                     "btn_web_iniciar", "btn_web_detener", "web_mode_var"):
            afirmar(hasattr(app, attr), f"falta el control {attr}")

        # El bloqueo durante la ejecucion tiene que alcanzar tambien a esta pestana,
        # o se podrian lanzar dos conversiones a la vez sobre el mismo log.
        app._set_running_state(True)
        afirmar(str(app.btn_web_iniciar["state"]) == "disabled",
                "el boton de la pestana web no se bloquea durante la ejecucion")
        afirmar(str(app.btn_web_detener["state"]) == "normal",
                "el boton de detener no se habilita")
        app._set_running_state(False)
        afirmar(str(app.btn_web_iniciar["state"]) == "normal",
                "el boton no se libera al terminar")

        # Las opciones de la pestana se traducen bien al motor
        app.web_var_completa.set(True)
        app.web_var_images.set(True)
        opts = app._opciones_web()
        afirmar(opts.quitar_cromo is False and opts.solo_cuerpo is False,
                "«pagina completa» no desactiva la poda")
        afirmar(opts.exportar_imagenes is True, "no se traslado la descarga de imagenes")

        # Y se recuerdan entre sesiones, como el resto de la interfaz
        claves = set(app._campos_config()) | set(app._opciones_config())
        for clave in ("web_in", "web_out", "web_url", "web_pausa",
                      "web_images", "web_completa", "web_links"):
            afirmar(clave in claves, f"{clave} no se persiste en la configuracion")

        print("  -> PASSED: la GUI tiene la pestana web construida y cableada")
    finally:
        root.destroy()


# ---------------------------------------------------------------------------

PRUEBAS = [
    test_htmlmd_importado_en_la_aplicacion,
    test_runner_emite_los_eventos_de_la_interfaz,
    test_runner_declara_la_procedencia_de_un_html_local,
    test_runner_en_lote_no_se_detiene_ante_un_fallo,
    test_cli_web_documento_suelto,
    test_cli_web_en_lote,
    test_cli_conserva_los_subcomandos_anteriores,
    test_gui_tiene_la_pestana_web_cableada,
]


def run_all():
    print("--- PRUEBAS DE INTEGRACION DEL CONVERSOR WEB EN KIWI ---")
    for prueba in PRUEBAS:
        prueba()
    print(f"[OK] LAS {len(PRUEBAS)} PRUEBAS DE INTEGRACION PASARON CON EXITO")


if __name__ == "__main__":
    run_all()

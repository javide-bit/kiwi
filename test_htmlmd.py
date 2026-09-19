# -*- coding: utf-8 -*-
"""
Pruebas del conversor de web a Markdown (htmlmd).

Todas se ejecutan sin red: se construye una `Respuesta` a mano con el HTML de
prueba. Que la suite no dependa de internet no es comodidad, es correccion: una
prueba que descarga una pagina real falla el dia que esa pagina cambia, y
entonces deja de decir nada sobre el codigo.
"""

import os
import shutil
import tempfile

from htmlmd.batch import convertir_lote, escribir_manifiesto, leer_lista
from htmlmd.fetch import Respuesta, decodificar, dominio, resolver_url
from htmlmd.models import WebOptions
from htmlmd.pipeline import convertir_respuesta, nombre_de_salida


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def respuesta(html: str, url: str = "https://ejemplo.com/articulo") -> Respuesta:
    crudo = html.encode("utf-8")
    return Respuesta(
        html=html, crudo=crudo, url_solicitada=url, url_final=url,
        estado_http=200, content_type="text/html; charset=utf-8",
        encoding="utf-8", sha256="0" * 64, fecha_captura="2026-08-31 12:00:00",
    )


def convertir(html: str, opts: WebOptions = None, url: str = "https://ejemplo.com/articulo"):
    return convertir_respuesta(respuesta(html, url), "salida.md", opts or WebOptions())


def pagina(cuerpo: str, cabeza: str = "", lang: str = "es") -> str:
    return (f'<!doctype html><html lang="{lang}"><head><title>Documento de prueba</title>'
            f"{cabeza}</head><body>{cuerpo}</body></html>")


# Relleno para superar el umbral de contenido escaso sin ensuciar cada prueba
RELLENO = ("<p>" + ("Texto de relleno suficiente para que la pagina no se marque "
                    "como escasa en el diagnostico de conversion. ") * 8 + "</p>")


def afirmar(condicion, mensaje):
    if not condicion:
        raise AssertionError(mensaje)


# ---------------------------------------------------------------------------
# Estructura
# ---------------------------------------------------------------------------

def test_estructura_declarada_se_respeta():
    """Los encabezados salen del propio HTML, no de una heuristica de tamano."""
    md, _ = convertir(pagina("<article><h1>Titulo</h1><h2>Seccion</h2>"
                             "<h3>Subseccion</h3>" + RELLENO + "</article>"))
    afirmar("\n# Titulo" in md, "falta el H1")
    afirmar("\n## Seccion" in md, "falta el H2")
    afirmar("\n### Subseccion" in md, "falta el H3")
    print("  -> PASSED: jerarquia de encabezados")


def test_listas_anidadas_conservan_la_sangria():
    """
    Una sublista con un solo espacio de sangria deja de ser sublista en Markdown.
    Esta es la prueba que obligo a conservar la sangria real en render_span.
    """
    md, _ = convertir(pagina("<article>" + RELLENO +
                             "<ul><li>Uno</li><li>Dos<ul><li>Anidado</li></ul></li></ul>"
                             "<ol><li>Primero</li><li>Segundo</li></ol></article>"))
    afirmar("- Uno" in md, "falta el item de primer nivel")
    afirmar("\n  - Anidado" in md,
            f"la sublista no conserva dos espacios de sangria:\n{md}")
    afirmar("1. Primero" in md and "2. Segundo" in md, "la lista ordenada no se numera")
    print("  -> PASSED: listas anidadas y numeradas")


def test_tabla_real_si_tabla_de_maquetacion_no():
    """Una tabla de una sola columna es maquetacion: emitirla seria ruido."""
    md, _ = convertir(pagina("<article>" + RELLENO +
                             "<table><tr><th>A</th><th>B</th></tr>"
                             "<tr><td>1</td><td>2</td></tr></table></article>"))
    afirmar("| A" in md and "| B" in md and "| 1" in md, "no se emitio la tabla")
    afirmar("| ---" in md, "falta la fila separadora de la tabla")

    md2, _ = convertir(pagina("<article>" + RELLENO +
                              "<table><tr><td><p>Solo texto maquetado</p></td></tr>"
                              "</table></article>"))
    afirmar("Solo texto maquetado" in md2, "se perdio el texto de la tabla de maquetacion")
    afirmar("| Solo texto" not in md2, "una tabla de maquetacion se emitio como tabla")
    print("  -> PASSED: tablas reales frente a maquetacion")


def test_codigo_y_lenguaje():
    md, _ = convertir(pagina('<article>' + RELLENO +
                             '<pre class="language-python">def f():\n    return 1</pre>'
                             '</article>'))
    afirmar("```python" in md, f"no se detecto el lenguaje del bloque:\n{md}")
    afirmar("    return 1" in md, "el bloque de codigo perdio la sangria")
    print("  -> PASSED: bloque de codigo con lenguaje")


def test_estilos_en_linea():
    md, _ = convertir(pagina("<article>" + RELLENO +
                             "<p>Esto es <strong>negrita</strong>, <em>cursiva</em> "
                             "y <code>codigo</code>.</p></article>"))
    afirmar("**negrita**" in md, "falta la negrita")
    afirmar("*cursiva*" in md, "falta la cursiva")
    afirmar("`codigo`" in md, "falta el monoespaciado")
    print("  -> PASSED: estilos en linea")


def test_html_roto_no_pierde_contenido():
    """El HTML real viene con etiquetas sin cerrar; ninguna debe tragarse el resto."""
    md, meta = convertir(pagina("<article>" + RELLENO +
                                "<p>Uno<p>Dos<ul><li>A<li>B</ul></article>"))
    for esperado in ("Uno", "Dos", "- A", "- B"):
        afirmar(esperado in md, f"se perdio {esperado!r} con HTML mal cerrado:\n{md}")
    print("  -> PASSED: HTML mal cerrado")


# ---------------------------------------------------------------------------
# Enlaces: forman parte del contenido del documento
# ---------------------------------------------------------------------------

def test_enlaces_relativos_se_hacen_absolutos():
    md, meta = convertir(pagina('<article>' + RELLENO +
                                '<p>Ver el <a href="/docs/informe.pdf">informe</a>.</p>'
                                '</article>'),
                         url="https://ejemplo.com/seccion/noticia")
    afirmar("[informe](https://ejemplo.com/docs/informe.pdf)" in md,
            f"el enlace relativo no se resolvio:\n{md}")
    afirmar(meta.enlaces == 1, f"contador de enlaces incorrecto: {meta.enlaces}")
    print("  -> PASSED: enlaces relativos resueltos y contados")


def test_anclas_internas_no_son_enlaces():
    """Un salto a #seccion no es procedencia y ensuciaria el recuento de enlaces."""
    md, meta = convertir(pagina('<article>' + RELLENO +
                                '<p>Ir a <a href="#final">el final</a>.</p></article>'))
    afirmar("el final" in md, "se perdio el texto del ancla interna")
    afirmar("](#final)" not in md, "se emitio un ancla interna como enlace")
    afirmar(meta.enlaces == 0, f"el ancla interna se conto como enlace: {meta.enlaces}")
    print("  -> PASSED: anclas internas descartadas")


def test_enlaces_dentro_de_celdas_de_tabla():
    """
    La celda de una tabla suele ser justo donde vive el enlace al documento
    fuente. Extraerla en texto plano conservaria el ancla y perderia su destino.
    """
    md, meta = convertir(pagina("<article>" + RELLENO +
                                "<table><tr><th>Documento</th><th>Fecha</th></tr>"
                                '<tr><td><a href="/docs/a.pdf">Informe A</a></td>'
                                "<td>2026-01-02</td></tr></table></article>"))
    afirmar("[Informe A](https://ejemplo.com/docs/a.pdf)" in md,
            f"la celda perdio el destino del enlace:\n{md}")
    afirmar(meta.enlaces == 1,
            f"el enlace de la celda no se conto: {meta.enlaces}")
    print("  -> PASSED: enlaces conservados dentro de tablas")


def test_definiciones_conservan_enlaces_y_parrafos():
    """
    La documentacion tecnica mete parrafos y enlaces enteros dentro del <dd>.
    Aplanar la definicion a texto plano se los llevaria por delante.
    """
    md, meta = convertir(pagina("<article>" + RELLENO +
                                "<dl><dt>Termino</dt>"
                                '<dd><p>Definicion con <a href="/ref.html">referencia</a>.</p>'
                                "</dd></dl></article>"))
    afirmar("**Termino**" in md, f"el termino no salio en negrita:\n{md}")
    afirmar("[referencia](https://ejemplo.com/ref.html)" in md,
            f"la definicion perdio el enlace:\n{md}")
    afirmar(meta.enlaces == 1, f"el enlace del <dd> no se conto: {meta.enlaces}")
    print("  -> PASSED: definiciones con enlaces y parrafos")


def test_enlaces_desactivables():
    opts = WebOptions(conservar_enlaces=False)
    md, meta = convertir(pagina('<article>' + RELLENO +
                                '<p><a href="https://otro.com/x">texto</a></p></article>'), opts)
    afirmar("texto" in md, "se perdio el texto del enlace")
    afirmar("https://otro.com/x" not in md, "se emitio el destino con --no-links")
    print("  -> PASSED: extraccion de enlaces desactivable")


# ---------------------------------------------------------------------------
# Separacion del articulo respecto del cromo
# ---------------------------------------------------------------------------

def test_se_poda_el_cromo_y_se_deja_constancia():
    html = pagina(
        '<nav class="site-nav"><a href="/a">Inicio</a></nav>'
        '<div class="cookie-consent">Acepte las cookies</div>'
        '<aside class="related"><a href="/z">Le puede interesar</a></aside>'
        '<article><h1>Titulo</h1>' + RELLENO + '</article>'
        '<footer class="site-footer">Aviso legal</footer>'
    )
    md, meta = convertir(html)
    for ruido in ("Inicio", "Acepte las cookies", "Le puede interesar", "Aviso legal"):
        afirmar(ruido not in md, f"el cromo {ruido!r} se colo en el Markdown")
    afirmar("Texto de relleno" in md, "se perdio el cuerpo del articulo")
    afirmar(meta.elementos_suprimidos, "no se registro que se suprimio")
    afirmar(meta.criterio_cuerpo == "article",
            f"criterio de cuerpo inesperado: {meta.criterio_cuerpo}")
    print("  -> PASSED: cromo podado y auditable")


def test_sin_article_se_elige_por_densidad():
    """Sin marcado semantico, gana el contenedor con mas prosa y menos enlaces."""
    html = pagina(
        '<div id="lista-de-enlaces">' +
        "".join(f'<a href="/n{i}">Noticia numero {i} del listado</a>' for i in range(40)) +
        '</div><div id="cuerpo">' + RELLENO + '</div>'
    )
    md, meta = convertir(html)
    afirmar("Texto de relleno" in md, f"no se eligio el cuerpo correcto:\n{md[:400]}")
    afirmar(meta.criterio_cuerpo == "densidad",
            f"criterio inesperado: {meta.criterio_cuerpo}")
    print("  -> PASSED: seleccion del cuerpo por densidad")


def test_pagina_completa_no_poda():
    opts = WebOptions(quitar_cromo=False, solo_cuerpo=False)
    md, meta = convertir(pagina('<nav class="site-nav">Menu principal</nav>'
                                '<article>' + RELLENO + '</article>'), opts)
    afirmar("Menu principal" in md, "--pagina-completa deberia conservar el menu")
    print("  -> PASSED: modo pagina completa")


# ---------------------------------------------------------------------------
# Procedencia y diagnostico
# ---------------------------------------------------------------------------

def test_frontmatter_de_procedencia():
    cabeza = ('<meta name="author" content="Nombre Apellido">'
              '<meta property="og:site_name" content="Diario">'
              '<meta name="description" content="Resumen del articulo">'
              '<link rel="canonical" href="https://ejemplo.com/canonica">')
    md, meta = convertir(pagina("<article><h1>Titulo</h1>" + RELLENO + "</article>", cabeza))
    for esperado in ('title: "Documento de prueba"',   # del <title>, no del <h1>
                     'author: "Nombre Apellido"', 'sitio: "Diario"',
                     'url_origen: "https://ejemplo.com/canonica"',
                     'sha256_html: "' + "0" * 64 + '"', 'estado_http: 200',
                     'generator: "KIWI (htmlmd)"', 'idioma: "es"'):
        afirmar(esperado in md, f"falta {esperado!r} en el frontmatter:\n{md[:800]}")
    afirmar('url_solicitada: "https://ejemplo.com/articulo"' in md,
            "no consta que la URL final difiere de la pedida")
    print("  -> PASSED: frontmatter de procedencia")


def test_json_ld_manda_en_autor_y_fecha():
    """En prensa digital el <meta author> suele ser el medio; el JSON-LD, la firma."""
    cabeza = ('<meta name="author" content="Redaccion generica">'
              '<script type="application/ld+json">'
              '{"@type":"NewsArticle","headline":"Titular firmado",'
              '"datePublished":"2026-08-30","author":{"name":"Firma Real"}}</script>')
    md, meta = convertir(pagina("<article>" + RELLENO + "</article>", cabeza))
    afirmar(meta.autor == "Firma Real", f"autor tomado del sitio equivocado: {meta.autor}")
    afirmar(meta.fecha_publicacion == "2026-08-30", "falta la fecha de publicacion")
    print("  -> PASSED: JSON-LD manda en autor y fecha")


def test_el_titulo_no_sale_del_headline_del_json_ld():
    """
    Hay sitios cuyo `headline` no es el titular sino la descripcion de la entidad:
    Wikipedia sirve "fruta comestible" en el articulo "Kiwi". Un titulo asi
    tambien estropea el nombre del fichero, no solo el frontmatter.
    """
    cabeza = ('<meta property="og:title" content="Kiwi - Wikipedia">'
              '<script type="application/ld+json">'
              '{"@type":"Article","headline":"fruta comestible","name":"Kiwi"}</script>')
    _, meta = convertir(pagina("<article>" + RELLENO + "</article>", cabeza))
    afirmar(meta.titulo != "fruta comestible",
            "la descripcion del JSON-LD se colo como titulo")
    # Sin og:site_name y con dominio ejemplo.com, el sufijo no se recorta: el
    # recorte solo procede cuando la cola es de verdad el nombre del medio.
    afirmar(meta.titulo == "Kiwi - Wikipedia", f"titulo inesperado: {meta.titulo!r}")
    print("  -> PASSED: el headline del JSON-LD no secuestra el titulo")


def test_sufijo_del_medio_se_recorta_solo_si_es_el_medio():
    """Recortar por el mero hecho de haber un guion destrozaria titulos legitimos."""
    cabeza_medio = '<meta property="og:site_name" content="Wikipedia">'
    _, con_sufijo = convertir(pagina("<article>" + RELLENO + "</article>",
                                     '<meta property="og:title" content="Kiwi - Wikipedia">'
                                     + cabeza_medio))
    afirmar(con_sufijo.titulo == "Kiwi", f"no se recorto el medio: {con_sufijo.titulo!r}")

    _, legitimo = convertir(pagina("<article>" + RELLENO + "</article>",
                                   '<meta property="og:title" '
                                   'content="Manual practico - segunda edicion">' + cabeza_medio))
    afirmar(legitimo.titulo == "Manual practico - segunda edicion",
            f"se mutilo un titulo legitimo: {legitimo.titulo!r}")
    print("  -> PASSED: recorte del sufijo del medio, solo cuando procede")


def test_contenido_escaso_avisa_en_el_cuerpo():
    """
    El equivalente web del PDF escaneado: una pagina que solo pinta con JavaScript
    produce un .md casi vacio, y eso no puede pasar en silencio.
    """
    md, meta = convertir(pagina('<div id="root"></div>'
                                '<noscript>Active JavaScript</noscript>'))
    afirmar(meta.contenido in ("escaso", "ausente"),
            f"no se diagnostico el contenido: {meta.contenido}")
    afirmar("AVISO DE CONVERSION" in md, f"no hay aviso visible en el cuerpo:\n{md}")
    afirmar('contenido: "' in md, "el frontmatter no clasifica el contenido")
    afirmar(meta.avisos, "no se registro ningun aviso")
    print("  -> PASSED: aviso de contenido escaso")


def test_pagina_normal_no_avisa():
    md, meta = convertir(pagina("<article><h1>Titulo</h1>" + RELLENO + RELLENO + "</article>"))
    afirmar(meta.contenido == "completo", f"falso positivo de escasez: {meta.contenido}")
    afirmar("AVISO DE CONVERSION" not in md, "aviso emitido sobre una pagina correcta")
    print("  -> PASSED: sin falsos avisos en pagina normal")


def test_titulo_se_antepone_si_falta_h1():
    md, meta = convertir(pagina("<article><h2>Solo un H2</h2>" + RELLENO + "</article>"))
    afirmar(md.count("# Documento de prueba") >= 1,
            f"no se antepuso el titulo como H1:\n{md[:600]}")
    print("  -> PASSED: H1 garantizado")


def test_titulo_no_se_duplica():
    md, _ = convertir(pagina("<article><h1>Documento de prueba</h1>" + RELLENO + "</article>"))
    afirmar(md.count("\n# Documento de prueba") == 1,
            f"el titulo se duplico:\n{md[:600]}")
    print("  -> PASSED: el titulo no se duplica")


def test_frontmatter_desactivable():
    md, _ = convertir(pagina("<article>" + RELLENO + "</article>"),
                      WebOptions(incluir_frontmatter=False))
    afirmar(not md.startswith("---"), "se emitio frontmatter con la opcion desactivada")
    print("  -> PASSED: frontmatter desactivable")


# ---------------------------------------------------------------------------
# Imagenes
# ---------------------------------------------------------------------------

def test_imagen_conserva_el_alt_y_se_hace_absoluta():
    md, meta = convertir(pagina('<article>' + RELLENO +
                                '<img src="/img/foto.jpg" alt="Kiwi cortado en rodajas">'
                                '</article>'))
    afirmar("![Kiwi cortado en rodajas](https://ejemplo.com/img/foto.jpg)" in md,
            f"la imagen perdio el alt o la URL absoluta:\n{md}")
    afirmar(meta.imagenes == 1, f"contador de imagenes incorrecto: {meta.imagenes}")
    print("  -> PASSED: imagenes con alt y URL absoluta")


def test_imagen_repetida_no_se_duplica():
    md, meta = convertir(pagina('<article>' + RELLENO +
                                '<img src="/logo.png" alt="Logo">'
                                '<p>Medio.</p><img src="/logo.png" alt="Logo">'
                                '</article>'))
    afirmar(meta.imagenes == 1, f"el logotipo repetido se emitio dos veces: {meta.imagenes}")
    print("  -> PASSED: imagenes repetidas deduplicadas")


def test_data_uri_se_ignora():
    md, meta = convertir(pagina('<article>' + RELLENO +
                                '<img src="data:image/gif;base64,R0lGOD" alt="pixel">'
                                '</article>'))
    afirmar(meta.imagenes == 0, "se emitio un data: URI como imagen")
    afirmar("data:image" not in md, "el data: URI se colo en el Markdown")
    print("  -> PASSED: data: URI descartado")


# ---------------------------------------------------------------------------
# Red y utilidades
# ---------------------------------------------------------------------------

def test_decodificacion_por_meta_charset():
    crudo = ('<html><head><meta charset="iso-8859-1"></head><body>'
             '<p>Situacion</p></body></html>').encode("iso-8859-1")
    texto, enc = decodificar(crudo, content_type="text/html")
    afirmar("Situacion" in texto, "no se decodifico el documento")
    afirmar(enc == "iso-8859-1", f"encoding detectado incorrecto: {enc}")
    print("  -> PASSED: charset leido del propio documento")


def test_decodificacion_nunca_falla():
    """Bytes invalidos en cualquier codificacion: mejor un acento roto que nada."""
    texto, enc = decodificar(b"<p>\xff\xfe roto \x81</p>")
    afirmar(isinstance(texto, str) and "roto" in texto, "la decodificacion perdio el texto")
    print("  -> PASSED: decodificacion tolerante")


def test_resolucion_de_urls():
    base = "https://ejemplo.com/seccion/pagina.html"
    afirmar(resolver_url(base, "/a.pdf") == "https://ejemplo.com/a.pdf", "ruta absoluta")
    afirmar(resolver_url(base, "b.pdf") == "https://ejemplo.com/seccion/b.pdf", "ruta relativa")
    afirmar(resolver_url(base, "https://otro.com/c") == "https://otro.com/c", "url completa")
    afirmar(dominio("https://www.ejemplo.com/x") == "ejemplo.com", "dominio con www")
    print("  -> PASSED: resolucion de URLs y dominio")


def test_nombre_de_salida_en_ascii():
    html = ('<!doctype html><html lang="es"><head>'
            "<title>Informe Anual: Situación 2026's</title></head>"
            f"<body><article>{RELLENO}</article></body></html>")
    _, meta = convertir(html)
    nombre = nombre_de_salida("https://ejemplo.com/x", meta)
    afirmar(nombre == "informe-anual-situacion-2026s.md",
            f"nombre de salida inesperado: {nombre}")
    print("  -> PASSED: nombre de salida en ASCII")


# ---------------------------------------------------------------------------
# Lote
# ---------------------------------------------------------------------------

def test_lista_admite_comentarios():
    tmp = tempfile.mkdtemp(prefix="kiwi_web_")
    try:
        ruta = os.path.join(tmp, "urls.txt")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write("# Enlaces pendientes\n\nhttps://a.com/1\nhttps://b.com/2  # nota\n\n")
        urls = leer_lista(ruta)
        afirmar(urls == ["https://a.com/1", "https://b.com/2"], f"lista mal leida: {urls}")
        print("  -> PASSED: lista de URLs con comentarios")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_manifiesto_ordenado_por_texto_ascendente():
    """Las primeras filas tienen que ser las capturas con riesgo de estar vacias."""
    tmp = tempfile.mkdtemp(prefix="kiwi_web_")
    try:
        ruta = os.path.join(tmp, "manifiesto.tsv")
        escribir_manifiesto([
            {"archivo": "larga.md", "url": "u1", "chars": "9000", "contenido": "completo"},
            {"archivo": "vacia.md", "url": "u2", "chars": "12", "contenido": "escaso"},
            {"archivo": "media.md", "url": "u3", "chars": "3000", "contenido": "completo"},
        ], ruta)
        with open(ruta, encoding="utf-8-sig") as f:
            filas = [l.split("\t")[0] for l in f.read().splitlines()[1:]]
        afirmar(filas == ["vacia.md", "media.md", "larga.md"],
                f"el manifiesto no esta ordenado por texto ascendente: {filas}")
        print("  -> PASSED: manifiesto ordenado por texto ascendente")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_lote_reanudable_por_url():
    """Un lote de 200 capturas no puede reempezar de cero por un fallo en la 150."""
    tmp = tempfile.mkdtemp(prefix="kiwi_web_")
    try:
        origen = os.path.join(tmp, "fuente.html")
        with open(origen, "w", encoding="utf-8") as f:
            f.write(pagina("<article><h1>Capturada</h1>" + RELLENO + "</article>"))
        salida = os.path.join(tmp, "md")

        primero = convertir_lote([origen], salida, pausa=0)
        afirmar(primero["convertidas"] == 1, f"no se convirtio: {primero}")

        segundo = convertir_lote([origen], salida, pausa=0)
        afirmar(segundo["omitidas"] == 1 and segundo["convertidas"] == 0,
                f"la segunda pasada no reanudo: {segundo}")

        forzado = convertir_lote([origen], salida, pausa=0, refrescar=True)
        afirmar(forzado["convertidas"] == 1, f"--refrescar no reconvirtio: {forzado}")
        print("  -> PASSED: lote reanudable por URL ya convertida")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_lote_no_se_detiene_ante_un_fallo():
    tmp = tempfile.mkdtemp(prefix="kiwi_web_")
    try:
        bueno = os.path.join(tmp, "bueno.html")
        with open(bueno, "w", encoding="utf-8") as f:
            f.write(pagina("<article><h1>Bueno</h1>" + RELLENO + "</article>"))
        salida = os.path.join(tmp, "md")

        resumen = convertir_lote([os.path.join(tmp, "no_existe.html"), bueno],
                                 salida, pausa=0)
        afirmar(resumen["fallidas"] == 1, f"no se registro el fallo: {resumen}")
        afirmar(resumen["convertidas"] == 1,
                f"el fallo detuvo el lote: {resumen}")
        afirmar(os.path.exists(os.path.join(salida, "manifiesto.tsv")),
                "no se escribio el manifiesto")
        print("  -> PASSED: un fallo no detiene el lote")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_escritura_en_disco():
    tmp = tempfile.mkdtemp(prefix="kiwi_web_")
    try:
        origen = os.path.join(tmp, "p.html")
        with open(origen, "w", encoding="utf-8") as f:
            f.write(pagina("<article><h1>En disco</h1>" + RELLENO + "</article>"))
        destino = os.path.join(tmp, "sub", "resultado.md")

        from htmlmd.pipeline import html_to_markdown
        md, meta = html_to_markdown(origen, destino, url_origen="https://ejemplo.com/real")
        afirmar(os.path.exists(destino), "no se escribio el fichero de salida")
        with open(destino, encoding="utf-8") as f:
            afirmar(f.read() == md, "lo escrito no coincide con lo devuelto")
        afirmar('url_origen: "https://ejemplo.com/real"' in md,
                "no se conservo la procedencia declarada del HTML local")
        print("  -> PASSED: escritura en disco y procedencia declarada")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_leer_lista_estructurada_y_secciones():
    tmp = tempfile.mkdtemp(prefix="kiwi_lista_test_")
    try:
        doc_list = os.path.join(tmp, "URLS_TEST.md")
        local_html = os.path.join(tmp, "recetas_doc.html")
        with open(local_html, "w", encoding="utf-8") as f:
            f.write("<html><body><h1>Recetas de temporada</h1><p>" + ("Texto de la receta de temporada. " * 30) + "</p></body></html>")

        with open(doc_list, "w", encoding="utf-8") as f:
            f.write(
                "# URLs de contenido web por sección — lista de lecturas\n"
                "Generado: 2026-09-01 05:29 UTC\n"
                "Fuente: archivos .url y capturas HTML\n\n"
                "## RECETAS — 2 web refs + 1 capturas HTML locales\n"
                "https://ejemplo.com/recetas1\n"
                "https://ejemplo.com/recetas2\n"
                "[CAPTURA LOCAL] recetas_doc.html\n\n"
                "## VIAJES — 0 web refs\n\n"
                "## LIBROS — 1 web ref\n"
                "https://ejemplo.com/libros1\n"
            )

        from htmlmd.batch import leer_lista_estructurada
        items = leer_lista_estructurada(doc_list)
        afirmar(len(items) == 4, f"se esperaban 4 items, se obtuvieron {len(items)}")

        afirmar(items[0].seccion == "RECETAS" and items[0].origen == "https://ejemplo.com/recetas1", "item 0 incorrecto")
        afirmar(items[1].seccion == "RECETAS" and items[1].origen == "https://ejemplo.com/recetas2", "item 1 incorrecto")
        afirmar(items[2].seccion == "RECETAS" and items[2].tipo == "local" and os.path.normpath(items[2].origen) == os.path.normpath(local_html), "item local 2 incorrecto")
        afirmar(items[3].seccion == "LIBROS" and items[3].origen == "https://ejemplo.com/libros1", "item 3 incorrecto")
        print("  -> PASSED: parser estructurado de secciones y capturas locales")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_lote_por_secciones_e_index():
    tmp = tempfile.mkdtemp(prefix="kiwi_lote_secciones_")
    try:
        h1 = os.path.join(tmp, "recetas.html")
        h2 = os.path.join(tmp, "viajes.html")
        with open(h1, "w", encoding="utf-8") as f:
            f.write("<html><head><title>Recetas de Otono</title></head><body><h1>Recetas de Otono</h1><p>" + ("Contenido detallado de analisis. " * 30) + "</p></body></html>")
        with open(h2, "w", encoding="utf-8") as f:
            f.write("<html><head><title>Guia de Viajes</title></head><body><h1>Guia de Viajes</h1><p>" + ("Contenido detallado de analisis. " * 30) + "</p></body></html>")

        from htmlmd.batch import ItemLista, convertir_lote
        items = [
            ItemLista(origen=h1, seccion="RECETAS", tipo="local"),
            ItemLista(origen=h2, seccion="VIAJES", tipo="local"),
        ]
        out_dir = os.path.join(tmp, "salida")
        resumen = convertir_lote(items, out_dir, organizar_por_secciones=True, generar_index=True)

        afirmar(resumen["convertidas"] == 2, "deberian haberse convertido 2 capturas")
        fr_md = os.path.join(out_dir, "RECETAS", "recetas-de-otono.md")
        es_md = os.path.join(out_dir, "VIAJES", "guia-de-viajes.md")
        index_file = os.path.join(out_dir, "INDEX.md")

        afirmar(os.path.exists(fr_md), f"no se genero {fr_md}")
        afirmar(os.path.exists(es_md), f"no se genero {es_md}")
        afirmar(os.path.exists(index_file), f"no se genero {index_file}")

        with open(fr_md, encoding="utf-8") as f:
            cuerpo_fr = f.read()
            afirmar('seccion: "RECETAS"' in cuerpo_fr, "no se incluyo seccion en frontmatter")

        with open(index_file, encoding="utf-8") as f:
            idx_txt = f.read()
            afirmar("RECETAS" in idx_txt and "VIAJES" in idx_txt, "secciones ausentes en INDEX.md")
            afirmar("recetas-de-otono.md" in idx_txt and "guia-de-viajes.md" in idx_txt, "ficheros ausentes en INDEX.md")

        print("  -> PASSED: subcarpetas por sección y generación de INDEX.md")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------

PRUEBAS = [
    test_estructura_declarada_se_respeta,
    test_listas_anidadas_conservan_la_sangria,
    test_tabla_real_si_tabla_de_maquetacion_no,
    test_codigo_y_lenguaje,
    test_estilos_en_linea,
    test_html_roto_no_pierde_contenido,
    test_enlaces_relativos_se_hacen_absolutos,
    test_anclas_internas_no_son_enlaces,
    test_enlaces_dentro_de_celdas_de_tabla,
    test_definiciones_conservan_enlaces_y_parrafos,
    test_enlaces_desactivables,
    test_se_poda_el_cromo_y_se_deja_constancia,
    test_sin_article_se_elige_por_densidad,
    test_pagina_completa_no_poda,
    test_frontmatter_de_procedencia,
    test_json_ld_manda_en_autor_y_fecha,
    test_el_titulo_no_sale_del_headline_del_json_ld,
    test_sufijo_del_medio_se_recorta_solo_si_es_el_medio,
    test_contenido_escaso_avisa_en_el_cuerpo,
    test_pagina_normal_no_avisa,
    test_titulo_se_antepone_si_falta_h1,
    test_titulo_no_se_duplica,
    test_frontmatter_desactivable,
    test_imagen_conserva_el_alt_y_se_hace_absoluta,
    test_imagen_repetida_no_se_duplica,
    test_data_uri_se_ignora,
    test_decodificacion_por_meta_charset,
    test_decodificacion_nunca_falla,
    test_resolucion_de_urls,
    test_nombre_de_salida_en_ascii,
    test_lista_admite_comentarios,
    test_manifiesto_ordenado_por_texto_ascendente,
    test_lote_reanudable_por_url,
    test_lote_no_se_detiene_ante_un_fallo,
    test_escritura_en_disco,
    test_leer_lista_estructurada_y_secciones,
    test_lote_por_secciones_e_index,
]


def run_all():
    print("--- PRUEBAS DEL CONVERSOR DE WEB A MARKDOWN (htmlmd) ---")
    for prueba in PRUEBAS:
        prueba()
    print(f"[OK] LAS {len(PRUEBAS)} PRUEBAS DE htmlmd PASARON CON EXITO")


if __name__ == "__main__":
    run_all()

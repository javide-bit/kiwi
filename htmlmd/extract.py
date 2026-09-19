# -*- coding: utf-8 -*-
"""
htmlmd.extract — Traduccion del arbol HTML al modelo de bloques de KIWI.

La clave del conversor web esta aqui: la salida de este modulo son exactamente los
`PageText` / `Block` / `Line` / `Span` de `pdfmd.models`. A partir de ese punto se
reutiliza `pdfmd.render.render_document` sin una sola linea nueva.

Diferencia de fondo con el conversor de PDF: alli la estructura se *infiere*
(un titulo se reconoce por el tamano de fuente, una tabla por la alineacion de
los espacios). Aqui la estructura viene declarada en el propio documento, asi
que inferir seria degradar un dato a conjetura. Por eso este modulo no invoca
`pdfmd.transform`: no hay nada que adivinar.
"""

import json
import re
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from htmlmd import boilerplate
from htmlmd.dom import Nodo, parsear
from htmlmd.fetch import Respuesta, dominio, resolver_url
from htmlmd.models import WebMetadata, WebOptions
from pdfmd.models import Block, Line, PageText, Span
from pdfmd.render import render_line
from pdfmd.utils import detectar_idioma, normalizar_texto_pdf


ENCABEZADOS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# Elementos que no aportan texto propio y se atraviesan sin abrir bloque
TRANSPARENTES = {
    "div", "section", "article", "main", "body", "html", "[document]",
    "span", "font", "center", "figure", "picture", "details", "summary",
    "header", "footer", "tbody", "thead", "tfoot", "colgroup", "col",
}

_RE_ESPACIOS = re.compile(r"\s+")
_RE_ENLACE_MD = re.compile(r"\]\(|<https?://")
_RE_LENGUAJE = re.compile(r"(?:language|lang|highlight)[-_]([A-Za-z0-9+#]+)", re.IGNORECASE)


class _Estilo:
    """Estilo tipografico heredado por los spans mientras se desciende el arbol."""

    __slots__ = ("bold", "italic", "mono", "link")

    def __init__(self, bold=False, italic=False, mono=False, link=""):
        self.bold, self.italic, self.mono, self.link = bold, italic, mono, link

    def con(self, bold=None, italic=None, mono=None, link=None):
        return _Estilo(
            self.bold if bold is None else bold,
            self.italic if italic is None else italic,
            self.mono if mono is None else mono,
            self.link if link is None else link,
        )


def _texto_normalizado(bruto: str, preservar: bool = False) -> str:
    """Colapsa el espacio en blanco como hace un navegador, salvo dentro de <pre>."""
    texto = normalizar_texto_pdf(bruto)
    if preservar:
        return texto
    return _RE_ESPACIOS.sub(" ", texto)


# --------------------------------------------------------------------------
# Contenido en linea -> lineas de spans
# --------------------------------------------------------------------------

class _AcumuladorDeLineas:
    """Va formando lineas de spans; <br> cierra la linea en curso."""

    def __init__(self):
        self.lineas: List[Line] = []
        self.actual: List[Span] = []

    def anadir(self, texto: str, estilo: _Estilo) -> None:
        if not texto:
            return
        # Un espacio suelto al principio de linea no aporta nada y ensucia el render
        if not self.actual and not texto.strip():
            return
        self.actual.append(Span(
            text=texto,
            bold=estilo.bold,
            italic=estilo.italic,
            monospace=estilo.mono,
            link=estilo.link,
        ))

    def salto(self) -> None:
        self.cerrar()

    def cerrar(self) -> None:
        if not self.actual:
            return
        # Recortar el espacio sobrante en los extremos de la linea
        self.actual[0].text = self.actual[0].text.lstrip()
        self.actual[-1].text = self.actual[-1].text.rstrip()
        spans = [s for s in self.actual if s.text]
        if spans:
            self.lineas.append(Line(spans=spans))
        self.actual = []

    def resultado(self) -> List[Line]:
        self.cerrar()
        return self.lineas


def _recorrer_inline(nodo: Nodo, acc: _AcumuladorDeLineas, estilo: _Estilo,
                     base_url: str, opts: WebOptions, preservar: bool = False) -> None:
    """Desciende un subarbol en linea acumulando spans con su estilo y su enlace."""
    for hijo in nodo.hijos:
        if hijo.es_texto:
            acc.anadir(_texto_normalizado(hijo.texto, preservar), estilo)
            continue

        tag = hijo.tag

        if tag == "br":
            acc.salto()
        elif tag in ("strong", "b"):
            _recorrer_inline(hijo, acc, estilo.con(bold=True), base_url, opts, preservar)
        elif tag in ("em", "i", "cite", "var", "dfn"):
            _recorrer_inline(hijo, acc, estilo.con(italic=True), base_url, opts, preservar)
        elif tag in ("code", "kbd", "samp", "tt"):
            _recorrer_inline(hijo, acc, estilo.con(mono=True), base_url, opts, preservar)
        elif tag == "a":
            href = hijo.attr("href")
            # El juicio se hace sobre el href tal cual venia: resolverlo primero
            # convertiria "#seccion" en una URL absoluta con aspecto de enlace real.
            util = bool(href) and not href.lower().startswith(("#", "javascript:", "data:"))
            destino = resolver_url(base_url, href) if (util and opts.enlaces_absolutos) else href
            if util:
                util = destino.startswith(("http://", "https://", "mailto:", "ftp://"))
            nuevo = estilo.con(link=destino) if (opts.conservar_enlaces and util) else estilo
            _recorrer_inline(hijo, acc, nuevo, base_url, opts, preservar)
        elif tag in ("script", "style", "noscript"):
            continue
        else:
            _recorrer_inline(hijo, acc, estilo, base_url, opts, preservar)


def _lineas_de(nodo: Nodo, base_url: str, opts: WebOptions,
               estilo: Optional[_Estilo] = None, preservar: bool = False) -> List[Line]:
    acc = _AcumuladorDeLineas()
    _recorrer_inline(nodo, acc, estilo or _Estilo(), base_url, opts, preservar)
    return acc.resultado()


def _texto_de(nodo: Nodo) -> str:
    return _texto_normalizado(nodo.texto_plano()).strip()


# --------------------------------------------------------------------------
# Bloques
# --------------------------------------------------------------------------

def _lenguaje_de_codigo(nodo: Nodo) -> str:
    for candidato in (nodo, nodo.primero("code") or nodo):
        firma = f"{candidato.attr('class')} {candidato.attr('data-lang')}"
        m = _RE_LENGUAJE.search(firma)
        if m:
            return m.group(1).lower()
    return ""


def _tabla_a_datos(tabla: Nodo, base_url: str = "",
                   opts: Optional[WebOptions] = None) -> Optional[List[List[str]]]:
    """
    Convierte un <table> en la matriz que espera `render_table`.

    Las celdas se renderizan como Markdown en linea, no como texto plano: una
    celda suele ser justo donde vive el enlace al documento fuente, y extraerla
    en plano tiraria el destino y dejaria solo el texto del ancla. `render_table`
    ya escapa las barras verticales, asi que el enlace no rompe la tabla.

    Devuelve None si la tabla es de maquetacion (una sola columna): emitirla como
    tabla Markdown produciria un marco vacio alrededor de texto normal.
    """
    opts = opts or WebOptions()
    filas: List[List[str]] = []
    for tr in tabla.buscar("tr"):
        celdas = [c for c in tr.hijos if c.tag in ("td", "th")]
        if not celdas:
            continue
        fila: List[str] = []
        for celda in celdas:
            lineas = _lineas_de(celda, base_url, opts)
            valor = " ".join(render_line(l) for l in lineas).strip()
            # colspan: se replica el hueco para que no se descuadren las columnas
            try:
                repetir = max(1, min(8, int(celda.attr("colspan") or 1)))
            except ValueError:
                repetir = 1
            fila.extend([valor] + [""] * (repetir - 1))
        filas.append(fila)

    if not filas or max(len(f) for f in filas) < 2:
        return None
    return filas


def _prefijo_de_lista(tag_lista: str, indice: int, nivel: int) -> str:
    sangria = "  " * max(0, nivel)
    return f"{sangria}{indice}. " if tag_lista == "ol" else f"{sangria}- "


def _emitir_lista(lista: Nodo, bloques: List[Block], base_url: str,
                  opts: WebOptions, ctx, nivel: int = 0) -> None:
    """
    Emite la lista completa como UN solo bloque.

    Un bloque por item haria que `render_document` los separase con linea en
    blanco y Markdown dejaria de verlos como una lista.
    """
    lineas: List[Line] = []
    items = [h for h in lista.hijos if h.tag == "li"]
    for i, li in enumerate(items, start=1):
        # Las sublistas se extraen antes para que no contaminen el texto del item
        sublistas = [h for h in li.hijos if h.tag in ("ul", "ol")]
        for s in sublistas:
            s.desconectar()

        propias = _lineas_de(li, base_url, opts)
        prefijo = _prefijo_de_lista(lista.tag, i, nivel)
        if propias:
            primera = propias[0]
            primera.spans.insert(0, Span(text=prefijo))
            lineas.append(primera)
            # Las lineas siguientes del mismo item se sangran bajo el
            for extra in propias[1:]:
                extra.spans.insert(0, Span(text="  " * (nivel + 1)))
                lineas.append(extra)
        elif sublistas:
            lineas.append(Line(spans=[Span(text=prefijo.rstrip())]))

        for s in sublistas:
            anidados: List[Block] = []
            _emitir_lista(s, anidados, base_url, opts, ctx, nivel + 1)
            for b in anidados:
                lineas.extend(b.lines)

    if lineas:
        bloques.append(Block(lines=lineas, block_type="list_item", page_num=1))


class _Contexto:
    """Estado que atraviesa toda la extraccion (imagenes vistas, contadores)."""

    def __init__(self, base_url: str, opts: WebOptions,
                 guardar_imagen: Optional[Callable[[str, str], str]] = None):
        self.base_url = base_url
        self.opts = opts
        self.guardar_imagen = guardar_imagen
        self.imagenes = 0
        self.vistas = set()


def _emitir_imagen(img: Nodo, bloques: List[Block], ctx: "_Contexto") -> None:
    """
    Emite la imagen como linea Markdown literal en un bloque de texto.

    No se usa el exportador de imagenes de `pdfmd.render` a proposito: ese
    rotula las imagenes como "Imagen 3 (pag. 1)" porque un PDF no tiene texto
    alternativo. Una pagina web si lo tiene, y el `alt` suele ser la unica
    descripcion de lo que muestra la figura.
    """
    src = img.attr("src") or img.attr("data-src") or img.attr("data-original")
    if not src:
        # <img srcset="a.jpg 1x, b.jpg 2x">: basta el primer candidato
        srcset = img.attr("srcset")
        if srcset:
            src = srcset.split(",")[0].strip().split(" ")[0]
    if not src or src.startswith("data:"):
        return

    url = resolver_url(ctx.base_url, src)
    if url in ctx.vistas and ctx.opts.dedupe_imagenes:
        return
    ctx.vistas.add(url)

    alt = _texto_normalizado(img.attr("alt")).strip() if ctx.opts.conservar_alt else ""
    alt = alt.replace("[", "\\[").replace("]", "\\]")

    destino = url
    if ctx.opts.exportar_imagenes and ctx.guardar_imagen is not None:
        guardada = ctx.guardar_imagen(url, alt)
        if not guardada:
            return
        destino = guardada

    ctx.imagenes += 1
    etiqueta = alt or f"Imagen {ctx.imagenes}"
    marca = f"![{etiqueta}]({destino})"
    bloques.append(Block(lines=[Line(spans=[Span(text=marca)])],
                         block_type="text", page_num=1))


def _emitir(nodo: Nodo, bloques: List[Block], ctx: "_Contexto") -> None:
    """Recorre el cuerpo emitiendo un bloque por elemento estructural."""
    base_url, opts = ctx.base_url, ctx.opts
    sueltos = _AcumuladorDeLineas()

    def _volcar_sueltos() -> None:
        """Texto en linea colgando directamente de un contenedor: no se pierde."""
        lineas = sueltos.resultado()
        if lineas:
            bloques.append(Block(lines=lineas, block_type="text", page_num=1))
        sueltos.lineas, sueltos.actual = [], []

    for hijo in nodo.hijos:
        if hijo.es_texto:
            sueltos.anadir(_texto_normalizado(hijo.texto), _Estilo())
            continue

        tag = hijo.tag

        if tag in ENCABEZADOS:
            _volcar_sueltos()
            lineas = _lineas_de(hijo, base_url, opts)
            if lineas:
                # Un titulo es una sola linea: los <br> internos no deben partirlo
                spans = [s for l in lineas for s in l.spans]
                bloques.append(Block(lines=[Line(spans=spans)], block_type="heading",
                                     heading_level=ENCABEZADOS[tag], page_num=1))

        elif tag == "p":
            _volcar_sueltos()
            lineas = _lineas_de(hijo, base_url, opts)
            if lineas:
                bloques.append(Block(lines=lineas, block_type="text", page_num=1))

        elif tag in ("ul", "ol") and opts.normalizar_listas:
            _volcar_sueltos()
            _emitir_lista(hijo, bloques, base_url, opts, ctx)

        elif tag == "blockquote":
            _volcar_sueltos()
            internos: List[Block] = []
            _emitir(hijo, internos, ctx)
            lineas = [l for b in internos for l in b.lines]
            if lineas:
                bloques.append(Block(lines=lineas, block_type="quote", page_num=1))

        elif tag == "pre" and opts.detectar_codigo:
            _volcar_sueltos()
            crudo = hijo.texto_plano().strip("\n")
            if crudo.strip():
                lineas = [Line(spans=[Span(text=l)]) for l in crudo.split("\n")]
                bloques.append(Block(lines=lineas, block_type="code",
                                     code_lang=_lenguaje_de_codigo(hijo), page_num=1))

        elif tag == "table":
            _volcar_sueltos()
            datos = _tabla_a_datos(hijo, base_url, opts) if opts.detectar_tablas else None
            if datos:
                bloques.append(Block(block_type="table", table_data=datos, page_num=1))
            else:
                _emitir(hijo, bloques, ctx)   # tabla de maquetacion: se atraviesa

        elif tag == "img":
            _volcar_sueltos()
            _emitir_imagen(hijo, bloques, ctx)

        elif tag == "figcaption":
            _volcar_sueltos()
            texto = _texto_de(hijo)
            if texto:
                bloques.append(Block(
                    lines=[Line(spans=[Span(text=texto, italic=True)])],
                    block_type="text", page_num=1))

        elif tag == "hr":
            _volcar_sueltos()
            if bloques:
                bloques.append(Block(lines=[Line(spans=[Span(text="---")])],
                                     block_type="text", page_num=1))

        elif tag == "dl":
            # El termino se emite en negrita y la definicion se recorre como
            # contenido normal. Aplanarla a texto plano seria mas parecido a una
            # lista de definiciones, pero se comeria los enlaces y los parrafos
            # que viven dentro del <dd> —que es justo donde los pone la
            # documentacion tecnica—, y el contenido pesa mas que el aspecto.
            _volcar_sueltos()
            for item in hijo.hijos:
                if item.tag == "dt":
                    lineas = _lineas_de(item, base_url, opts, _Estilo(bold=True))
                    if lineas:
                        bloques.append(Block(lines=lineas, block_type="text", page_num=1))
                elif item.tag == "dd":
                    _emitir(item, bloques, ctx)

        elif tag in ("script", "style", "noscript", "template"):
            continue

        elif tag in TRANSPARENTES:
            _volcar_sueltos()
            _emitir(hijo, bloques, ctx)

        else:
            # Elemento en linea colgando de un contenedor (un <a> o un <b> suelto)
            _recorrer_inline(hijo, sueltos, _Estilo(), base_url, opts)

    _volcar_sueltos()


# --------------------------------------------------------------------------
# Metadatos
# --------------------------------------------------------------------------

def _metas(raiz: Nodo) -> dict:
    """Indexa los <meta> por name y por property, en minusculas."""
    encontrados = {}
    for m in raiz.buscar("meta"):
        clave = (m.attr("name") or m.attr("property") or m.attr("itemprop")).lower()
        valor = m.attr("content")
        if clave and valor and clave not in encontrados:
            encontrados[clave] = valor
    return encontrados


def _json_ld(raiz: Nodo) -> dict:
    """
    Datos estructurados schema.org, si el documento los trae.

    Es la fuente mas fiable de autor y fecha de publicacion en prensa digital,
    muy por encima de los <meta>, que a menudo llevan el nombre del medio.
    """
    for script in raiz.buscar("script"):
        if "ld+json" not in script.attr("type").lower():
            continue
        try:
            datos = json.loads(script.texto_plano())
        except (json.JSONDecodeError, ValueError):
            continue
        candidatos = datos if isinstance(datos, list) else [datos]
        for c in list(candidatos):
            if not isinstance(c, dict):
                continue
            if isinstance(c.get("@graph"), list):
                candidatos.extend(x for x in c["@graph"] if isinstance(x, dict))
                continue
            tipo = str(c.get("@type", "")).lower()
            if any(t in tipo for t in ("article", "posting", "webpage", "report")):
                return c
    return {}


def _autor_de_json_ld(bloque: dict) -> str:
    autor = bloque.get("author")
    if isinstance(autor, dict):
        return str(autor.get("name", "")).strip()
    if isinstance(autor, list):
        nombres = [str(a.get("name", "")).strip() if isinstance(a, dict) else str(a).strip()
                   for a in autor]
        return ", ".join(n for n in nombres if n)
    return str(autor or "").strip()


_SEPARADORES_DE_TITULO = (" | ", " - ", " — ", " – ", " · ", " :: ", " » ")


def _limpiar_titulo(titulo: str, sitio: str, dominio_web: str) -> str:
    """
    Quita el nombre del medio del final del titulo: "Kiwi - Wikipedia" -> "Kiwi".

    Solo se recorta cuando la cola coincide de verdad con el sitio. Recortar por
    el simple hecho de haber un guion destrozaria titulos legitimos del tipo
    "Manual practico - segunda edicion".
    """
    if not titulo:
        return titulo

    fichas = {p for p in re.split(r"[^\wáéíóúñ]+", f"{sitio} {dominio_web}".lower()) if len(p) > 3}
    fichas.discard("www")
    if not fichas:
        return titulo

    for sep in _SEPARADORES_DE_TITULO:
        if sep not in titulo:
            continue
        cabeza, _, cola = titulo.rpartition(sep)
        if not cabeza.strip():
            continue
        cola_fichas = {p for p in re.split(r"[^\wáéíóúñ]+", cola.lower()) if len(p) > 3}
        if cola_fichas & fichas:
            return cabeza.strip()
    return titulo


def extraer_metadatos(raiz: Nodo, respuesta: Respuesta, opts: WebOptions) -> WebMetadata:
    metas = _metas(raiz)
    ld = _json_ld(raiz)

    def _primero(*claves: str) -> str:
        for k in claves:
            if metas.get(k):
                return metas[k].strip()
        return ""

    titulo_tag = raiz.primero("title")
    # El <title> y og:title van por delante del JSON-LD deliberadamente: hay
    # sitios cuyo `headline` no es el titular sino la descripcion de la entidad
    # (Wikipedia sirve ahi "fruta comestible" para el articulo "Kiwi"), y una
    # descripcion colocada como titulo estropea el nombre del fichero ademas del
    # frontmatter. El JSON-LD queda de reserva por si no hay nada mejor.
    titulo = (_primero("og:title", "twitter:title", "dc.title")
              or (_texto_de(titulo_tag) if titulo_tag else "")
              or str(ld.get("headline", "")).strip()
              or str(ld.get("name", "")).strip())

    html_tag = raiz.primero("html")
    idioma = html_tag.attr("lang").split("-")[0].lower() if html_tag else ""

    url_final = respuesta.url_final or respuesta.url_solicitada
    canonica = ""
    for enlace in raiz.buscar("link"):
        if "canonical" in enlace.attr("rel").lower():
            canonica = resolver_url(url_final, enlace.attr("href"))
            break

    nombre_sitio = _primero("og:site_name") or dominio(url_final)

    return WebMetadata(
        titulo=_limpiar_titulo(titulo, nombre_sitio, dominio(url_final)),
        autor=_autor_de_json_ld(ld) or _primero("author", "article:author", "dc.creator"),
        descripcion=_primero("description", "og:description", "twitter:description"),
        keywords=_primero("keywords", "news_keywords"),
        sitio=nombre_sitio,
        idioma=idioma,
        url_solicitada=respuesta.url_solicitada,
        url_final=canonica or url_final,
        estado_http=respuesta.estado_http,
        content_type=respuesta.content_type,
        sha256_html=respuesta.sha256,
        fecha_publicacion=(str(ld.get("datePublished", "")).strip()
                           or _primero("article:published_time", "date", "dc.date",
                                       "citation_publication_date")),
        fecha_captura=respuesta.fecha_captura,
        fecha_conversion=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


# --------------------------------------------------------------------------
# Entrada del modulo
# --------------------------------------------------------------------------

def extraer(respuesta: Respuesta, opts: Optional[WebOptions] = None,
            guardar_imagen: Optional[Callable[[str, str], str]] = None
            ) -> Tuple[List[PageText], WebMetadata]:
    """
    Convierte una respuesta HTML en (paginas, metadatos).

    Se devuelve una lista de `PageText` con un unico elemento por coherencia con
    `render_document`: una pagina web no tiene paginacion, y fingir uno de esos
    marcadores `<!-- p.N -->` seria inventarse una cita que nadie puede verificar.
    """
    opts = opts or WebOptions()
    raiz = parsear(respuesta.html)

    # Los metadatos se leen del arbol completo: el <head>, los <meta> y el
    # JSON-LD viven justo en las ramas que la limpieza esta a punto de podar.
    metadatos = extraer_metadatos(raiz, respuesta, opts)
    base_url = metadatos.url_final or respuesta.url_solicitada

    if opts.quitar_cromo:
        metadatos.elementos_suprimidos = boilerplate.limpiar(raiz)

    if opts.solo_cuerpo:
        cuerpo, criterio = boilerplate.seleccionar_cuerpo(raiz)
    else:
        cuerpo, criterio = (raiz.primero("body") or raiz), "documento-completo"
    metadatos.criterio_cuerpo = criterio

    ctx = _Contexto(base_url, opts, guardar_imagen)
    bloques: List[Block] = []
    _emitir(cuerpo, bloques, ctx)

    # Un titulo que ya viene como <h1> no se duplica; si no viene, se antepone:
    # sin encabezado de nivel 1 el documento no se indexa bien en Obsidian.
    if metadatos.titulo and not any(b.block_type == "heading" and b.heading_level == 1
                                    for b in bloques):
        bloques.insert(0, Block(
            lines=[Line(spans=[Span(text=metadatos.titulo)])],
            block_type="heading", heading_level=1, page_num=1))

    texto_total = "\n".join(b.text() for b in bloques)
    metadatos.chars_extraidos = len(texto_total)
    metadatos.bloques = len(bloques)
    metadatos.imagenes = ctx.imagenes
    # Los enlaces de las celdas ya vienen renderizados como Markdown, sin span
    # propio que contar, asi que se suman aparte: si no, el recuento
    # diria menos enlaces de los que el documento realmente lleva.
    en_spans = sum(1 for b in bloques for l in b.lines for s in l.spans if s.link)
    en_tablas = sum(len(_RE_ENLACE_MD.findall(c))
                    for b in bloques if b.table_data
                    for fila in b.table_data for c in fila)
    metadatos.enlaces = en_spans + en_tablas

    if opts.detectar_idioma and not metadatos.idioma:
        metadatos.idioma = detectar_idioma(texto_total)

    pagina = PageText(blocks=bloques, page_num=1, char_count=len(texto_total),
                      has_images=ctx.imagenes > 0)
    return [pagina], metadatos

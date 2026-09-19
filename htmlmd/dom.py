# -*- coding: utf-8 -*-
"""
htmlmd.dom — Arbol DOM minimo construido sobre html.parser de la biblioteca estandar.

Se construye un arbol propio, y no se trabaja sobre los eventos del parser, porque
para separar el articulo del menu hay que poder *medir* subarboles (cuanto texto
cuelga de cada contenedor, cuanto de ese texto es de enlaces). Eso es imposible
con un parser de eventos, que ve el documento una sola vez y hacia delante.

Sin dependencias externas: KIWI se distribuye como .exe y cada dependencia nueva
engorda el binario y anade una superficie que mantener.
"""

from html.parser import HTMLParser
from typing import Dict, List, Optional


# Etiquetas sin cierre: no abren contexto y nunca esperan un </tag>
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Etiquetas de bloque: su apertura cierra implicitamente un <p> pendiente.
# El HTML real esta lleno de <p> sin cerrar y, sin esto, un parrafo se tragaria
# el resto del documento como si fuera hijo suyo.
BLOQUE = {
    "address", "article", "aside", "blockquote", "details", "div", "dl",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre",
    "section", "table", "ul",
}

# Cierres implicitos: abrir la clave cierra cualquiera de los valores que este abierto.
AUTOCIERRE: Dict[str, set] = {
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "option": {"option"},
    "thead": {"thead", "tbody", "tfoot"},
    "tbody": {"thead", "tbody", "tfoot"},
    "tfoot": {"thead", "tbody", "tfoot"},
}


class Nodo:
    """Elemento o nodo de texto. `tag` vacio significa nodo de texto."""

    __slots__ = ("tag", "attrs", "hijos", "padre", "texto")

    def __init__(self, tag: str = "", attrs: Optional[Dict[str, str]] = None,
                 texto: str = "", padre: Optional["Nodo"] = None):
        self.tag = tag
        self.attrs: Dict[str, str] = attrs or {}
        self.hijos: List["Nodo"] = []
        self.padre = padre
        self.texto = texto

    # -- consultas -----------------------------------------------------------

    @property
    def es_texto(self) -> bool:
        return self.tag == ""

    def attr(self, nombre: str) -> str:
        return (self.attrs.get(nombre) or "").strip()

    def texto_plano(self) -> str:
        """Todo el texto que cuelga del nodo, concatenado."""
        if self.es_texto:
            return self.texto
        return "".join(h.texto_plano() for h in self.hijos)

    def texto_de_enlaces(self) -> str:
        """Texto que cuelga de elementos <a>. Un menu es casi todo esto."""
        if self.es_texto:
            return ""
        if self.tag == "a":
            return self.texto_plano()
        return "".join(h.texto_de_enlaces() for h in self.hijos)

    def buscar(self, *tags: str) -> List["Nodo"]:
        """Todos los descendientes con alguna de las etiquetas dadas, en orden de documento."""
        encontrados: List["Nodo"] = []
        for h in self.hijos:
            if h.tag in tags:
                encontrados.append(h)
            encontrados.extend(h.buscar(*tags))
        return encontrados

    def primero(self, *tags: str) -> Optional["Nodo"]:
        encontrados = self.buscar(*tags)
        return encontrados[0] if encontrados else None

    def desconectar(self) -> None:
        if self.padre is not None:
            try:
                self.padre.hijos.remove(self)
            except ValueError:
                pass
            self.padre = None

    def __repr__(self) -> str:  # pragma: no cover - solo para depuracion
        if self.es_texto:
            return f"Texto({self.texto[:30]!r})"
        return f"<{self.tag}> x{len(self.hijos)}"


class _Constructor(HTMLParser):
    """Convierte el flujo de eventos de html.parser en un arbol de `Nodo`."""

    def __init__(self):
        # convert_charrefs=True resuelve &amp;, &nbsp; y demas sin trabajo extra
        super().__init__(convert_charrefs=True)
        self.raiz = Nodo("[document]")
        self.pila: List[Nodo] = [self.raiz]

    # -- utilidades de pila --------------------------------------------------

    @property
    def actual(self) -> Nodo:
        return self.pila[-1]

    def _cerrar_hasta(self, tags: set) -> None:
        """Cierra los nodos abiertos cuya etiqueta este en `tags`, sin pasar de un bloque."""
        while len(self.pila) > 1 and self.actual.tag in tags:
            self.pila.pop()

    # -- eventos -------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        atributos = {k.lower(): (v or "") for k, v in attrs}

        if tag in AUTOCIERRE:
            self._cerrar_hasta(AUTOCIERRE[tag])
        elif tag in BLOQUE:
            self._cerrar_hasta({"p"})

        nodo = Nodo(tag, atributos, padre=self.actual)
        self.actual.hijos.append(nodo)
        if tag not in VOID:
            self.pila.append(nodo)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        nodo = Nodo(tag, {k.lower(): (v or "") for k, v in attrs}, padre=self.actual)
        self.actual.hijos.append(nodo)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID:
            return
        # Cierre tolerante: si el documento cierra una etiqueta que no es la de
        # arriba, se buscan hacia abajo y se descartan las intermedias mal cerradas.
        for i in range(len(self.pila) - 1, 0, -1):
            if self.pila[i].tag == tag:
                del self.pila[i:]
                return
        # Cierre huerfano (</div> de mas): se ignora en lugar de vaciar la pila

    def handle_data(self, data):
        if not data:
            return
        self.actual.hijos.append(Nodo("", texto=data, padre=self.actual))


def parsear(html: str) -> Nodo:
    """Construye el arbol del documento. Nunca lanza: el HTML roto es la norma."""
    constructor = _Constructor()
    try:
        constructor.feed(html)
        constructor.close()
    except Exception:
        # html.parser puede atragantarse con entradas patologicas. Lo parseado
        # hasta ese punto sigue siendo util y es preferible a no devolver nada.
        pass
    return constructor.raiz

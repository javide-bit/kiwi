# -*- coding: utf-8 -*-
"""
htmlmd.boilerplate — Separacion del articulo respecto del cromo de la pagina.

Este modulo es el equivalente web de `transform.find_repetitive_headers_footers`:
en el PDF se detectan las cabeceras y pies porque se repiten pagina tras pagina;
en una pagina web hay una sola "pagina", asi que la senal tiene que salir de la
estructura y de la densidad de enlaces.

Criterio de fondo, el mismo del resto de KIWI: ante la duda, conservar. Un menu
colado en el Markdown es ruido molesto; un parrafo del articulo suprimido por un
filtro agresivo es una perdida silenciosa de contenido, que es mucho peor.
"""

import re
from typing import List, Optional, Tuple

from htmlmd.dom import Nodo


# Contenido que nunca es texto del documento
TAGS_DESCARTE = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "embed", "form", "input", "button", "select", "textarea",
    "nav", "aside", "dialog",
}

# Se descartan por rol semantico explicito del propio documento
ROLES_DESCARTE = {"navigation", "banner", "search", "complementary", "contentinfo", "dialog"}

# Marcadores en class/id que delatan cromo. Deliberadamente conservador: cada
# patron aqui puede borrar contenido real si el sitio nombra mal sus divs.
_RE_CROMO = re.compile(
    r"(^|[-_ ])("
    r"nav|navbar|menu|sidebar|side-bar|breadcrumb|pagination|paginator|"
    r"cookie|consent|gdpr|banner|advert|advertisement|ads?|sponsor|promo|"
    r"social|share|sharing|follow|subscribe|newsletter|signup|paywall|"
    r"comment|comments|disqus|related|recommend|trending|popular|"
    r"skip-link|screen-reader|sr-only|visually-hidden|hidden|"
    r"footer|site-footer|page-footer|masthead|site-header|toolbar|widget|modal|popup|overlay"
    r")([-_ ]|$)",
    re.IGNORECASE,
)

# Cabecera y pie solo se descartan si son los del *sitio*, no los de un <article>
_TAGS_MARCO = {"header", "footer"}

# Elementos estructurales que jamas se podan por sus atributos. <html> y <body>
# llevan a menudo clases de tema ("client-nojs", "no-sidebar") que disparan los
# patrones de cromo; podarlos borra el documento entero.
_INTOCABLES = {"html", "body", "[document]", "head", "main", "article"}

# Un contenedor que acumula mas de esta fraccion del texto del documento es el
# documento, no su decoracion. Es la red de seguridad del criterio de fondo:
# ante la duda, conservar.
_FRACCION_MAXIMA_PODABLE = 0.4


def _es_cromo_por_atributos(nodo: Nodo) -> bool:
    if nodo.attr("role").lower() in ROLES_DESCARTE:
        return True
    if nodo.attr("aria-hidden").lower() == "true":
        return True
    if "hidden" in nodo.attrs:
        return True
    firma = f"{nodo.attr('class')} {nodo.attr('id')} {nodo.attr('data-testid')}"
    return bool(firma.strip()) and bool(_RE_CROMO.search(firma))


def limpiar(raiz: Nodo) -> List[str]:
    """
    Poda el arbol in situ y devuelve la lista de lo suprimido, para el frontmatter.

    La trazabilidad no es adorno: `elementos_suprimidos` es lo que permite
    auditar despues por que falta un parrafo, igual que `lineas_suprimidas` en
    el conversor de PDF.
    """
    suprimidos: List[str] = []
    total_texto = len(raiz.texto_plano())

    def _demasiado_grande(nodo: Nodo) -> bool:
        """Un menu nunca contiene media pagina de texto; el articulo, si."""
        if total_texto <= 0:
            return False
        return len(nodo.texto_plano()) / total_texto > _FRACCION_MAXIMA_PODABLE

    def _recorrer(nodo: Nodo, dentro_de_articulo: bool) -> None:
        for hijo in list(nodo.hijos):
            if hijo.es_texto:
                continue

            motivo = ""
            if hijo.tag in TAGS_DESCARTE:
                # Un <script> o un <form> no son texto del documento por mucho
                # que ocupen, asi que estos no pasan por la red de seguridad.
                motivo = hijo.tag
            elif hijo.tag in _INTOCABLES:
                motivo = ""
            elif hijo.tag in _TAGS_MARCO and not dentro_de_articulo:
                # <header>/<footer> dentro de un <article> son del articulo
                motivo = "" if _demasiado_grande(hijo) else hijo.tag
            elif _es_cromo_por_atributos(hijo) and not _demasiado_grande(hijo):
                etiqueta = hijo.attr("class") or hijo.attr("id") or hijo.tag
                motivo = f"{hijo.tag}.{etiqueta.split()[0]}" if etiqueta else hijo.tag

            if motivo:
                suprimidos.append(motivo)
                hijo.desconectar()
                continue

            _recorrer(hijo, dentro_de_articulo or hijo.tag == "article")

    _recorrer(raiz, dentro_de_articulo=False)

    # Sin duplicados y en orden estable, como hace lineas_suprimidas
    vistos, unicos = set(), []
    for s in suprimidos:
        if s not in vistos:
            vistos.add(s)
            unicos.append(s)
    return unicos


def _puntuar(nodo: Nodo) -> float:
    """
    Puntua un contenedor como candidato a cuerpo del articulo.

    Longitud del texto penalizada por densidad de enlaces: una lista de enlaces
    de 2.000 caracteres no es un articulo, aunque mida mas que el articulo.
    """
    texto = nodo.texto_plano().strip()
    n = len(texto)
    if n < 140:
        return 0.0

    densidad_enlaces = len(nodo.texto_de_enlaces()) / n
    if densidad_enlaces > 0.55:
        return 0.0

    parrafos = len(nodo.buscar("p"))
    puntos = n * (1.0 - densidad_enlaces)
    puntos += parrafos * 60          # la prosa se escribe en <p>
    return puntos


def seleccionar_cuerpo(raiz: Nodo) -> Tuple[Nodo, str]:
    """
    Devuelve (nodo_cuerpo, criterio_usado).

    Se prefiere siempre lo que el propio documento declara —<article>, <main>,
    role=main— sobre cualquier heuristica nuestra: si el autor ha marcado donde
    esta el articulo, adivinarlo seria sustituir un dato por una conjetura.
    """
    for tag, criterio in (("article", "article"), ("main", "main")):
        candidatos = raiz.buscar(tag)
        if candidatos:
            mejor = max(candidatos, key=lambda n: len(n.texto_plano()))
            if len(mejor.texto_plano().strip()) >= 140:
                return mejor, criterio

    for nodo in raiz.buscar("div", "section"):
        if nodo.attr("role").lower() == "main":
            return nodo, "role=main"

    # Heuristica: el contenedor con mas prosa util
    mejor_nodo: Optional[Nodo] = None
    mejor_puntos = 0.0
    for nodo in raiz.buscar("div", "section", "td", "body"):
        puntos = _puntuar(nodo)
        if puntos > mejor_puntos:
            mejor_puntos, mejor_nodo = puntos, nodo

    if mejor_nodo is not None:
        return mejor_nodo, "densidad"

    cuerpo = raiz.primero("body")
    return (cuerpo or raiz), "documento-completo"

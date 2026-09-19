# -*- coding: utf-8 -*-
"""
htmlmd.pipeline — Orquestador de la conversion de pagina web a Markdown.

    obtener  ->  extraer  ->  clasificar  ->  render_web  ->  .md
                                                  |
                                    reutiliza pdfmd.render.render_document
"""

import hashlib
import os
import ssl
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from htmlmd.extract import extraer
from htmlmd.fetch import ErrorDeDescarga, Respuesta, obtener
from htmlmd.models import WebMetadata, WebOptions
from htmlmd.render import render_web, ruta_de_assets
from pdfmd.utils import slugify


# Extensiones que aceptamos guardar en _assets. Todo lo demas (svg de iconos,
# webp de tracking, respuestas HTML disfrazadas) se deja como enlace remoto.
_EXTENSIONES = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/avif": "avif",
    "image/svg+xml": "svg", "image/bmp": "bmp", "image/tiff": "tiff",
}


class _DescargadorDeImagenes:
    """
    Guarda las imagenes del articulo en `<nombre>_assets/`.

    Si una imagen falla se devuelve cadena vacia y el llamante conserva la URL
    remota: perder la imagen local es un inconveniente, perder la referencia a
    donde estaba seria perder informacion.
    """

    def __init__(self, output_md_path: str, opts: WebOptions,
                 log_cb: Optional[Callable[[str], None]] = None):
        self.dir_abs, self.dir_rel = ruta_de_assets(output_md_path)
        self.opts = opts
        self.log_cb = log_cb
        self.cache: Dict[str, str] = {}     # sha1 del contenido -> ruta relativa
        self.por_url: Dict[str, str] = {}
        self.contador = 0
        self.guardadas = 0
        self.reutilizadas = 0

    def _log(self, mensaje: str) -> None:
        if self.log_cb:
            self.log_cb(mensaje)

    def __call__(self, url: str, alt: str = "") -> str:
        if url in self.por_url:
            return self.por_url[url]

        peticion = urllib.request.Request(
            url, headers={"User-Agent": self.opts.user_agent, "Accept": "image/*,*/*;q=0.8"}
        )
        contexto = None
        if not self.opts.verificar_tls:
            contexto = ssl.create_default_context()
            contexto.check_hostname = False
            contexto.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(peticion, timeout=self.opts.timeout,
                                        context=contexto) as r:
                datos = r.read(self.opts.max_bytes + 1)[:self.opts.max_bytes]
                tipo = (r.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        except (urllib.error.URLError, urllib.error.HTTPError, ssl.SSLError,
                OSError, ValueError) as ex:
            self._log(f"No se pudo descargar la imagen {url}: {ex}")
            return ""

        # Los pixeles de seguimiento y los iconos de 1x1 no son contenido
        if len(datos) < self.opts.min_bytes_imagen:
            return ""

        extension = _EXTENSIONES.get(tipo, "")
        if not extension:
            sufijo = os.path.splitext(urlparse(url).path)[1].lstrip(".").lower()
            extension = sufijo if sufijo in set(_EXTENSIONES.values()) else ""
        if not extension:
            self._log(f"Tipo de imagen no reconocido ({tipo or 'sin Content-Type'}): {url}")
            return ""

        # Deduplicacion por contenido: el logotipo repetido no se guarda dos veces
        digest = hashlib.sha1(datos).hexdigest()
        if self.opts.dedupe_imagenes and digest in self.cache:
            self.reutilizadas += 1
            self.por_url[url] = self.cache[digest]
            return self.cache[digest]

        self.contador += 1
        nombre_base = slugify(alt or os.path.splitext(os.path.basename(urlparse(url).path))[0],
                              por_defecto="imagen", max_len=40)
        nombre = f"img_{self.contador:03d}_{nombre_base}.{extension}"
        try:
            os.makedirs(self.dir_abs, exist_ok=True)
            with open(os.path.join(self.dir_abs, nombre), "wb") as f:
                f.write(datos)
        except OSError as ex:
            self._log(f"No se pudo guardar la imagen {nombre}: {ex}")
            return ""

        rel = f"{self.dir_rel}/{nombre}"
        self.cache[digest] = rel
        self.por_url[url] = rel
        self.guardadas += 1
        return rel


def convertir_respuesta(respuesta: Respuesta, output_md: str,
                        options: Optional[WebOptions] = None,
                        log_cb: Optional[Callable[[str], None]] = None
                        ) -> Tuple[str, WebMetadata]:
    """Convierte un HTML ya obtenido. Separado para poder probarlo sin red."""
    from htmlmd.render import clasificar_contenido

    opts = options or WebOptions()

    descargador = None
    if opts.exportar_imagenes:
        descargador = _DescargadorDeImagenes(output_md, opts, log_cb)

    paginas, metadatos = extraer(respuesta, opts, guardar_imagen=descargador)
    if opts.seccion and not metadatos.seccion:
        metadatos.seccion = opts.seccion
    clasificar_contenido(metadatos, opts)

    if log_cb:
        log_cb(f"Cuerpo localizado por: {metadatos.criterio_cuerpo}; "
               f"{metadatos.bloques} bloques, {metadatos.chars_extraidos} caracteres, "
               f"{metadatos.enlaces} enlaces.")
        if metadatos.contenido != "completo":
            log_cb(f"AVISO: contenido {metadatos.contenido}. Revisar contra el original.")
        if descargador and descargador.reutilizadas:
            log_cb("Imagenes repetidas reutilizadas sin duplicar fichero: "
                   f"{descargador.reutilizadas}")

    markdown = render_web(paginas, output_md, opts, metadatos, log_cb)
    return markdown, metadatos


def html_to_markdown(origen: str, output_md: str,
                     options: Optional[WebOptions] = None,
                     log_cb: Optional[Callable[[str], None]] = None,
                     url_origen: str = "",
                     escribir: bool = True) -> Tuple[str, WebMetadata]:
    """
    Convierte una URL o un .html local a Markdown y lo escribe en `output_md`.

    Devuelve (markdown, metadatos). Lanza `ErrorDeDescarga` si la pagina no se
    pudo obtener; cualquier otro fallo produce un documento con su aviso, nunca
    un fichero vacio en silencio.
    """
    opts = options or WebOptions()
    if log_cb:
        log_cb(f"Obteniendo: {origen}")

    respuesta = obtener(origen, opts, url_origen=url_origen)

    tipo = (respuesta.content_type or "").lower()
    if tipo and "html" not in tipo and "xml" not in tipo and "text/plain" not in tipo:
        raise ErrorDeDescarga(
            f"{origen} no devolvio HTML sino {tipo!r}. "
            "Para un PDF usa el conversor pdfmd de KIWI."
        )

    markdown, metadatos = convertir_respuesta(respuesta, output_md, opts, log_cb)

    if escribir:
        destino = os.path.abspath(output_md)
        os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
        with open(destino, "w", encoding="utf-8", newline="\n") as f:
            f.write(markdown)
        if log_cb:
            log_cb(f"Escrito: {destino}")

    return markdown, metadatos


def nombre_de_salida(origen: str, metadata: Optional[WebMetadata] = None,
                     usar_slug: bool = True) -> str:
    """
    Propone un nombre de fichero .md a partir del titulo o de la URL.

    Se prefiere el titulo del documento sobre el ultimo tramo de la URL porque
    muchas rutas terminan en un identificador opaco ("/2026/08/31/1234567.html").
    """
    if metadata is not None and metadata.titulo:
        base = metadata.titulo
    else:
        ruta = urlparse(origen).path if "://" in origen else origen
        base = os.path.splitext(os.path.basename(ruta.rstrip("/")))[0] or "pagina"
    return (slugify(base, por_defecto="pagina") if usar_slug else base) + ".md"

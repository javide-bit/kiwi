# -*- coding: utf-8 -*-
"""
htmlmd.render — Frontmatter propio de la web y ensamblado del documento.

El cuerpo lo renderiza `pdfmd.render.render_document` sin modificaciones: la
unica pieza que htmlmd necesita escribir es el frontmatter, porque el del PDF
habla de paginas, de OCR y de `sha256_pdf`, y ninguno de esos campos significa
nada para una pagina web. Reutilizar aquel produciria metadatos que mienten,
que es peor que no tener metadatos.
"""

import os
from typing import Callable, List, Optional

from htmlmd.models import WebMetadata, WebOptions
from pdfmd.models import PageText
from pdfmd.render import render_document, yaml_lista, yaml_quote


AVISO_CONTENIDO_ESCASO = (
    "> **AVISO DE CONVERSION:** de esta pagina se ha extraido muy poco texto. Suele\n"
    "> significar que el contenido se pinta con JavaScript, que hay un muro de pago\n"
    "> o que se ha servido un aviso de cookies en lugar del articulo. Verificar\n"
    "> contra el original antes de darla por convertida."
)


def generar_frontmatter(metadata: Optional[WebMetadata]) -> str:
    """Bloque YAML con la procedencia y el diagnostico de la captura."""
    if metadata is None:
        return ""

    lineas = ["---", f"title: {yaml_quote(metadata.titulo or 'Sin titulo')}"]

    for etiqueta, valor in (
        ("author", metadata.autor),
        ("description", metadata.descripcion),
        ("keywords", metadata.keywords),
        ("sitio", metadata.sitio),
        ("idioma", metadata.idioma),
        ("seccion", metadata.seccion),
    ):
        if valor:
            lineas.append(f"{etiqueta}: {yaml_quote(valor)}")

    # Procedencia. La URL final puede no ser la solicitada: si hubo
    # redireccion o canonica, el documento convertido es el de la URL final y
    # ocultarlo invalidaria la cita.
    lineas.append(f"url_origen: {yaml_quote(metadata.url_final)}"
                  if metadata.url_final else "url_origen: null")
    if metadata.url_solicitada and metadata.url_solicitada != metadata.url_final:
        lineas.append(f"url_solicitada: {yaml_quote(metadata.url_solicitada)}")
    if metadata.estado_http:
        lineas.append(f"estado_http: {metadata.estado_http}")
    if metadata.fecha_publicacion:
        lineas.append(f"fecha_publicacion: {yaml_quote(metadata.fecha_publicacion)}")
    if metadata.fecha_captura:
        lineas.append(f"fecha_captura: {yaml_quote(metadata.fecha_captura)}")
    if metadata.fecha_conversion:
        lineas.append(f"fecha_conversion: {yaml_quote(metadata.fecha_conversion)}")
    # Huella del HTML tal y como vino por el cable: es lo que permite demostrar
    # dentro de anos que este .md corresponde a aquella captura y no a otra.
    lineas.append(f"sha256_html: {yaml_quote(metadata.sha256_html)}"
                  if metadata.sha256_html else "sha256_html: null")

    # Diagnostico: permite decidir de que ficheros fiarse sin volver a abrir la web
    lineas.append(f"chars_extraidos: {metadata.chars_extraidos}")
    lineas.append(f"bloques: {metadata.bloques}")
    lineas.append(f"enlaces: {metadata.enlaces}")
    lineas.append(f"imagenes: {metadata.imagenes}")
    lineas.append(f"contenido: {yaml_quote(metadata.contenido)}")
    lineas.append(f"criterio_cuerpo: {yaml_quote(metadata.criterio_cuerpo)}")
    if metadata.elementos_suprimidos:
        lineas.append(f"elementos_suprimidos: {yaml_lista(metadata.elementos_suprimidos)}")
    if metadata.avisos:
        lineas.append(f"avisos: {yaml_lista(metadata.avisos)}")
    lineas.append('generator: "KIWI (htmlmd)"')
    lineas.append("---\n")
    return "\n".join(lineas)


def clasificar_contenido(metadata: WebMetadata, opts: WebOptions) -> None:
    """
    Etiqueta la calidad de la captura y deja constancia del aviso.

    Es el equivalente web de `capa_texto` en el conversor de PDF: alli el modo
    de fallo silencioso es el documento escaneado; aqui, la pagina que solo
    existe despues de ejecutar JavaScript. En ambos casos lo inaceptable es que
    el fichero salga vacio sin que nada lo advierta.
    """
    if metadata.chars_extraidos == 0:
        metadata.contenido = "ausente"
    elif metadata.chars_extraidos < opts.min_chars_contenido:
        metadata.contenido = "escaso"
    else:
        metadata.contenido = "completo"

    if metadata.contenido != "completo":
        aviso = (f"contenido {metadata.contenido}: "
                 f"{metadata.chars_extraidos} caracteres extraidos")
        if aviso not in metadata.avisos:
            metadata.avisos.append(aviso)


def render_web(pages: List[PageText], output_md_path: str, opts: WebOptions,
               metadata: Optional[WebMetadata] = None,
               log_cb: Optional[Callable[[str], None]] = None) -> str:
    """Ensambla el documento final: aviso (si procede) + cuerpo + frontmatter."""
    cuerpo = render_document(
        pages=pages,
        output_md_path=output_md_path,
        options=opts.opciones_render(),
        metadata=None,          # el frontmatter lo escribe este modulo
        log_cb=log_cb,
    ).strip()

    if (metadata is not None and opts.avisar_contenido_escaso
            and metadata.contenido != "completo"):
        # Delante del todo: el frontmatter se lee si se busca, esto se ve al abrir
        cuerpo = f"{AVISO_CONTENIDO_ESCASO}\n\n{cuerpo}".strip()

    if not opts.incluir_frontmatter:
        return cuerpo + "\n"

    fm = generar_frontmatter(metadata)
    return f"{fm}\n\n{cuerpo}\n" if fm else cuerpo + "\n"


def ruta_de_assets(output_md_path: str):
    """Devuelve (directorio_absoluto, nombre_relativo) de la carpeta de imagenes."""
    out_dir = os.path.dirname(os.path.abspath(output_md_path))
    base = os.path.splitext(os.path.basename(output_md_path))[0]
    rel = f"{base}_assets"
    return os.path.join(out_dir, rel), rel

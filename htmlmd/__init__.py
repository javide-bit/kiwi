# -*- coding: utf-8 -*-
"""
htmlmd — Conversor de paginas web a Markdown estructurado, hermano de `pdfmd`.

Comparte con el conversor de PDF el modelo de bloques y el renderizador, de modo
que un .md salido de una web y otro salido de un PDF son el mismo formato: mismo
frontmatter de procedencia, mismas tablas, mismos enlaces conservados.
Solo cambia la fase de extraccion.

Sin dependencias externas: todo el modulo se apoya en la biblioteca estandar.
"""

from htmlmd.batch import convertir_lote, escribir_manifiesto, leer_lista
from htmlmd.models import WebMetadata, WebOptions
from htmlmd.pipeline import html_to_markdown, nombre_de_salida

__version__ = "0.1.0"

__all__ = [
    "html_to_markdown",
    "convertir_lote",
    "escribir_manifiesto",
    "leer_lista",
    "nombre_de_salida",
    "WebOptions",
    "WebMetadata",
    "__version__",
]

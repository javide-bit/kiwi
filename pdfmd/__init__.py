# -*- coding: utf-8 -*-
"""
pdfmd — Conversor inteligente de PDF a Markdown estructurado con soporte local y offline.
"""

from pdfmd.models import Options
from pdfmd.batch import convertir_lote, escribir_manifiesto
from pdfmd.pipeline import pdf_to_markdown

__version__ = "1.9.0"

__all__ = [
    "pdf_to_markdown",
    "convertir_lote",
    "escribir_manifiesto",
    "Options",
    "__version__"
]

# -*- coding: utf-8 -*-
"""
recolector_pdf.py — Motor de recolección de PDFs de KIWI.
Mantiene compatibilidad total por línea de comandos y puente hacia la interfaz KIWI.

Uso:
    python recolector_pdf.py <carpeta_origen> <carpeta_destino> [opciones]
    O simplemente ejecuta sin argumentos para abrir la interfaz visual KIWI.
"""

import sys
from kiwi_app import main

if __name__ == "__main__":
    sys.exit(main())

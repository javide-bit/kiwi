# -*- coding: utf-8 -*-
"""
Ejecuta toda la batería de pruebas de KIWI y devuelve un código de salida
distinto de cero si alguna falla (apto para lanzarlo desde un .bat o CI).

    python run_tests.py
"""

import sys
import traceback

SUITES = [
    ("Recolector KIWI", "test_kiwi", "test_engine"),
    ("Pipeline pdfmd", "test_pdfmd", "test_pdfmd_pipeline"),
    ("Unidades", "test_unidades", "run_all"),
    ("Notas y citabilidad", "test_notas", "run_all"),
    ("Mejoras de conversión", "test_mejoras", "run_all"),
    ("Web a Markdown (htmlmd)", "test_htmlmd", "run_all"),
    ("Integración del conversor web", "test_integracion_web", "run_all"),
]


def main():
    fallos = []
    for titulo, modulo, funcion in SUITES:
        print(f"\n{'=' * 60}\n  {titulo}  ({modulo}.py)\n{'=' * 60}")
        try:
            mod = __import__(modulo)
            getattr(mod, funcion)()
        except Exception:
            fallos.append(titulo)
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    if fallos:
        print(f"  FALLARON {len(fallos)} de {len(SUITES)} suites: {', '.join(fallos)}")
        return 1
    print(f"  LAS {len(SUITES)} SUITES DE PRUEBAS PASARON CON EXITO")
    return 0


if __name__ == "__main__":
    sys.exit(main())

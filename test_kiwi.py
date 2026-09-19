# -*- coding: utf-8 -*-
"""
Script de verificación y pruebas automatizadas de KIWI.
"""
import os
import shutil
import tempfile
import csv
from kiwi_app import KiwiRunner, main

def create_synthetic_tree(base_dir):
    src = os.path.join(base_dir, "origen")
    dest = os.path.join(base_dir, "destino")
    os.makedirs(src, exist_ok=True)
    os.makedirs(dest, exist_ok=True)

    # 1. Subcarpeta 1 con un PDF válido
    sub1 = os.path.join(src, "Carpeta A")
    os.makedirs(sub1, exist_ok=True)
    with open(os.path.join(sub1, "documento.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\nContenido unico A\n%%EOF")

    # 2. Subcarpeta 2 con homonimo (mismo nombre 'documento.pdf', contenido distinto)
    sub2 = os.path.join(src, "Carpeta B")
    os.makedirs(sub2, exist_ok=True)
    with open(os.path.join(sub2, "documento.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\nContenido unico B pero mas largo\n%%EOF")

    # 3. Subcarpeta 3 con duplicado exacto del PDF de Carpeta A
    sub3 = os.path.join(src, "Carpeta C")
    os.makedirs(sub3, exist_ok=True)
    with open(os.path.join(sub3, "copia_doc.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\nContenido unico A\n%%EOF")

    # 4. Archivo que no es PDF real
    with open(os.path.join(src, "falso.pdf"), "wb") as f:
        f.write(b"NOT A REAL PDF FILE")

    # 5. Archivo vacío
    with open(os.path.join(src, "vacio.pdf"), "wb") as f:
        pass

    # 6. Carpeta excluida
    sub_excl = os.path.join(src, "backups")
    os.makedirs(sub_excl, exist_ok=True)
    with open(os.path.join(sub_excl, "antiguo.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\nExcluido\n%%EOF")

    return src, dest

def test_engine():
    temp_dir = tempfile.mkdtemp(prefix="kiwi_test_")
    print(f"Directorio temporal de prueba: {temp_dir}")
    try:
        src, dest = create_synthetic_tree(temp_dir)
        
        events = []
        def cb(event, data):
            events.append((event, data))

        # Test 1: Modo Simulacro
        print("\n--- TEST 1: Modo Simulacro ---")
        runner_sim = KiwiRunner(
            origen=src,
            destino=dest,
            simulacro=True,
            dedup=True,
            validar_cabecera=True,
            replicar=False,
            exclusiones=["backups"],
            progress_cb=cb
        )
        runner_sim.run()

        finished_events = [data for ev, data in events if ev == "finished"]
        assert len(finished_events) == 1, "Debe finalizar con evento 'finished'"
        stats_sim = finished_events[0]
        print(f"Stats simulacro: {stats_sim}")

        assert stats_sim["encontrados"] == 3, f"Esperados 3 PDFs válidos, encontrados {stats_sim['encontrados']}"
        assert stats_sim["duplicados"] == 1, f"Esperado 1 duplicado, obtenido {stats_sim['duplicados']}"
        assert stats_sim["copiados"] == 2, f"Esperados 2 simulados, obtenido {stats_sim['copiados']}"
        assert stats_sim["incidencias"] == 2, f"Esperadas 2 incidencias (vacio y no-pdf), obtenido {stats_sim['incidencias']}"
        assert os.path.exists(stats_sim["informe"]), "Debe generarse el informe CSV"

        # En simulacro no se copia ningún PDF (solo se deja el informe junto al destino)
        dest_pdfs = [f for f in os.listdir(dest) if f.lower().endswith(".pdf")]
        assert len(dest_pdfs) == 0, f"En simulacro no debe copiarse ningún PDF, pero hay: {dest_pdfs}"
        assert os.path.dirname(stats_sim["informe"]) == dest, \
            "El informe de simulacro debe guardarse junto al destino, no en el directorio de trabajo"
        print("  -> PASSED: Modo Simulacro verificado con éxito.")

        # Test 2: Modo Copia Real
        print("\n--- TEST 2: Modo Copia Real ---")
        events.clear()
        runner_real = KiwiRunner(
            origen=src,
            destino=dest,
            simulacro=False,
            dedup=True,
            validar_cabecera=True,
            replicar=False,
            exclusiones=["backups"],
            progress_cb=cb
        )
        runner_real.run()

        stats_real = [data for ev, data in events if ev == "finished"][0]
        print(f"Stats copia real: {stats_real}")
        assert stats_real["copiados"] == 2, f"Esperados 2 copiados, obtenido {stats_real['copiados']}"
        
        dest_files_real = [f for f in os.listdir(dest) if f.endswith(".pdf")]
        print(f"Archivos copiados en destino: {dest_files_real}")
        assert len(dest_files_real) == 2, f"Deben haber 2 archivos PDF en destino, hay {len(dest_files_real)}"
        # Nombres aplanados con prefijo
        assert any("Carpeta A__documento.pdf" in f for f in dest_files_real)
        assert any("Carpeta B__documento.pdf" in f for f in dest_files_real)
        print("  -> PASSED: Modo Copia Real verificado con éxito.")

        # Test 3: Reejecución (Idempotencia / YA_EXISTE)
        print("\n--- TEST 3: Reejecución (Idempotencia) ---")
        events.clear()
        runner_re = KiwiRunner(
            origen=src,
            destino=dest,
            simulacro=False,
            dedup=True,
            validar_cabecera=True,
            replicar=False,
            exclusiones=["backups"],
            progress_cb=cb
        )
        runner_re.run()

        stats_re = [data for ev, data in events if ev == "finished"][0]
        print(f"Stats reejecución: {stats_re}")
        assert stats_re["copiados"] == 0, f"No debe copiar nada nuevo, copió {stats_re['copiados']}"
        assert stats_re["ya_estaban"] == 2, f"Deben marcarse 2 como ya_estaban, marcado {stats_re['ya_estaban']}"
        print("  -> PASSED: Idempotencia verificada con éxito.")

        print("\n==========================================")
        print(" TODAS LAS PRUEBAS DE KIWI PASARON CON ÉXITO")
        print("==========================================")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    test_engine()

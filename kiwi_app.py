# -*- coding: utf-8 -*-
"""
KIWI — Suite Inteligente de Recolección, Deduplicación y Conversión de PDFs a Markdown.
Integra motor de recolección segura y motor pdfmd para estructuración en Markdown con visor integrado.
"""

import argparse
import concurrent.futures
import csv
import ctypes
import dataclasses
import getpass
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# GUI imports
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False
    TkinterDnD = None
    DND_FILES = None

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Importar motor pdfmd integrado
from pdfmd.models import Options as PdfmdOptions
from pdfmd.pipeline import pdf_to_markdown
from pdfmd.batch import (
    convertir_lote,
    escribir_manifiesto,
    fila_de_manifiesto,
    ya_convertido,
)
from pdfmd.utils import (
    chequear_dependencias_ocr,
    diagnosticar_entorno_ocr,
    is_ocrmypdf_available,
    is_tesseract_available,
    sha256_fichero,
    slugify,
    version_motor_ocr,
)

# Importar motor htmlmd integrado (web -> Markdown). El import aqui, ademas de
# dar acceso al conversor, es lo que hace que PyInstaller incluya el paquete en
# el ejecutable: su analisis de dependencias es estatico.
from htmlmd.models import WebOptions
from htmlmd.pipeline import html_to_markdown, nombre_de_salida
from htmlmd.batch import (
    convertir_lote as convertir_lote_web,
    leer_lista,
    leer_lista_estructurada,
)
from htmlmd.fetch import ErrorDeDescarga, es_url

BLOQUE = 1024 * 1024  # 1 MB por lectura al hashear
CONFIG_PATH = os.path.join(Path.home(), ".kiwi_config.json")
MAX_LINEAS_LOG = 2000  # Tope de líneas visibles en el panel de actividad


# --------------------------------------------------------------------------
# Rutas Largas en Windows (>260 caracteres)
# --------------------------------------------------------------------------
def ruta_larga(ruta):
    r"""Prefija \\?\ en Windows para superar el límite MAX_PATH de 260 caracteres."""
    if os.name != "nt":
        return ruta
    ruta = os.path.abspath(ruta)
    if ruta.startswith("\\\\?\\"):
        return ruta
    if ruta.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ruta[2:]
    return "\\\\?\\" + ruta


def recurso(*partes):
    """
    Resuelve un recurso empaquetado (logotipo, icono).
    Al ejecutarse desde el .exe de PyInstaller los datos viven en sys._MEIPASS;
    en ejecución normal, junto al script.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *partes)


def ruta_visible(ruta):
    r"""Elimina el prefijo \\?\ para mostrar rutas legibles en logs e informes."""
    if not ruta:
        return ruta
    if ruta.startswith("\\\\?\\UNC\\"):
        return "\\\\" + ruta[8:]
    if ruta.startswith("\\\\?\\"):
        return ruta[4:]
    return ruta


def abrir_en_sistema(ruta):
    """Abre un fichero o carpeta con la aplicación predeterminada del sistema."""
    if not ruta or not os.path.exists(ruta):
        return
    if os.name == "nt":
        os.startfile(ruta)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", ruta])
    else:
        subprocess.Popen(["xdg-open", ruta])


def hash_fichero(ruta, cancel_event=None):
    h = hashlib.sha256()
    with open(ruta_larga(ruta), "rb") as f:
        for bloque in iter(lambda: f.read(BLOQUE), b""):
            if cancel_event and cancel_event.is_set():
                return None
            h.update(bloque)
    return h.hexdigest()


def es_pdf_real(ruta):
    try:
        with open(ruta_larga(ruta), "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _mismo_contenido(a, b):
    try:
        if os.path.getsize(ruta_larga(a)) != os.path.getsize(ruta_larga(b)):
            return False
        return hash_fichero(a) == hash_fichero(b)
    except OSError:
        return False


def nombre_destino(ruta_origen, origen_base, destino, replicar, ocupados):
    if replicar:
        rel = os.path.relpath(ruta_visible(ruta_origen), ruta_visible(origen_base))
        candidato = os.path.join(destino, rel)
        os.makedirs(os.path.dirname(ruta_larga(candidato)), exist_ok=True)
        if os.path.exists(ruta_larga(candidato)) and _mismo_contenido(ruta_origen, candidato):
            return candidato, True
        return candidato, False

    carpeta = os.path.basename(os.path.dirname(ruta_origen))
    base = os.path.basename(ruta_origen)
    raiz_nom, ext = os.path.splitext(base)
    limpio = "".join(c for c in carpeta if c not in '<>:"/\\|?*').strip()[:60]
    propuesta = f"{limpio}__{raiz_nom}{ext}" if limpio else base

    candidato = os.path.join(destino, propuesta)
    n = 2
    while True:
        if candidato.lower() in ocupados:
            pass
        elif not os.path.exists(ruta_larga(candidato)):
            break
        elif _mismo_contenido(ruta_origen, candidato):
            ocupados.add(candidato.lower())
            return candidato, True
        candidato = os.path.join(destino, f"{limpio}__{raiz_nom}_{n}{ext}")
        n += 1
    ocupados.add(candidato.lower())
    return candidato, False


# --------------------------------------------------------------------------
# Motor del Recolector KIWI
# --------------------------------------------------------------------------
class KiwiRunner:
    def __init__(self, origen, destino, simulacro=True, dedup=True,
                 validar_cabecera=True, replicar=False, exclusiones=None,
                 cancel_event=None, progress_cb=None):
        self.origen = os.path.abspath(origen)
        self.destino = os.path.abspath(destino)
        self.simulacro = simulacro
        self.dedup = dedup
        self.validar_cabecera = validar_cabecera
        self.replicar = replicar
        self.exclusiones = exclusiones or []
        self.cancel_event = cancel_event or threading.Event()
        self.progress_cb = progress_cb or (lambda event, data: None)

    def log(self, text, tag="info"):
        self.progress_cb("log", {"text": text, "tag": tag})

    def _dir_para_informe(self):
        """
        El informe acompaña a los archivos: en copia real va al destino y en
        simulacro a la carpeta destino si ya existe (o a su padre), evitando
        ensuciar el directorio de trabajo desde el que se lanzó KIWI.
        """
        candidatos = [self.destino]
        if self.simulacro:
            candidatos = [self.destino, os.path.dirname(self.destino), os.getcwd()]
        for cand in candidatos:
            if not cand:
                continue
            try:
                if self.simulacro and not os.path.isdir(ruta_larga(cand)):
                    continue
                os.makedirs(ruta_larga(cand), exist_ok=True)
                return cand
            except OSError:
                continue
        return os.getcwd()

    def run(self):
        try:
            if not os.path.isdir(self.origen):
                raise ValueError(f"La carpeta origen no existe: {self.origen}")

            if self.destino.lower().startswith(self.origen.lower() + os.sep):
                raise ValueError("La carpeta destino está DENTRO de origen. Se crearía un bucle.")

            # Fase 1: Inventario
            self.progress_cb("phase", {"phase": 1, "title": "Escaneando carpetas de origen..."})
            self.log(f"Iniciando escaneo en: {self.origen}")
            encontrados = []
            incidencias = []
            excluir_norm = {e.strip().lower() for e in self.exclusiones if e.strip()}

            for raiz, dirs, ficheros in os.walk(ruta_larga(self.origen), onerror=incidencias.append):
                if self.cancel_event.is_set():
                    self.log("Operación cancelada por el usuario.", "warning")
                    self.progress_cb("cancelled", {})
                    return None

                dirs[:] = [d for d in dirs if d.lower() not in excluir_norm]

                for nombre in ficheros:
                    if not nombre.lower().endswith(".pdf"):
                        continue
                    # El recorrido usa rutas extendidas; hacia fuera se manejan
                    # siempre rutas legibles y se reexpanden al tocar el disco.
                    completa = ruta_visible(os.path.join(raiz, nombre))
                    try:
                        tam = os.path.getsize(ruta_larga(completa))
                    except OSError as e:
                        incidencias.append(("SIN_ACCESO", completa, str(e)))
                        continue
                    if tam == 0:
                        incidencias.append(("VACIO", completa, "0 bytes"))
                        continue
                    if self.validar_cabecera and not es_pdf_real(completa):
                        incidencias.append(("NO_ES_PDF", completa, "sin firma %PDF-"))
                        continue
                    encontrados.append((completa, tam))

                if len(encontrados) % 200 == 0:
                    self.progress_cb("progress_scan", {"count": len(encontrados)})

            self.progress_cb("progress_scan", {"count": len(encontrados)})
            self.log(f"Escaneo finalizado: {len(encontrados)} PDFs localizados, {len(incidencias)} incidencias descartadas.")

            if not encontrados:
                self.log("No se encontraron archivos PDF válidos para procesar.", "warning")
                self.progress_cb("finished", {
                    "encontrados": 0, "duplicados": 0, "ya_estaban": 0,
                    "copiados": 0, "fallos": 0, "incidencias": len(incidencias),
                    "informe": None, "simulacro": self.simulacro,
                    "plan": []
                })
                return

            # Fase 2: Deduplicación
            hashes = {}
            if self.dedup:
                self.progress_cb("phase", {"phase": 2, "title": "Buscando duplicados por contenido (SHA-256)..."})
                por_tam = defaultdict(list)
                for ruta, tam in encontrados:
                    por_tam[tam].append(ruta)
                candidatos = [r for grupo in por_tam.values() if len(grupo) > 1 for r in grupo]
                self.log(f"{len(candidatos)} archivos comparten tamaño idéntico: calculando huella SHA-256...")

                for i, ruta in enumerate(candidatos, 1):
                    if self.cancel_event.is_set():
                        self.log("Operación cancelada por el usuario.", "warning")
                        self.progress_cb("cancelled", {})
                        return None
                    try:
                        hashes[ruta] = hash_fichero(ruta, self.cancel_event)
                    except OSError as e:
                        hashes[ruta] = None
                        self.log(f"No se pudo leer {os.path.basename(ruta)}: {e}", "warning")
                    if i % 100 == 0 or i == len(candidatos):
                        self.progress_cb("progress_dedup", {"current": i, "total": len(candidatos)})
            else:
                self.log("Deduplicación desactivada: se procesarán todos los archivos.")

            # Fase 3: Planificación de nombres
            self.progress_cb("phase", {"phase": 3, "title": "Planificando rutas de destino sin colisiones..."})
            if not self.simulacro:
                os.makedirs(ruta_larga(self.destino), exist_ok=True)

            ocupados, vistos_hash, plan = set(), {}, []
            for ruta, tam in sorted(encontrados):
                if self.cancel_event.is_set():
                    self.log("Operación cancelada.", "warning")
                    self.progress_cb("cancelled", {})
                    return None
                h = hashes.get(ruta)
                if h and h in vistos_hash:
                    plan.append((ruta, "", tam, "DUPLICADO", vistos_hash[h]))
                    continue
                dest, ya = nombre_destino(ruta, self.origen, self.destino, self.replicar, ocupados)
                if h:
                    vistos_hash[h] = dest
                plan.append((ruta, dest, tam, "YA_EXISTE" if ya else "PENDIENTE", ""))

            a_copiar = [f for f in plan if f[3] == "PENDIENTE"]
            duplicados = sum(1 for f in plan if f[3] == "DUPLICADO")
            ya_estaban = sum(1 for f in plan if f[3] == "YA_EXISTE")
            mb_total = sum(f[2] for f in a_copiar) / (1024 * 1024)

            self.log(f"Plan: {len(a_copiar)} a procesar ({mb_total:.1f} MB), {duplicados} duplicados omitidos, {ya_estaban} ya en destino.")

            # Fase 4: Copia / Simulación
            fase_titulo = "Simulando recolección..." if self.simulacro else "Copiando archivos al destino..."
            self.progress_cb("phase", {"phase": 4, "title": fase_titulo})
            
            resultado = []
            copiados = fallos = 0
            total_plan = len(plan)

            for idx, (origen_f, dest_f, tam, estado, nota) in enumerate(plan, 1):
                if self.cancel_event.is_set():
                    self.log("Operación cancelada durante la copia.", "warning")
                    self.progress_cb("cancelled", {})
                    return None

                if estado == "DUPLICADO":
                    resultado.append((origen_f, "", tam, "DUPLICADO", f"identico a {nota}"))
                elif estado == "YA_EXISTE":
                    resultado.append((origen_f, dest_f, tam, "YA_EXISTE", "presente de una pasada anterior"))
                elif self.simulacro:
                    resultado.append((origen_f, dest_f, tam, "SIMULADO", ""))
                    copiados += 1
                else:
                    try:
                        dest_dir = os.path.dirname(ruta_larga(dest_f))
                        if dest_dir:
                            os.makedirs(dest_dir, exist_ok=True)
                        shutil.copy2(ruta_larga(origen_f), ruta_larga(dest_f))
                        copiados += 1
                        resultado.append((origen_f, dest_f, tam, "COPIADO", ""))
                    except OSError as e:
                        fallos += 1
                        resultado.append((origen_f, dest_f, tam, "ERROR", str(e)))
                        self.log(f"Error copiando {os.path.basename(origen_f)}: {e}", "error")

                if idx % 50 == 0 or idx == total_plan:
                    self.progress_cb("progress_copy", {
                        "current": idx, "total": total_plan,
                        "copiados": copiados, "fallos": fallos
                    })

            # Generar informe CSV
            marca = datetime.now().strftime("%Y%m%d_%H%M%S")
            sufijo = "simulacro" if self.simulacro else "copia"
            dir_informe = self._dir_para_informe()

            informe_path = os.path.join(dir_informe, f"KIWI_informe_{sufijo}_{marca}.csv")
            with open(ruta_larga(informe_path), "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["origen", "destino", "bytes", "estado", "observaciones"])
                w.writerows(
                    (ruta_visible(orig), ruta_visible(dst), tam, estado, obs)
                    for orig, dst, tam, estado, obs in resultado
                )
                for inc in incidencias:
                    w.writerow([ruta_visible(inc[1]) if isinstance(inc, tuple) else str(inc), "", "",
                                inc[0] if isinstance(inc, tuple) else "ERROR_RECORRIDO",
                                inc[2] if isinstance(inc, tuple) else ""])

            self.log(f"Informe guardado en: {informe_path}", "success")
            
            stats = {
                "encontrados": len(encontrados),
                "duplicados": duplicados,
                "ya_estaban": ya_estaban,
                "copiados": copiados,
                "fallos": fallos,
                "incidencias": len(incidencias),
                "informe": informe_path,
                "simulacro": self.simulacro,
                "destino": self.destino,
                "plan": plan
            }
            self.progress_cb("finished", stats)

        except Exception as ex:
            self.log(f"Error crítico: {str(ex)}", "error")
            self.progress_cb("error", {"message": str(ex)})


# --------------------------------------------------------------------------
# Motor de Conversión Batch a Markdown (KIWI + pdfmd)
# --------------------------------------------------------------------------
class KiwiConverterRunner:
    """
    Convierte una lista de PDFs a Markdown.

    Cada elemento de `items` puede ser una ruta de PDF (el nombre de salida se
    deriva del PDF, garantizando unicidad) o una tupla (pdf, ruta_md_destino)
    cuando el llamante decide el nombre exacto del fichero generado.
    """

    def __init__(self, items, output_dir, options=None, cancel_event=None, progress_cb=None,
                 pdf_password=None, usar_slug=True, reanudar=True,
                 manifiesto="manifiesto_conversion.tsv", max_workers=1):
        self.items = items
        self.usar_slug = usar_slug
        # Una conversion de cientos de documentos no deberia reiniciarse desde
        # cero porque falle el numero 150.
        self.reanudar = reanudar
        self.manifiesto = manifiesto
        self.output_dir = os.path.abspath(output_dir)
        self.options = options or PdfmdOptions()
        self.cancel_event = cancel_event or threading.Event()
        self.progress_cb = progress_cb or (lambda event, data: None)
        self.pdf_password = pdf_password
        self.max_workers = max(1, int(max_workers or 1))

    def log(self, text, tag="info"):
        self.progress_cb("log", {"text": text, "tag": tag})

    def _ruta_salida_unica(self, pdf_path, usados):
        """Evita que dos PDFs con el mismo nombre en subcarpetas distintas se pisen."""
        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
        if self.usar_slug:
            base_name = slugify(base_name)
        candidato = os.path.join(self.output_dir, f"{base_name}.md")
        n = 2
        # Solo se comprueba contra los nombres emitidos en esta misma pasada:
        # así reconvertir la misma carpeta sobrescribe en vez de acumular copias.
        while candidato.lower() in usados:
            candidato = os.path.join(self.output_dir, f"{base_name}_{n}.md")
            n += 1
        usados.add(candidato.lower())
        return candidato

    def run(self):
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            total = len(self.items)
            self.log(f"Iniciando conversión de {total} archivo(s) a Markdown...")
            converted = 0
            errors = 0
            saltados = 0
            completados = 0
            last_md_file = None
            usados = set()
            filas_manifiesto = []
            lock = threading.Lock()

            tareas = []
            for idx, item in enumerate(self.items, 1):
                if isinstance(item, (tuple, list)):
                    pdf_path, out_md = item[0], item[1]
                else:
                    pdf_path = item
                    out_md = self._ruta_salida_unica(pdf_path, usados)
                tareas.append((idx, pdf_path, out_md))
                self.progress_cb("task_update", {
                    "task_id": f"conv_{idx}",
                    "tipo": "PDF -> MD",
                    "origen": os.path.basename(pdf_path),
                    "salida": os.path.basename(out_md),
                    "estado": "⏳ En cola",
                    "detalle": "Pendiente",
                    "out_full": out_md,
                })

            def _procesar_item(idx, pdf_path, out_md):
                nonlocal converted, errors, saltados, last_md_file, completados
                if self.cancel_event.is_set():
                    return

                self.progress_cb("task_update", {
                    "task_id": f"conv_{idx}",
                    "tipo": "PDF -> MD",
                    "origen": os.path.basename(pdf_path),
                    "salida": os.path.basename(out_md),
                    "estado": "⚙️ Procesando",
                    "detalle": "Convirtiendo...",
                    "out_full": out_md,
                })

                # Reanudacion: si ya existe el .md generado desde ESTE mismo PDF
                # (mismo SHA-256), no hay nada que rehacer.
                if self.reanudar and self.options.compute_hash and ya_convertido(
                        out_md, sha256_fichero(pdf_path),
                        rehacer_ausentes=self.options.ocr_mode != "off"):
                    with lock:
                        saltados += 1
                        completados += 1
                        last_md_file = out_md
                        try:
                            with open(out_md, "r", encoding="utf-8") as f_md:
                                filas_manifiesto.append(fila_de_manifiesto(
                                    os.path.basename(out_md), f_md.read(8192), pdf_path))
                        except OSError:
                            pass
                        self.log(f"[{completados}/{total}] Ya convertido, se omite: {os.path.basename(out_md)}")
                        self.progress_cb("phase", {
                            "phase": completados,
                            "title": f"Omitido ({completados}/{total}): {os.path.basename(pdf_path)}"
                        })
                        self.progress_cb("task_update", {
                            "task_id": f"conv_{idx}",
                            "tipo": "PDF -> MD",
                            "origen": os.path.basename(pdf_path),
                            "salida": os.path.basename(out_md),
                            "estado": "⏭️ Omitido",
                            "detalle": "Ya al día (SHA idéntico)",
                            "out_full": out_md,
                        })
                    return

                def doc_log(msg):
                    self.log(f"  [{os.path.basename(pdf_path)}] {msg}")

                def doc_prog(done, p_tot):
                    self.progress_cb("progress_doc", {
                        "current_file": idx,
                        "total_files": total,
                        "page_done": done,
                        "page_total": p_tot,
                        "task_id": f"conv_{idx}",
                    })

                try:
                    md_generado = pdf_to_markdown(
                        input_pdf=pdf_path,
                        output_md=out_md,
                        options=self.options,
                        progress_cb=doc_prog,
                        log_cb=doc_log,
                        pdf_password=self.pdf_password,
                        cancel_event=self.cancel_event
                    )
                    with lock:
                        fila_m = fila_de_manifiesto(os.path.basename(out_md), md_generado, pdf_path)
                        filas_manifiesto.append(fila_m)
                        converted += 1
                        completados += 1
                        last_md_file = out_md
                        capa_m = fila_m.get("capa_texto", "") or "nativa"
                        pags_m = int(fila_m.get("paginas", 0) or 0)
                        enl_m = int(fila_m.get("enlaces", 0) or 0)
                        es_escaso = capa_m in ("ausente", "escasa")
                        estado_ui = "⚠️ Escaso" if es_escaso else "✅ Generado"
                        det_ui = f"{len(md_generado):,} chars · {pags_m}p · [{capa_m}]"
                        self.log(
                            f"[{completados}/{total}] {'AVISO (' + capa_m + ')' if es_escaso else 'Generado'}: "
                            f"{os.path.basename(out_md)} ({det_ui})",
                            "warning" if es_escaso else "success"
                        )
                        self.progress_cb("phase", {
                            "phase": completados,
                            "title": f"Convertido ({completados}/{total}): {os.path.basename(pdf_path)}"
                        })
                        self.progress_cb("task_update", {
                            "task_id": f"conv_{idx}",
                            "tipo": "PDF -> MD",
                            "origen": os.path.basename(pdf_path),
                            "salida": os.path.basename(out_md),
                            "estado": estado_ui,
                            "detalle": det_ui,
                            "out_full": out_md,
                            "pages_delta": pags_m,
                            "elem_delta": enl_m,
                        })
                except InterruptedError:
                    self.log("Conversión cancelada por el usuario.", "warning")
                except PermissionError:
                    with lock:
                        errors += 1
                        completados += 1
                        self.log(f"{os.path.basename(pdf_path)} está protegido con contraseña: omitido.", "warning")
                        self.progress_cb("task_update", {
                            "task_id": f"conv_{idx}",
                            "tipo": "PDF -> MD",
                            "origen": os.path.basename(pdf_path),
                            "salida": os.path.basename(out_md),
                            "estado": "🔒 Protegido",
                            "detalle": "Requiere contraseña",
                            "out_full": out_md,
                        })
                except Exception as ex:
                    with lock:
                        errors += 1
                        completados += 1
                        self.log(f"Error convirtiendo {os.path.basename(pdf_path)}: {ex}", "error")
                        self.progress_cb("task_update", {
                            "task_id": f"conv_{idx}",
                            "tipo": "PDF -> MD",
                            "origen": os.path.basename(pdf_path),
                            "salida": os.path.basename(out_md),
                            "estado": "❌ Error",
                            "detalle": str(ex),
                            "out_full": out_md,
                        })

            if self.max_workers > 1 and total > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = [executor.submit(_procesar_item, idx, pdf_p, out_m)
                               for idx, pdf_p, out_m in tareas]
                    for f in concurrent.futures.as_completed(futures):
                        if self.cancel_event.is_set():
                            break
            else:
                for idx, pdf_p, out_m in tareas:
                    if self.cancel_event.is_set():
                        self.log("Conversión cancelada por el usuario.", "warning")
                        self.progress_cb("cancelled", {})
                        return
                    _procesar_item(idx, pdf_p, out_m)

            # El manifiesto ordena por caracteres por pagina: las primeras filas
            # son los documentos que hay que revisar a mano.
            if self.manifiesto and total > 1 and filas_manifiesto:
                try:
                    ruta_man = escribir_manifiesto(
                        filas_manifiesto, os.path.join(self.output_dir, self.manifiesto))
                    self.log(f"Manifiesto de conversión: {os.path.basename(ruta_man)}", "success")
                except OSError as ex:
                    self.log(f"No se pudo escribir el manifiesto: {ex}", "warning")

            if saltados:
                self.log(f"Documentos ya convertidos que se omitieron: {saltados}")

            # Resumen por capa de texto y lista de los que piden OCR
            capas = {}
            for fila in filas_manifiesto:
                capa = fila.get("capa_texto") or "desconocida"
                capas[capa] = capas.get(capa, 0) + 1
            salida_de = {pdf: md for _i, pdf, md in tareas}
            sin_texto = [(f["entrada"], salida_de.get(f["entrada"]))
                         for f in filas_manifiesto
                         if f.get("capa_texto") in ("ausente", "escasa") and f.get("entrada")]

            self.progress_cb("conv_finished", {
                "total": total,
                "skipped": saltados,
                "converted": converted,
                "errors": errors,
                "output_dir": self.output_dir,
                "last_md": last_md_file,
                "capas": capas,
                "sin_texto": sin_texto,
            })

        except Exception as ex:
            self.log(f"Error en conversor: {ex}", "error")
            self.progress_cb("error", {"message": str(ex)})


class KiwiWebRunner:
    """
    Ejecuta la conversion de paginas web a Markdown en un hilo aparte.

    Emite los mismos eventos que el runner de PDF, de modo que la barra de
    progreso, el log y la cancelacion de la interfaz funcionan sin distinguir
    de que motor viene el trabajo.

    Cuando hay varias URLs se delega en `htmlmd.batch.convertir_lote`, que ya
    resuelve la reanudacion, el manifiesto y la espera entre peticiones; aqui
    solo se traduce su progreso a eventos de la interfaz.
    """

    def __init__(self, items, output_dir, options=None, cancel_event=None,
                 progress_cb=None, salida_fija=None, refrescar=False, pausa=1.0,
                 url_origen="", organizar_por_secciones=True, generar_index=True,
                 max_workers=1, conservar_nombre=False):
        self.items = list(items)
        self.conservar_nombre = conservar_nombre
        self.output_dir = os.path.abspath(output_dir)
        self.options = options or WebOptions()
        self.cancel_event = cancel_event or threading.Event()
        self.progress_cb = progress_cb or (lambda event, data: None)
        # Ruta .md exacta cuando el usuario convierte una sola pagina
        self.salida_fija = salida_fija
        self.refrescar = refrescar
        self.pausa = pausa
        # Procedencia declarada de un .html de disco, que no la trae consigo
        self.url_origen = url_origen
        self.organizar_por_secciones = organizar_por_secciones
        self.generar_index = generar_index
        self.max_workers = max(1, int(max_workers or 1))

    def log(self, text, tag="info"):
        self.progress_cb("log", {"text": text, "tag": tag})

    def _una_sola(self):
        origen = self.items[0]
        destino = self.salida_fija
        self.progress_cb("task_update", {
            "task_id": "web_1",
            "tipo": "Web -> MD",
            "origen": str(origen),
            "salida": os.path.basename(destino) if destino else "",
            "estado": "⚙️ Procesando",
            "detalle": "Descargando...",
            "out_full": destino,
        })
        md, meta = html_to_markdown(origen, destino, self.options, log_cb=self.log,
                                    url_origen=self.url_origen)

        # El diagnostico se sube al log de la interfaz: un .md que sale casi
        # vacio porque la pagina se pinta con JavaScript tiene que verse aqui,
        # no solo en el frontmatter del fichero.
        if meta.contenido != "completo":
            self.log(f"AVISO: contenido {meta.contenido} "
                     f"({meta.chars_extraidos} caracteres). Revisar contra el original.",
                     "warning")
        else:
            self.log(f"Convertida: {meta.chars_extraidos} caracteres, "
                     f"{meta.enlaces} enlaces, {meta.imagenes} imagenes.", "success")

        self.progress_cb("task_update", {
            "task_id": "web_1",
            "tipo": "Web -> MD",
            "origen": str(origen),
            "salida": os.path.basename(destino) if destino else "",
            "estado": "✅ Generado" if meta.contenido == "completo" else "⚠️ Escaso",
            "detalle": f"{meta.chars_extraidos:,} chars",
            "out_full": destino,
        })

        return {
            "total": 1,
            "converted": 1,
            "skipped": 0,
            "errors": 0,
            "output_dir": os.path.dirname(destino) or self.output_dir,
            "last_md": destino,
            "manifiesto": "",
            "escasas": 0 if meta.contenido == "completo" else 1,
        }

    def _en_lote(self):
        def prog(i, total):
            self.progress_cb("progress_copy", {"current": i, "total": total})

        def on_item_event(event, data):
            idx = data.get("index", 1)
            tid = f"web_{idx}"
            if event == "start":
                self.progress_cb("task_update", {
                    "task_id": tid,
                    "tipo": "Web -> MD",
                    "origen": data.get("origen", ""),
                    "salida": data.get("salida", "En proceso"),
                    "estado": "⚙️ Procesando",
                    "detalle": "Descargando...",
                    "out_full": "",
                })
            elif event == "done":
                self.progress_cb("task_update", {
                    "task_id": tid,
                    "tipo": "Web -> MD",
                    "origen": data.get("origen", ""),
                    "salida": data.get("salida", ""),
                    "estado": data.get("estado", ""),
                    "detalle": data.get("detalle", ""),
                    "out_full": data.get("out_full", ""),
                })

        for idx, item in enumerate(self.items, 1):
            origen_str = item[0] if isinstance(item, (tuple, list)) else str(item)
            self.progress_cb("task_update", {
                "task_id": f"web_{idx}",
                "tipo": "Web -> MD",
                "origen": origen_str,
                "salida": "En proceso",
                "estado": "⏳ En cola",
                "detalle": "Pendiente",
                "out_full": "",
            })

        resumen = convertir_lote_web(
            self.items, self.output_dir, self.options,
            log_cb=self.log, progress_cb=prog,
            refrescar=self.refrescar, pausa=self.pausa,
            organizar_por_secciones=self.organizar_por_secciones,
            generar_index=self.generar_index,
            cancel_event=self.cancel_event,
            max_workers=self.max_workers,
            item_cb=on_item_event,
            conservar_nombre=self.conservar_nombre,
        )

        filas = resumen.get("filas", [])
        for idx, fila in enumerate(filas, 1):
            arch = fila.get("archivo", "")
            out_full = os.path.join(self.output_dir, arch) if arch else ""
            cont = fila.get("contenido", "completo")
            if cont == "completo":
                estado = "✅ Generado"
            elif fila.get("motivo") == "ya_convertido":
                estado = "⏭️ Omitido"
            elif cont in ("error", "fallo"):
                estado = "❌ Error"
            else:
                estado = "⚠️ Escaso"

            try:
                c_val = int(fila.get("chars", 0) or 0)
                det_chars = f"{c_val:,} chars"
            except (ValueError, TypeError):
                det_chars = f"{fila.get('chars', 0)} chars"

            self.progress_cb("task_update", {
                "task_id": f"web_{idx}",
                "tipo": "Web -> MD",
                "origen": fila.get("url", ""),
                "salida": arch,
                "estado": estado,
                "detalle": det_chars,
                "out_full": out_full,
            })
        escasas = sum(1 for f in filas if f.get("contenido") not in ("completo", None))
        if escasas:
            self.log(f"Capturas con contenido escaso o fallido: {escasas}. "
                     "Las primeras filas del manifiesto son justo esas.", "warning")

        ultimo = ""
        for fila in filas:
            if fila.get("archivo"):
                ultimo = os.path.join(self.output_dir, fila["archivo"])

        return {
            "total": len(self.items),
            "converted": resumen.get("convertidas", 0),
            "skipped": resumen.get("omitidas", 0),
            "errors": resumen.get("fallidas", 0),
            "output_dir": self.output_dir,
            "last_md": ultimo,
            "manifiesto": resumen.get("manifiesto", ""),
            "escasas": escasas,
        }

    def run(self):
        try:
            if not self.items:
                self.log("No hay ninguna URL que convertir.", "warning")
                self.progress_cb("web_finished", {"total": 0, "converted": 0, "skipped": 0,
                                                  "errors": 0, "output_dir": self.output_dir,
                                                  "last_md": "", "manifiesto": "", "escasas": 0})
                return

            stats = (self._una_sola() if (len(self.items) == 1 and self.salida_fija)
                     else self._en_lote())

            if self.cancel_event.is_set():
                self.log("Conversion web cancelada por el usuario.", "warning")
                self.progress_cb("cancelled", {})
                return

            self.progress_cb("web_finished", stats)

        except ErrorDeDescarga as ex:
            self.log(str(ex), "error")
            self.progress_cb("error", {"message": str(ex)})
        except Exception as ex:
            self.log(f"Error en el conversor web: {ex}", "error")
            self.progress_cb("error", {"message": str(ex)})


# --------------------------------------------------------------------------
# Visor Integrado de Markdown
# --------------------------------------------------------------------------
def _extraer_resumen_markdown(content: str) -> str:
    """Devuelve una cadena compacta con las métricas y metadatos clave del documento Markdown."""
    if not content:
        return "Sin documento cargado"
    chars = len(content)
    palabras = len(content.split())
    titulos = sum(1 for l in content.splitlines() if l.startswith("#"))
    enlaces = content.count("](") + content.count("[[")
    capa = ""
    motor = ""
    if content.startswith("---\n"):
        fin = content.find("\n---\n", 4)
        if fin != -1:
            fm = content[4:fin]
            m_capa = re.search(r'^capa_texto:\s*"?([^"\n]+)"?', fm, re.MULTILINE)
            if m_capa:
                capa = m_capa.group(1).strip()
            m_cont = re.search(r'^contenido:\s*"?([^"\n]+)"?', fm, re.MULTILINE)
            if not capa and m_cont:
                capa = m_cont.group(1).strip()
            m_mot = re.search(r'^ocr_motor:\s*"?([^"\n]+)"?', fm, re.MULTILINE)
            if m_mot:
                motor = m_mot.group(1).strip()
    partes = [f"📏 {chars:,} chars", f"📝 {palabras:,} palabras", f"📑 {titulos} títulos", f"🔗 {enlaces} enlaces"]
    if capa:
        partes.append(f"🔍 capa: {capa}")
    if motor:
        partes.append(f"⚙️ {motor}")
    return "   •   ".join(partes)


def format_markdown_into_text(txt_widget: tk.Text, content: str, dark_mode: bool = False) -> None:
    """Parsea y formatea contenido Markdown con estilos visuales nativos en un tk.Text."""
    txt_widget.config(state="normal")
    txt_widget.delete("1.0", tk.END)

    c_h1 = "#1d5e21" if not dark_mode else "#66bb6a"
    c_h2 = "#2e7d32" if not dark_mode else "#81c784"
    c_h3 = "#333333" if not dark_mode else "#e0e0e0"
    c_h4 = "#4a6b4a" if not dark_mode else "#a5d6a7"
    c_code_bg = "#f0f4f0" if not dark_mode else "#202528"
    c_code_fg = "#1e1e1e" if not dark_mode else "#e0e0e0"
    c_quote_fg = "#2e7d32" if not dark_mode else "#81c784"
    c_call_note_bg = "#e8f4fd" if not dark_mode else "#15283c"
    c_call_note_fg = "#0d47a1" if not dark_mode else "#64b5f6"
    c_call_warn_bg = "#fff8e1" if not dark_mode else "#3b3014"
    c_call_warn_fg = "#f57f17" if not dark_mode else "#ffd54f"
    c_call_tip_bg = "#e8f8f5" if not dark_mode else "#14332b"
    c_call_tip_fg = "#00796b" if not dark_mode else "#4db6ac"
    c_call_danger_bg = "#ffebee" if not dark_mode else "#3d1818"
    c_call_danger_fg = "#c62828" if not dark_mode else "#ef5350"
    c_link_fg = "#1565c0" if not dark_mode else "#64b5f6"
    c_fm_bg = "#f7faf7" if not dark_mode else "#1c2126"

    txt_widget.tag_config("h1", font=("Segoe UI", 16, "bold"), foreground=c_h1, spacing1=8, spacing3=4)
    txt_widget.tag_config("h2", font=("Segoe UI", 13, "bold"), foreground=c_h2, spacing1=6, spacing3=3)
    txt_widget.tag_config("h3", font=("Segoe UI", 11, "bold"), foreground=c_h3, spacing1=4, spacing3=2)
    txt_widget.tag_config("h4", font=("Segoe UI", 10, "bold"), foreground=c_h4, spacing1=3, spacing3=1)
    txt_widget.tag_config("code_block", font=("Consolas", 9), background=c_code_bg, foreground=c_code_fg,
                          lmargin1=16, lmargin2=16, spacing1=2, spacing3=2)
    txt_widget.tag_config("quote", font=("Segoe UI", 9, "italic"), foreground=c_quote_fg,
                          lmargin1=16, lmargin2=16, spacing1=2, spacing3=2)
    txt_widget.tag_config("callout_note", font=("Segoe UI", 9), background=c_call_note_bg,
                          foreground=c_call_note_fg, lmargin1=16, lmargin2=16, spacing1=3, spacing3=3)
    txt_widget.tag_config("callout_warn", font=("Segoe UI", 9), background=c_call_warn_bg,
                          foreground=c_call_warn_fg, lmargin1=16, lmargin2=16, spacing1=3, spacing3=3)
    txt_widget.tag_config("callout_tip", font=("Segoe UI", 9), background=c_call_tip_bg,
                          foreground=c_call_tip_fg, lmargin1=16, lmargin2=16, spacing1=3, spacing3=3)
    txt_widget.tag_config("callout_danger", font=("Segoe UI", 9), background=c_call_danger_bg,
                          foreground=c_call_danger_fg, lmargin1=16, lmargin2=16, spacing1=3, spacing3=3)
    txt_widget.tag_config("table", font=("Consolas", 9), background=c_code_bg, spacing1=1, spacing3=1)
    txt_widget.tag_config("hr", font=("Segoe UI", 4), underline=True, foreground="#888888")
    txt_widget.tag_config("fm", font=("Consolas", 8), background=c_fm_bg,
                          foreground="#607d8b" if not dark_mode else "#90a4ae")
    txt_widget.tag_config("link", font=("Segoe UI", 9, "underline"), foreground=c_link_fg)
    txt_widget.tag_config("list_item", lmargin1=14, lmargin2=26, spacing1=1, spacing3=1)
    txt_widget.tag_config("search_hit", background="#fff176" if not dark_mode else "#f57f17",
                          foreground="#000000")
    txt_widget.tag_config("search_active", background="#ff9800", foreground="#000000")

    lines = content.splitlines()
    in_code = False
    in_fm = False

    for idx, line in enumerate(lines):
        if line.strip() == "---" and (idx == 0 or in_fm):
            if not in_fm and idx == 0:
                in_fm = True
                txt_widget.insert(tk.END, line + "\n", "fm")
                continue
            elif in_fm:
                in_fm = False
                txt_widget.insert(tk.END, line + "\n", "fm")
                continue
        if in_fm:
            txt_widget.insert(tk.END, line + "\n", "fm")
            continue

        if line.strip().startswith("```"):
            in_code = not in_code
            txt_widget.insert(tk.END, line + "\n", "code_block")
            continue
        if in_code:
            txt_widget.insert(tk.END, line + "\n", "code_block")
            continue

        if line.startswith("# "):
            txt_widget.insert(tk.END, line[2:] + "\n", "h1")
        elif line.startswith("## "):
            txt_widget.insert(tk.END, line[3:] + "\n", "h2")
        elif line.startswith("### "):
            txt_widget.insert(tk.END, line[4:] + "\n", "h3")
        elif line.startswith("#### "):
            txt_widget.insert(tk.END, line[5:] + "\n", "h4")
        elif line.strip() in ("---", "***", "___"):
            txt_widget.insert(tk.END, " \n", "hr")
        elif line.strip().startswith("> [!NOTE]") or line.strip().startswith("> [!INFO]"):
            txt_widget.insert(tk.END, line + "\n", "callout_note")
        elif line.strip().startswith("> [!WARN]") or line.strip().startswith("> [!WARNING]"):
            txt_widget.insert(tk.END, line + "\n", "callout_warn")
        elif line.strip().startswith("> [!TIP]"):
            txt_widget.insert(tk.END, line + "\n", "callout_tip")
        elif line.strip().startswith("> [!DANGER]") or line.strip().startswith("> [!ERROR]"):
            txt_widget.insert(tk.END, line + "\n", "callout_danger")
        elif line.strip().startswith(">"):
            txt_widget.insert(tk.END, line + "\n", "quote")
        elif "|" in line and line.strip().startswith("|"):
            txt_widget.insert(tk.END, line + "\n", "table")
        elif "[[" in line or "](" in line:
            txt_widget.insert(tk.END, line + "\n", "link")
        elif line.lstrip().startswith(("- ", "* ")) or re.match(r"^\s*\d+[\.\)]\s+", line):
            txt_widget.insert(tk.END, line + "\n", "list_item")
        else:
            txt_widget.insert(tk.END, line + "\n")

    txt_widget.config(state="disabled")


class MarkdownViewerWindow(tk.Toplevel):
    def __init__(self, parent, file_path, dark_mode=False):
        super().__init__(parent)
        self.file_path = file_path
        self.dark_mode = dark_mode
        self.formatted_view = True
        self.raw_content = ""
        self.search_matches = []
        self.search_idx = -1
        self.title(f"KIWI — Visor de Markdown [{os.path.basename(file_path)}]")
        self.geometry("860x660")
        self.minsize(640, 420)
        bg = "#181a1b" if self.dark_mode else "#f8faf8"
        self.configure(bg=bg)

        self._build_ui()
        self._load_file()

    def _build_ui(self):
        bg_bar = "#222629" if self.dark_mode else "#ffffff"
        fg_bar = "#e0e0e0" if self.dark_mode else "#2b3a1a"
        top_bar = tk.Frame(self, bg=bg_bar, bd=1, relief="solid", padx=12, pady=6)
        top_bar.pack(fill="x")

        lbl_path = tk.Label(top_bar, text=self.file_path, font=("Segoe UI", 9, "bold"), fg=fg_bar, bg=bg_bar)
        lbl_path.pack(side="left")

        btn_copy = tk.Button(top_bar, text="📋 Copiar", font=("Segoe UI", 8), bg="#e8f0e8", fg="#2b3a1a",
                             bd=1, relief="solid", cursor="hand2", padx=8, command=self._copy_all)
        btn_copy.pack(side="right", padx=(6, 0))

        btn_open_ext = tk.Button(top_bar, text="🚀 Editor", font=("Segoe UI", 8), bg="#e8f0e8", fg="#2b3a1a",
                                 bd=1, relief="solid", cursor="hand2", padx=8, command=self._open_external)
        btn_open_ext.pack(side="right", padx=(6, 0))

        self.btn_mode = tk.Button(top_bar, text="🔀 Modo Código", font=("Segoe UI", 8), bg="#e8f0e8", fg="#2b3a1a",
                                  bd=1, relief="solid", cursor="hand2", padx=8, command=self._toggle_mode)
        self.btn_mode.pack(side="right")

        # Barra de estadísticas y búsqueda rápida
        sub_bar = tk.Frame(self, bg=bg_bar, padx=12, pady=4)
        sub_bar.pack(fill="x")

        self.lbl_stats = tk.Label(sub_bar, text="", font=("Segoe UI", 8),
                                  fg="#81c784" if self.dark_mode else "#2e7d32", bg=bg_bar)
        self.lbl_stats.pack(side="left")

        self.lbl_search_count = tk.Label(sub_bar, text="", font=("Segoe UI", 8), fg=fg_bar, bg=bg_bar)
        self.lbl_search_count.pack(side="right", padx=(4, 0))
        tk.Button(sub_bar, text="⬇️", font=("Segoe UI", 7), bd=1, relief="solid", cursor="hand2",
                  command=lambda: self._step_search(1)).pack(side="right", padx=(2, 0))
        tk.Button(sub_bar, text="⬆️", font=("Segoe UI", 7), bd=1, relief="solid", cursor="hand2",
                  command=lambda: self._step_search(-1)).pack(side="right", padx=(2, 0))
        self.ent_search = tk.Entry(sub_bar, font=("Segoe UI", 8), width=22, bd=1, relief="solid")
        self.ent_search.pack(side="right", padx=(4, 2))
        self.ent_search.bind("<KeyRelease>", lambda e: self._run_search())
        self.ent_search.bind("<Return>", lambda e: self._step_search(1))
        tk.Label(sub_bar, text="🔍 Buscar:", font=("Segoe UI", 8), fg=fg_bar, bg=bg_bar).pack(side="right")

        f_text = tk.Frame(self, bg=bg_bar)
        f_text.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        txt_bg = "#222629" if self.dark_mode else "#ffffff"
        txt_fg = "#e0e0e0" if self.dark_mode else "#1e1e1e"
        self.txt_content = tk.Text(f_text, font=("Consolas", 10), bg=txt_bg, fg=txt_fg,
                                   wrap="word", bd=1, relief="solid", padx=10, pady=10)
        scroll_y = tk.Scrollbar(f_text, orient="vertical", command=self.txt_content.yview)
        self.txt_content.configure(yscrollcommand=scroll_y.set)

        self.txt_content.pack(side="left", fill="both", expand=True)
        scroll_y.pack(side="right", fill="y")

    def _load_file(self):
        if os.path.exists(self.file_path):
            with open(self.file_path, "r", encoding="utf-8", errors="replace") as f:
                self.raw_content = f.read()
            self.lbl_stats.config(text=_extraer_resumen_markdown(self.raw_content))
            self._render()

    def _render(self):
        if self.formatted_view:
            format_markdown_into_text(self.txt_content, self.raw_content, dark_mode=self.dark_mode)
            self.btn_mode.config(text="🔀 Ver Código")
        else:
            self.txt_content.config(state="normal")
            self.txt_content.delete("1.0", tk.END)
            self.txt_content.insert("1.0", self.raw_content)
            self.txt_content.config(state="disabled")
            self.btn_mode.config(text="🔀 Ver Formateado")
        if hasattr(self, "ent_search") and self.ent_search.get().strip():
            self._run_search()

    def _run_search(self):
        self.txt_content.tag_remove("search_hit", "1.0", tk.END)
        self.txt_content.tag_remove("search_active", "1.0", tk.END)
        self.search_matches = []
        self.search_idx = -1
        q = self.ent_search.get().strip()
        if not q:
            self.lbl_search_count.config(text="")
            return
        pos = "1.0"
        while True:
            pos = self.txt_content.search(q, pos, nocase=True, stopindex=tk.END)
            if not pos:
                break
            end = f"{pos}+{len(q)}c"
            self.search_matches.append((pos, end))
            self.txt_content.tag_add("search_hit", pos, end)
            pos = end
        if self.search_matches:
            self.search_idx = 0
            self._highlight_active_match()
        else:
            self.lbl_search_count.config(text="0/0")

    def _step_search(self, delta):
        if not self.search_matches:
            return
        self.search_idx = (self.search_idx + delta) % len(self.search_matches)
        self._highlight_active_match()

    def _highlight_active_match(self):
        self.txt_content.tag_remove("search_active", "1.0", tk.END)
        if 0 <= self.search_idx < len(self.search_matches):
            pos, end = self.search_matches[self.search_idx]
            self.txt_content.tag_add("search_active", pos, end)
            self.txt_content.see(pos)
            self.lbl_search_count.config(text=f"{self.search_idx + 1}/{len(self.search_matches)}")

    def _toggle_mode(self):
        self.formatted_view = not self.formatted_view
        self._render()

    def _copy_all(self):
        self.clipboard_clear()
        self.clipboard_append(self.raw_content)
        messagebox.showinfo("Copiado", "Contenido Markdown copiado al portapapeles.")

    def _open_external(self):
        abrir_en_sistema(self.file_path)


# --------------------------------------------------------------------------
# Interfaz Gráfica de Usuario (GUI Unificada KIWI)
# --------------------------------------------------------------------------
class KiwiAppGUI:
    def __init__(self, root):
        self.root = root
        # Adaptación dinámica a la pantalla del usuario (evita desbordar en portátiles y pantallas escaladas)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        target_w = min(960, max(820, screen_w - 40))
        target_h = min(640, max(500, screen_h - 90))
        self.root.geometry(f"{target_w}x{target_h}")
        self.root.minsize(800, 480)

        # Paleta temática KIWI y Estado Visual
        self.dark_mode = False
        self.c_bg = "#f5f9f5"
        self.c_card = "#ffffff"
        self.c_primary = "#4a8505"
        self.c_primary_hover = "#3c6d04"
        self.c_dark = "#2b3a1a"
        self.c_accent = "#8cc63f"
        self.c_border = "#d8e6d8"
        self.c_text = "#333333"
        self.c_text_muted = "#666666"

        self.root.configure(bg=self.c_bg)

        # Métricas KPI en vivo
        self.kpi_docs = 0
        self.kpi_pages = 0
        self.kpi_elem = 0
        self.kpi_time = 0.0
        self.current_start_time = None

        # Seguimiento interactivo de tareas y visor
        self.task_row_map = {}
        self.task_meta_map = {}
        self.viewer_file_path = ""
        self.viewer_raw_content = ""
        self.viewer_formatted_mode = True

        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread = None
        self.pipeline_active = False
        self.last_report = None
        self.last_dest = None
        self.last_md = None

        self._setup_styles()
        self._load_icons()
        self._build_ui()
        self._load_config()

        # Atajos de teclado
        self.root.bind("<Control-Return>", lambda e: self._on_enter_pressed())
        self.root.bind("<Escape>", lambda e: self._cancel_process())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.root.after(100, self._process_queue)
        self.root.after(800, self._chequear_dependencias)

    def _palette(self):
        if self.dark_mode:
            return {
                "bg": "#191d21",
                "card": "#22272b",
                "primary": "#4caf50",
                "primary_hover": "#66bb6a",
                "dark": "#a5d6a7",
                "accent": "#81c784",
                "border": "#373e47",
                "text": "#e6edf3",
                "text_muted": "#8b949e",
                "input_bg": "#2d333b",
                "input_fg": "#f0f6fc",
                "tab_bg": "#22272b",
                "tab_fg": "#c9d1d9",
                "tab_sel_bg": "#2e7d32",
                "tab_sel_fg": "#ffffff",
                "tree_bg": "#22272b",
                "tree_fg": "#e6edf3",
                "tree_head_bg": "#2d333b",
                "tree_head_fg": "#f0f6fc",
                "tree_sel_bg": "#2e7d32",
                "tree_sel_fg": "#ffffff",
                "log_bg": "#161b22",
                "log_fg": "#e6edf3",
                "kpi_bg": "#22272b",
                "kpi_val": "#81c784",
            }
        else:
            return {
                "bg": "#f5f9f5",
                "card": "#ffffff",
                "primary": "#4a8505",
                "primary_hover": "#3c6d04",
                "dark": "#2b3a1a",
                "accent": "#8cc63f",
                "border": "#d8e6d8",
                "text": "#333333",
                "text_muted": "#666666",
                "input_bg": "#ffffff",
                "input_fg": "#222222",
                "tab_bg": "#e2ede2",
                "tab_fg": "#2b3a1a",
                "tab_sel_bg": "#4a8505",
                "tab_sel_fg": "#ffffff",
                "tree_bg": "#ffffff",
                "tree_fg": "#222222",
                "tree_head_bg": "#eef4ee",
                "tree_head_fg": "#2b3a1a",
                "tree_sel_bg": "#c8e6c9",
                "tree_sel_fg": "#1b5e20",
                "log_bg": "#fafcfa",
                "log_fg": "#222222",
                "kpi_bg": "#ffffff",
                "kpi_val": "#2e7d32",
            }

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        p = self._palette()

        style.configure("TNotebook", background=p["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=p["tab_bg"], foreground=p["tab_fg"],
                        padding=[12, 5], font=("Segoe UI", 9, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", p["tab_sel_bg"])],
                  foreground=[("selected", p["tab_sel_fg"])])

        style.configure("Treeview", background=p["tree_bg"], foreground=p["tree_fg"],
                        fieldbackground=p["tree_bg"], font=("Segoe UI", 8), rowheight=22)
        style.configure("Treeview.Heading", background=p["tree_head_bg"], foreground=p["tree_head_fg"],
                        font=("Segoe UI", 8, "bold"))
        style.map("Treeview",
                  background=[("selected", p["tree_sel_bg"])],
                  foreground=[("selected", p["tree_sel_fg"])])

        style.configure("TProgressbar", thickness=16, troughcolor=p["bg"], background=p["primary"])

    def _apply_theme(self):
        p = self._palette()
        self.c_bg = p["bg"]
        self.c_card = p["card"]
        self.c_primary = p["primary"]
        self.c_primary_hover = p["primary_hover"]
        self.c_dark = p["dark"]
        self.c_accent = p["accent"]
        self.c_border = p["border"]
        self.c_text = p["text"]
        self.c_text_muted = p["text_muted"]

        self.root.configure(bg=self.c_bg)
        self._setup_styles()

        if hasattr(self, "btn_theme"):
            self.btn_theme.config(text="☀️ Modo Claro" if self.dark_mode else "🌙 Modo Oscuro",
                                  bg=p["card"], fg=p["text"])

        if hasattr(self, "kpi_cards_list"):
            for card, val_lbl, sub_lbl, title_lbl in self.kpi_cards_list:
                card.config(bg=p["kpi_bg"], highlightbackground=p["border"])
                val_lbl.config(bg=p["kpi_bg"], fg=p["kpi_val"])
                sub_lbl.config(bg=p["kpi_bg"], fg=p["text_muted"])
                title_lbl.config(bg=p["kpi_bg"], fg=p["text"])

        if hasattr(self, "txt_log"):
            self.txt_log.config(bg=p["log_bg"], fg=p["log_fg"])
            self.txt_log.tag_config("info", foreground=p["text"])
            self.txt_log.tag_config("success", foreground="#66bb6a" if self.dark_mode else "#2e7d32")
            self.txt_log.tag_config("warning", foreground="#ffa726" if self.dark_mode else "#d87a00")
            self.txt_log.tag_config("error", foreground="#ef5350" if self.dark_mode else "#c62828")

        if hasattr(self, "viewer_text"):
            self.viewer_text.config(bg=p["log_bg"], fg=p["log_fg"])
            if self.viewer_raw_content:
                self._render_viewer()

        if hasattr(self, "btn_clear_log"):
            self.btn_clear_log.config(bg=p["card"], fg=p["text_muted"])
        if hasattr(self, "prog_sub_nb"):
            self._on_sub_tab_changed()
        self._configure_tree_tags()

        self._recolor_widget_tree(self.root, p)

    def _recolor_widget_tree(self, widget, p):
        try:
            w_class = widget.winfo_class()
            if w_class == "Frame":
                cur_bg = widget.cget("bg")
                if cur_bg in ("#ffffff", "#22272b", "#282c34"):
                    widget.configure(bg=p["card"])
                elif cur_bg in ("#f5f9f5", "#191d21", "#1e2227"):
                    widget.configure(bg=p["bg"])
            elif w_class == "LabelFrame":
                widget.configure(bg=p["card"], fg=p["dark"])
            elif w_class == "Label":
                cur_bg = widget.cget("bg")
                if cur_bg in ("#ffffff", "#22272b", "#282c34"):
                    widget.configure(bg=p["card"])
                else:
                    widget.configure(bg=p["bg"])
                cur_fg = widget.cget("fg")
                if cur_fg not in (p["primary"], "#2e7d32", "#c62828", "#1d5e21", "#81c784", "#66bb6a", "#ef5350"):
                    widget.configure(fg=p["text"])
            elif w_class == "Entry":
                widget.configure(bg=p["input_bg"], fg=p["input_fg"], insertbackground=p["text"])
            elif w_class == "Checkbutton":
                widget.configure(bg=p["bg"], fg=p["text"], selectcolor=p["input_bg"], activebackground=p["bg"])
            elif w_class == "Radiobutton":
                widget.configure(bg=p["bg"], fg=p["text"], selectcolor=p["input_bg"], activebackground=p["bg"])
        except Exception:
            pass

        for child in widget.winfo_children():
            self._recolor_widget_tree(child, p)

    def _toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self._apply_theme()
        self._save_config()

    def _load_icons(self):
        self.logo_img = None
        ico_path = recurso("assets", "kiwi_logo.ico")
        png_path = recurso("assets", "kiwi_logo.png")

        if os.path.exists(ico_path):
            try:
                self.root.iconbitmap(ico_path)
            except Exception:
                pass

        if HAS_PIL and os.path.exists(png_path):
            try:
                pil_img = Image.open(png_path)
                pil_img = pil_img.resize((48, 48), Image.Resampling.LANCZOS)
                self.logo_img = ImageTk.PhotoImage(pil_img)
            except Exception:
                self.logo_img = None

    def _build_kpi_cards(self, parent):
        self.f_kpi = tk.Frame(parent, bg=self.c_bg)
        self.f_kpi.pack(fill="x", pady=(0, 4))
        self.f_kpi.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.kpi_cards_list = []
        cards_info = [
            ("📄 Documentos", "0", "Procesados en sesión", "#2e7d32"),
            ("📑 Páginas / URLs", "0", "Extraídas / capturadas", "#1565c0"),
            ("🔗 Enlaces y Tablas", "0", "Referencias preservadas", "#6a1b9a"),
            ("⏱️ Tiempo Total", "0.0s", "Duración acumulada", "#ef6c00"),
        ]

        for i, (title, val_init, sub, accent_color) in enumerate(cards_info):
            card = tk.Frame(self.f_kpi, bg=self.c_card, bd=1, relief="solid", highlightthickness=1)
            card.configure(highlightbackground=self.c_border)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 3, 0 if i == 3 else 3), pady=0)

            accent_bar = tk.Frame(card, bg=accent_color, width=4)
            accent_bar.pack(side="left", fill="y")

            left_box = tk.Frame(card, bg=self.c_card)
            left_box.pack(side="left", fill="both", expand=True, padx=(6, 2), pady=2)

            t_lbl = tk.Label(left_box, text=title, font=("Segoe UI", 8, "bold"), fg=self.c_text, bg=self.c_card, anchor="w")
            t_lbl.pack(anchor="w")

            s_lbl = tk.Label(left_box, text=sub, font=("Segoe UI", 7), fg=self.c_text_muted, bg=self.c_card, anchor="w")
            s_lbl.pack(anchor="w")

            v_lbl = tk.Label(card, text=val_init, font=("Segoe UI", 12, "bold"), fg="#2e7d32", bg=self.c_card)
            v_lbl.pack(side="right", padx=(4, 8), pady=2)

            self.kpi_cards_list.append((card, v_lbl, s_lbl, t_lbl))

    def _update_kpis(self, docs_delta=0, pages_delta=0, elem_delta=0, time_delta=0.0):
        self.kpi_docs += docs_delta
        self.kpi_pages += pages_delta
        self.kpi_elem += elem_delta
        self.kpi_time += time_delta

        if hasattr(self, "kpi_cards_list") and len(self.kpi_cards_list) == 4:
            self.kpi_cards_list[0][1].config(text=f"{self.kpi_docs:,}")
            self.kpi_cards_list[1][1].config(text=f"{self.kpi_pages:,}")
            self.kpi_cards_list[2][1].config(text=f"{self.kpi_elem:,}")
            self.kpi_cards_list[3][1].config(text=f"{self.kpi_time:.1f}s")

    def _build_ui(self):
        main_frame = tk.Frame(self.root, bg=self.c_bg, padx=12, pady=6)
        main_frame.pack(fill="both", expand=True)

        # 1. Cabecera KIWI
        header_card = tk.Frame(main_frame, bg=self.c_card, bd=1, relief="solid", highlightthickness=0)
        header_card.configure(highlightbackground=self.c_border)
        header_card.pack(fill="x", pady=(0, 4))

        header_inner = tk.Frame(header_card, bg=self.c_card, padx=10, pady=4)
        header_inner.pack(fill="x")

        if self.logo_img:
            logo_label = tk.Label(header_inner, image=self.logo_img, bg=self.c_card)
            logo_label.pack(side="left", padx=(0, 10))

        title_box = tk.Frame(header_inner, bg=self.c_card)
        title_box.pack(side="left", fill="both", expand=True)

        title_row = tk.Frame(title_box, bg=self.c_card)
        title_row.pack(anchor="w")

        lbl_app_name = tk.Label(title_row, text="KIWI", font=("Segoe UI", 16, "bold"),
                                fg=self.c_primary, bg=self.c_card)
        lbl_app_name.pack(side="left")

        lbl_app_ver = tk.Label(title_row, text="  pdfmd + htmlmd · Suite Documental Offline",
                               font=("Segoe UI", 8, "bold"), fg=self.c_text_muted, bg=self.c_card)
        lbl_app_ver.pack(side="left", pady=(4, 0))

        lbl_app_desc = tk.Label(title_box,
                                text="Suite Inteligente de documentos: recolección masiva, deduplicación SHA-256 y conversión estructurada a Markdown con OCR multilingüe.",
                                font=("Segoe UI", 8), fg=self.c_text_muted, bg=self.c_card, wraplength=540, justify="left")
        lbl_app_desc.pack(anchor="w", pady=(1, 0))

        # Botón Toggle Modo Oscuro en cabecera
        self.btn_theme = tk.Button(header_inner, text="🌙 Modo Oscuro", font=("Segoe UI", 8, "bold"),
                                   bg=self.c_card, fg=self.c_text, bd=1, relief="solid",
                                   cursor="hand2", padx=8, pady=3, command=self._toggle_theme)
        self.btn_theme.pack(side="right", padx=(4, 0))

        # Botón rápido Perfil Corpus en cabecera
        self.btn_corpus_header = tk.Button(
            header_inner, text="⚡ Perfil Corpus", font=("Segoe UI", 8, "bold"),
            bg="#e8f5e9", fg="#1b5e20", bd=1, relief="solid",
            cursor="hand2", padx=8, pady=3, command=self._aplicar_perfil_corpus
        )
        self.btn_corpus_header.pack(side="right", padx=(4, 0))

        # Indicador en vivo de estado OCR
        self.btn_ocr_badge = tk.Button(
            header_inner, text="🔍 Verificando OCR...", font=("Segoe UI", 8, "bold"),
            bg="#f0f4f0", fg=self.c_dark, bd=1, relief="solid",
            cursor="hand2", padx=8, pady=3, command=self._mostrar_diagnostico_ocr
        )
        self.btn_ocr_badge.pack(side="right", padx=(4, 0))

        # 2. Dashboard Superior de Métricas (Tarjetas KPI)
        self._build_kpi_cards(main_frame)

        # 3. Pestañas (Notebook)
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill="x", pady=(0, 4))

        self.tab_collector = tk.Frame(self.notebook, bg=self.c_bg, padx=8, pady=6)
        self.tab_converter = tk.Frame(self.notebook, bg=self.c_bg, padx=8, pady=6)
        self.tab_pipeline = tk.Frame(self.notebook, bg=self.c_bg, padx=8, pady=6)
        self.tab_web = tk.Frame(self.notebook, bg=self.c_bg, padx=8, pady=6)
        self.tab_viewer = tk.Frame(self.notebook, bg=self.c_bg, padx=8, pady=6)

        self.notebook.add(self.tab_collector, text=" 📁 Recolector y Deduplicador ")
        self.notebook.add(self.tab_converter, text=" 📝 Conversor a Markdown (pdfmd) ")
        self.notebook.add(self.tab_pipeline, text=" ⚡ Recolectar + Convertir a MD ")
        self.notebook.add(self.tab_web, text=" 🌐 Web a Markdown (htmlmd) ")
        self.notebook.add(self.tab_viewer, text=" 👁️ Visor Markdown ")

        self._build_tab_collector()
        self._build_tab_converter()
        self._build_tab_pipeline()
        self._build_tab_web()
        self._build_tab_viewer()

        # 4. Progreso y Actividad (Sub-notebook: Tabla Interactiva y Log en vivo)
        prog_card = tk.LabelFrame(main_frame, text=" 📊 Progreso y Actividad en Vivo ", font=("Segoe UI", 8, "bold"),
                                  fg=self.c_dark, bg=self.c_card, bd=1, relief="solid", padx=8, pady=4)
        prog_card.pack(fill="both", expand=True, pady=(0, 4))

        self.lbl_fase = tk.Label(prog_card, text="Listo para comenzar. Presiona Iniciar o pulsa Ctrl+Enter.",
                                 font=("Segoe UI", 8, "italic"), fg=self.c_text_muted, bg=self.c_card)
        self.lbl_fase.pack(anchor="w", pady=(0, 2))

        self.prog_bar = ttk.Progressbar(prog_card, style="TProgressbar", mode="determinate")
        self.prog_bar.pack(fill="x", pady=(0, 4))

        # Barra de conmutación de vista rápida (Tabla vs Log)
        f_view_switch = tk.Frame(prog_card, bg=self.c_card)
        f_view_switch.pack(fill="x", pady=(0, 3))

        self.btn_view_tree = tk.Button(
            f_view_switch, text="📋 Cola de Trabajos (Tabla)", font=("Segoe UI", 9, "bold"),
            bg="#2e7d32", fg="white", bd=0, padx=12, pady=3, cursor="hand2",
            command=lambda: self._switch_prog_view(0)
        )
        self.btn_view_tree.pack(side="left", padx=(0, 4))

        self.btn_view_log = tk.Button(
            f_view_switch, text="📜 Registro de Actividad (Log)", font=("Segoe UI", 9),
            bg="#e2efe2", fg=self.c_dark, bd=0, padx=12, pady=3, cursor="hand2",
            command=lambda: self._switch_prog_view(1)
        )
        self.btn_view_log.pack(side="left", padx=(0, 4))

        self.lbl_queue_summary = tk.Label(
            f_view_switch, text="", font=("Segoe UI", 8, "bold"),
            fg=self.c_text_muted, bg=self.c_card
        )
        self.lbl_queue_summary.pack(side="left", padx=(10, 4))

        self.btn_clear_log = tk.Button(
            f_view_switch, text="🧹 Limpiar Log", font=("Segoe UI", 8),
            bg=self.c_card, fg=self.c_text_muted, bd=1, relief="solid", padx=8, pady=2, cursor="hand2",
            command=lambda: self.txt_log.delete("1.0", tk.END)
        )
        self.btn_clear_log.pack(side="right")

        # Sub-notebook con dos vistas: Cola de tareas (Treeview) y Log de actividad
        self.prog_sub_nb = ttk.Notebook(prog_card)
        self.prog_sub_nb.pack(fill="both", expand=True)

        self.tab_sub_tree = tk.Frame(self.prog_sub_nb, bg=self.c_card)
        self.tab_sub_log = tk.Frame(self.prog_sub_nb, bg=self.c_card)

        self.prog_sub_nb.add(self.tab_sub_tree, text=" 📋 Cola de Trabajos (Tabla) ")
        self.prog_sub_nb.add(self.tab_sub_log, text=" 📜 Registro de Actividad (Log) ")

        self.prog_sub_nb.bind("<<NotebookTabChanged>>", lambda e: self._on_sub_tab_changed())

        self._build_treeview_tasks(self.tab_sub_tree)
        self._build_log_text(self.tab_sub_log)

        # 5. Botones Rápidos de Apertura y Visor
        post_frame = tk.Frame(main_frame, bg=self.c_bg)
        post_frame.pack(fill="x")

        self.btn_open_dest = tk.Button(post_frame, text="📁 Abrir Carpeta Salida", font=("Segoe UI", 9),
                                       bg="#e2efe2", fg=self.c_dark, bd=1, relief="solid",
                                       state="disabled", cursor="hand2", padx=12, pady=3,
                                       command=self._open_destination)
        self.btn_open_dest.pack(side="left", padx=(0, 6))

        self.btn_open_csv = tk.Button(post_frame, text="📊 Abrir Informe CSV", font=("Segoe UI", 9),
                                      bg="#e2efe2", fg=self.c_dark, bd=1, relief="solid",
                                      state="disabled", cursor="hand2", padx=12, pady=3,
                                      command=self._open_report)
        self.btn_open_csv.pack(side="left", padx=(0, 6))

        self.btn_view_md = tk.Button(post_frame, text="👁️ Ver Markdown en KIWI", font=("Segoe UI", 9, "bold"),
                                     bg="#d0e8d0", fg=self.c_dark, bd=1, relief="solid",
                                     state="disabled", cursor="hand2", padx=12, pady=3,
                                     command=self._view_markdown_inline)
        self.btn_view_md.pack(side="left", padx=(0, 6))

        self.btn_open_md = tk.Button(post_frame, text="📄 Abrir en Editor", font=("Segoe UI", 9),
                                     bg="#e2efe2", fg=self.c_dark, bd=1, relief="solid",
                                     state="disabled", cursor="hand2", padx=12, pady=3,
                                     command=self._open_markdown)
        self.btn_open_md.pack(side="left")

        # Configurar Drag & Drop
        self._setup_drag_and_drop()

    def _configure_tree_tags(self):
        if not hasattr(self, "tree_tasks"):
            return
        if self.dark_mode:
            self.tree_tasks.tag_configure("tag_ok", foreground="#81c784")
            self.tree_tasks.tag_configure("tag_warn", foreground="#ffd54f")
            self.tree_tasks.tag_configure("tag_err", foreground="#ef5350")
            self.tree_tasks.tag_configure("tag_proc", foreground="#64b5f6")
            self.tree_tasks.tag_configure("tag_skip", foreground="#8b949e")
        else:
            self.tree_tasks.tag_configure("tag_ok", foreground="#1b5e20", background="#f4fbf4")
            self.tree_tasks.tag_configure("tag_warn", foreground="#b26a00", background="#fffdf0")
            self.tree_tasks.tag_configure("tag_err", foreground="#b71c1c", background="#fff5f5")
            self.tree_tasks.tag_configure("tag_proc", foreground="#0d47a1", background="#f0f7ff")
            self.tree_tasks.tag_configure("tag_skip", foreground="#666666")

    def _tag_for_estado(self, estado: str) -> str:
        if estado.startswith("✅"):
            return "tag_ok"
        if estado.startswith("⚠️"):
            return "tag_warn"
        if estado.startswith(("❌", "🔒")):
            return "tag_err"
        if estado.startswith("⚙️"):
            return "tag_proc"
        return "tag_skip"

    def _refresh_queue_summary(self):
        if not hasattr(self, "tree_tasks") or not hasattr(self, "lbl_queue_summary"):
            return
        items = self.tree_tasks.get_children()
        if not items:
            self.lbl_queue_summary.config(text="")
            return
        ok = warn = err = skip = proc = 0
        for it in items:
            vals = self.tree_tasks.item(it, "values")
            est = vals[0] if vals else ""
            if est.startswith("✅"):
                ok += 1
            elif est.startswith("⚠️"):
                warn += 1
            elif est.startswith(("❌", "🔒")):
                err += 1
            elif est.startswith("⚙️"):
                proc += 1
            else:
                skip += 1
        partes = [f"Total: {len(items)}"]
        if ok:
            partes.append(f"✅ {ok}")
        if warn:
            partes.append(f"⚠️ {warn}")
        if err:
            partes.append(f"❌ {err}")
        if skip:
            partes.append(f"⏭️ {skip}")
        if proc:
            partes.append(f"⚙️ {proc}")
        self.lbl_queue_summary.config(text="  |  ".join(partes))

    def _build_treeview_tasks(self, parent):
        cols = ("estado", "tipo", "origen", "salida", "detalle")
        self.tree_tasks = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse", height=4)

        self.tree_tasks.heading("estado", text="Estado")
        self.tree_tasks.heading("tipo", text="Tipo")
        self.tree_tasks.heading("origen", text="Entrada / Origen")
        self.tree_tasks.heading("salida", text="Salida / Destino")
        self.tree_tasks.heading("detalle", text="Detalle")

        self.tree_tasks.column("estado", width=105, anchor="center")
        self.tree_tasks.column("tipo", width=95, anchor="center")
        self.tree_tasks.column("origen", width=215, anchor="w")
        self.tree_tasks.column("salida", width=215, anchor="w")
        self.tree_tasks.column("detalle", width=170, anchor="w")

        self._configure_tree_tags()

        tree_scroll_y = tk.Scrollbar(parent, orient="vertical", command=self.tree_tasks.yview)
        self.tree_tasks.configure(yscrollcommand=tree_scroll_y.set)

        self.tree_tasks.pack(side="left", fill="both", expand=True)
        tree_scroll_y.pack(side="right", fill="y")

        self.tree_tasks.bind("<Double-1>", self._on_tree_double_click)
        self.tree_tasks.bind("<Button-3>", self._on_tree_right_click)

        self.tree_menu = tk.Menu(self.root, tearoff=0)
        self.tree_menu.add_command(label="👁️ Ver en Visor Markdown", command=self._menu_tree_view_md)
        self.tree_menu.add_command(label="🪟 Abrir en Ventana Flotante", command=self._menu_tree_popup_md)
        self.tree_menu.add_command(label="📁 Abrir Carpeta Destino", command=self._menu_tree_open_dest)
        self.tree_menu.add_command(label="🚀 Abrir en Editor Externo", command=self._menu_tree_open_ext)
        self.tree_menu.add_command(label="📋 Copiar Ruta de Salida", command=self._menu_tree_copy_path)

    def _build_log_text(self, parent):
        self.txt_log = tk.Text(parent, font=("Consolas", 8), bg="#fafcfa", fg="#222222",
                               relief="solid", bd=1, wrap="word", height=6)
        log_scroll_y = tk.Scrollbar(parent, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=log_scroll_y.set)

        self.txt_log.pack(side="left", fill="both", expand=True)
        log_scroll_y.pack(side="right", fill="y")

        self.txt_log.tag_config("info", foreground="#333333")
        self.txt_log.tag_config("success", foreground="#2e7d32", font=("Consolas", 8, "bold"))
        self.txt_log.tag_config("warning", foreground="#d87a00")
        self.txt_log.tag_config("error", foreground="#c62828", font=("Consolas", 8, "bold"))

    def _switch_prog_view(self, index):
        self.prog_sub_nb.select(index)
        self._update_switcher_buttons(index)

    def _on_sub_tab_changed(self):
        try:
            idx = self.prog_sub_nb.index(self.prog_sub_nb.select())
            self._update_switcher_buttons(idx)
        except Exception:
            pass

    def _update_switcher_buttons(self, active_index):
        if not hasattr(self, "btn_view_tree") or not hasattr(self, "btn_view_log"):
            return
        bg_active = "#2e7d32" if not self.dark_mode else "#388e3c"
        fg_active = "white"
        bg_inactive = "#e2efe2" if not self.dark_mode else "#2d333b"
        fg_inactive = self.c_dark if not self.dark_mode else "#e6edf3"

        if active_index == 0:
            self.btn_view_tree.config(bg=bg_active, fg=fg_active, font=("Segoe UI", 9, "bold"))
            self.btn_view_log.config(bg=bg_inactive, fg=fg_inactive, font=("Segoe UI", 9))
        else:
            self.btn_view_log.config(bg=bg_active, fg=fg_active, font=("Segoe UI", 9, "bold"))
            self.btn_view_tree.config(bg=bg_inactive, fg=fg_inactive, font=("Segoe UI", 9))

    def _get_selected_tree_item(self):
        sel = self.tree_tasks.selection()
        if not sel:
            return None
        item_id = sel[0]
        vals = self.tree_tasks.item(item_id, "values")
        data = getattr(self, "task_meta_map", {}).get(item_id, {})
        return vals, data

    def _on_tree_double_click(self, event):
        item = self.tree_tasks.identify_row(event.y)
        if not item:
            return
        data = getattr(self, "task_meta_map", {}).get(item, {})
        out_f = data.get("out_full", "")
        if out_f and os.path.exists(out_f) and out_f.lower().endswith(".md"):
            self.load_in_viewer(out_f)
        elif out_f and os.path.exists(out_f):
            abrir_en_sistema(out_f)

    def _on_tree_right_click(self, event):
        item = self.tree_tasks.identify_row(event.y)
        if item:
            self.tree_tasks.selection_set(item)
            self.tree_menu.post(event.x_root, event.y_root)

    def _menu_tree_view_md(self):
        res = self._get_selected_tree_item()
        if res:
            _, data = res
            out_f = data.get("out_full", "")
            if out_f and os.path.exists(out_f):
                self.load_in_viewer(out_f)

    def _menu_tree_popup_md(self):
        res = self._get_selected_tree_item()
        if res:
            _, data = res
            out_f = data.get("out_full", "")
            if out_f and os.path.exists(out_f):
                MarkdownViewerWindow(self.root, out_f, dark_mode=self.dark_mode)

    def _menu_tree_open_dest(self):
        res = self._get_selected_tree_item()
        if res:
            _, data = res
            out_f = data.get("out_full", "")
            if out_f:
                folder = out_f if os.path.isdir(out_f) else os.path.dirname(out_f)
                abrir_en_sistema(folder)

    def _menu_tree_open_ext(self):
        res = self._get_selected_tree_item()
        if res:
            _, data = res
            out_f = data.get("out_full", "")
            if out_f and os.path.exists(out_f):
                abrir_en_sistema(out_f)

    def _menu_tree_copy_path(self):
        res = self._get_selected_tree_item()
        if res:
            vals, data = res
            out_f = data.get("out_full") or vals[3]
            self.root.clipboard_clear()
            self.root.clipboard_append(out_f)

    def _setup_drag_and_drop(self):
        if not HAS_DND:
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self._on_drop_files)
        except Exception:
            pass

    def _on_drop_files(self, event):
        if not getattr(event, "data", None):
            return
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]

        for p in paths:
            p = os.path.normpath(p.strip())
            if not p:
                continue
            self._handle_dropped_path(p)

    def _handle_dropped_path(self, p):
        if os.path.isdir(p):
            cur = self.notebook.index(self.notebook.select())
            if cur == 0:
                self.col_txt_origen.delete(0, tk.END)
                self.col_txt_origen.insert(0, p)
            elif cur == 2:
                self.pipe_txt_origen.delete(0, tk.END)
                self.pipe_txt_origen.insert(0, p)
            elif cur == 3:
                self.web_mode_var.set("carpeta")
                self.web_txt_in.delete(0, tk.END)
                self.web_txt_in.insert(0, p)
                self.web_txt_out.delete(0, tk.END)
                self.web_txt_out.insert(0, os.path.join(p, "markdown_web"))
            else:
                self.notebook.select(self.tab_converter)
                self.conv_mode_var.set("folder")
                self.conv_txt_in.delete(0, tk.END)
                self.conv_txt_in.insert(0, p)
                self.conv_txt_out.delete(0, tk.END)
                self.conv_txt_out.insert(0, os.path.join(p, "markdown_export"))
            self._log_msg(f"📥 Carpeta arrastrada: {p}", "info")
        elif p.lower().endswith(".pdf"):
            self.notebook.select(self.tab_converter)
            self.conv_mode_var.set("file")
            self.conv_txt_in.delete(0, tk.END)
            self.conv_txt_in.insert(0, p)
            base, _ = os.path.splitext(p)
            self.conv_txt_out.delete(0, tk.END)
            self.conv_txt_out.insert(0, base + ".md")
            self._log_msg(f"📥 PDF arrastrado: {os.path.basename(p)}", "info")
        elif p.lower().endswith(".md"):
            self.load_in_viewer(p)
            self._log_msg(f"📥 Markdown cargado en Visor: {os.path.basename(p)}", "info")
        elif p.lower().endswith((".html", ".htm", ".txt", ".csv")):
            self.notebook.select(self.tab_web)
            if p.lower().endswith((".txt", ".csv")):
                self.web_mode_var.set("lista")
            else:
                self.web_mode_var.set("url")
            self.web_txt_in.delete(0, tk.END)
            self.web_txt_in.insert(0, p)
            self._log_msg(f"📥 Archivo web arrastrado: {os.path.basename(p)}", "info")

    def _build_tab_viewer(self):
        self.viewer_search_matches = []
        self.viewer_search_idx = -1

        f_top = tk.Frame(self.tab_viewer, bg=self.c_bg)
        f_top.pack(fill="x", pady=(0, 3))

        tk.Label(f_top, text="Fichero Markdown:", font=("Segoe UI", 9, "bold"),
                 fg=self.c_text, bg=self.c_bg).pack(side="left", padx=(0, 6))

        self.viewer_txt_path = tk.Entry(f_top, font=("Segoe UI", 9), bd=1, relief="solid")
        self.viewer_txt_path.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.viewer_txt_path.bind("<Return>", lambda e: self._viewer_load_from_entry())

        btn_browse = tk.Button(f_top, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                               bd=1, relief="solid", cursor="hand2", command=self._viewer_browse)
        btn_browse.pack(side="left", padx=(0, 4))

        btn_reload = tk.Button(f_top, text="🔄 Recargar", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                               bd=1, relief="solid", cursor="hand2", command=self._viewer_reload)
        btn_reload.pack(side="left", padx=(0, 4))

        btn_copy = tk.Button(f_top, text="📋 Copiar", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                              bd=1, relief="solid", cursor="hand2", command=self._viewer_copy)
        btn_copy.pack(side="left", padx=(0, 4))

        btn_ext = tk.Button(f_top, text="🚀 Editor", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                            bd=1, relief="solid", cursor="hand2", command=self._viewer_open_external)
        btn_ext.pack(side="left", padx=(0, 4))

        self.btn_viewer_mode = tk.Button(f_top, text="🔀 Modo Código", font=("Segoe UI", 8, "bold"),
                                         bg="#d0e8d0", fg=self.c_dark, bd=1, relief="solid",
                                         cursor="hand2", command=self._viewer_toggle_mode)
        self.btn_viewer_mode.pack(side="left")

        # Segunda fila del visor: píldoras de metadatos + buscador integrado
        f_sub = tk.Frame(self.tab_viewer, bg=self.c_bg)
        f_sub.pack(fill="x", pady=(0, 4))

        self.lbl_viewer_stats = tk.Label(
            f_sub, text="Sin documento cargado", font=("Segoe UI", 8),
            fg=self.c_primary, bg=self.c_bg
        )
        self.lbl_viewer_stats.pack(side="left")

        self.lbl_viewer_search_count = tk.Label(f_sub, text="", font=("Segoe UI", 8), fg=self.c_text_muted, bg=self.c_bg)
        self.lbl_viewer_search_count.pack(side="right", padx=(4, 0))
        tk.Button(f_sub, text="⬇️", font=("Segoe UI", 7), bd=1, relief="solid", cursor="hand2",
                  command=lambda: self._viewer_step_search(1)).pack(side="right", padx=(2, 0))
        tk.Button(f_sub, text="⬆️", font=("Segoe UI", 7), bd=1, relief="solid", cursor="hand2",
                  command=lambda: self._viewer_step_search(-1)).pack(side="right", padx=(2, 0))
        self.viewer_ent_search = tk.Entry(f_sub, font=("Segoe UI", 8), width=20, bd=1, relief="solid")
        self.viewer_ent_search.pack(side="right", padx=(4, 2))
        self.viewer_ent_search.bind("<KeyRelease>", lambda e: self._viewer_run_search())
        self.viewer_ent_search.bind("<Return>", lambda e: self._viewer_step_search(1))
        tk.Label(f_sub, text="🔍 Buscar:", font=("Segoe UI", 8), fg=self.c_text, bg=self.c_bg).pack(side="right")

        f_vtext = tk.Frame(self.tab_viewer, bg=self.c_card, bd=1, relief="solid")
        f_vtext.pack(fill="both", expand=True)

        self.viewer_text = tk.Text(f_vtext, font=("Consolas", 10), bg="#ffffff", fg="#222222",
                                   wrap="word", bd=0, padx=12, pady=10)
        vscroll = tk.Scrollbar(f_vtext, orient="vertical", command=self.viewer_text.yview)
        self.viewer_text.configure(yscrollcommand=vscroll.set)

        self.viewer_text.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

    def _viewer_browse(self):
        sel = filedialog.askopenfilename(title="Seleccionar archivo Markdown",
                                         filetypes=[("Markdown", "*.md"), ("Texto", "*.txt"), ("Todos", "*.*")])
        if sel:
            self.load_in_viewer(sel)

    def _viewer_load_from_entry(self):
        p = self.viewer_txt_path.get().strip()
        if p and os.path.exists(p):
            self.load_in_viewer(p)

    def _viewer_reload(self):
        if self.viewer_file_path and os.path.exists(self.viewer_file_path):
            self.load_in_viewer(self.viewer_file_path)

    def _viewer_copy(self):
        if self.viewer_raw_content:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.viewer_raw_content)
            messagebox.showinfo("Copiado", "Contenido Markdown copiado al portapapeles.")

    def _viewer_open_external(self):
        if self.viewer_file_path and os.path.exists(self.viewer_file_path):
            abrir_en_sistema(self.viewer_file_path)

    def _viewer_toggle_mode(self):
        self.viewer_formatted_mode = not self.viewer_formatted_mode
        self._render_viewer()

    def load_in_viewer(self, file_path):
        if not file_path or not os.path.exists(file_path):
            return
        self.viewer_file_path = file_path
        self.viewer_txt_path.delete(0, tk.END)
        self.viewer_txt_path.insert(0, file_path)
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                self.viewer_raw_content = f.read()
        except OSError as ex:
            messagebox.showerror("Error", f"No se pudo leer el archivo:\n{ex}")
            return
        if hasattr(self, "lbl_viewer_stats"):
            self.lbl_viewer_stats.config(text=_extraer_resumen_markdown(self.viewer_raw_content))
        self._render_viewer()
        self.notebook.select(self.tab_viewer)

    def _render_viewer(self):
        p = self._palette()
        txt_bg = p["log_bg"]
        txt_fg = p["log_fg"]
        self.viewer_text.config(bg=txt_bg, fg=txt_fg)

        if self.viewer_formatted_mode:
            format_markdown_into_text(self.viewer_text, self.viewer_raw_content, dark_mode=self.dark_mode)
            self.btn_viewer_mode.config(text="🔀 Ver Código")
        else:
            self.viewer_text.config(state="normal")
            self.viewer_text.delete("1.0", tk.END)
            self.viewer_text.insert("1.0", self.viewer_raw_content)
            self.viewer_text.config(state="disabled")
            self.btn_viewer_mode.config(text="🔀 Ver Formateado")
        if hasattr(self, "viewer_ent_search") and self.viewer_ent_search.get().strip():
            self._viewer_run_search()

    def _viewer_run_search(self):
        self.viewer_text.tag_remove("search_hit", "1.0", tk.END)
        self.viewer_text.tag_remove("search_active", "1.0", tk.END)
        self.viewer_search_matches = []
        self.viewer_search_idx = -1
        q = self.viewer_ent_search.get().strip()
        if not q:
            self.lbl_viewer_search_count.config(text="")
            return
        pos = "1.0"
        while True:
            pos = self.viewer_text.search(q, pos, nocase=True, stopindex=tk.END)
            if not pos:
                break
            end = f"{pos}+{len(q)}c"
            self.viewer_search_matches.append((pos, end))
            self.viewer_text.tag_add("search_hit", pos, end)
            pos = end
        if self.viewer_search_matches:
            self.viewer_search_idx = 0
            self._viewer_highlight_match()
        else:
            self.lbl_viewer_search_count.config(text="0/0")

    def _viewer_step_search(self, delta):
        if not self.viewer_search_matches:
            return
        self.viewer_search_idx = (self.viewer_search_idx + delta) % len(self.viewer_search_matches)
        self._viewer_highlight_match()

    def _viewer_highlight_match(self):
        self.viewer_text.tag_remove("search_active", "1.0", tk.END)
        if 0 <= self.viewer_search_idx < len(self.viewer_search_matches):
            pos, end = self.viewer_search_matches[self.viewer_search_idx]
            self.viewer_text.tag_add("search_active", pos, end)
            self.viewer_text.see(pos)
            self.lbl_viewer_search_count.config(text=f"{self.viewer_search_idx + 1}/{len(self.viewer_search_matches)}")

    # ----------------------------------------------------
    # Construcción Pestaña 1: Recolector
    # ----------------------------------------------------
    def _build_tab_collector(self):
        f_paths = tk.Frame(self.tab_collector, bg=self.c_bg)
        f_paths.pack(fill="x", pady=(0, 4))

        tk.Label(f_paths, text="Carpeta Origen:", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=0, column=0, sticky="w")
        self.col_txt_origen = tk.Entry(f_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.col_txt_origen.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(2, 4))
        tk.Button(f_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.col_txt_origen)).grid(row=1, column=1, pady=(2, 4))

        tk.Label(f_paths, text="Carpeta Destino (Salida):", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=2, column=0, sticky="w")
        self.col_txt_destino = tk.Entry(f_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.col_txt_destino.grid(row=3, column=0, sticky="ew", padx=(0, 6), pady=(2, 4))
        tk.Button(f_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.col_txt_destino)).grid(row=3, column=1, pady=(2, 4))

        tk.Label(f_paths, text="Subcarpetas a excluir (separadas por comas):",
                 font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=4, column=0, sticky="w")
        self.col_txt_exclusiones = tk.Entry(f_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.col_txt_exclusiones.grid(row=5, column=0, sticky="ew", padx=(0, 6), pady=(2, 4))
        tk.Label(f_paths, text="ej: backups, temp", font=("Segoe UI", 8, "italic"),
                 fg=self.c_text_muted, bg=self.c_bg).grid(row=5, column=1, sticky="w", padx=(4, 0))

        f_paths.grid_columnconfigure(0, weight=1)

        # Botón Iniciar Recolección (inmediatamente visible)
        f_act = tk.Frame(self.tab_collector, bg=self.c_bg)
        f_act.pack(fill="x", pady=(2, 4))

        self.btn_col_iniciar = tk.Button(f_act, text="🥝 Iniciar Recolección", font=("Segoe UI", 10, "bold"),
                                         bg=self.c_primary, fg="white", bd=0, cursor="hand2", padx=16, pady=5,
                                         command=self._start_collector)
        self.btn_col_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_col_detener = tk.Button(f_act, text="⏹ Detener", font=("Segoe UI", 9),
                                         bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=5,
                                         state="disabled", command=self._cancel_process)
        self.btn_col_detener.pack(side="right")

        # Opciones
        f_opts = tk.Frame(self.tab_collector, bg=self.c_bg)
        f_opts.pack(fill="x", pady=(0, 4))

        self.col_var_simulacro = tk.BooleanVar(value=True)
        self.col_var_dedup = tk.BooleanVar(value=True)
        self.col_var_cabecera = tk.BooleanVar(value=True)
        self.col_var_replicar = tk.BooleanVar(value=False)

        tk.Checkbutton(f_opts, text="🛡️ Modo Simulacro (No copia, genera informe previo)", variable=self.col_var_simulacro,
                       font=("Segoe UI", 8, "bold"), fg="#1d5e21", bg=self.c_bg).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(f_opts, text="🔍 Deduplicar por SHA-256", variable=self.col_var_dedup,
                       font=("Segoe UI", 8), fg=self.c_text, bg=self.c_bg).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_opts, text="📑 Validar cabecera real %PDF-", variable=self.col_var_cabecera,
                       font=("Segoe UI", 8), fg=self.c_text, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        tk.Checkbutton(f_opts, text="🌳 Replicar subcarpetas", variable=self.col_var_replicar,
                       font=("Segoe UI", 8), fg=self.c_text, bg=self.c_bg).grid(row=1, column=1, sticky="w", padx=(10, 0))

    # ----------------------------------------------------
    # Construcción Pestaña 2: Conversor a Markdown (pdfmd)
    # ----------------------------------------------------
    def _build_tab_converter(self):
        f_conv_paths = tk.Frame(self.tab_converter, bg=self.c_bg)
        f_conv_paths.pack(fill="x", pady=(0, 4))

        # Modo (Archivo o Carpeta)
        self.conv_mode_var = tk.StringVar(value="file")
        f_mode = tk.Frame(f_conv_paths, bg=self.c_bg)
        f_mode.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        tk.Radiobutton(f_mode, text="📄 Archivo PDF individual", variable=self.conv_mode_var, value="file",
                       bg=self.c_bg, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 10))
        tk.Radiobutton(f_mode, text="📚 Carpeta completa (Lote)", variable=self.conv_mode_var, value="folder",
                       bg=self.c_bg, font=("Segoe UI", 8, "bold")).pack(side="left")

        # Entrada
        tk.Label(f_conv_paths, text="PDF o Carpeta de Entrada:", font=("Segoe UI", 8, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        self.conv_txt_in = tk.Entry(f_conv_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.conv_txt_in.grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=(1, 3))
        tk.Button(f_conv_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=self._browse_converter_input).grid(row=2, column=1, pady=(1, 3))

        # Salida
        tk.Label(f_conv_paths, text="Archivo .md o Carpeta de Salida:", font=("Segoe UI", 8, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        self.conv_txt_out = tk.Entry(f_conv_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.conv_txt_out.grid(row=4, column=0, sticky="ew", padx=(0, 6), pady=(1, 3))
        tk.Button(f_conv_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=self._browse_converter_output).grid(row=4, column=1, pady=(1, 3))

        f_conv_paths.grid_columnconfigure(0, weight=1)

        # Botón Iniciar Conversión (directamente tras rutas, SIEMPRE visible)
        f_act_conv = tk.Frame(self.tab_converter, bg=self.c_bg)
        f_act_conv.pack(fill="x", pady=(2, 4))

        self.btn_conv_iniciar = tk.Button(f_act_conv, text="📝 Convertir a Markdown", font=("Segoe UI", 10, "bold"),
                                          bg=self.c_primary, fg="white", bd=0, cursor="hand2", padx=16, pady=5,
                                          command=self._start_converter)
        self.btn_conv_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_conv_detener = tk.Button(f_act_conv, text="⏹ Detener", font=("Segoe UI", 9),
                                          bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=5,
                                          state="disabled", command=self._cancel_process)
        self.btn_conv_detener.pack(side="right")

        # Opciones pdfmd enriquecidas (3 columnas)
        f_conv_opts = tk.LabelFrame(self.tab_converter, text=" Opciones de Conversión pdfmd ", font=("Segoe UI", 8, "bold"),
                                    fg=self.c_dark, bg=self.c_bg, bd=1, relief="solid", padx=6, pady=3)
        f_conv_opts.pack(fill="x", pady=(0, 4))
        f_conv_opts.grid_columnconfigure((0, 1, 2), weight=1)

        self.conv_var_images = tk.BooleanVar(value=True)
        self.conv_var_tables = tk.BooleanVar(value=True)
        self.conv_var_eqs = tk.BooleanVar(value=True)
        self.conv_var_breaks = tk.BooleanVar(value=True)
        self.conv_var_clean = tk.BooleanVar(value=True)
        self.conv_var_frontmatter = tk.BooleanVar(value=True)
        self.conv_var_code = tk.BooleanVar(value=True)
        self.conv_var_footnotes = tk.BooleanVar(value=True)
        self.conv_var_markers = tk.BooleanVar(value=True)
        self.conv_var_cover = tk.BooleanVar(value=True)
        self.conv_var_links = tk.BooleanVar(value=True)
        self.conv_var_hyphen = tk.BooleanVar(value=True)
        self.conv_var_reanudar = tk.BooleanVar(value=True)
        self.conv_var_clean_mode = tk.BooleanVar(value=False)
        self.conv_var_columns = tk.BooleanVar(value=True)
        self.conv_var_obsidian_callouts = tk.BooleanVar(value=False)
        self.conv_var_obsidian_wikilinks = tk.BooleanVar(value=False)
        self.conv_spin_workers = tk.StringVar(value="1")
        self.conv_ocr_mode = tk.StringVar(value="auto")
        self.conv_ocr_lang = tk.StringVar(value="spa+eng")
        self.conv_var_conservar_nombre = tk.BooleanVar(value=False)

        # Fila 0
        tk.Checkbutton(f_conv_opts, text="🖼️ Extraer imágenes a _assets", variable=self.conv_var_images, bg=self.c_bg).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="📊 Detección de tablas (pipe)", variable=self.conv_var_tables, bg=self.c_bg).grid(row=0, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_conv_opts, text="🧮 Fórmulas a LaTeX ($...$)", variable=self.conv_var_eqs, bg=self.c_bg).grid(row=0, column=2, sticky="w", padx=(6, 0))

        # Fila 1
        tk.Checkbutton(f_conv_opts, text="📑 Saltos de página (---)", variable=self.conv_var_breaks, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="📝 Metadatos Frontmatter", variable=self.conv_var_frontmatter, bg=self.c_bg).grid(row=1, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_conv_opts, text="💻 Bloques de código (mono)", variable=self.conv_var_code, bg=self.c_bg).grid(row=1, column=2, sticky="w", padx=(6, 0))

        # Fila 2
        tk.Checkbutton(f_conv_opts, text="🧹 Limpiar cabeceras/pies", variable=self.conv_var_clean, bg=self.c_bg).grid(row=2, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="🔖 Notas al pie ([^1])", variable=self.conv_var_footnotes, bg=self.c_bg).grid(row=2, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_conv_opts, text="📍 Marcador (<!-- p.7 -->)", variable=self.conv_var_markers, bg=self.c_bg).grid(row=2, column=2, sticky="w", padx=(6, 0))

        # Fila 3
        tk.Checkbutton(f_conv_opts, text="📖 Marcar fin portada", variable=self.conv_var_cover, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="🔗 Conservar hipervínculos", variable=self.conv_var_links, bg=self.c_bg).grid(row=3, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_conv_opts, text="✂️ Unir palabras partidas", variable=self.conv_var_hyphen, bg=self.c_bg).grid(row=3, column=2, sticky="w", padx=(6, 0))

        # Fila 4
        tk.Checkbutton(f_conv_opts, text="⏭️ Omitir ya convertidos", variable=self.conv_var_reanudar, bg=self.c_bg).grid(row=4, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="📰 Multi-columna (papers)", variable=self.conv_var_columns, bg=self.c_bg).grid(row=4, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_conv_opts, text="✨ Modo visual limpio", variable=self.conv_var_clean_mode,
                       bg=self.c_bg, font=("Segoe UI", 8, "bold"), fg="#1b5e20",
                       command=self._on_conv_clean_mode_toggle).grid(row=4, column=2, sticky="w", padx=(6, 0))

        # Fila 5
        tk.Checkbutton(f_conv_opts, text="💎 Obsidian Callouts", variable=self.conv_var_obsidian_callouts, bg=self.c_bg).grid(row=5, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="🔗 Obsidian Wikilinks", variable=self.conv_var_obsidian_wikilinks, bg=self.c_bg).grid(row=5, column=1, sticky="w", padx=(6, 0))

        f_cworkers = tk.Frame(f_conv_opts, bg=self.c_bg)
        f_cworkers.grid(row=5, column=2, sticky="w", padx=(6, 0))
        tk.Label(f_cworkers, text="⚡ Hilos:", bg=self.c_bg, font=("Segoe UI", 8)).pack(side="left")
        ttk.Spinbox(f_cworkers, from_=1, to=32, textvariable=self.conv_spin_workers, width=3).pack(side="left", padx=(2, 6))
        tk.Label(f_cworkers, text="OCR:", bg=self.c_bg, font=("Segoe UI", 8)).pack(side="left")
        cb_ocr = ttk.Combobox(f_cworkers, textvariable=self.conv_ocr_mode, values=["off", "auto", "tesseract", "ocrmypdf"], state="readonly", width=8)
        cb_ocr.pack(side="left", padx=(2, 0))
        tk.Entry(f_cworkers, textvariable=self.conv_ocr_lang, width=8, font=("Segoe UI", 8),
                 bd=1, relief="solid").pack(side="left", padx=(4, 0))

        # Fila 6
        tk.Checkbutton(f_conv_opts, text="🏷️ Conservar nombre original", variable=self.conv_var_conservar_nombre,
                       bg=self.c_bg).grid(row=6, column=0, sticky="w")
        tk.Button(f_conv_opts, text="⚡ Perfil Corpus", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                  bd=1, relief="solid", cursor="hand2",
                  command=self._aplicar_perfil_corpus).grid(row=6, column=1, sticky="w", padx=(6, 0))

    # ----------------------------------------------------
    # Construcción Pestaña 3: Pipeline Combinado
    # ----------------------------------------------------
    def _build_tab_pipeline(self):
        f_pipe_paths = tk.Frame(self.tab_pipeline, bg=self.c_bg)
        f_pipe_paths.pack(fill="x", pady=(0, 4))

        tk.Label(f_pipe_paths, text="Árbol Origen con PDFs:", font=("Segoe UI", 8, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=0, column=0, sticky="w")
        self.pipe_txt_origen = tk.Entry(f_pipe_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.pipe_txt_origen.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(1, 3))
        tk.Button(f_pipe_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.pipe_txt_origen)).grid(row=1, column=1, pady=(1, 3))

        tk.Label(f_pipe_paths, text="Carpeta Destino (PDFs + Markdown):", font=("Segoe UI", 8, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=2, column=0, sticky="w")
        self.pipe_txt_destino = tk.Entry(f_pipe_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.pipe_txt_destino.grid(row=3, column=0, sticky="ew", padx=(0, 6), pady=(1, 3))
        tk.Button(f_pipe_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.pipe_txt_destino)).grid(row=3, column=1, pady=(1, 3))

        f_pipe_paths.grid_columnconfigure(0, weight=1)

        # Botón Iniciar Pipeline (inmediatamente visible tras rutas)
        f_act_pipe = tk.Frame(self.tab_pipeline, bg=self.c_bg)
        f_act_pipe.pack(fill="x", pady=(2, 4))

        self.btn_pipe_iniciar = tk.Button(f_act_pipe, text="⚡ Ejecutar Recolección + Conversión a Markdown",
                                          font=("Segoe UI", 10, "bold"), bg="#1d5e21", fg="white", bd=0,
                                          cursor="hand2", padx=16, pady=5, command=self._start_pipeline_combo)
        self.btn_pipe_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_pipe_detener = tk.Button(f_act_pipe, text="⏹ Detener", font=("Segoe UI", 9),
                                          bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=5,
                                          state="disabled", command=self._cancel_process)
        self.btn_pipe_detener.pack(side="right")

        tk.Label(self.tab_pipeline,
                 text="⚡ Este modo recorre todo el árbol de carpetas, deduplica los PDFs por contenido SHA-256 y genera automáticamente su archivo Markdown estructurado (.md) en destino.",
                 font=("Segoe UI", 8, "italic"), fg=self.c_text_muted, bg=self.c_bg, wraplength=750, justify="left").pack(anchor="w", pady=(0, 4))

    # ----------------------------------------------------
    # Construcción Pestaña 4: Web a Markdown (htmlmd)
    # ----------------------------------------------------
    def _build_tab_web(self):
        f_web_paths = tk.Frame(self.tab_web, bg=self.c_bg)
        f_web_paths.pack(fill="x", pady=(0, 4))

        # Modo (una pagina, lista de URLs o carpeta de .html)
        self.web_mode_var = tk.StringVar(value="url")
        f_wmode = tk.Frame(f_web_paths, bg=self.c_bg)
        f_wmode.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        tk.Radiobutton(f_wmode, text="🌐 Una página (URL o .html guardado)", variable=self.web_mode_var,
                       value="url", bg=self.c_bg, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 10))
        tk.Radiobutton(f_wmode, text="📋 Lista (.txt / .csv: URL o ruta;url;id)", variable=self.web_mode_var,
                       value="lista", bg=self.c_bg, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 10))
        tk.Radiobutton(f_wmode, text="📁 Carpeta de archivos .html", variable=self.web_mode_var,
                       value="carpeta", bg=self.c_bg, font=("Segoe UI", 8, "bold")).pack(side="left")

        tk.Label(f_web_paths, text="URL, fichero .html, lista .txt/.csv o carpeta:", font=("Segoe UI", 8, "bold"),
                 fg=self.c_text, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        self.web_txt_in = tk.Entry(f_web_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.web_txt_in.grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=(1, 3))
        tk.Button(f_web_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                  bd=1, relief="solid", cursor="hand2",
                  command=self._browse_web_input).grid(row=2, column=1, pady=(1, 3))

        tk.Label(f_web_paths, text="Archivo .md o Carpeta de Salida:", font=("Segoe UI", 8, "bold"),
                 fg=self.c_text, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        self.web_txt_out = tk.Entry(f_web_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.web_txt_out.grid(row=4, column=0, sticky="ew", padx=(0, 6), pady=(1, 3))
        tk.Button(f_web_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                  bd=1, relief="solid", cursor="hand2",
                  command=self._browse_web_output).grid(row=4, column=1, pady=(1, 3))

        # Procedencia declarada de un .html guardado en disco, que no la trae
        tk.Label(f_web_paths, text="URL de procedencia (solo para .html locales):",
                 font=("Segoe UI", 7), fg=self.c_text_muted, bg=self.c_bg).grid(row=5, column=0, sticky="w")
        self.web_txt_url = tk.Entry(f_web_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.web_txt_url.grid(row=6, column=0, sticky="ew", padx=(0, 6), pady=(1, 3))

        f_web_paths.grid_columnconfigure(0, weight=1)

        # Botón Iniciar Web (directamente tras rutas, SIEMPRE visible)
        f_act_web = tk.Frame(self.tab_web, bg=self.c_bg)
        f_act_web.pack(fill="x", pady=(2, 4))

        self.btn_web_iniciar = tk.Button(f_act_web, text="🌐 Convertir Web a Markdown",
                                         font=("Segoe UI", 10, "bold"), bg="#1d5e21", fg="white", bd=0,
                                         cursor="hand2", padx=16, pady=5, command=self._start_web)
        self.btn_web_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_web_detener = tk.Button(f_act_web, text="⏹ Detener", font=("Segoe UI", 9),
                                         bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=5,
                                         state="disabled", command=self._cancel_process)
        self.btn_web_detener.pack(side="right")

        f_web_opts = tk.LabelFrame(self.tab_web, text=" Opciones de Conversión htmlmd ",
                                   font=("Segoe UI", 8, "bold"), fg=self.c_dark, bg=self.c_bg,
                                   bd=1, relief="solid", padx=6, pady=3)
        f_web_opts.pack(fill="x", pady=(0, 4))
        f_web_opts.grid_columnconfigure((0, 1, 2), weight=1)

        self.web_var_frontmatter = tk.BooleanVar(value=True)
        self.web_var_tables = tk.BooleanVar(value=True)
        self.web_var_links = tk.BooleanVar(value=True)
        self.web_var_code = tk.BooleanVar(value=True)
        self.web_var_images = tk.BooleanVar(value=False)
        self.web_var_alt = tk.BooleanVar(value=True)
        self.web_var_completa = tk.BooleanVar(value=False)
        self.web_var_refrescar = tk.BooleanVar(value=False)
        self.web_var_subcarpetas = tk.BooleanVar(value=True)
        self.web_var_index = tk.BooleanVar(value=True)
        self.web_var_clean_mode = tk.BooleanVar(value=False)
        self.web_var_obsidian_callouts = tk.BooleanVar(value=False)
        self.web_var_obsidian_wikilinks = tk.BooleanVar(value=False)
        self.web_spin_workers = tk.StringVar(value="4")

        # Fila 0
        tk.Checkbutton(f_web_opts, text="📝 Metadatos Frontmatter", variable=self.web_var_frontmatter, bg=self.c_bg).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="📊 Detección de tablas", variable=self.web_var_tables, bg=self.c_bg).grid(row=0, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_web_opts, text="🔗 Conservar hipervínculos", variable=self.web_var_links, bg=self.c_bg).grid(row=0, column=2, sticky="w", padx=(6, 0))

        # Fila 1
        tk.Checkbutton(f_web_opts, text="💻 Bloques de código (<pre>)", variable=self.web_var_code, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="🖼️ Descargar imágenes", variable=self.web_var_images, bg=self.c_bg).grid(row=1, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_web_opts, text="🏷️ Conservar texto alt", variable=self.web_var_alt, bg=self.c_bg).grid(row=1, column=2, sticky="w", padx=(6, 0))

        # Fila 2
        tk.Checkbutton(f_web_opts, text="🧹 Página completa (no podar)", variable=self.web_var_completa, bg=self.c_bg).grid(row=2, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="🔁 Recapturar ya convertidas", variable=self.web_var_refrescar, bg=self.c_bg).grid(row=2, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_web_opts, text="📁 Organizar subcarpetas", variable=self.web_var_subcarpetas, bg=self.c_bg).grid(row=2, column=2, sticky="w", padx=(6, 0))

        # Fila 3
        tk.Checkbutton(f_web_opts, text="📑 Generar índice (INDEX.md)", variable=self.web_var_index, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="💎 Obsidian Callouts", variable=self.web_var_obsidian_callouts, bg=self.c_bg).grid(row=3, column=1, sticky="w", padx=(6, 0))
        tk.Checkbutton(f_web_opts, text="🔗 Obsidian Wikilinks", variable=self.web_var_obsidian_wikilinks, bg=self.c_bg).grid(row=3, column=2, sticky="w", padx=(6, 0))

        # Fila 4
        tk.Checkbutton(f_web_opts, text="✨ Modo visual limpio", variable=self.web_var_clean_mode,
                       bg=self.c_bg, font=("Segoe UI", 8, "bold"), fg="#1b5e20").grid(row=4, column=0, sticky="w")

        f_pausa = tk.Frame(f_web_opts, bg=self.c_bg)
        f_pausa.grid(row=4, column=1, sticky="w", padx=(6, 0))
        tk.Label(f_pausa, text="Pausa (s):", bg=self.c_bg, font=("Segoe UI", 8)).pack(side="left")
        self.web_txt_pausa = tk.Entry(f_pausa, font=("Segoe UI", 9), bd=1, relief="solid", width=5)
        self.web_txt_pausa.insert(0, "0.3")
        self.web_txt_pausa.pack(side="left", padx=(3, 0))

        f_wworkers = tk.Frame(f_web_opts, bg=self.c_bg)
        f_wworkers.grid(row=4, column=2, sticky="w", padx=(6, 0))
        tk.Label(f_wworkers, text="⚡ Hilos:", bg=self.c_bg, font=("Segoe UI", 8)).pack(side="left")
        ttk.Spinbox(f_wworkers, from_=1, to=32, textvariable=self.web_spin_workers, width=3).pack(side="left", padx=(3, 0))

        # Fila 5
        tk.Checkbutton(f_web_opts, text="🏷️ Conservar nombre original", variable=self.conv_var_conservar_nombre,
                       bg=self.c_bg).grid(row=5, column=0, sticky="w")
        tk.Button(f_web_opts, text="⚡ Perfil Corpus", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                  bd=1, relief="solid", cursor="hand2",
                  command=self._aplicar_perfil_corpus).grid(row=5, column=1, sticky="w", padx=(6, 0))

        tk.Label(self.tab_web,
                 text="🌐 Convierte páginas web al mismo Markdown que el conversor de PDF: mismo frontmatter de "
                      "procedencia (URL canónica, fecha y SHA-256), tablas y enlaces conservados.",
                 font=("Segoe UI", 8, "italic"), fg=self.c_text_muted, bg=self.c_bg,
                 wraplength=750, justify="left").pack(anchor="w", pady=(0, 4))

    # ----------------------------------------------------
    # Persistencia de Configuración
    # ----------------------------------------------------
    def _campos_config(self):
        """Mapa clave de configuración -> widget Entry cuyo texto se recuerda."""
        return {
            "col_origen": self.col_txt_origen,
            "col_destino": self.col_txt_destino,
            "col_exclusiones": self.col_txt_exclusiones,
            "pipe_origen": self.pipe_txt_origen,
            "pipe_destino": self.pipe_txt_destino,
            "conv_in": self.conv_txt_in,
            "conv_out": self.conv_txt_out,
            "web_in": self.web_txt_in,
            "web_out": self.web_txt_out,
            "web_url": self.web_txt_url,
            "web_pausa": self.web_txt_pausa,
        }

    def _opciones_config(self):
        """Mapa clave de configuración -> variable booleana de opción."""
        return {
            "col_simulacro": self.col_var_simulacro,
            "col_dedup": self.col_var_dedup,
            "col_cabecera": self.col_var_cabecera,
            "col_replicar": self.col_var_replicar,
            "conv_images": self.conv_var_images,
            "conv_tables": self.conv_var_tables,
            "conv_eqs": self.conv_var_eqs,
            "conv_breaks": self.conv_var_breaks,
            "conv_clean": self.conv_var_clean,
            "conv_frontmatter": self.conv_var_frontmatter,
            "conv_code": self.conv_var_code,
            "conv_footnotes": self.conv_var_footnotes,
            "conv_markers": self.conv_var_markers,
            "conv_cover": self.conv_var_cover,
            "conv_links": self.conv_var_links,
            "conv_hyphen": self.conv_var_hyphen,
            "conv_reanudar": self.conv_var_reanudar,
            "conv_clean_mode": self.conv_var_clean_mode,
            "conv_columns": self.conv_var_columns,
            "conv_obsidian_callouts": self.conv_var_obsidian_callouts,
            "conv_obsidian_wikilinks": self.conv_var_obsidian_wikilinks,
            "conv_conservar_nombre": self.conv_var_conservar_nombre,
            "web_frontmatter": self.web_var_frontmatter,
            "web_tables": self.web_var_tables,
            "web_links": self.web_var_links,
            "web_code": self.web_var_code,
            "web_images": self.web_var_images,
            "web_alt": self.web_var_alt,
            "web_completa": self.web_var_completa,
            "web_refrescar": self.web_var_refrescar,
            "web_subcarpetas": self.web_var_subcarpetas,
            "web_index": self.web_var_index,
            "web_clean_mode": self.web_var_clean_mode,
            "web_obsidian_callouts": self.web_var_obsidian_callouts,
            "web_obsidian_wikilinks": self.web_var_obsidian_wikilinks,
        }

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return

        for key, widget in self._campos_config().items():
            valor = cfg.get(key)
            if isinstance(valor, str) and valor:
                widget.delete(0, tk.END)
                widget.insert(0, valor)

        for key, var in self._opciones_config().items():
            if isinstance(cfg.get(key), bool):
                var.set(cfg[key])

        if cfg.get("ocr_mode") in ("off", "auto", "tesseract", "ocrmypdf"):
            self.conv_ocr_mode.set(cfg["ocr_mode"])
        if isinstance(cfg.get("ocr_lang"), str) and cfg["ocr_lang"].strip():
            self.conv_ocr_lang.set(cfg["ocr_lang"].strip())
        if cfg.get("conv_mode") in ("file", "folder"):
            self.conv_mode_var.set(cfg["conv_mode"])
        if cfg.get("web_mode") in ("url", "lista", "carpeta"):
            self.web_mode_var.set(cfg["web_mode"])

        if "conv_workers" in cfg:
            self.conv_spin_workers.set(str(cfg.get("conv_workers", 1)))
        if "web_workers" in cfg:
            self.web_spin_workers.set(str(cfg.get("web_workers", 1)))
        if cfg.get("dark_mode"):
            self.dark_mode = True
            self._apply_theme()

    def _save_config(self):
        try:
            cfg = {key: widget.get().strip() for key, widget in self._campos_config().items()}
            cfg.update({key: bool(var.get()) for key, var in self._opciones_config().items()})
            cfg["ocr_mode"] = self.conv_ocr_mode.get()
            cfg["ocr_lang"] = self.conv_ocr_lang.get().strip() or "spa+eng"
            cfg["conv_mode"] = self.conv_mode_var.get()
            cfg["web_mode"] = self.web_mode_var.get()
            cfg["conv_workers"] = self.conv_spin_workers.get()
            cfg["web_workers"] = self.web_spin_workers.get()
            cfg["dark_mode"] = self.dark_mode
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("KIWI en ejecución",
                                       "Hay un proceso en curso. ¿Cerrar KIWI y cancelarlo?"):
                return
            self.cancel_event.set()
        self._save_config()
        self.root.destroy()

    def _on_enter_pressed(self):
        current_tab = self.notebook.index(self.notebook.select())
        if current_tab == 0:
            self._start_collector()
        elif current_tab == 1:
            self._start_converter()
        elif current_tab == 2:
            self._start_pipeline_combo()
        elif current_tab == 3:
            self._start_web()

    # ----------------------------------------------------
    # Interacción de UI y Exploradores
    # ----------------------------------------------------
    def _browse_dir(self, entry_widget):
        sel = filedialog.askdirectory(title="Seleccionar carpeta")
        if sel:
            entry_widget.delete(0, tk.END)
            entry_widget.insert(0, os.path.normpath(sel))

    def _browse_converter_input(self):
        mode = self.conv_mode_var.get()
        if mode == "file":
            sel = filedialog.askopenfilename(title="Seleccionar archivo PDF", filetypes=[("Archivos PDF", "*.pdf")])
        else:
            sel = filedialog.askdirectory(title="Seleccionar carpeta con PDFs")
        if sel:
            self.conv_txt_in.delete(0, tk.END)
            self.conv_txt_in.insert(0, os.path.normpath(sel))
            if mode == "file":
                base, _ = os.path.splitext(sel)
                self.conv_txt_out.delete(0, tk.END)
                self.conv_txt_out.insert(0, base + ".md")
            else:
                out_sug = os.path.join(sel, "markdown_export")
                self.conv_txt_out.delete(0, tk.END)
                self.conv_txt_out.insert(0, out_sug)

    def _browse_web_input(self):
        modo = self.web_mode_var.get()
        if modo == "lista":
            sel = filedialog.askopenfilename(title="Seleccionar lista de URLs o rutas",
                                             filetypes=[("Listas", "*.txt;*.csv"), ("Todos", "*.*")])
        elif modo == "carpeta":
            sel = filedialog.askdirectory(title="Seleccionar carpeta con archivos .html")
        else:
            sel = filedialog.askopenfilename(title="Seleccionar página HTML guardada",
                                             filetypes=[("HTML", "*.html;*.htm"), ("Todos", "*.*")])
        if not sel:
            return
        self.web_txt_in.delete(0, tk.END)
        self.web_txt_in.insert(0, os.path.normpath(sel))

        # Sugerencia de salida coherente con el modo elegido
        self.web_txt_out.delete(0, tk.END)
        if modo == "lista":
            self.web_txt_out.insert(0, os.path.join(os.path.dirname(sel), "markdown_web"))
        elif modo == "carpeta":
            self.web_txt_out.insert(0, os.path.join(sel, "markdown_web"))
        else:
            self.web_txt_out.insert(0, os.path.splitext(sel)[0] + ".md")

    def _browse_web_output(self):
        if self.web_mode_var.get() in ("lista", "carpeta"):
            sel = filedialog.askdirectory(title="Carpeta de salida para las capturas")
        else:
            sel = filedialog.asksaveasfilename(title="Guardar como Markdown", defaultextension=".md",
                                               filetypes=[("Markdown", "*.md")])
        if sel:
            self.web_txt_out.delete(0, tk.END)
            self.web_txt_out.insert(0, os.path.normpath(sel))

    def _browse_converter_output(self):
        mode = self.conv_mode_var.get()
        if mode == "file":
            sel = filedialog.asksaveasfilename(title="Guardar como Markdown", defaultextension=".md",
                                               filetypes=[("Markdown", "*.md")])
        else:
            sel = filedialog.askdirectory(title="Seleccionar carpeta de salida para Markdown")
        if sel:
            self.conv_txt_out.delete(0, tk.END)
            self.conv_txt_out.insert(0, os.path.normpath(sel))

    def _log_msg(self, text, tag="info"):
        self.txt_log.insert(tk.END, text + "\n", tag)
        if text.strip():
            self.lbl_fase.config(text=text.strip()[:110])
        # En lotes de miles de PDFs el log crece sin control: se conserva la cola
        lineas = int(self.txt_log.index("end-1c").split(".")[0])
        if lineas > MAX_LINEAS_LOG:
            self.txt_log.delete("1.0", f"{lineas - MAX_LINEAS_LOG}.0")
        self.txt_log.see(tk.END)

    def _set_running_state(self, running=True):
        state_btn = "disabled" if running else "normal"
        state_stop = "normal" if running else "disabled"

        self.btn_col_iniciar.config(state=state_btn)
        self.btn_col_detener.config(state=state_stop)
        self.btn_conv_iniciar.config(state=state_btn)
        self.btn_conv_detener.config(state=state_stop)
        self.btn_pipe_iniciar.config(state=state_btn)
        self.btn_pipe_detener.config(state=state_stop)
        self.btn_web_iniciar.config(state=state_btn)
        self.btn_web_detener.config(state=state_stop)

        if running:
            self.prog_bar.config(mode="indeterminate")
            self.prog_bar.start(10)
            self.btn_open_dest.config(state="disabled")
            self.btn_open_csv.config(state="disabled")
            self.btn_open_md.config(state="disabled")
            self.btn_view_md.config(state="disabled")
        else:
            self.prog_bar.stop()

    def _cancel_process(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if messagebox.askyesno("Detener", "¿Deseas cancelar el proceso en curso?"):
                self.cancel_event.set()
                self._log_msg("Solicitando la cancelación...", "warning")

    # ----------------------------------------------------
    # Ejecutores de Tareas
    # ----------------------------------------------------
    def _start_collector(self):
        orig = self.col_txt_origen.get().strip()
        dest = self.col_txt_destino.get().strip()
        if not orig or not dest:
            messagebox.showwarning("Atención", "Especifica la carpeta Origen y Destino.")
            return

        self.current_start_time = time.time()
        self.task_row_map.clear()
        self.task_meta_map.clear()
        for it in self.tree_tasks.get_children():
            self.tree_tasks.delete(it)
        self._refresh_queue_summary()

        self.cancel_event.clear()
        self.txt_log.delete("1.0", tk.END)
        self._set_running_state(True)

        runner = KiwiRunner(
            origen=orig,
            destino=dest,
            simulacro=self.col_var_simulacro.get(),
            dedup=self.col_var_dedup.get(),
            validar_cabecera=self.col_var_cabecera.get(),
            replicar=self.col_var_replicar.get(),
            exclusiones=[e for e in self.col_txt_exclusiones.get().split(",") if e.strip()],
            cancel_event=self.cancel_event,
            progress_cb=self._enqueue_event
        )
        self.worker_thread = threading.Thread(target=runner.run, daemon=True)
        self.worker_thread.start()

    def _aplicar_perfil_corpus(self):
        """Ajustes para lotes de corpus: OCR auto, spa+eng, nombres originales y trazabilidad."""
        self.conv_ocr_mode.set("auto")
        self.conv_ocr_lang.set("spa+eng")
        self.conv_var_conservar_nombre.set(True)
        self.conv_var_reanudar.set(True)
        self.conv_var_frontmatter.set(True)
        self.conv_var_clean_mode.set(False)
        self.conv_var_markers.set(True)
        self.conv_var_clean.set(True)
        self.web_var_frontmatter.set(True)
        self.web_var_subcarpetas.set(False)
        self.web_var_index.set(False)
        self.web_var_clean_mode.set(False)
        self._save_config()
        self._log_msg(
            "⚡ Perfil Corpus aplicado y guardado: OCR auto (spa+eng), conservar nombres originales, "
            "frontmatter de procedencia activo y lote web plano sin INDEX.md.",
            "success"
        )

    def _chequear_dependencias(self):
        """Avisa al arrancar si falta algo del OCR y actualiza la insignia visual de estado."""
        diag = diagnosticar_entorno_ocr()
        avisos = diag.get("avisos", [])
        langs = diag.get("tessdata_langs", [])
        if hasattr(self, "btn_ocr_badge"):
            if diag.get("ok"):
                self.btn_ocr_badge.config(
                    text=f"✅ OCR Listo ({len(langs)} idiomas)",
                    bg="#e8f5e9" if not self.dark_mode else "#1b3b22",
                    fg="#1b5e20" if not self.dark_mode else "#81c784"
                )
            else:
                self.btn_ocr_badge.config(
                    text="⚠️ Revisar OCR",
                    bg="#fff8e1" if not self.dark_mode else "#3b3014",
                    fg="#e65100" if not self.dark_mode else "#ffd54f"
                )
        for aviso in avisos:
            self._log_msg(f"⚠️ {aviso}", "warning")
        if avisos:
            messagebox.showwarning("KIWI — dependencias de OCR", "\n\n".join(avisos))

    def _mostrar_diagnostico_ocr(self):
        """Muestra una ventana o cuadro informativo con el diagnóstico detallado del entorno OCR."""
        diag = diagnosticar_entorno_ocr()
        langs = ", ".join(diag.get("tessdata_langs", [])) or "(ninguno)"
        estado = "✅ ENTORNO OCR COMPLETO Y OPERATIVO" if diag.get("ok") else "⚠️ ENTORNO OCR CON AVISOS"
        lineas = [
            estado,
            "",
            f"• Motor principal: {diag.get('ocrmypdf_version') or '(no disponible)'}",
            f"• Binario ocrmypdf: {diag.get('ocrmypdf_bin') or '(no encontrado)'}",
            f"• Binario Tesseract: {diag.get('tesseract_bin') or '(no encontrado)'}",
            f"• Ghostscript (gswin64c): {diag.get('ghostscript_bin') or '(no encontrado)'}",
            f"• TESSDATA_PREFIX activo: {diag.get('tessdata_prefix') or '(no definido)'}",
            f"• configs/hocr presente: {'Sí' if diag.get('has_hocr_config') else 'NO (causa fallo hocr)'}",
            f"• pdf.ttf presente: {'Sí' if diag.get('has_pdf_ttf') else 'No'}",
            f"• Idiomas instalados ({len(diag.get('tessdata_langs', []))}): {langs}",
        ]
        if diag.get("avisos"):
            lineas.append("\nAvisos detectados:")
            for av in diag["avisos"]:
                lineas.append(f"  - {av}")
        messagebox.showinfo("KIWI — Diagnóstico de Entorno OCR", "\n".join(lineas))

    def _avisar_ocr_no_disponible(self, modo):
        """Advierte si se pide un OCR cuyo motor no está instalado en el equipo."""
        if modo in ("auto", "tesseract") and not is_tesseract_available():
            messagebox.showwarning(
                "OCR no disponible",
                "Tesseract no está instalado o no se encuentra en el PATH.\n"
                "La conversión continuará sin OCR en las páginas escaneadas."
            )
        elif modo == "ocrmypdf" and not is_ocrmypdf_available():
            messagebox.showwarning(
                "OCR no disponible",
                "ocrmypdf no está instalado o no se encuentra en el PATH.\n"
                "La conversión continuará sin OCR."
            )

    def _on_conv_clean_mode_toggle(self):
        if self.conv_var_clean_mode.get():
            self.conv_var_breaks.set(False)
            self.conv_var_markers.set(False)
            self.conv_var_cover.set(False)

    def _start_converter(self):
        in_path = self.conv_txt_in.get().strip()
        out_path = self.conv_txt_out.get().strip()
        mode = self.conv_mode_var.get()

        if not in_path or not out_path:
            messagebox.showwarning("Atención", "Especifica la Entrada y la Salida para la conversión.")
            return

        items = []
        if mode == "file":
            if not os.path.isfile(in_path):
                messagebox.showerror("Error", f"No se encontró el archivo:\n{in_path}")
                return
            out_md = os.path.abspath(out_path)
            if not out_md.lower().endswith(".md"):
                out_md += ".md"
            items = [(in_path, out_md)]
            out_dir = os.path.dirname(out_md) or os.getcwd()
        else:
            if not os.path.isdir(in_path):
                messagebox.showerror("Error", f"No se encontró la carpeta:\n{in_path}")
                return
            for raiz, _, ficheros in os.walk(in_path):
                for f in sorted(ficheros):
                    if f.lower().endswith(".pdf"):
                        items.append(os.path.join(raiz, f))
            out_dir = out_path

        if not items:
            messagebox.showinfo("Sin PDFs", "No se encontraron archivos PDF para convertir.")
            return

        self._avisar_ocr_no_disponible(self.conv_ocr_mode.get())

        self.current_start_time = time.time()
        self.task_row_map.clear()
        self.task_meta_map.clear()
        for it in self.tree_tasks.get_children():
            self.tree_tasks.delete(it)
        self._refresh_queue_summary()

        opts = PdfmdOptions(
            ocr_mode=self.conv_ocr_mode.get(),
            ocr_lang=self.conv_ocr_lang.get().strip() or "spa+eng",
            export_images=self.conv_var_images.get(),
            detect_tables=self.conv_var_tables.get(),
            convert_equations=self.conv_var_eqs.get(),
            insert_page_breaks=self.conv_var_breaks.get(),
            remove_headers_footers=self.conv_var_clean.get(),
            include_frontmatter=self.conv_var_frontmatter.get(),
            detect_code_blocks=self.conv_var_code.get(),
            detect_footnotes=self.conv_var_footnotes.get(),
            page_markers=self.conv_var_markers.get(),
            mark_cover_end=self.conv_var_cover.get(),
            extract_links=self.conv_var_links.get(),
            join_hyphens=self.conv_var_hyphen.get(),
            clean_mode=self.conv_var_clean_mode.get(),
            detect_columns=self.conv_var_columns.get(),
            obsidian_callouts=self.conv_var_obsidian_callouts.get(),
            obsidian_wikilinks=self.conv_var_obsidian_wikilinks.get()
        )

        self.cancel_event.clear()
        self.txt_log.delete("1.0", tk.END)
        self._set_running_state(True)

        workers = max(1, int(self.conv_spin_workers.get() or 1))
        runner = KiwiConverterRunner(
            items=items,
            output_dir=out_dir,
            options=opts,
            cancel_event=self.cancel_event,
            progress_cb=self._enqueue_event,
            reanudar=self.conv_var_reanudar.get(),
            usar_slug=not self.conv_var_conservar_nombre.get(),
            max_workers=workers
        )
        self.last_conv_opts = opts
        self.worker_thread = threading.Thread(target=runner.run, daemon=True)
        self.worker_thread.start()

    def _repetir_con_ocr(self, items, output_dir):
        """Reconvierte solo los documentos sin texto o escasos, con OCR en todas las páginas."""
        base = getattr(self, "last_conv_opts", None) or PdfmdOptions()
        motor = "ocrmypdf" if is_ocrmypdf_available() else "tesseract"
        opts = dataclasses.replace(base, ocr_mode=motor,
                                   ocr_lang=self.conv_ocr_lang.get().strip() or "spa+eng")
        self.current_start_time = time.time()
        self.cancel_event.clear()
        self._set_running_state(True)
        self._log_msg(f"Repitiendo con OCR ({motor}) {len(items)} documento(s)...")
        runner = KiwiConverterRunner(
            items=[(pdf, md) for pdf, md in items if md],
            output_dir=output_dir,
            options=opts,
            cancel_event=self.cancel_event,
            progress_cb=self._enqueue_event,
            reanudar=False,
            max_workers=max(1, int(self.conv_spin_workers.get() or 1)),
        )
        self.worker_thread = threading.Thread(target=runner.run, daemon=True)
        self.worker_thread.start()

    def _opciones_web(self):
        """Traduce los controles de la pestaña a las opciones de htmlmd."""
        completa = self.web_var_completa.get()
        return WebOptions(
            incluir_frontmatter=self.web_var_frontmatter.get(),
            clean_mode=self.web_var_clean_mode.get(),
            obsidian_callouts=self.web_var_obsidian_callouts.get(),
            obsidian_wikilinks=self.web_var_obsidian_wikilinks.get(),
            detectar_tablas=self.web_var_tables.get(),
            conservar_enlaces=self.web_var_links.get(),
            detectar_codigo=self.web_var_code.get(),
            exportar_imagenes=self.web_var_images.get(),
            conservar_alt=self.web_var_alt.get(),
            quitar_cromo=not completa,
            solo_cuerpo=not completa,
        )

    def _start_web(self):
        entrada = self.web_txt_in.get().strip()
        salida = self.web_txt_out.get().strip()
        modo = self.web_mode_var.get()

        if not entrada or not salida:
            messagebox.showwarning("Atención", "Especifica la Entrada y la Salida para la conversión web.")
            return

        try:
            pausa = max(0.0, float(self.web_txt_pausa.get().strip().replace(",", ".") or 0))
        except ValueError:
            messagebox.showwarning("Atención", "La espera entre peticiones debe ser un número de segundos.")
            return

        items, salida_fija = [], None
        if modo == "lista":
            if not os.path.isfile(entrada):
                messagebox.showerror("Error", f"No se encontró la lista de URLs:\n{entrada}")
                return
            try:
                items = leer_lista_estructurada(entrada)
            except OSError as ex:
                messagebox.showerror("Error", f"No se pudo leer la lista:\n{ex}")
                return
            if not items:
                messagebox.showinfo("Lista vacía", "El fichero no contiene ninguna URL ni captura local.")
                return
            out_dir = salida
        elif modo == "carpeta" or (os.path.isdir(entrada) and not es_url(entrada)):
            if not os.path.isdir(entrada):
                messagebox.showerror("Error", f"No se encontró la carpeta:\n{entrada}")
                return
            for raiz, _dirs, ficheros in os.walk(entrada):
                for f in sorted(ficheros):
                    if f.lower().endswith((".html", ".htm")):
                        items.append(os.path.join(raiz, f))
            if not items:
                messagebox.showinfo("Sin HTML", "No se encontraron archivos .html/.htm en la carpeta.")
                return
            out_dir = salida
        else:
            if not es_url(entrada) and not os.path.isfile(entrada):
                messagebox.showerror("Error",
                                     f"No es una URL ni un fichero existente:\n{entrada}")
                return
            items = [entrada]
            salida_fija = os.path.abspath(salida)
            if not salida_fija.lower().endswith(".md"):
                salida_fija += ".md"
            out_dir = os.path.dirname(salida_fija) or os.getcwd()

        self.current_start_time = time.time()
        self.task_row_map.clear()
        self.task_meta_map.clear()
        for it in self.tree_tasks.get_children():
            self.tree_tasks.delete(it)
        self._refresh_queue_summary()

        self.cancel_event.clear()
        self.txt_log.delete("1.0", tk.END)
        self._set_running_state(True)

        url_declarada = self.web_txt_url.get().strip()
        opts = self._opciones_web()
        workers = max(1, int(self.web_spin_workers.get() or 1))

        runner = KiwiWebRunner(
            items=items,
            output_dir=out_dir,
            options=opts,
            cancel_event=self.cancel_event,
            progress_cb=self._enqueue_event,
            salida_fija=salida_fija,
            refrescar=self.web_var_refrescar.get(),
            pausa=pausa,
            organizar_por_secciones=self.web_var_subcarpetas.get(),
            generar_index=self.web_var_index.get(),
            url_origen=(url_declarada if not es_url(entrada) else ""),
            max_workers=workers,
            conservar_nombre=self.conv_var_conservar_nombre.get(),
        )

        self.worker_thread = threading.Thread(target=runner.run, daemon=True)
        self.worker_thread.start()

    def _start_pipeline_combo(self):
        orig = self.pipe_txt_origen.get().strip()
        dest = self.pipe_txt_destino.get().strip()
        if not orig or not dest:
            messagebox.showwarning("Atención", "Especifica la carpeta Origen y Destino.")
            return

        self.current_start_time = time.time()
        self.task_row_map.clear()
        self.task_meta_map.clear()
        for it in self.tree_tasks.get_children():
            self.tree_tasks.delete(it)
        self._refresh_queue_summary()

        self.cancel_event.clear()
        self.txt_log.delete("1.0", tk.END)
        self._set_running_state(True)

        self.pipeline_active = True

        def combo_log(texto, tag="info"):
            self._enqueue_event("log", {"text": texto, "tag": tag})

        def combo_thread():
            combo_log("=== PASO 1/2: Recolección y Deduplicación de PDFs ===")
            col_runner = KiwiRunner(
                origen=orig,
                destino=dest,
                simulacro=False,
                dedup=True,
                validar_cabecera=self.col_var_cabecera.get(),
                replicar=False,
                exclusiones=[e for e in self.col_txt_exclusiones.get().split(",") if e.strip()],
                cancel_event=self.cancel_event,
                progress_cb=self._enqueue_event
            )
            col_runner.run()

            if self.cancel_event.is_set():
                return

            combo_log("")
            combo_log("=== PASO 2/2: Conversión a Markdown de PDFs recolectados ===")
            try:
                pdf_files = sorted(
                    os.path.join(dest, f) for f in os.listdir(dest) if f.lower().endswith(".pdf")
                )
            except OSError as ex:
                combo_log(f"No se pudo leer la carpeta destino: {ex}", "error")
                self._enqueue_event("error", {"message": str(ex)})
                return

            if not pdf_files:
                combo_log("No hay PDFs para convertir a Markdown.", "warning")
                self._enqueue_event("conv_finished", {
                    "total": 0, "converted": 0, "errors": 0,
                    "output_dir": dest, "last_md": None
                })
                return

            opts = PdfmdOptions(
                ocr_mode=self.conv_ocr_mode.get(),
                ocr_lang=self.conv_ocr_lang.get().strip() or "spa+eng",
                export_images=self.conv_var_images.get(),
                detect_tables=self.conv_var_tables.get(),
                convert_equations=self.conv_var_eqs.get(),
                insert_page_breaks=self.conv_var_breaks.get(),
                remove_headers_footers=self.conv_var_clean.get(),
                include_frontmatter=self.conv_var_frontmatter.get(),
                detect_code_blocks=self.conv_var_code.get(),
                detect_footnotes=self.conv_var_footnotes.get(),
                page_markers=self.conv_var_markers.get(),
                mark_cover_end=self.conv_var_cover.get(),
                extract_links=self.conv_var_links.get(),
                join_hyphens=self.conv_var_hyphen.get(),
                clean_mode=self.conv_var_clean_mode.get(),
                detect_columns=self.conv_var_columns.get(),
                obsidian_callouts=self.conv_var_obsidian_callouts.get(),
                obsidian_wikilinks=self.conv_var_obsidian_wikilinks.get()
            )
            workers = max(1, int(self.conv_spin_workers.get() or 1))
            conv_runner = KiwiConverterRunner(
                items=pdf_files,
                output_dir=dest,
                options=opts,
                cancel_event=self.cancel_event,
                progress_cb=self._enqueue_event,
                reanudar=self.conv_var_reanudar.get(),
                usar_slug=not self.conv_var_conservar_nombre.get(),
                max_workers=workers
            )
            conv_runner.run()

        self.worker_thread = threading.Thread(target=combo_thread, daemon=True)
        self.worker_thread.start()

    # ----------------------------------------------------
    # Procesamiento de Eventos y Callbacks
    # ----------------------------------------------------
    def _enqueue_event(self, event_type, data):
        self.queue.put((event_type, data))

    def _process_queue(self):
        try:
            while not self.queue.empty():
                event_type, data = self.queue.get_nowait()
                if event_type == "log":
                    self._log_msg(data.get("text", ""), data.get("tag", "info"))
                elif event_type == "task_update":
                    tid = data.get("task_id", "")
                    estado = data.get("estado", "")
                    tipo = data.get("tipo", "")
                    origen = data.get("origen", "")
                    salida = data.get("salida", "")
                    detalle = data.get("detalle", "")
                    out_full = data.get("out_full", "")

                    if tid in self.task_row_map:
                        item_id = self.task_row_map[tid]
                        cur = self.tree_tasks.item(item_id, "values")
                        new_est = estado or cur[0]
                        new_tip = tipo or cur[1]
                        new_ori = origen or cur[2]
                        new_sal = salida or cur[3]
                        new_det = detalle or cur[4]
                        tag_row = self._tag_for_estado(new_est)
                        self.tree_tasks.item(
                            item_id,
                            values=(new_est, new_tip, new_ori, new_sal, new_det),
                            tags=(tag_row,)
                        )
                        if out_full:
                            self.task_meta_map[item_id]["out_full"] = out_full
                    else:
                        tag_row = self._tag_for_estado(estado)
                        item_id = self.tree_tasks.insert(
                            "", tk.END,
                            values=(estado, tipo, origen, salida, detalle),
                            tags=(tag_row,)
                        )
                        self.task_row_map[tid] = item_id
                        self.task_meta_map[item_id] = {"out_full": out_full, "origen": origen}

                    if estado.startswith(("✅", "⚠️")):
                        p_delta = int(data.get("pages_delta", 0) or (1 if tipo == "Web -> MD" else 0))
                        e_delta = int(data.get("elem_delta", 0) or 0)
                        self._update_kpis(docs_delta=1, pages_delta=p_delta, elem_delta=e_delta)
                    self._refresh_queue_summary()

                elif event_type == "phase":
                    p = data.get("phase", 1)
                    t = data.get("title", "")
                    self.lbl_fase.config(text=f"Fase {p}: {t}")
                elif event_type == "progress_scan":
                    cnt = data.get("count", 0)
                    self.lbl_fase.config(text=f"Escaneando... {cnt:,} PDFs localizados")
                elif event_type == "progress_dedup":
                    cur = data.get("current", 0)
                    tot = data.get("total", 1)
                    self.prog_bar.stop()
                    self.prog_bar.config(mode="determinate", maximum=tot, value=cur)
                elif event_type == "progress_copy":
                    cur = data.get("current", 0)
                    tot = data.get("total", 1)
                    self.prog_bar.stop()
                    self.prog_bar.config(mode="determinate", maximum=tot, value=cur)
                elif event_type == "progress_doc":
                    cur_f = data.get("current_file", 1)
                    tot_f = data.get("total_files", 1)
                    p_done = data.get("page_done", 1)
                    p_tot = data.get("page_total", 1)
                    self.prog_bar.stop()
                    self.prog_bar.config(mode="determinate", maximum=p_tot, value=p_done)
                    self.lbl_fase.config(text=f"Doc {cur_f}/{tot_f} — Pág {p_done}/{p_tot}")
                elif event_type == "finished":
                    self._on_collector_finished(data)
                elif event_type == "conv_finished":
                    self._on_converter_finished(data)
                elif event_type == "web_finished":
                    self._on_web_finished(data)
                elif event_type == "cancelled":
                    self.pipeline_active = False
                    self._set_running_state(False)
                    self.lbl_fase.config(text="Operación cancelada.")
                elif event_type == "error":
                    self.pipeline_active = False
                    self._set_running_state(False)
                    self.lbl_fase.config(text="Error en la ejecución.")
                    messagebox.showerror("KIWI — Error", data.get("message", "Error desconocido."))
        finally:
            self.root.after(100, self._process_queue)

    def _on_collector_finished(self, stats):
        if self.current_start_time:
            self._update_kpis(time_delta=time.time() - self.current_start_time)
            self.current_start_time = None

        self.last_dest = stats.get("destino")
        self.last_report = stats.get("informe")

        if self.last_dest and os.path.exists(self.last_dest):
            self.btn_open_dest.config(state="normal")
        if self.last_report and os.path.exists(self.last_report):
            self.btn_open_csv.config(state="normal")

        modo_txt = "Simulacro finalizado" if stats.get("simulacro") else "Recolección completada"
        self._log_msg(f"✅ {modo_txt}: {stats.get('copiados', 0)} archivos procesados.", "success")

        if self.pipeline_active:
            self.lbl_fase.config(text=f"{modo_txt}. Continuando con la conversión...")
            return

        self._set_running_state(False)
        self.lbl_fase.config(text=f"✅ {modo_txt}.")
        messagebox.showinfo("KIWI — Recolección",
                            f"{modo_txt}.\n\n"
                            f"• Localizados: {stats.get('encontrados', 0)}\n"
                            f"• Procesados: {stats.get('copiados', 0)}\n"
                            f"• Duplicados omitidos: {stats.get('duplicados', 0)}\n"
                            f"• Ya existentes: {stats.get('ya_estaban', 0)}")

    def _on_converter_finished(self, stats):
        if self.current_start_time:
            self._update_kpis(time_delta=time.time() - self.current_start_time)
            self.current_start_time = None

        self.pipeline_active = False
        self._set_running_state(False)
        self.last_dest = stats.get("output_dir")
        self.last_md = stats.get("last_md")

        if self.last_dest and os.path.exists(self.last_dest):
            self.btn_open_dest.config(state="normal")
        if self.last_md and os.path.exists(self.last_md):
            self.btn_open_md.config(state="normal")
            self.btn_view_md.config(state="normal")

        self.lbl_fase.config(text="✅ Conversión a Markdown completada.")
        self._log_msg(f"✅ Conversión finalizada: {stats.get('converted', 0)}/{stats.get('total', 0)} convertidos.", "success")
        capas = stats.get("capas") or {}
        resumen_capas = ", ".join(f"{n} {capa}" for capa, n in sorted(capas.items()))
        if resumen_capas:
            self._log_msg(f"Capa de texto: {resumen_capas}")
        texto = (f"Conversión completada.\n\n"
                 f"• Archivos convertidos: {stats.get('converted', 0)}\n"
                 f"• Omitidos (ya al día): {stats.get('skipped', 0)}\n"
                 f"• Errores: {stats.get('errors', 0)}\n"
                 f"• Capa de texto: nativa {capas.get('nativa', 0)}, OCR {capas.get('ocr', 0)}, "
                 f"escasa {capas.get('escasa', 0)}, sin texto {capas.get('ausente', 0)}\n"
                 f"• Carpeta de salida: {stats.get('output_dir', '')}")
        sin_texto = stats.get("sin_texto") or []
        if sin_texto and (is_tesseract_available() or is_ocrmypdf_available()):
            if messagebox.askyesno("KIWI — pdfmd",
                                   texto + f"\n\n¿Repetir con OCR solo esos {len(sin_texto)} "
                                           f"documento(s) escasos o sin texto?"):
                self._repetir_con_ocr(sin_texto, stats.get("output_dir", ""))
            return
        messagebox.showinfo("KIWI — pdfmd", texto)

    def _on_web_finished(self, stats):
        if self.current_start_time:
            self._update_kpis(time_delta=time.time() - self.current_start_time)
            self.current_start_time = None

        self._set_running_state(False)
        self.last_dest = stats.get("output_dir")
        self.last_md = stats.get("last_md")

        if self.last_dest and os.path.exists(self.last_dest):
            self.btn_open_dest.config(state="normal")
        if self.last_md and os.path.exists(self.last_md):
            self.btn_open_md.config(state="normal")
            self.btn_view_md.config(state="normal")

        escasas = stats.get("escasas", 0)
        self.lbl_fase.config(text="✅ Conversión web a Markdown completada.")
        self._log_msg(f"✅ Conversión web finalizada: {stats.get('converted', 0)}/"
                      f"{stats.get('total', 0)} capturadas.", "success")

        detalle = (f"Conversión web completada.\n\n"
                   f"• Páginas capturadas: {stats.get('converted', 0)}\n"
                   f"• Omitidas por estar ya convertidas: {stats.get('skipped', 0)}\n"
                   f"• Fallidas: {stats.get('errors', 0)}\n"
                   f"• Carpeta de salida: {stats.get('output_dir', '')}")
        if escasas:
            detalle += (f"\n\n⚠️ {escasas} con contenido escaso o fallido. "
                        "Suelen ser páginas que se pintan con JavaScript o con muro de pago. "
                        "Revísalas contra el original.")
        messagebox.showinfo("KIWI — htmlmd", detalle)

    def _open_destination(self):
        abrir_en_sistema(self.last_dest)

    def _open_report(self):
        abrir_en_sistema(self.last_report)

    def _open_markdown(self):
        abrir_en_sistema(self.last_md)

    def _view_markdown_inline(self):
        if self.last_md and os.path.exists(self.last_md):
            self.load_in_viewer(self.last_md)


# --------------------------------------------------------------------------
# CLI y Punto de Entrada Principal
# --------------------------------------------------------------------------
def launch_gui():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    if HAS_DND:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()
    app = KiwiAppGUI(root)
    root.mainloop()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="kiwi_app.py",
        description="KIWI — Suite de Gestión y Conversión de PDFs a Markdown",
        epilog="Sin argumentos abre la interfaz gráfica."
    )
    parser.add_argument("--check-ocr", action="store_true",
                        help="Verifica el entorno de OCR (Tesseract, ocrmypdf, Ghostscript, tessdata, hocr) y termina")
    subparsers = parser.add_subparsers(dest="command", help="Comando a ejecutar")

    # Subcomando: convert
    p_conv = subparsers.add_parser("convert", help="Convierte un archivo PDF a Markdown estructurado")
    p_conv.add_argument("input_pdf", nargs="?", help="Ruta al archivo PDF o carpeta (con --lote)")
    p_conv.add_argument("-o", "--output", dest="output_md", help="Ruta del archivo Markdown de salida")
    p_conv.add_argument("--ocr", action="store_true",
                        help="Fuerza OCR (usa ocrmypdf o tesseract sobre el documento)")
    p_conv.add_argument("--ocr-mode", choices=["off", "auto", "tesseract", "ocrmypdf"], default="auto",
                        help="auto: OCR en páginas sin texto y reintento si el PDF sale sin capa")
    p_conv.add_argument("-l", "--ocr-lang", dest="ocr_lang", default="spa+eng",
                        help="Idioma para el OCR (ej: spa, spa+eng)")
    p_conv.add_argument("--check-ocr", action="store_true",
                        help="Verifica el entorno de OCR y termina")
    p_conv.add_argument("--password", help="Contraseña para PDFs cifrados")
    p_conv.add_argument("--preview-only", action="store_true", help="Procesa solo las primeras páginas")
    p_conv.add_argument("--export-images", action="store_true")
    p_conv.add_argument("--insert-page-breaks", action="store_true")
    p_conv.add_argument("--no-tables", action="store_true", help="Desactiva la detección de tablas")
    p_conv.add_argument("--no-equations", action="store_true", help="Desactiva la conversión a LaTeX")
    p_conv.add_argument("--no-headers-footers", action="store_true",
                        help="Conserva cabeceras y pies de página recurrentes")
    p_conv.add_argument("--no-frontmatter", action="store_true")
    p_conv.add_argument("--no-code-blocks", action="store_true")
    p_conv.add_argument("--no-footnotes", action="store_true",
                        help="Deja las notas al pie en su sitio, sin recolectarlas al final")
    p_conv.add_argument("--no-page-markers", action="store_true",
                        help="Omite los comentarios <!-- p.N --> de cada pagina")
    p_conv.add_argument("--no-cover-mark", action="store_true",
                        help="No intenta marcar el fin de la portada")
    p_conv.add_argument("--no-hash", action="store_true", help="Omite el SHA-256 del PDF")
    p_conv.add_argument("--no-slug", "--conservar-nombre", dest="no_slug", action="store_true",
                        help="Conserva el nombre original en vez de normalizarlo a ASCII")
    p_conv.add_argument("--lote", metavar="CARPETA_SALIDA",
                        help="Convierte en lote: input_pdf pasa a ser una carpeta de PDFs")
    p_conv.add_argument("--no-reanudar", action="store_true",
                        help="En modo lote, reconvierte también los que ya están al día")
    p_conv.add_argument("--manifiesto", default="manifiesto_conversion.tsv",
                        help="Nombre del TSV de control que deja el lote")
    p_conv.add_argument("--no-links", action="store_true",
                        help="No extrae los hipervínculos del PDF")
    p_conv.add_argument("--no-join-hyphens", action="store_true",
                        help="Conserva las palabras partidas por el guión de fin de línea")
    p_conv.add_argument("--no-lang", action="store_true",
                        help="No detecta el idioma del documento")
    p_conv.add_argument("--modo-limpio", "--clean", dest="clean_mode", action="store_true",
                        help="Genera Markdown visualmente limpio (sin comentarios <!-- p.N -->, sin saltos ---, frontmatter minimalista y párrafos unidos)")
    p_conv.add_argument("--workers", type=int, default=1,
                        help="Número de hilos concurrentes para procesar el lote")
    p_conv.add_argument("--no-columns", action="store_true",
                        help="Desactiva la detección y reordenación de maquetación multi-columna")
    p_conv.add_argument("--obsidian-callouts", action="store_true",
                        help="Renderiza notas y avisos como callouts de Obsidian (> [!NOTE])")
    p_conv.add_argument("--obsidian-wikilinks", action="store_true",
                        help="Convierte enlaces relativos e internos en wikilinks de Obsidian ([[doc]])")
    p_conv.add_argument("--min-chars-pagina", type=int, default=100,
                        help="Media de caracteres/página por debajo de la cual se marca capa_texto: ausente")
    p_conv.add_argument("--url-origen", default="", help="URL de procedencia del documento")
    p_conv.add_argument("--fecha-captura", default="", help="Fecha de descarga (AAAA-MM-DD)")

    # Subcomando: web
    p_web = subparsers.add_parser("web", help="Convierte una página web (URL, .html o carpeta) a Markdown")
    p_web.add_argument("origen", nargs="?", help="URL, ruta a un fichero .html o carpeta con ficheros .html")
    p_web.add_argument("-o", "--output", dest="output_md",
                       help="Fichero .md de salida, o carpeta destino con --lista o carpeta")
    p_web.add_argument("--lista", metavar="FICHERO",
                       help="Fichero de texto con una URL o ruta;url;id por línea para conversión por lotes")
    p_web.add_argument("--url", default="", dest="url_declarada",
                       help="Procedencia real de un .html local, para el frontmatter")
    p_web.add_argument("--export-images", action="store_true",
                       help="Descarga las imágenes a <nombre>_assets/")
    p_web.add_argument("--modo-limpio", "--clean", dest="clean_mode", action="store_true",
                       help="Genera Markdown visualmente limpio (frontmatter minimalista y sin avisos)")
    p_web.add_argument("--workers", type=int, default=1,
                       help="Número de hilos concurrentes para procesar el lote")
    p_web.add_argument("--obsidian-callouts", action="store_true",
                       help="Renderiza notas y avisos como callouts de Obsidian (> [!NOTE])")
    p_web.add_argument("--obsidian-wikilinks", action="store_true",
                       help="Convierte enlaces relativos e internos en wikilinks de Obsidian ([[doc]])")
    p_web.add_argument("--no-frontmatter", action="store_true")
    p_web.add_argument("--no-tables", action="store_true", help="No convierte <table> en tablas Markdown")
    p_web.add_argument("--no-links", action="store_true", help="Emite el texto de los enlaces sin su destino")
    p_web.add_argument("--no-code-blocks", action="store_true")
    p_web.add_argument("--no-alt", action="store_true",
                       help="No conserva el texto alternativo de las imágenes")
    p_web.add_argument("--no-slug", "--conservar-nombre", dest="no_slug", action="store_true",
                       help="Conserva el nombre original en vez de normalizarlo a ASCII")
    p_web.add_argument("--pagina-completa", action="store_true",
                       help="No poda menús ni pies: convierte la página entera")
    p_web.add_argument("--timeout", type=float, default=20.0, metavar="SEG")
    p_web.add_argument("--insecure", action="store_true",
                       help="No verificar el certificado TLS. Usar solo con motivo")
    p_web.add_argument("--pausa", type=float, default=1.0, metavar="SEG",
                       help="Espera entre peticiones en modo lote (por defecto 1 s)")
    p_web.add_argument("--refrescar", action="store_true",
                       help="En modo lote, recapturar también las URLs ya convertidas")
    p_web.add_argument("--sin-subcarpetas", action="store_true",
                       help="No clasifica los ficheros en subcarpetas por sección")
    p_web.add_argument("--no-index", action="store_true",
                       help="No genera el fichero INDEX.md al finalizar el lote")
    p_web.add_argument("--index-name", default="INDEX.md",
                       help="Nombre del archivo índice generado en modo lote (por defecto INDEX.md)")

    # Subcomando: collect
    p_col = subparsers.add_parser("collect", help="Recolecta y deduplica PDFs de un árbol de carpetas")
    p_col.add_argument("origen", help="Carpeta origen")
    p_col.add_argument("destino", help="Carpeta destino")
    p_col.add_argument("--simulacro", action="store_true", help="No copia nada: solo genera el informe previo")
    p_col.add_argument("--sin-dedup", action="store_true", help="Desactiva la deduplicación SHA-256")
    p_col.add_argument("--replicar", action="store_true", help="Replica el árbol de subcarpetas en destino")
    p_col.add_argument("--excluir", default="", help="Subcarpetas a omitir, separadas por comas")
    p_col.add_argument("--validar-cabecera", action="store_true",
                       help="Activo por defecto; se mantiene por compatibilidad")
    p_col.add_argument("--sin-validar-cabecera", action="store_true",
                       help="Acepta archivos .pdf aunque no tengan la firma %%PDF-")

    parser.add_argument("--gui", action="store_true", help="Forzar apertura de la interfaz gráfica")
    return parser


def _normalizar_argv(argv):
    """
    Compatibilidad histórica: `kiwi_app.py ORIGEN DESTINO [opciones]` equivale a
    `kiwi_app.py collect ORIGEN DESTINO [opciones]`.
    """
    if argv and argv[0] not in ("convert", "collect", "web") and not argv[0].startswith("-"):
        return ["collect"] + list(argv)
    return list(argv)


def _cli_check_ocr():
    """Imprime el diagnóstico del entorno OCR y devuelve 0 si está operativo."""
    info = diagnosticar_entorno_ocr()
    print("=== Diagnóstico de entorno OCR (KIWI) ===")
    print(f"Tesseract:    {info['tesseract_bin'] or 'NO ENCONTRADO'}")
    print(f"Ghostscript:  {info['ghostscript_bin'] or 'NO ENCONTRADO'}")
    print(f"ocrmypdf:     {info['ocrmypdf_bin'] or 'NO ENCONTRADO'}")
    print(f"Versión OCR:  {info['ocrmypdf_version'] or 'N/A'}")
    print(f"TESSDATA:     {info['tessdata_prefix'] or 'NO DEFINIDO'}")
    print(f"Idiomas:      {', '.join(info['tessdata_langs']) if info['tessdata_langs'] else 'NINGUNO'}")
    print(f"configs/hocr: {'OK' if info['has_hocr_config'] else 'FALTA'}")
    if info["avisos"]:
        print("\nAvisos:")
        for av in info["avisos"]:
            print(f"  [!] {av}")
        return 1
    print("\n[OK] Entorno de OCR listo y verificado.")
    return 0


def _cli_opciones(args):
    """Traduce los argumentos de la CLI a las opciones del conversor."""
    modo_ocr = args.ocr_mode
    if getattr(args, "ocr", False) and modo_ocr in ("off", "auto"):
        modo_ocr = "ocrmypdf" if is_ocrmypdf_available() else "tesseract"
    return PdfmdOptions(
        ocr_mode=modo_ocr,
        ocr_lang=args.ocr_lang,
        preview_only=args.preview_only,
        export_images=args.export_images,
        insert_page_breaks=args.insert_page_breaks,
        detect_tables=not args.no_tables,
        convert_equations=not args.no_equations,
        remove_headers_footers=not args.no_headers_footers,
        include_frontmatter=not args.no_frontmatter,
        detect_code_blocks=not args.no_code_blocks,
        detect_footnotes=not args.no_footnotes,
        page_markers=not args.no_page_markers,
        mark_cover_end=not args.no_cover_mark,
        compute_hash=not args.no_hash,
        url_origen=args.url_origen,
        fecha_captura=args.fecha_captura,
        extract_links=not args.no_links,
        join_hyphens=not args.no_join_hyphens,
        detect_language=not args.no_lang,
        min_chars_por_pagina=args.min_chars_pagina,
        clean_mode=getattr(args, "clean_mode", False),
        detect_columns=not getattr(args, "no_columns", False),
        obsidian_callouts=getattr(args, "obsidian_callouts", False),
        obsidian_wikilinks=getattr(args, "obsidian_wikilinks", False),
    )


def _cli_lote(args):
    """Convierte en lote una carpeta de PDFs, con reanudación y manifiesto."""
    origen = os.path.abspath(args.input_pdf)
    if not os.path.isdir(origen):
        print(f"Error: en modo lote {origen} debe ser una carpeta", file=sys.stderr)
        return 1

    pdfs = sorted(
        os.path.join(raiz, f)
        for raiz, _dirs, ficheros in os.walk(origen)
        for f in ficheros if f.lower().endswith(".pdf")
    )
    if not pdfs:
        print(f"No se encontró ningún PDF en {origen}", file=sys.stderr)
        return 1

    print(f"[KIWI] {len(pdfs)} PDF(s) encontrados en {origen}")
    convertir_lote(
        pdfs,
        output_dir=os.path.abspath(args.lote),
        options=_cli_opciones(args),
        reanudar=not args.no_reanudar,
        manifiesto=args.manifiesto,
        usar_slug=not args.no_slug,
        log_cb=lambda m: print(f"[KIWI] {m}"),
        max_workers=getattr(args, "workers", 1),
    )
    return 0


def _cli_convert(args):
    if getattr(args, "check_ocr", False):
        return _cli_check_ocr()
    if not getattr(args, "input_pdf", None):
        print("Error: se requiere especificar la ruta al archivo PDF o carpeta.", file=sys.stderr)
        return 2
    if getattr(args, "lote", None):
        return _cli_lote(args)

    in_pdf = os.path.abspath(args.input_pdf)
    if args.output_md:
        out_md = os.path.abspath(args.output_md)
    else:
        carpeta = os.path.dirname(in_pdf)
        base = os.path.splitext(os.path.basename(in_pdf))[0]
        if not args.no_slug:
            base = slugify(base)
        out_md = os.path.join(carpeta, base + ".md")
    opts = _cli_opciones(args)
    pwd = args.password
    try:
        pdf_to_markdown(in_pdf, out_md, options=opts, log_cb=print, pdf_password=pwd)
    except PermissionError:
        if pwd:
            raise
        print("El documento está protegido con contraseña.")
        pwd = getpass.getpass("Introduce la contraseña del PDF: ")
        pdf_to_markdown(in_pdf, out_md, options=opts, log_cb=print, pdf_password=pwd)
    print(f"[OK] Generado: {out_md}")
    return 0


def _cli_web(args):
    """Convierte una página web, una lista de URLs o una carpeta de .html desde la línea de comandos."""
    opts = WebOptions(
        incluir_frontmatter=not args.no_frontmatter,
        clean_mode=getattr(args, "clean_mode", False),
        obsidian_callouts=getattr(args, "obsidian_callouts", False),
        obsidian_wikilinks=getattr(args, "obsidian_wikilinks", False),
        detectar_tablas=not args.no_tables,
        conservar_enlaces=not args.no_links,
        detectar_codigo=not args.no_code_blocks,
        exportar_imagenes=args.export_images,
        conservar_alt=not args.no_alt,
        quitar_cromo=not args.pagina_completa,
        solo_cuerpo=not args.pagina_completa,
        timeout=args.timeout,
        verificar_tls=not args.insecure,
    )
    log = lambda m: print(m, file=sys.stderr)

    if args.lista:
        destino = args.output_md or "salida_md"
        origenes = leer_lista_estructurada(args.lista)
        if not origenes:
            print(f"La lista {args.lista} no contiene ninguna URL ni captura.", file=sys.stderr)
            return 1
        resumen = convertir_lote_web(origenes, destino, opts, log_cb=log,
                                     refrescar=args.refrescar, pausa=args.pausa,
                                     organizar_por_secciones=not args.sin_subcarpetas,
                                     generar_index=not args.no_index,
                                     max_workers=getattr(args, "workers", 1),
                                     conservar_nombre=args.no_slug,
                                     nombre_index=getattr(args, "index_name", "INDEX.md"))
        # Un lote con fallos no puede devolver 0: en un .bat nadie leeria el
        # resumen y las capturas perdidas pasarian inadvertidas.
        return 1 if resumen["fallidas"] else 0

    if args.origen and os.path.isdir(args.origen) and not es_url(args.origen):
        destino = args.output_md or os.path.join(args.origen, "salida_md")
        origenes = sorted(
            os.path.join(raiz, f)
            for raiz, _dirs, ficheros in os.walk(args.origen)
            for f in ficheros if f.lower().endswith((".html", ".htm"))
        )
        if not origenes:
            print(f"No se encontró ningún fichero .html en {args.origen}", file=sys.stderr)
            return 1
        resumen = convertir_lote_web(origenes, destino, opts, log_cb=log,
                                     refrescar=args.refrescar, pausa=args.pausa,
                                     organizar_por_secciones=not args.sin_subcarpetas,
                                     generar_index=not args.no_index,
                                     max_workers=getattr(args, "workers", 1),
                                     conservar_nombre=args.no_slug,
                                     nombre_index=getattr(args, "index_name", "INDEX.md"))
        return 1 if resumen["fallidas"] else 0

    if not args.origen:
        print("Hay que indicar una URL/fichero o bien --lista.", file=sys.stderr)
        return 2

    salida = args.output_md
    if not salida:
        salida = nombre_de_salida(args.origen, usar_slug=not args.no_slug)
    elif os.path.isdir(salida):
        salida = os.path.join(salida, nombre_de_salida(args.origen, usar_slug=not args.no_slug))

    try:
        _, meta = html_to_markdown(args.origen, salida, opts, log_cb=log,
                                   url_origen=args.url_declarada)
    except ErrorDeDescarga as ex:
        print(str(ex), file=sys.stderr)
        return 1

    print(f"{os.path.abspath(salida)}  [{meta.chars_extraidos} chars, "
          f"{meta.enlaces} enlaces, contenido: {meta.contenido}]")
    return 1 if meta.contenido == "ausente" else 0


def _cli_collect(args):
    resumen = {}

    def cb(evento, datos):
        if evento == "log":
            print(f"[KIWI] {datos.get('text', '')}")
        elif evento in ("finished", "error"):
            resumen.update(datos)
            resumen["_evento"] = evento

    runner = KiwiRunner(
        origen=args.origen,
        destino=args.destino,
        simulacro=args.simulacro,
        dedup=not args.sin_dedup,
        validar_cabecera=not args.sin_validar_cabecera,
        replicar=args.replicar,
        exclusiones=[e for e in args.excluir.split(",") if e.strip()],
        progress_cb=cb
    )
    runner.run()

    if resumen.get("_evento") == "error":
        return 1
    if resumen:
        print(f"[OK] Localizados: {resumen.get('encontrados', 0)} | "
              f"Procesados: {resumen.get('copiados', 0)} | "
              f"Duplicados: {resumen.get('duplicados', 0)} | "
              f"Ya existentes: {resumen.get('ya_estaban', 0)}")
        if resumen.get("informe"):
            print(f"[OK] Informe: {resumen['informe']}")
    return 0


def main(argv=None):
    parser = build_parser()
    argv = _normalizar_argv(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)

    if getattr(args, "check_ocr", False):
        return _cli_check_ocr()

    if args.command and not getattr(args, "gui", False):
        despacho = {"convert": _cli_convert, "collect": _cli_collect, "web": _cli_web}
        try:
            return despacho[args.command](args)
        except (OSError, ValueError, PermissionError, InterruptedError) as ex:
            print(f"Error: {ex}", file=sys.stderr)
            return 1

    launch_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())

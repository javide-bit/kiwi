# -*- coding: utf-8 -*-
"""
KIWI — Suite Inteligente de Recolección, Deduplicación y Conversión de PDFs a Markdown.
Integra motor de recolección segura y motor pdfmd para estructuración en Markdown con visor integrado.
"""

import argparse
import csv
import ctypes
import getpass
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# GUI imports
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

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
    is_ocrmypdf_available,
    is_tesseract_available,
    sha256_fichero,
    slugify,
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
                 manifiesto="manifiesto_conversion.tsv"):
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
            last_md_file = None
            usados = set()
            filas_manifiesto = []

            for idx, item in enumerate(self.items, 1):
                if self.cancel_event.is_set():
                    self.log("Conversión cancelada por el usuario.", "warning")
                    self.progress_cb("cancelled", {})
                    return

                if isinstance(item, (tuple, list)):
                    pdf_path, out_md = item[0], item[1]
                else:
                    pdf_path = item
                    out_md = self._ruta_salida_unica(pdf_path, usados)

                self.progress_cb("phase", {
                    "phase": idx,
                    "title": f"Convirtiendo ({idx}/{total}): {os.path.basename(pdf_path)}"
                })

                # Reanudacion: si ya existe el .md generado desde ESTE mismo PDF
                # (mismo SHA-256), no hay nada que rehacer.
                if self.reanudar and self.options.compute_hash and ya_convertido(
                        out_md, sha256_fichero(pdf_path)):
                    saltados += 1
                    last_md_file = out_md
                    try:
                        with open(out_md, "r", encoding="utf-8") as f_md:
                            filas_manifiesto.append(fila_de_manifiesto(
                                os.path.basename(out_md), f_md.read(8192)))
                    except OSError:
                        pass
                    self.log(f"Ya convertido, se omite: {os.path.basename(out_md)}")
                    continue

                def doc_log(msg):
                    self.log(f"  [{os.path.basename(pdf_path)}] {msg}")

                def doc_prog(done, p_tot):
                    self.progress_cb("progress_doc", {
                        "current_file": idx,
                        "total_files": total,
                        "page_done": done,
                        "page_total": p_tot
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
                    filas_manifiesto.append(fila_de_manifiesto(
                        os.path.basename(out_md), md_generado))
                    converted += 1
                    last_md_file = out_md
                    self.log(f"Generado: {os.path.basename(out_md)}", "success")
                except InterruptedError:
                    self.log("Conversión cancelada por el usuario.", "warning")
                    self.progress_cb("cancelled", {})
                    return
                except PermissionError:
                    errors += 1
                    self.log(f"{os.path.basename(pdf_path)} está protegido con contraseña: omitido.", "warning")
                except Exception as ex:
                    errors += 1
                    self.log(f"Error convirtiendo {os.path.basename(pdf_path)}: {ex}", "error")

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

            self.progress_cb("conv_finished", {
                "total": total,
                "skipped": saltados,
                "converted": converted,
                "errors": errors,
                "output_dir": self.output_dir,
                "last_md": last_md_file
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
                 url_origen="", organizar_por_secciones=True, generar_index=True):
        self.items = list(items)
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

    def log(self, text, tag="info"):
        self.progress_cb("log", {"text": text, "tag": tag})

    def _una_sola(self):
        origen = self.items[0]
        destino = self.salida_fija
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

        resumen = convertir_lote_web(
            self.items, self.output_dir, self.options,
            log_cb=self.log, progress_cb=prog,
            refrescar=self.refrescar, pausa=self.pausa,
            organizar_por_secciones=self.organizar_por_secciones,
            generar_index=self.generar_index,
            cancel_event=self.cancel_event,
        )

        filas = resumen.get("filas", [])
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
class MarkdownViewerWindow(tk.Toplevel):
    def __init__(self, parent, file_path):
        super().__init__(parent)
        self.file_path = file_path
        self.title(f"KIWI — Visor de Markdown [{os.path.basename(file_path)}]")
        self.geometry("780x600")
        self.minsize(600, 400)
        self.configure(bg="#f8faf8")

        self._build_ui()
        self._load_file()

    def _build_ui(self):
        top_bar = tk.Frame(self, bg="#ffffff", bd=1, relief="solid", padx=12, pady=6)
        top_bar.pack(fill="x")

        lbl_path = tk.Label(top_bar, text=self.file_path, font=("Segoe UI", 9, "bold"), fg="#2b3a1a", bg="#ffffff")
        lbl_path.pack(side="left")

        btn_copy = tk.Button(top_bar, text="📋 Copiar Todo", font=("Segoe UI", 8), bg="#e8f0e8", fg="#2b3a1a",
                             bd=1, relief="solid", cursor="hand2", padx=8, command=self._copy_all)
        btn_copy.pack(side="right", padx=(6, 0))

        btn_open_ext = tk.Button(top_bar, text="🚀 Abrir en Editor", font=("Segoe UI", 8), bg="#e8f0e8", fg="#2b3a1a",
                                 bd=1, relief="solid", cursor="hand2", padx=8, command=self._open_external)
        btn_open_ext.pack(side="right")

        # Área de texto
        f_text = tk.Frame(self, bg="#ffffff")
        f_text.pack(fill="both", expand=True, padx=10, pady=10)

        self.txt_content = tk.Text(f_text, font=("Consolas", 10), bg="#ffffff", fg="#1e1e1e",
                                   wrap="word", bd=1, relief="solid", padx=10, pady=10)
        scroll_y = tk.Scrollbar(f_text, orient="vertical", command=self.txt_content.yview)
        self.txt_content.configure(yscrollcommand=scroll_y.set)

        self.txt_content.pack(side="left", fill="both", expand=True)
        scroll_y.pack(side="right", fill="y")

    def _load_file(self):
        if os.path.exists(self.file_path):
            with open(self.file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self.txt_content.insert("1.0", content)
            self.txt_content.config(state="disabled")

    def _copy_all(self):
        self.txt_content.config(state="normal")
        text = self.txt_content.get("1.0", tk.END)
        self.txt_content.config(state="disabled")
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("Copiado", "Contenido Markdown copiado al portapapeles.")

    def _open_external(self):
        abrir_en_sistema(self.file_path)


# --------------------------------------------------------------------------
# Interfaz Gráfica de Usuario (GUI Unificada KIWI)
# --------------------------------------------------------------------------
class KiwiAppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("KIWI — Suite de Gestión y Conversión de PDFs a Markdown")
        self.root.geometry("880x820")
        self.root.minsize(820, 740)

        # Paleta temática KIWI
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

        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread = None
        self.pipeline_active = False  # True mientras corre el modo Recolectar + Convertir
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

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("TNotebook", background=self.c_bg, borderwidth=0)
        style.configure("TNotebook.Tab", background="#e2ede2", foreground=self.c_dark,
                        padding=[14, 6], font=("Segoe UI", 9, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", self.c_primary)],
                  foreground=[("selected", "#ffffff")])

        style.configure("TProgressbar", thickness=16, troughcolor="#e6efe6", background=self.c_primary)

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
                pil_img = pil_img.resize((76, 76), Image.Resampling.LANCZOS)
                self.logo_img = ImageTk.PhotoImage(pil_img)
            except Exception:
                self.logo_img = None

    def _build_ui(self):
        main_frame = tk.Frame(self.root, bg=self.c_bg, padx=16, pady=12)
        main_frame.pack(fill="both", expand=True)

        # 1. Cabecera KIWI
        header_card = tk.Frame(main_frame, bg=self.c_card, bd=1, relief="solid", highlightthickness=0)
        header_card.configure(highlightbackground=self.c_border)
        header_card.pack(fill="x", pady=(0, 10))

        header_inner = tk.Frame(header_card, bg=self.c_card, padx=14, pady=8)
        header_inner.pack(fill="x")

        if self.logo_img:
            logo_label = tk.Label(header_inner, image=self.logo_img, bg=self.c_card)
            logo_label.pack(side="left", padx=(0, 12))

        title_box = tk.Frame(header_inner, bg=self.c_card)
        title_box.pack(side="left", fill="both", expand=True)

        lbl_app_name = tk.Label(title_box, text="KIWI", font=("Segoe UI", 18, "bold"),
                                fg=self.c_primary, bg=self.c_card)
        lbl_app_name.pack(anchor="w")

        lbl_app_desc = tk.Label(title_box,
                                text="Suite Inteligente de documentos: recolección masiva, deduplicación SHA-256 y conversión estructurada a Markdown desde PDF (pdfmd) y desde la web (htmlmd).",
                                font=("Segoe UI", 9), fg=self.c_text_muted, bg=self.c_card, wraplength=600, justify="left")
        lbl_app_desc.pack(anchor="w", pady=(1, 0))

        # 2. Pestañas (Notebook)
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill="x", pady=(0, 8))

        self.tab_collector = tk.Frame(self.notebook, bg=self.c_bg, padx=10, pady=10)
        self.tab_converter = tk.Frame(self.notebook, bg=self.c_bg, padx=10, pady=10)
        self.tab_pipeline = tk.Frame(self.notebook, bg=self.c_bg, padx=10, pady=10)
        self.tab_web = tk.Frame(self.notebook, bg=self.c_bg, padx=10, pady=10)

        self.notebook.add(self.tab_collector, text=" 📁 Recolector y Deduplicador ")
        self.notebook.add(self.tab_converter, text=" 📝 Conversor a Markdown (pdfmd) ")
        self.notebook.add(self.tab_pipeline, text=" ⚡ Recolectar + Convertir a MD ")
        self.notebook.add(self.tab_web, text=" 🌐 Web a Markdown (htmlmd) ")

        self._build_tab_collector()
        self._build_tab_converter()
        self._build_tab_pipeline()
        self._build_tab_web()

        # 3. Controles Comunes de Progreso y Log
        prog_card = tk.LabelFrame(main_frame, text=" 📊 Progreso y Actividad en Vivo ", font=("Segoe UI", 9, "bold"),
                                  fg=self.c_dark, bg=self.c_card, bd=1, relief="solid", padx=12, pady=8)
        prog_card.pack(fill="both", expand=True, pady=(0, 8))

        self.lbl_fase = tk.Label(prog_card, text="Listo para comenzar. Presiona Iniciar o pulsa Ctrl+Enter.",
                                 font=("Segoe UI", 9, "italic"), fg=self.c_text_muted, bg=self.c_card)
        self.lbl_fase.pack(anchor="w", pady=(0, 3))

        self.prog_bar = ttk.Progressbar(prog_card, style="TProgressbar", mode="determinate")
        self.prog_bar.pack(fill="x", pady=(0, 6))

        # Text Log
        log_inner = tk.Frame(prog_card, bg=self.c_card)
        log_inner.pack(fill="both", expand=True)

        self.txt_log = tk.Text(log_inner, font=("Consolas", 8), bg="#fafcfa", fg="#222222",
                               relief="solid", bd=1, wrap="none", height=5)
        log_scroll_y = tk.Scrollbar(log_inner, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=log_scroll_y.set)

        self.txt_log.pack(side="left", fill="both", expand=True)
        log_scroll_y.pack(side="right", fill="y")

        self.txt_log.tag_config("info", foreground="#333333")
        self.txt_log.tag_config("success", foreground="#2e7d32", font=("Consolas", 8, "bold"))
        self.txt_log.tag_config("warning", foreground="#d87a00")
        self.txt_log.tag_config("error", foreground="#c62828", font=("Consolas", 8, "bold"))

        # 4. Botones Rápidos de Apertura y Visor
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

    # ----------------------------------------------------
    # Construcción Pestaña 1: Recolector
    # ----------------------------------------------------
    def _build_tab_collector(self):
        f_paths = tk.Frame(self.tab_collector, bg=self.c_bg)
        f_paths.pack(fill="x", pady=(0, 6))

        tk.Label(f_paths, text="Carpeta Origen:", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=0, column=0, sticky="w")
        self.col_txt_origen = tk.Entry(f_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.col_txt_origen.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.col_txt_origen)).grid(row=1, column=1, pady=(2, 6))

        tk.Label(f_paths, text="Carpeta Destino (Salida):", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=2, column=0, sticky="w")
        self.col_txt_destino = tk.Entry(f_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.col_txt_destino.grid(row=3, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.col_txt_destino)).grid(row=3, column=1, pady=(2, 6))

        tk.Label(f_paths, text="Subcarpetas a excluir (separadas por comas):",
                 font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=4, column=0, sticky="w")
        self.col_txt_exclusiones = tk.Entry(f_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.col_txt_exclusiones.grid(row=5, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Label(f_paths, text="ej: backups, temp", font=("Segoe UI", 8, "italic"),
                 fg=self.c_text_muted, bg=self.c_bg).grid(row=5, column=1, sticky="w", padx=(4, 0))

        f_paths.grid_columnconfigure(0, weight=1)

        # Opciones
        f_opts = tk.Frame(self.tab_collector, bg=self.c_bg)
        f_opts.pack(fill="x", pady=(0, 6))

        self.col_var_simulacro = tk.BooleanVar(value=True)
        self.col_var_dedup = tk.BooleanVar(value=True)
        self.col_var_cabecera = tk.BooleanVar(value=True)
        self.col_var_replicar = tk.BooleanVar(value=False)

        tk.Checkbutton(f_opts, text="🛡️ Modo Simulacro (No copia, genera informe previo)", variable=self.col_var_simulacro,
                       font=("Segoe UI", 9, "bold"), fg="#1d5e21", bg=self.c_bg).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(f_opts, text="🔍 Deduplicar por SHA-256", variable=self.col_var_dedup,
                       font=("Segoe UI", 9), fg=self.c_text, bg=self.c_bg).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_opts, text="📑 Validar cabecera real %PDF-", variable=self.col_var_cabecera,
                       font=("Segoe UI", 9), fg=self.c_text, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        tk.Checkbutton(f_opts, text="🌳 Replicar subcarpetas", variable=self.col_var_replicar,
                       font=("Segoe UI", 9), fg=self.c_text, bg=self.c_bg).grid(row=1, column=1, sticky="w", padx=(10, 0))

        # Botón Iniciar
        f_act = tk.Frame(self.tab_collector, bg=self.c_bg)
        f_act.pack(fill="x", pady=(4, 0))

        self.btn_col_iniciar = tk.Button(f_act, text="🥝 Iniciar Recolección", font=("Segoe UI", 10, "bold"),
                                         bg=self.c_primary, fg="white", bd=0, cursor="hand2", padx=16, pady=6,
                                         command=self._start_collector)
        self.btn_col_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_col_detener = tk.Button(f_act, text="⏹ Detener", font=("Segoe UI", 9),
                                         bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=6,
                                         state="disabled", command=self._cancel_process)
        self.btn_col_detener.pack(side="right")

    # ----------------------------------------------------
    # Construcción Pestaña 2: Conversor a Markdown (pdfmd)
    # ----------------------------------------------------
    def _build_tab_converter(self):
        f_conv_paths = tk.Frame(self.tab_converter, bg=self.c_bg)
        f_conv_paths.pack(fill="x", pady=(0, 6))

        # Modo (Archivo o Carpeta)
        self.conv_mode_var = tk.StringVar(value="file")
        f_mode = tk.Frame(f_conv_paths, bg=self.c_bg)
        f_mode.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Radiobutton(f_mode, text="📄 Archivo PDF individual", variable=self.conv_mode_var, value="file",
                       bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))
        tk.Radiobutton(f_mode, text="📚 Carpeta completa (Lote)", variable=self.conv_mode_var, value="folder",
                       bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side="left")

        # Entrada
        tk.Label(f_conv_paths, text="PDF o Carpeta de Entrada:", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        self.conv_txt_in = tk.Entry(f_conv_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.conv_txt_in.grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_conv_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=self._browse_converter_input).grid(row=2, column=1, pady=(2, 6))

        # Salida
        tk.Label(f_conv_paths, text="Archivo .md o Carpeta de Salida:", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        self.conv_txt_out = tk.Entry(f_conv_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.conv_txt_out.grid(row=4, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_conv_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=self._browse_converter_output).grid(row=4, column=1, pady=(2, 6))

        f_conv_paths.grid_columnconfigure(0, weight=1)

        # Opciones pdfmd enriquecidas
        f_conv_opts = tk.LabelFrame(self.tab_converter, text=" Opciones de Conversión pdfmd ", font=("Segoe UI", 8, "bold"),
                                    fg=self.c_dark, bg=self.c_bg, bd=1, relief="solid", padx=8, pady=4)
        f_conv_opts.pack(fill="x", pady=(0, 6))

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
        self.conv_ocr_mode = tk.StringVar(value="off")

        tk.Checkbutton(f_conv_opts, text="🖼️ Extraer imágenes a _assets", variable=self.conv_var_images, bg=self.c_bg).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="📊 Detección de tablas (pipe-tables)", variable=self.conv_var_tables, bg=self.c_bg).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_conv_opts, text="🧮 Fórmulas a LaTeX ($...$)", variable=self.conv_var_eqs, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="📑 Saltos de página (---)", variable=self.conv_var_breaks, bg=self.c_bg).grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_conv_opts, text="📝 Metadatos YAML Frontmatter", variable=self.conv_var_frontmatter, bg=self.c_bg).grid(row=2, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="💻 Bloques de código (monospace)", variable=self.conv_var_code, bg=self.c_bg).grid(row=2, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_conv_opts, text="🧹 Limpiar cabeceras/pies", variable=self.conv_var_clean, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="🔖 Notas al pie ([^1] al final)", variable=self.conv_var_footnotes, bg=self.c_bg).grid(row=4, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="📍 Marcador de página (<!-- p.7 -->)", variable=self.conv_var_markers, bg=self.c_bg).grid(row=4, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_conv_opts, text="📖 Marcar fin de portada", variable=self.conv_var_cover, bg=self.c_bg).grid(row=5, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="🔗 Conservar hipervínculos", variable=self.conv_var_links, bg=self.c_bg).grid(row=5, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_conv_opts, text="✂️ Unir palabras partidas", variable=self.conv_var_hyphen, bg=self.c_bg).grid(row=6, column=0, sticky="w")
        tk.Checkbutton(f_conv_opts, text="⏭️ Omitir los ya convertidos", variable=self.conv_var_reanudar, bg=self.c_bg).grid(row=6, column=1, sticky="w", padx=(10, 0))

        f_ocr = tk.Frame(f_conv_opts, bg=self.c_bg)
        f_ocr.grid(row=3, column=1, sticky="w", padx=(10, 0))
        tk.Label(f_ocr, text="Modo OCR:", bg=self.c_bg, font=("Segoe UI", 8)).pack(side="left")
        cb_ocr = ttk.Combobox(f_ocr, textvariable=self.conv_ocr_mode, values=["off", "auto", "tesseract", "ocrmypdf"], state="readonly", width=9)
        cb_ocr.pack(side="left", padx=(4, 0))

        # Botón Iniciar Conversión
        f_act_conv = tk.Frame(self.tab_converter, bg=self.c_bg)
        f_act_conv.pack(fill="x", pady=(4, 0))

        self.btn_conv_iniciar = tk.Button(f_act_conv, text="📝 Convertir a Markdown", font=("Segoe UI", 10, "bold"),
                                          bg=self.c_primary, fg="white", bd=0, cursor="hand2", padx=16, pady=6,
                                          command=self._start_converter)
        self.btn_conv_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_conv_detener = tk.Button(f_act_conv, text="⏹ Detener", font=("Segoe UI", 9),
                                          bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=6,
                                          state="disabled", command=self._cancel_process)
        self.btn_conv_detener.pack(side="right")

    # ----------------------------------------------------
    # Construcción Pestaña 3: Pipeline Combinado
    # ----------------------------------------------------
    def _build_tab_pipeline(self):
        f_pipe_paths = tk.Frame(self.tab_pipeline, bg=self.c_bg)
        f_pipe_paths.pack(fill="x", pady=(0, 6))

        tk.Label(f_pipe_paths, text="Árbol Origen con PDFs:", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=0, column=0, sticky="w")
        self.pipe_txt_origen = tk.Entry(f_pipe_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.pipe_txt_origen.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_pipe_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.pipe_txt_origen)).grid(row=1, column=1, pady=(2, 6))

        tk.Label(f_pipe_paths, text="Carpeta Destino (PDFs + Markdown):", font=("Segoe UI", 9, "bold"), fg=self.c_text, bg=self.c_bg).grid(row=2, column=0, sticky="w")
        self.pipe_txt_destino = tk.Entry(f_pipe_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.pipe_txt_destino.grid(row=3, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_pipe_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark, bd=1, relief="solid",
                  cursor="hand2", command=lambda: self._browse_dir(self.pipe_txt_destino)).grid(row=3, column=1, pady=(2, 6))

        f_pipe_paths.grid_columnconfigure(0, weight=1)

        tk.Label(self.tab_pipeline,
                 text="⚡ Este modo recorre todo el árbol de carpetas, deduplica los PDFs por contenido SHA-256 y genera automáticamente su archivo Markdown estructurado (.md) en destino.",
                 font=("Segoe UI", 8, "italic"), fg=self.c_text_muted, bg=self.c_bg, wraplength=750, justify="left").pack(anchor="w", pady=(0, 6))

        # Botón Iniciar Pipeline
        f_act_pipe = tk.Frame(self.tab_pipeline, bg=self.c_bg)
        f_act_pipe.pack(fill="x", pady=(4, 0))

        self.btn_pipe_iniciar = tk.Button(f_act_pipe, text="⚡ Ejecutar Recolección + Conversión a Markdown",
                                          font=("Segoe UI", 10, "bold"), bg="#1d5e21", fg="white", bd=0,
                                          cursor="hand2", padx=16, pady=6, command=self._start_pipeline_combo)
        self.btn_pipe_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_pipe_detener = tk.Button(f_act_pipe, text="⏹ Detener", font=("Segoe UI", 9),
                                          bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=6,
                                          state="disabled", command=self._cancel_process)
        self.btn_pipe_detener.pack(side="right")

    # ----------------------------------------------------
    # Construcción Pestaña 4: Web a Markdown (htmlmd)
    # ----------------------------------------------------
    def _build_tab_web(self):
        f_web_paths = tk.Frame(self.tab_web, bg=self.c_bg)
        f_web_paths.pack(fill="x", pady=(0, 6))

        # Modo (una pagina o lista de URLs)
        self.web_mode_var = tk.StringVar(value="url")
        f_wmode = tk.Frame(f_web_paths, bg=self.c_bg)
        f_wmode.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Radiobutton(f_wmode, text="🌐 Una página (URL o .html guardado)", variable=self.web_mode_var,
                       value="url", bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))
        tk.Radiobutton(f_wmode, text="📋 Lista de URLs (.txt, una por línea)", variable=self.web_mode_var,
                       value="lista", bg=self.c_bg, font=("Segoe UI", 9, "bold")).pack(side="left")

        tk.Label(f_web_paths, text="URL, fichero .html o lista .txt:", font=("Segoe UI", 9, "bold"),
                 fg=self.c_text, bg=self.c_bg).grid(row=1, column=0, sticky="w")
        self.web_txt_in = tk.Entry(f_web_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.web_txt_in.grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_web_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                  bd=1, relief="solid", cursor="hand2",
                  command=self._browse_web_input).grid(row=2, column=1, pady=(2, 6))

        tk.Label(f_web_paths, text="Archivo .md o Carpeta de Salida:", font=("Segoe UI", 9, "bold"),
                 fg=self.c_text, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        self.web_txt_out = tk.Entry(f_web_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.web_txt_out.grid(row=4, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))
        tk.Button(f_web_paths, text="Examinar...", font=("Segoe UI", 8), bg="#e8f0e8", fg=self.c_dark,
                  bd=1, relief="solid", cursor="hand2",
                  command=self._browse_web_output).grid(row=4, column=1, pady=(2, 6))

        # Procedencia declarada de un .html guardado en disco, que no la trae
        tk.Label(f_web_paths, text="URL de procedencia (solo para .html locales):",
                 font=("Segoe UI", 8), fg=self.c_text_muted, bg=self.c_bg).grid(row=5, column=0, sticky="w")
        self.web_txt_url = tk.Entry(f_web_paths, font=("Segoe UI", 9), bd=1, relief="solid")
        self.web_txt_url.grid(row=6, column=0, sticky="ew", padx=(0, 6), pady=(2, 6))

        f_web_paths.grid_columnconfigure(0, weight=1)

        f_web_opts = tk.LabelFrame(self.tab_web, text=" Opciones de Conversión htmlmd ",
                                   font=("Segoe UI", 8, "bold"), fg=self.c_dark, bg=self.c_bg,
                                   bd=1, relief="solid", padx=8, pady=4)
        f_web_opts.pack(fill="x", pady=(0, 6))

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

        tk.Checkbutton(f_web_opts, text="📝 Metadatos YAML Frontmatter", variable=self.web_var_frontmatter,
                       bg=self.c_bg).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="📊 Detección de tablas (pipe-tables)", variable=self.web_var_tables,
                       bg=self.c_bg).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_web_opts, text="🔗 Conservar hipervínculos", variable=self.web_var_links,
                       bg=self.c_bg).grid(row=1, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="💻 Bloques de código (<pre>)", variable=self.web_var_code,
                       bg=self.c_bg).grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_web_opts, text="🖼️ Descargar imágenes a _assets", variable=self.web_var_images,
                       bg=self.c_bg).grid(row=2, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="🏷️ Conservar texto alternativo (alt)", variable=self.web_var_alt,
                       bg=self.c_bg).grid(row=2, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_web_opts, text="🧹 Página completa (no podar menús ni pies)",
                       variable=self.web_var_completa, bg=self.c_bg).grid(row=3, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="🔁 Recapturar las URLs ya convertidas",
                       variable=self.web_var_refrescar, bg=self.c_bg).grid(row=3, column=1, sticky="w", padx=(10, 0))
        tk.Checkbutton(f_web_opts, text="📁 Organizar por subcarpetas (por sección)",
                       variable=self.web_var_subcarpetas, bg=self.c_bg).grid(row=4, column=0, sticky="w")
        tk.Checkbutton(f_web_opts, text="📑 Generar índice (INDEX.md)",
                       variable=self.web_var_index, bg=self.c_bg).grid(row=4, column=1, sticky="w", padx=(10, 0))

        # Espera entre peticiones: descargar 200 paginas de un sitio a toda
        # velocidad es indistinguible de un ataque y acaba en un bloqueo por IP.
        f_pausa = tk.Frame(f_web_opts, bg=self.c_bg)
        f_pausa.grid(row=5, column=0, sticky="w", pady=(4, 0))
        tk.Label(f_pausa, text="Espera entre peticiones (s):", bg=self.c_bg,
                 font=("Segoe UI", 8)).pack(side="left")
        self.web_txt_pausa = tk.Entry(f_pausa, font=("Segoe UI", 9), bd=1, relief="solid", width=6)
        self.web_txt_pausa.insert(0, "1.0")
        self.web_txt_pausa.pack(side="left", padx=(4, 0))

        tk.Label(self.tab_web,
                 text="🌐 Convierte páginas web al mismo Markdown que el conversor de PDF: mismo frontmatter de "
                      "procedencia (URL canónica, fecha de captura y SHA-256 del HTML recibido), mismas tablas y "
                      "los enlaces conservados. Las páginas que solo se pintan con JavaScript se "
                      "marcan como contenido escaso, con aviso visible en el propio .md.",
                 font=("Segoe UI", 8, "italic"), fg=self.c_text_muted, bg=self.c_bg,
                 wraplength=750, justify="left").pack(anchor="w", pady=(0, 6))

        f_act_web = tk.Frame(self.tab_web, bg=self.c_bg)
        f_act_web.pack(fill="x", pady=(4, 0))

        self.btn_web_iniciar = tk.Button(f_act_web, text="🌐 Convertir Web a Markdown",
                                         font=("Segoe UI", 10, "bold"), bg="#1d5e21", fg="white", bd=0,
                                         cursor="hand2", padx=16, pady=6, command=self._start_web)
        self.btn_web_iniciar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_web_detener = tk.Button(f_act_web, text="⏹ Detener", font=("Segoe UI", 9),
                                         bg="#d9534f", fg="white", bd=0, cursor="hand2", padx=14, pady=6,
                                         state="disabled", command=self._cancel_process)
        self.btn_web_detener.pack(side="right")

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
        if cfg.get("conv_mode") in ("file", "folder"):
            self.conv_mode_var.set(cfg["conv_mode"])
        if cfg.get("web_mode") in ("url", "lista"):
            self.web_mode_var.set(cfg["web_mode"])

    def _save_config(self):
        try:
            cfg = {key: widget.get().strip() for key, widget in self._campos_config().items()}
            cfg.update({key: bool(var.get()) for key, var in self._opciones_config().items()})
            cfg["ocr_mode"] = self.conv_ocr_mode.get()
            cfg["conv_mode"] = self.conv_mode_var.get()
            cfg["web_mode"] = self.web_mode_var.get()
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
        if self.web_mode_var.get() == "lista":
            sel = filedialog.askopenfilename(title="Seleccionar lista de URLs",
                                             filetypes=[("Texto", "*.txt"), ("Todos", "*.*")])
        else:
            sel = filedialog.askopenfilename(title="Seleccionar página HTML guardada",
                                             filetypes=[("HTML", "*.html;*.htm"), ("Todos", "*.*")])
        if not sel:
            return
        self.web_txt_in.delete(0, tk.END)
        self.web_txt_in.insert(0, os.path.normpath(sel))

        # Sugerencia de salida coherente con el modo elegido
        self.web_txt_out.delete(0, tk.END)
        if self.web_mode_var.get() == "lista":
            self.web_txt_out.insert(0, os.path.join(os.path.dirname(sel), "markdown_web"))
        else:
            self.web_txt_out.insert(0, os.path.splitext(sel)[0] + ".md")

    def _browse_web_output(self):
        if self.web_mode_var.get() == "lista":
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
            # En modo archivo se respeta el nombre exacto elegido por el usuario
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

        opts = PdfmdOptions(
            ocr_mode=self.conv_ocr_mode.get(),
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
            join_hyphens=self.conv_var_hyphen.get()
        )

        self.cancel_event.clear()
        self.txt_log.delete("1.0", tk.END)
        self._set_running_state(True)

        runner = KiwiConverterRunner(
            items=items,
            output_dir=out_dir,
            options=opts,
            cancel_event=self.cancel_event,
            progress_cb=self._enqueue_event,
            reanudar=self.conv_var_reanudar.get()
        )
        self.worker_thread = threading.Thread(target=runner.run, daemon=True)
        self.worker_thread.start()

    def _opciones_web(self):
        """Traduce los controles de la pestaña a las opciones de htmlmd."""
        completa = self.web_var_completa.get()
        return WebOptions(
            incluir_frontmatter=self.web_var_frontmatter.get(),
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

        self.cancel_event.clear()
        self.txt_log.delete("1.0", tk.END)
        self._set_running_state(True)

        url_declarada = self.web_txt_url.get().strip()
        opts = self._opciones_web()

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
            # Solo tiene sentido para un .html de disco: sobre una URL real
            # seria sustituir la procedencia verdadera por otra declarada.
            url_origen=(url_declarada if not es_url(entrada) else ""),
        )

        self.worker_thread = threading.Thread(target=runner.run, daemon=True)
        self.worker_thread.start()

    def _start_pipeline_combo(self):
        orig = self.pipe_txt_origen.get().strip()
        dest = self.pipe_txt_destino.get().strip()
        if not orig or not dest:
            messagebox.showwarning("Atención", "Especifica la carpeta Origen y Destino.")
            return

        self.cancel_event.clear()
        self.txt_log.delete("1.0", tk.END)
        self._set_running_state(True)

        self.pipeline_active = True

        def combo_log(texto, tag="info"):
            # Tkinter no es seguro fuera del hilo principal: todo va por la cola
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
                join_hyphens=self.conv_var_hyphen.get()
            )
            conv_runner = KiwiConverterRunner(
                items=pdf_files,
                output_dir=dest,
                options=opts,
                cancel_event=self.cancel_event,
                progress_cb=self._enqueue_event,
                reanudar=self.conv_var_reanudar.get()
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
        self.last_dest = stats.get("destino")
        self.last_report = stats.get("informe")

        if self.last_dest and os.path.exists(self.last_dest):
            self.btn_open_dest.config(state="normal")
        if self.last_report and os.path.exists(self.last_report):
            self.btn_open_csv.config(state="normal")

        modo_txt = "Simulacro finalizado" if stats.get("simulacro") else "Recolección completada"
        self._log_msg(f"✅ {modo_txt}: {stats.get('copiados', 0)} archivos procesados.", "success")

        # Dentro del pipeline combinado esto es solo el paso 1: ni se libera la
        # interfaz ni se interrumpe al usuario con un diálogo intermedio.
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
        messagebox.showinfo("KIWI — pdfmd",
                            f"Conversión completada.\n\n"
                            f"• Archivos convertidos: {stats.get('converted', 0)}\n"
                            f"• Errores: {stats.get('errors', 0)}\n"
                            f"• Carpeta de salida: {stats.get('output_dir', '')}")

    def _on_web_finished(self, stats):
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
            # Esto no puede quedarse solo en el log: una captura vacia que pasa
            # por buena es peor que una que falla, porque nadie la revisa.
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
            MarkdownViewerWindow(self.root, self.last_md)


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

    root = tk.Tk()
    app = KiwiAppGUI(root)
    root.mainloop()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="kiwi_app.py",
        description="KIWI — Suite de Gestión y Conversión de PDFs a Markdown",
        epilog="Sin argumentos abre la interfaz gráfica."
    )
    subparsers = parser.add_subparsers(dest="command", help="Comando a ejecutar")

    # Subcomando: convert
    p_conv = subparsers.add_parser("convert", help="Convierte un archivo PDF a Markdown estructurado")
    p_conv.add_argument("input_pdf", help="Ruta al archivo PDF")
    p_conv.add_argument("-o", "--output", dest="output_md", help="Ruta del archivo Markdown de salida")
    p_conv.add_argument("--ocr-mode", choices=["off", "auto", "tesseract", "ocrmypdf"], default="off")
    p_conv.add_argument("--ocr-lang", default="eng", help="Idioma para el OCR (ej: spa, eng+spa)")
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
    p_conv.add_argument("--no-slug", action="store_true",
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
    p_conv.add_argument("--min-chars-pagina", type=int, default=800,
                        help="Umbral por debajo del cual se marca capa_texto: ausente")
    p_conv.add_argument("--url-origen", default="", help="URL de procedencia del documento")
    p_conv.add_argument("--fecha-captura", default="", help="Fecha de descarga (AAAA-MM-DD)")

    # Subcomando: web
    p_web = subparsers.add_parser("web", help="Convierte una página web (URL o .html) a Markdown")
    p_web.add_argument("origen", nargs="?", help="URL o ruta a un fichero .html")
    p_web.add_argument("-o", "--output", dest="output_md",
                       help="Fichero .md de salida, o carpeta destino con --lista")
    p_web.add_argument("--lista", metavar="FICHERO",
                       help="Fichero de texto con una URL por línea para conversión por lotes")
    p_web.add_argument("--url", default="", dest="url_declarada",
                       help="Procedencia real de un .html local, para el frontmatter")
    p_web.add_argument("--export-images", action="store_true",
                       help="Descarga las imágenes a <nombre>_assets/")
    p_web.add_argument("--no-frontmatter", action="store_true")
    p_web.add_argument("--no-tables", action="store_true", help="No convierte <table> en tablas Markdown")
    p_web.add_argument("--no-links", action="store_true", help="Emite el texto de los enlaces sin su destino")
    p_web.add_argument("--no-code-blocks", action="store_true")
    p_web.add_argument("--no-alt", action="store_true",
                       help="No conserva el texto alternativo de las imágenes")
    p_web.add_argument("--no-slug", action="store_true",
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


def _cli_opciones(args):
    """Traduce los argumentos de la CLI a las opciones del conversor."""
    return PdfmdOptions(
        ocr_mode=args.ocr_mode,
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
        min_chars_por_pagina=args.min_chars_pagina
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
        log_cb=lambda m: print(f"[KIWI] {m}")
    )
    return 0


def _cli_convert(args):
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
    """Convierte una página web, o una lista de URLs, desde la línea de comandos."""
    opts = WebOptions(
        incluir_frontmatter=not args.no_frontmatter,
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
                                     generar_index=not args.no_index)
        # Un lote con fallos no puede devolver 0: en un .bat nadie leeria el
        # resumen y las capturas perdidas pasarian inadvertidas.
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

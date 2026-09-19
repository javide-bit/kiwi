# -*- coding: utf-8 -*-
"""
htmlmd.batch — Conversion por lotes de una lista de URLs, reanudable y auditable.

Mismas dos garantias que el lote de PDF, por las mismas razones: una tanda de
200 capturas no puede reiniciarse desde cero porque falle la numero 150, y al
terminar hace falta saber de que ficheros fiarse sin abrirlos uno a uno.

Soporta listados simples (una URL por linea) y listados estructurados
en Markdown (con cualquier tipo de secciones, categorías, temas, enlaces locales
`[CAPTURA LOCAL]` y metadatos).
"""

import csv
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Dict, List, Optional, Union

from htmlmd.fetch import ErrorDeDescarga, es_url
from htmlmd.models import WebOptions
from htmlmd.pipeline import html_to_markdown, nombre_de_salida
from pdfmd.utils import slugify


_RE_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*$", re.DOTALL | re.MULTILINE)

CAMPOS_MANIFIESTO = ["archivo", "seccion", "url", "chars", "bloques", "enlaces",
                     "imagenes", "contenido", "criterio_cuerpo", "avisos"]


@dataclass
class ItemLista:
    """Representa un elemento extraído de una lista estructurada."""
    origen: str
    seccion: str = ""
    tipo: str = "url"            # "url" | "local"
    etiqueta: str = ""
    url_declarada: str = ""


def leer_lista_estructurada(ruta: str, base_dir: Optional[str] = None) -> List[ItemLista]:
    """
    Lee un fichero de URLs o un listado Markdown/TXT enriquecido.

    Extrae secciones genéricas desde encabezados Markdown (p.ej. `## Recetas — 12 enlaces...` -> `"Recetas"`,
    `## Viajes` -> `"Viajes"`), capturas locales (`[CAPTURA LOCAL] path/to/file.html`),
    URLs crudas o en enlaces Markdown, resolviendo rutas locales y descartando comentarios
    y metadatos de cabecera. Adaptable a cualquier tipo de listado.
    """
    items: List[ItemLista] = []
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(ruta)) or "."

    seccion_actual = ""
    with open(ruta, "r", encoding="utf-8", errors="replace") as f:
        for linea in f:
            linea_str = linea.strip()
            if not linea_str:
                continue

            # Encabezados de sección (Markdown)
            if linea_str.startswith("#"):
                num_hashes = len(linea_str) - len(linea_str.lstrip("#"))
                header = linea_str.lstrip("#").strip()

                # Limpieza de anotaciones de sufijo habituales en títulos ('— 12 refs', ' - parte 1')
                header_limpio = header
                for sep in ("—", "–", " - "):
                    if sep in header_limpio:
                        header_limpio = header_limpio.split(sep, 1)[0].strip()

                header_low = header_limpio.lower()
                # Título general del documento vs. secciones/categorías de contenido
                es_titulo_documento = (num_hashes == 1 and not items) or any(
                    header_low.startswith(p) for p in (
                        "urls de", "dossier", "indice", "índice", "lista de", "contenido", "fuentes de", "listado de"
                    )
                )

                if not es_titulo_documento and header_limpio:
                    seccion_actual = header_limpio
                continue

            # Capturas locales marcadas con [CAPTURA LOCAL]
            if "[CAPTURA LOCAL]" in linea_str:
                path_raw = linea_str.split("[CAPTURA LOCAL]", 1)[1].strip()
                path_clean = path_raw.strip("`'\"")
                abs_path = os.path.normpath(os.path.join(base_dir, path_clean))
                items.append(ItemLista(
                    origen=abs_path,
                    seccion=seccion_actual,
                    tipo="local",
                    etiqueta=path_raw,
                    url_declarada=path_raw,
                ))
                continue

            # Comentarios tradicionales tipo #
            if linea_str.startswith("#"):
                continue

            # Detección de URLs (http:// o https://)
            urls = re.findall(r'https?://[^\s><"\'\)]+', linea_str)
            if urls:
                for url in urls:
                    url_clean = url.rstrip(".,;)")
                    items.append(ItemLista(
                        origen=url_clean,
                        seccion=seccion_actual,
                        tipo="url",
                        etiqueta=linea_str,
                    ))
                continue

            # Detección de rutas locales directas a archivos HTML/htm
            if linea_str.lower().endswith((".html", ".htm")):
                path_clean = linea_str.split("#", 1)[0].strip().strip("`'\"")
                if path_clean:
                    abs_path = os.path.normpath(os.path.join(base_dir, path_clean))
                    items.append(ItemLista(
                        origen=abs_path,
                        seccion=seccion_actual,
                        tipo="local",
                        etiqueta=path_clean,
                        url_declarada=path_clean,
                    ))
                continue

    return items


def leer_lista(ruta: str) -> List[str]:
    """
    Lee un fichero de URLs, una por linea (compatible con cualquier listado).
    """
    return [item.origen for item in leer_lista_estructurada(ruta)]


def _desescapar_yaml(valor: str) -> str:
    """Deshace el escapado de `yaml_quote`."""
    return valor.replace('\\"', '"').replace("\\\\", "\\")


def _urls_ya_convertidas(output_dir: str) -> Dict[str, str]:
    """Indexa las URLs presentes en el frontmatter de los .md ya generados (recursivo)."""
    hechas: Dict[str, str] = {}
    if not os.path.isdir(output_dir):
        return hechas
    for root, _, files in os.walk(output_dir):
        for nombre in files:
            if not nombre.lower().endswith(".md") or nombre.upper() == "INDEX.MD":
                continue
            ruta_completa = os.path.join(root, nombre)
            rel_nombre = os.path.relpath(ruta_completa, output_dir).replace("\\", "/")
            try:
                with open(ruta_completa, "r", encoding="utf-8", errors="replace") as f:
                    cabeza = f.read(4096)
            except OSError:
                continue
            m = _RE_FRONTMATTER.search(cabeza)
            if not m:
                continue
            for linea in m.group(1).splitlines():
                if linea.startswith(("url_origen:", "url_solicitada:")):
                    valor = _desescapar_yaml(linea.split(":", 1)[1].strip().strip('"'))
                    if valor and valor != "null":
                        hechas[valor] = rel_nombre
    return hechas


def escribir_manifiesto(filas: List[Dict[str, str]], ruta: str) -> str:
    """Escribe el TSV de control ordenado por caracteres extraidos, ascendente."""
    def _clave(fila):
        try:
            return int(fila.get("chars", 0) or 0)
        except (TypeError, ValueError):
            return 0

    ordenadas = sorted(filas, key=_clave)
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=CAMPOS_MANIFIESTO,
                                  delimiter="\t", extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(ordenadas)
    return ruta


def escribir_indice(filas: List[Dict[str, str]], ruta_index: str,
                           stats: Dict[str, int]) -> str:
    """
    Genera un índice Markdown (INDEX.md) interactivo agrupado por secciones o categorías.
    """
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    secciones = defaultdict(list)
    for f in filas:
        sec = f.get("seccion", "").strip() or "General"
        secciones[sec].append(f)

    lineas = [
        "# 📂 Índice",
        "",
        f"**Fecha de generación:** `{ahora}`  ",
        f"**Resumen del lote:** {stats.get('total', 0)} elementos total | "
        f"{stats.get('convertidas', 0)} convertidos | "
        f"{stats.get('omitidas', 0)} omitidos | "
        f"{stats.get('fallidas', 0)} fallidos",
        "",
        "---",
        ""
    ]

    for sec in sorted(secciones.keys()):
        elementos = secciones[sec]
        lineas.append(f"## 📍 {sec} ({len(elementos)} elementos)")
        lineas.append("")
        lineas.append("| Documento | Origen / URL | Caracteres | Enlaces | Estado | Avisos |")
        lineas.append("|---|---|---|---|---|---|")

        for elem in elementos:
            arch = elem.get("archivo", "")
            url = elem.get("url", "")
            chars = elem.get("chars", "0")
            enlaces = elem.get("enlaces", "0")
            contenido = elem.get("contenido", "desconocido")
            avisos = elem.get("avisos", "").replace("\n", " ") or "—"

            link_arch = f"[{os.path.basename(arch)}]({arch})" if arch else "*(sin archivo)*"
            if es_url(url):
                link_url = f"[URL]({url})"
            elif url:
                link_url = f"`{os.path.basename(url)}`"
            else:
                link_url = "—"

            badge_estado = f"`{contenido}`"
            if contenido == "completo":
                badge_estado = "✅ `completo`"
            elif contenido in ("escaso", "ausente"):
                badge_estado = "⚠️ `escaso`"
            elif contenido in ("fallo", "error"):
                badge_estado = "❌ `fallo`"

            try:
                chars_fmt = f"{int(chars):,}"
            except ValueError:
                chars_fmt = chars

            lineas.append(f"| {link_arch} | {link_url} | {chars_fmt} | {enlaces} | {badge_estado} | {avisos} |")

        lineas.append("")
        lineas.append("---")
        lineas.append("")

    with open(ruta_index, "w", encoding="utf-8") as f:
        f.write("\n".join(lineas))

    return ruta_index


def convertir_lote(origenes: List[Union[str, ItemLista]], output_dir: str,
                   options: Optional[WebOptions] = None,
                   log_cb: Optional[Callable[[str], None]] = None,
                   progress_cb: Optional[Callable[[int, int], None]] = None,
                   refrescar: bool = False,
                   pausa: float = 1.0,
                   organizar_por_secciones: bool = True,
                   generar_index: bool = True,
                   cancel_event=None) -> Dict[str, object]:
    """
    Convierte una lista de URLs, capturas locales u objetos `ItemLista` a Markdown.

    Soporta subcarpetas por sección/categoría y generación automática de `INDEX.md`.
    """
    opts = options or WebOptions()
    os.makedirs(output_dir, exist_ok=True)

    ya_hechas = {} if refrescar else _urls_ya_convertidas(output_dir)
    filas: List[Dict[str, str]] = []
    usados = set()
    convertidas = omitidas = fallidas = 0
    total = len(origenes)

    def _log(mensaje: str) -> None:
        if log_cb:
            log_cb(mensaje)

    for i, item in enumerate(origenes, start=1):
        if cancel_event is not None and cancel_event.is_set():
            _log("Lote cancelado por el usuario.")
            break

        if progress_cb:
            progress_cb(i, total)

        if isinstance(item, ItemLista):
            origen = item.origen
            seccion = item.seccion
            url_declarada = item.url_declarada
        else:
            origen = str(item)
            seccion = getattr(opts, "seccion", "")
            url_declarada = ""

        # Determinar subcarpeta si organizar_por_secciones está activo
        if organizar_por_secciones and seccion:
            sec_folder = slugify(seccion, por_defecto="GENERAL").upper()
            target_dir = os.path.join(output_dir, sec_folder)
        else:
            sec_folder = ""
            target_dir = output_dir

        os.makedirs(target_dir, exist_ok=True)

        if origen in ya_hechas:
            omitidas += 1
            rel_hecha = ya_hechas[origen]
            _log(f"[{i}/{total}] Ya convertida, se omite: {origen} -> {rel_hecha}")
            filas.append({
                "archivo": rel_hecha, "seccion": seccion, "url": origen,
                "chars": "0", "bloques": "0", "enlaces": "0", "imagenes": "0",
                "contenido": "omitida", "criterio_cuerpo": "", "avisos": "Ya convertida previamente",
            })
            continue

        provisional = nombre_de_salida(origen)
        destino = os.path.join(target_dir, provisional)

        opts_item = replace(opts, seccion=seccion) if hasattr(opts, "seccion") else opts

        try:
            _, meta = html_to_markdown(origen, destino, opts_item, log_cb=None,
                                        url_origen=url_declarada)
        except ErrorDeDescarga as ex:
            fallidas += 1
            _log(f"[{i}/{total}] FALLO: {ex}")
            filas.append({"archivo": "", "seccion": seccion, "url": origen, "chars": "0", "bloques": "0",
                          "enlaces": "0", "imagenes": "0", "contenido": "fallo",
                          "criterio_cuerpo": "", "avisos": str(ex)})
            continue
        except Exception as ex:                      # noqa: BLE001 - un lote no se detiene
            fallidas += 1
            _log(f"[{i}/{total}] ERROR inesperado con {origen}: {ex}")
            filas.append({"archivo": "", "seccion": seccion, "url": origen, "chars": "0", "bloques": "0",
                          "enlaces": "0", "imagenes": "0", "contenido": "error",
                          "criterio_cuerpo": "", "avisos": str(ex)})
            continue

        definitivo = nombre_de_salida(origen, meta)
        raiz, ext = os.path.splitext(definitivo)
        key_usado = f"{sec_folder}/{definitivo.lower()}"
        n = 2
        while key_usado in usados or (
                definitivo != provisional
                and os.path.exists(os.path.join(target_dir, definitivo))):
            definitivo = f"{raiz}-{n}{ext}"
            key_usado = f"{sec_folder}/{definitivo.lower()}"
            n += 1
        usados.add(key_usado)

        ruta_final = os.path.join(target_dir, definitivo)
        if definitivo != provisional:
            try:
                os.replace(destino, ruta_final)
            except OSError:
                ruta_final = destino
                definitivo = provisional

        convertidas += 1
        aviso = "; ".join(meta.avisos)
        rel_final = os.path.relpath(ruta_final, output_dir).replace("\\", "/")
        _log(f"[{i}/{total}] {origen} -> {rel_final} "
             f"({meta.chars_extraidos} chars, {meta.contenido})")
        filas.append({
            "archivo": rel_final, "seccion": seccion, "url": meta.url_final or origen,
            "chars": str(meta.chars_extraidos), "bloques": str(meta.bloques),
            "enlaces": str(meta.enlaces), "imagenes": str(meta.imagenes),
            "contenido": meta.contenido, "criterio_cuerpo": meta.criterio_cuerpo,
            "avisos": aviso,
        })

        if pausa > 0 and es_url(origen) and i < total:
            time.sleep(pausa)

    manifiesto = ""
    index_md = ""
    stats = {"total": total, "convertidas": convertidas, "omitidas": omitidas, "fallidas": fallidas}

    if filas:
        manifiesto = escribir_manifiesto(filas, os.path.join(output_dir, "manifiesto.tsv"))
        _log(f"Manifiesto escrito en: {manifiesto}")
        if generar_index:
            index_md = escribir_indice(filas, os.path.join(output_dir, "INDEX.md"), stats)
            _log(f"Índice escrito en: {index_md}")

    _log(f"Lote terminado: {convertidas} convertidas, {omitidas} omitidas, "
         f"{fallidas} fallidas.")
    return {"convertidas": convertidas, "omitidas": omitidas, "fallidas": fallidas,
            "manifiesto": manifiesto, "index_md": index_md, "filas": filas}

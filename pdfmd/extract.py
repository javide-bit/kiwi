# -*- coding: utf-8 -*-
"""
pdfmd.extract — Extracción avanzada de contenido de PDFs usando PyMuPDF y soporte OCR.
"""

import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from typing import Callable, List, Optional, Tuple
import pymupdf as fitz
from dataclasses import replace

from pdfmd.models import Block, DocumentMetadata, Line, Options, PageText, Span

# (rectangulo, url, texto del ancla) de una anotacion /Link
Enlace = Tuple[Tuple[float, float, float, float], str, str]
from pdfmd.utils import (
    detectar_idioma,
    fecha_de_fichero,
    fecha_pdf_iso,
    find_ocrmypdf_binary,
    find_tesseract_binary,
    inferir_idioma_ocr_documento,
    is_ocrmypdf_available,
    is_tesseract_available,
    leer_url_origen,
    normalizar_texto_pdf,
    preparar_entorno_ocr,
    resolver_idioma_ocr,
    sha256_fichero,
    version_motor_ocr,
)


def extract_document_metadata(doc: fitz.Document, filename: str,
                              options: Optional[Options] = None) -> DocumentMetadata:
    """Extrae metadatos estándar del PDF y su cadena de procedencia."""
    meta = doc.metadata or {}
    opts = options or Options()

    def _clean(key: str) -> str:
        return (meta.get(key) or "").strip()

    try:
        toc = doc.get_toc() or []
    except Exception:
        toc = []

    return DocumentMetadata(
        title=_clean("title"),
        author=_clean("author"),
        subject=_clean("subject"),
        keywords=_clean("keywords"),
        creator=_clean("creator"),
        creation_date=fecha_pdf_iso(_clean("creationDate")),
        mod_date=fecha_pdf_iso(_clean("modDate")),
        page_count=len(doc),
        filename=os.path.basename(filename),
        url_origen=opts.url_origen or leer_url_origen(filename),
        fecha_captura=opts.fecha_captura,
        fecha_archivo=fecha_de_fichero(filename),
        ocr_modo=opts.ocr_mode,
        fecha_conversion=datetime.now().strftime("%Y-%m-%d"),
        sha256_pdf=sha256_fichero(filename) if opts.compute_hash else "",
        outline=[(int(x[0]), str(x[1]), int(x[2])) for x in toc if len(x) >= 3],
    )


def should_apply_ocr(page: fitz.Page, options: Options) -> bool:
    """Heurística para determinar si una página requiere OCR."""
    if options.ocr_mode in ("off", "ocrmypdf"):
        # El modo ocrmypdf se resuelve en una pasada previa sobre el documento completo.
        return False
    if options.ocr_mode == "tesseract":
        return True

    # "auto" (por defecto) hace OCR en paginas que parecen escaneadas o diapositivas
    # con imagen de pagina completa y poco texto nativo.
    text = page.get_text("text").strip()
    if len(text) >= 340:
        return False

    img_area = 0.0
    page_rect = page.rect
    total_area = max(1.0, page_rect.width * page_rect.height)
    for img_info in page.get_images():
        try:
            rects = page.get_image_rects(img_info[0])
            for r in rects:
                img_area += r.width * r.height
        except Exception:
            pass
    ratio = img_area / total_area
    if len(text) < 100 and ratio > 0.20:
        return True
    if len(text) < 200 and ratio > 0.30:
        return True
    if len(text) < 340 and ratio > 0.95:
        return True
    return False


# ocrmypdf ya reparte cada documento entre todos los nucleos: varios a la vez
# (lote con varios hilos) solo multiplican memoria y procesos. Uno cada vez.
_OCRMYPDF_LOCK = threading.Lock()


def _necesita_force_ocr(input_pdf: str) -> bool:
    """
    True si el PDF carece de texto sustancial en la mayoría de sus páginas (p. ej.
    solo lleva un sello 'Downloaded by...' de <200 chars sobre páginas escaneadas).
    Si ya tiene >=25 % de páginas con >=250 chars de texto nativo, '--skip-text'
    conserva ese texto y solo hace OCR en las páginas vacías/escaneadas.
    """
    try:
        with fitz.open(input_pdf) as doc:
            if doc.is_encrypted or len(doc) == 0:
                return True
            con_texto_real = sum(1 for p in doc if len(p.get_text("text").strip()) >= 250)
            return con_texto_real * 4 < len(doc)
    except Exception:
        return True


def run_ocrmypdf(input_pdf: str, lang: str = "eng",
                 log_cb: Optional[Callable[[str], None]] = None,
                 forzar: bool = False,
                 avisos_out: Optional[List[str]] = None) -> Optional[str]:
    """
    Ejecuta ocrmypdf sobre el documento completo generando una copia temporal con
    capa de texto. Devuelve la ruta del PDF procesado o None si no fue posible.
    El llamante es responsable de eliminar el directorio temporal resultante.
    """
    preparar_entorno_ocr()
    binary = find_ocrmypdf_binary()
    if not binary:
        msg = "ocrmypdf no está instalado en el sistema: se continúa sin OCR."
        if log_cb:
            log_cb(msg)
        if avisos_out is not None:
            avisos_out.append(msg)
        return None

    tmp_dir = tempfile.mkdtemp(prefix="pdfmd_ocr_")
    out_pdf = os.path.join(tmp_dir, "ocr_" + os.path.basename(input_pdf))
    # --force-ocr rehace todas las paginas cuando apenas hay texto nativo (ej. sello
    # "Downloaded by..."); si el documento ya tiene muchas paginas con texto real,
    # --skip-text evita rasterizar lo que ya estaba bien extraido.
    usar_force = forzar and _necesita_force_ocr(input_pdf)
    modo = "--force-ocr" if usar_force else "--skip-text"
    idioma_efectivo = inferir_idioma_ocr_documento(input_pdf, base_lang=lang)
    jobs = str(min(4, max(1, os.cpu_count() or 2)))
    cmd = [binary, modo, "--jobs", jobs, "--quiet", "-l", idioma_efectivo, input_pdf, out_pdf]

    if log_cb:
        log_cb(f"Aplicando OCR con ocrmypdf ({modo}, -l {idioma_efectivo}) sobre el documento completo...")
    try:
        with _OCRMYPDF_LOCK:
            proc = subprocess.run(cmd, capture_output=True, timeout=3600,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode == 0 and os.path.exists(out_pdf):
            return out_pdf
        # Codigo 6 en ocrmypdf significa que ya habia texto; si no se creo out_pdf, copiar o reintentar con --skip-text
        if proc.returncode == 6 and os.path.exists(out_pdf):
            return out_pdf
        detalle = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        msg_err = f"ocrmypdf devolvió un error ({proc.returncode}): {detalle}"
        if log_cb:
            log_cb(msg_err)
        if avisos_out is not None:
            avisos_out.append(msg_err)
    except subprocess.TimeoutExpired:
        msg_timeout = "ocrmypdf superó el tiempo máximo de espera: se continúa sin OCR."
        if log_cb:
            log_cb(msg_timeout)
        if avisos_out is not None:
            avisos_out.append(msg_timeout)
    except Exception as ex:
        msg_ex = f"No se pudo ejecutar ocrmypdf: {ex}"
        if log_cb:
            log_cb(msg_ex)
        if avisos_out is not None:
            avisos_out.append(msg_ex)

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def run_tesseract_on_page(page: fitz.Page, lang: str = "eng") -> Optional[PageText]:
    """Ejecuta OCR sobre el pixmap de una página invocando directamente el binario tesseract."""
    try:
        preparar_entorno_ocr()
        tess_bin = find_tesseract_binary()
        if not tess_bin:
            return None

        dpi = 200
        escala = 72.0 / float(dpi)
        pix = page.get_pixmap(dpi=dpi)
        png_bytes = pix.tobytes("png")
        idioma = resolver_idioma_ocr(lang)

        proc = subprocess.run(
            [tess_bin, "stdin", "stdout", "-l", idioma, "tsv"],
            input=png_bytes,
            capture_output=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not proc.stdout:
            return None

        salida = proc.stdout.decode("utf-8", "replace").splitlines()
        blocks_map = {}
        for idx_linea, fila in enumerate(salida):
            if idx_linea == 0 or not fila.strip():
                continue
            partes = fila.split("\t")
            if len(partes) < 12:
                continue
            txt = normalizar_texto_pdf(partes[11].strip())
            if not txt:
                continue
            try:
                b_num = (int(partes[2]), int(partes[3]))
                l_num = int(partes[4])
                x = float(partes[6]) * escala
                y = float(partes[7]) * escala
                w = float(partes[8]) * escala
                h = float(partes[9]) * escala
            except ValueError:
                continue

            span = Span(text=txt + " ", size=max(6.0, round(h * 0.85, 1)), bbox=(x, y, x + w, y + h))
            if b_num not in blocks_map:
                blocks_map[b_num] = {}
            if l_num not in blocks_map[b_num]:
                blocks_map[b_num][l_num] = []
            blocks_map[b_num][l_num].append(span)

        out_blocks = []
        total_chars = 0
        for b_idx in sorted(blocks_map.keys()):
            lines_out = []
            for l_idx in sorted(blocks_map[b_idx].keys()):
                spans = blocks_map[b_idx][l_idx]
                # Un bbox real por línea permite depurar cabeceras y pies tras el OCR
                x0 = min(s.bbox[0] for s in spans)
                y0 = min(s.bbox[1] for s in spans)
                x1 = max(s.bbox[2] for s in spans)
                y1 = max(s.bbox[3] for s in spans)
                lines_out.append(Line(spans=spans, bbox=(x0, y0, x1, y1)))
                total_chars += sum(len(s.text) for s in spans)
            if lines_out:
                out_blocks.append(Block(lines=lines_out, block_type="text", page_num=page.number + 1))

        return PageText(
            blocks=out_blocks,
            page_num=page.number + 1,
            width=page.rect.width,
            height=page.rect.height,
            char_count=total_chars,
            has_images=True,
            ocr_aplicado=True,
        )
    except Exception:
        return None


def extract_native_tables(page: fitz.Page) -> List[Tuple[Tuple[float, float, float, float], List[List[str]]]]:
    """Extrae tablas nativas usando find_tables de PyMuPDF si están presentes."""
    tables_found = []
    if hasattr(page, "find_tables"):
        try:
            # pymupdf 2.x con pymupdf_layout instalado pasa cada pagina por un
            # modelo neuronal (~4 s/pagina); las tablas con lineas no lo necesitan.
            try:
                tabs = page.find_tables(use_layout=False)
            except TypeError:
                tabs = page.find_tables()
            for tab in tabs:
                extracted = tab.extract()
                if extracted and len(extracted) >= 2:
                    # Limpiar None y valores nulos
                    clean_rows = []
                    for row in extracted:
                        clean_rows.append([str(c or "").strip() for c in row])
                    tables_found.append((tuple(tab.bbox), clean_rows))
        except Exception:
            pass
    return tables_found


def extraer_enlaces(page: fitz.Page) -> List[Enlace]:
    """
    Devuelve las anotaciones /Link externas de la pagina como (rect, uri, ancla).

    Los enlaces forman parte del contenido (referencias, fuentes, documentos
    citados): convertir sin ellos conserva el texto pero pierde a donde apunta.
    El texto del ancla se lee del propio rectangulo, que es lo unico que permite
    saber que parte de la linea iba enlazada cuando el PDF no separa los spans.
    """
    salida: List[Enlace] = []
    try:
        for enlace in page.get_links():
            uri = (enlace.get("uri") or "").strip()
            if not uri:
                continue
            # Los saltos internos del propio PDF no aportan nada al Markdown
            if uri.lower().startswith(("#", "file:")):
                continue
            r = enlace.get("from")
            if r is None:
                continue
            try:
                ancla = " ".join(page.get_textbox(r).split())
            except Exception:
                ancla = ""
            salida.append(((float(r.x0), float(r.y0), float(r.x1), float(r.y1)), uri, ancla))
    except Exception:
        return []
    return salida


def _solapamiento(a: Tuple[float, float, float, float],
                  b: Tuple[float, float, float, float]) -> float:
    """Fraccion del area de `a` cubierta por `b`."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    if area_a <= 0:
        return 0.0
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    return (ix * iy) / area_a


def _partir_span(span: Span, ancla: str, uri: str) -> List[Span]:
    """
    Parte un span en (antes, enlazado, despues) cuando el ancla es solo un trozo.

    Muchos PDFs emiten la linea entera como un unico span y el rectangulo del
    /Link cubre nada mas que unas palabras: sin partir, o se enlaza la frase
    completa o se pierde el enlace.
    """
    if not ancla:
        return [replace(span, link=uri)]
    pos = span.text.find(ancla)
    if pos == -1:
        # El texto del rectangulo puede venir con los espacios normalizados
        compacto = ancla.replace(" ", "")
        sin_espacios = span.text.replace(" ", "")
        if compacto and compacto in sin_espacios:
            return [replace(span, link=uri)]
        return [replace(span, link=uri)]

    trozos = []
    if pos > 0:
        trozos.append(replace(span, text=span.text[:pos]))
    trozos.append(replace(span, text=span.text[pos:pos + len(ancla)], link=uri))
    resto = span.text[pos + len(ancla):]
    if resto:
        trozos.append(replace(span, text=resto))
    return trozos


def aplicar_enlaces(page_text: PageText, enlaces: List[Enlace]) -> int:
    """
    Marca con su URL de destino cada span cubierto por una anotacion /Link,
    partiendo el span cuando el enlace solo alcanza una parte de su texto.
    Devuelve cuantos spans quedaron enlazados.
    """
    if not enlaces:
        return 0
    marcados = 0
    for block in page_text.blocks:
        if block.block_type == "image":
            continue
        for line in block.lines:
            nuevos: List[Span] = []
            for span in line.spans:
                if not span.text.strip():
                    nuevos.append(span)
                    continue

                mejor: Optional[Enlace] = None
                mejor_area = 0.0
                for rect, uri, ancla in enlaces:
                    cubierto = _solapamiento(span.bbox, rect)
                    if cubierto > mejor_area:
                        mejor_area = cubierto
                        mejor = (rect, uri, ancla)

                if mejor is None or mejor_area <= 0.05:
                    nuevos.append(span)
                    continue

                _rect, uri, ancla = mejor
                if mejor_area > 0.85:
                    # El rectangulo cubre el span entero: no hay nada que partir
                    nuevos.append(replace(span, link=uri))
                    marcados += 1
                else:
                    partido = _partir_span(span, ancla, uri)
                    marcados += sum(1 for sp in partido if sp.link)
                    nuevos.extend(partido)
            line.spans = nuevos
    return marcados


def enlaces_sin_ancla(page_text: PageText, enlaces: List[Enlace]) -> List[str]:
    """
    URLs cuya anotacion no quedo pegada a ningun texto (enlaces sobre una imagen,
    sobre un boton o con el rectangulo desalineado). Perder el ancla es
    aceptable; perder la URL no.
    """
    ya_puestas = {
        s.link for b in page_text.blocks for l in b.lines for s in l.spans if s.link
    }
    huerfanas = []
    for _rect, uri, _ancla in enlaces:
        if uri not in ya_puestas and uri not in huerfanas:
            huerfanas.append(uri)
    return huerfanas


def detectar_raya_de_notas(page: fitz.Page) -> float:
    """
    Localiza la raya horizontal que separa el cuerpo de las notas al pie.
    Devuelve su coordenada Y, o 0.0 si no hay ninguna reconocible.
    """
    alto = page.rect.height
    ancho = page.rect.width
    mejor = 0.0
    try:
        for dibujo in page.get_drawings():
            r = dibujo["rect"]
            # Horizontal (casi sin altura), en la mitad inferior y sin cruzar toda la hoja
            if r.height > 3.0:
                continue
            if not (alto * 0.55 < r.y0 < alto * 0.97):
                continue
            if not (ancho * 0.15 < r.width < ancho * 0.85):
                continue
            mejor = max(mejor, r.y0)
    except Exception:
        return 0.0
    return mejor


def diagnosticar_capa_texto(pages: List[PageText], metadata: DocumentMetadata,
                            options: Options, motor_ocr: str = "",
                            log_cb: Optional[Callable[[str], None]] = None) -> None:
    """
    Rellena en los metadatos el volumen de texto extraido y clasifica la capa.

    Un PDF escaneado produce un MD vacio o con basura de OCR y nada avisa de que
    lo que se lee no es el documento: es un fallo silencioso y por eso peligroso.
    Los PDF nativos del corpus dan 3.100-3.900 caracteres por pagina; por debajo
    de `min_chars_por_pagina` hay que sospechar.
    """
    total_chars = sum(p.char_count for p in pages)
    n_paginas = len(pages) or 1
    metadata.chars_extraidos = total_chars
    metadata.chars_por_pagina = int(round(total_chars / n_paginas))
    paginas_con_texto = sorted(p.char_count for p in pages if p.char_count >= 50)
    paginas_vacias = len(pages) - len(paginas_con_texto)
    mediana_con_texto = (
        paginas_con_texto[len(paginas_con_texto) // 2] if paginas_con_texto else 0
    )

    hubo_ocr = bool(motor_ocr) or any(p.ocr_aplicado for p in pages)
    if hubo_ocr:
        metadata.capa_texto = "ocr"
        metadata.ocr_motor = version_motor_ocr(motor_ocr or "tesseract")
    else:
        metadata.capa_texto = "nativa"
        metadata.ocr_motor = ""

    # Detectar escaneados que solo traen un sello corto repetido (ej. JSTOR 103 chars/pag)
    solo_sello_en_escaneo = (
        not hubo_ocr
        and len(pages) >= 5
        and mediana_con_texto < 160
        and sum(1 for p in pages if p.has_images and p.char_count < 160) >= int(len(pages) * 0.75)
    )

    # Si un documento largo tiene varias paginas con texto real (mediana alta), aunque
    # tenga muchas paginas tachadas/vacias no es "ausente" sino "escasa".
    tiene_paginas_sustanciales = (
        len(paginas_con_texto) >= 4
        and mediana_con_texto >= max(200, options.min_chars_por_pagina * 2)
        and total_chars >= 1500
    )

    # "Ausente" es no tener texto (media casi nula o mayoria de paginas vacias sin
    # cuerpo sustancial); una presentacion o un informe con graficos da poco texto
    # por pagina pero real: eso es "escasa", y no dispara el aviso de sin capa.
    if solo_sello_en_escaneo or (
        not tiene_paginas_sustanciales
        and (
            metadata.chars_por_pagina < options.min_chars_por_pagina
            or paginas_vacias * 2 > len(pages)
            or mediana_con_texto < options.min_chars_por_pagina
        )
    ):
        # Aunque haya pasado el OCR: si tras el OCR sigue sin haber texto, lo que
        # el usuario tiene delante no es el documento.
        metadata.capa_texto = "ausente"
        aviso = (f"El PDF no tiene capa de texto utilizable "
                 f"({metadata.chars_por_pagina} caracteres por pagina).")
        if aviso not in metadata.avisos:
            metadata.avisos.append(aviso)
        if log_cb:
            log_cb(f"AVISO: {aviso} Verificar el resultado contra el original.")
    elif not hubo_ocr and (
        metadata.chars_por_pagina < options.escasa_chars_por_pagina
        or mediana_con_texto < options.escasa_chars_por_pagina
    ):
        metadata.capa_texto = "escasa"

    metadata.enlaces = sum(
        1 for p in pages for b in p.blocks for l in b.lines for sp in l.spans if sp.link
    )

    if options.detect_language and not metadata.idioma:
        muestra = " ".join(
            l.text() for p in pages[:12] for b in p.blocks
            if b.block_type in ("text", "list_item") for l in b.lines
        )
        metadata.idioma = detectar_idioma(muestra)


def _is_relevant_image(image_bytes: Optional[bytes], options: Options,
                       width: int = 0, height: int = 0,
                       page_bbox: Optional[Tuple[float, float, float, float]] = None) -> bool:
    """Descarta imágenes triviales (separadores, viñetas, barras alargadas y artefactos de pocos px)."""
    if not image_bytes:
        return False

    # 1. Filtro por dimensiones en píxeles (criterio principal si se conocen)
    if width and height:
        if width < options.min_image_side or height < options.min_image_side:
            return False
        aspect = max(width, height) / max(1, min(width, height))
        if aspect > options.max_image_aspect_ratio:
            return False
    else:
        # Si no constan dimensiones en píxeles, recurrir al peso en bytes
        if len(image_bytes) < options.min_image_bytes:
            return False

    # 3. Filtro por dimensiones en puntos de página
    if page_bbox and any(page_bbox):
        bx0, by0, bx1, by1 = page_bbox
        w_pt = max(0.0, bx1 - bx0)
        h_pt = max(0.0, by1 - by0)
        if w_pt > 0 and h_pt > 0:
            if w_pt < 15.0 or h_pt < 15.0:
                return False
            if (w_pt * h_pt) < options.min_image_area_points:
                return False
            aspect_pt = max(w_pt, h_pt) / max(1.0, min(w_pt, h_pt))
            if aspect_pt > options.max_image_aspect_ratio:
                return False

    return True


def extract_pages(
    input_pdf: str,
    options: Options,
    pdf_password: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None
) -> Tuple[List[PageText], DocumentMetadata]:
    """
    Extrae las páginas del PDF. Si el documento sale sin capa de texto y hay
    motor de OCR, reintenta con OCR y se queda con la extracción más completa.
    """
    if not os.path.exists(input_pdf):
        raise FileNotFoundError(f"No se encontró el archivo PDF: {input_pdf}")
    preparar_entorno_ocr()

    # Documento mayoritariamente escaneado: ocrmypdf de una vez usa todos los
    # nucleos; Tesseract pagina a pagina es secuencial (253 paginas, ~40 min).
    if options.ocr_mode == "auto" and is_ocrmypdf_available() and _mayoria_escaneada(input_pdf, options):
        if log_cb:
            log_cb("Documento escaneado: OCR completo con ocrmypdf...")
        pages, metadata = _extract_pages_once(input_pdf, replace(options, ocr_mode="ocrmypdf"),
                                              pdf_password, log_cb, progress_cb, cancel_event,
                                              forzar_ocr=True)
        if metadata.capa_texto == "ocr":
            metadata.ocr_modo = options.ocr_mode
            return pages, metadata

    pages, metadata = _extract_pages_once(input_pdf, options, pdf_password, log_cb,
                                          progress_cb, cancel_event,
                                          forzar_ocr=(options.ocr_mode == "ocrmypdf"))
    if (options.ocr_mode == "off" or not options.detect_text_layer
            or metadata.capa_texto != "ausente"):
        return pages, metadata
    # ponytail: sin imagenes no hay escaneo que leer; un PDF con el texto
    # convertido en trazos (sin imagen) no se reintenta. Forzar ocrmypdf si aparece.
    if not any(p.has_images for p in pages):
        return pages, metadata

    # Reintento: ocrmypdf forzado (todas las paginas, en paralelo) o, si no
    # esta, Tesseract en todas las paginas cuando la primera pasada no lo uso.
    if is_ocrmypdf_available():
        reintento, forzar = replace(options, ocr_mode="ocrmypdf"), True
    elif is_tesseract_available() and not all(p.ocr_aplicado for p in pages):
        reintento, forzar = replace(options, ocr_mode="tesseract"), False
    else:
        if log_cb:
            log_cb("Sin capa de texto y sin motor de OCR instalado: no se puede reintentar.")
        return pages, metadata

    if log_cb:
        log_cb(f"Sin capa de texto: reintentando con OCR ({reintento.ocr_mode})...")
    try:
        pages2, metadata2 = _extract_pages_once(input_pdf, reintento, pdf_password, log_cb,
                                                progress_cb, cancel_event, forzar_ocr=forzar)
    except (InterruptedError, PermissionError):
        raise
    except Exception as ex:
        if log_cb:
            log_cb(f"El reintento con OCR falló: {ex}")
        return pages, metadata
    # Solo si el OCR aporta de verdad: rasterizar un PDF nativo escueto y
    # volver a leerlo daria el mismo texto sin enlaces ni negritas.
    if metadata2.chars_extraidos >= 2 * metadata.chars_extraidos + 100 or metadata2.capa_texto == "ocr":
        metadata2.ocr_modo = options.ocr_mode
        return pages2, metadata2
    return pages, metadata


def _mayoria_escaneada(input_pdf: str, options: Options) -> bool:
    """True si al menos 3 paginas y mas de la mitad piden OCR (y no esta cifrado)."""
    try:
        with fitz.open(input_pdf) as doc:
            if doc.is_encrypted or len(doc) < 3:
                return False
            escaneadas = sum(1 for page in doc if should_apply_ocr(page, options))
            return escaneadas * 2 > len(doc)
    except Exception:
        return False


def _bloques_desde_texto_plano(texto: str, page_num: int) -> List[Block]:
    """Construye bloques de texto a partir de una extracción plana alternativa."""
    bloques: List[Block] = []
    parrafos = re.split(r"\n\s*\n", texto.strip()) if "re" in globals() else texto.strip().split("\n\n")
    for p in parrafos:
        lineas = [
            Line(spans=[Span(text=normalizar_texto_pdf(l.strip()))])
            for l in p.splitlines()
            if l.strip()
        ]
        if lineas:
            bloques.append(Block(lines=lineas, block_type="text", page_num=page_num))
    return bloques


def _extract_pages_once(
    input_pdf: str,
    options: Options,
    pdf_password: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    forzar_ocr: bool = False
) -> Tuple[List[PageText], DocumentMetadata]:
    """Una pasada de extracción con metadatos estructurados y tablas nativas."""
    if options.ocr_mode in ("auto", "tesseract") and not is_tesseract_available():
        if log_cb:
            log_cb("Aviso: Tesseract no está disponible; el OCR se omitirá en las páginas escaneadas.")

    avisos_ocr: List[str] = []
    # OCR previo del documento completo (modo ocrmypdf)
    ocr_tmp_pdf = None
    source_pdf = input_pdf
    if options.ocr_mode == "ocrmypdf":
        ocr_tmp_pdf = run_ocrmypdf(input_pdf, lang=options.ocr_lang, log_cb=log_cb,
                                   forzar=forzar_ocr, avisos_out=avisos_ocr)
        if ocr_tmp_pdf:
            source_pdf = ocr_tmp_pdf

    doc = fitz.open(source_pdf)
    try:
        if doc.is_encrypted:
            if pdf_password:
                if not doc.authenticate(pdf_password):
                    raise PermissionError("Contraseña incorrecta para el PDF cifrado.")
            else:
                raise PermissionError("El PDF está protegido con contraseña.")

        metadata = extract_document_metadata(doc, input_pdf, options)
        for av in avisos_ocr:
            if av not in metadata.avisos:
                metadata.avisos.append(av)
        idioma_doc_ocr = inferir_idioma_ocr_documento(
            input_pdf, base_lang=options.ocr_lang,
            title=metadata.title, subject=metadata.subject
        )

        total_pages = len(doc)
        max_pages = min(max(1, options.preview_pages), total_pages) if options.preview_only else total_pages
        pages_text: List[PageText] = []

        for idx in range(max_pages):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Extracción cancelada por el usuario.")

            page = doc[idx]
            page_num = idx + 1
            if log_cb:
                log_cb(f"Extrayendo página {page_num}/{max_pages}...")

            if should_apply_ocr(page, options):
                if log_cb:
                    log_cb(f"Aplicando OCR ({idioma_doc_ocr}) en página {page_num}...")
                p_dict = page.get_text("dict")
                nativa_pt = PageText.from_pymupdf_dict(p_dict, page_num=page_num)
                ocr_res = run_tesseract_on_page(page, lang=idioma_doc_ocr)
                if ocr_res and ocr_res.blocks and (
                    ocr_res.char_count >= nativa_pt.char_count or options.ocr_mode == "tesseract"
                ):
                    ocr_res.ocr_aplicado = True
                    pages_text.append(ocr_res)
                else:
                    # Si la capa nativa de esa pagina tenia mas texto que el OCR, conservarla
                    # sin perder la marca de que se paso el OCR en la pagina con imagen.
                    if ocr_res is not None:
                        nativa_pt.ocr_aplicado = True
                    if options.extract_links:
                        enlaces = extraer_enlaces(page)
                        if enlaces:
                            aplicar_enlaces(nativa_pt, enlaces)
                            nativa_pt.enlaces_huerfanos = enlaces_sin_ancla(nativa_pt, enlaces)
                    pages_text.append(nativa_pt)
            else:
                # 1. Extracción de tablas nativas si la opción está activa
                native_tables = extract_native_tables(page) if options.detect_tables else []
                table_bboxes = [t[0] for t in native_tables]

                p_dict = page.get_text("dict")
                page_text = PageText.from_pymupdf_dict(p_dict, page_num=page_num)

                # Comparación con extracción alternativa (get_text("text")) si la capa dict pierde texto
                if page_text.char_count < options.escasa_chars_por_pagina and not native_tables:
                    alt_txt = normalizar_texto_pdf(page.get_text("text") or "").strip()
                    if len(alt_txt) > page_text.char_count * 1.35 + 80:
                        bloques_alt = _bloques_desde_texto_plano(alt_txt, page_num)
                        if bloques_alt:
                            page_text.blocks = bloques_alt
                            page_text.char_count = len(alt_txt)

                # 2. Descartar imágenes irrelevantes o no solicitadas
                page_text.blocks = [
                    b for b in page_text.blocks
                    if b.block_type != "image" or (
                        options.export_images and _is_relevant_image(
                            b.image_bytes, options, b.image_width, b.image_height, page_bbox=b.bbox)
                    )
                ]

                # 3. Si se encontraron tablas nativas, reemplazar los bloques de texto coincidentes
                if native_tables:
                    filtered_blocks = []
                    # Inyectar tablas
                    for t_bbox, t_data in native_tables:
                        filtered_blocks.append(Block(
                            block_type="table",
                            table_data=t_data,
                            bbox=t_bbox,
                            page_num=page_num
                        ))
                    # Conservar bloques de texto que no intersecten significativamente con las tablas
                    for b in page_text.blocks:
                        bx0, by0, bx1, by1 = b.bbox
                        inside_table = False
                        for tx0, ty0, tx1, ty1 in table_bboxes:
                            if bx0 >= tx0 - 5 and by0 >= ty0 - 5 and bx1 <= tx1 + 5 and by1 <= ty1 + 5:
                                inside_table = True
                                break
                        if not inside_table:
                            filtered_blocks.append(b)

                    # Ordenar bloques visualmente de arriba hacia abajo (Y0)
                    filtered_blocks.sort(key=lambda blk: (blk.bbox[1], blk.bbox[0]))
                    page_text.blocks = filtered_blocks

                # 4. Extracción de imágenes adicionales del documento si export_images está activo
                if options.export_images:
                    try:
                        existing_bytes = {
                            b.image_bytes for b in page_text.blocks
                            if b.block_type == "image" and b.image_bytes
                        }
                        for img_item in page.get_images(full=True):
                            xref = img_item[0]
                            # Verificar si la imagen realmente se dibuja en esta página
                            rects = page.get_image_rects(xref)
                            if not rects:
                                continue

                            base_image = doc.extract_image(xref)
                            if not base_image:
                                continue
                            img_bytes = base_image.get("image")
                            img_ext = base_image.get("ext", "png")
                            if img_bytes in existing_bytes:
                                continue

                            img_w = int(base_image.get("width", 0) or 0)
                            img_h = int(base_image.get("height", 0) or 0)

                            # Usar el rectángulo visual en la página
                            prim_rect = rects[0]
                            r_bbox = (float(prim_rect.x0), float(prim_rect.y0),
                                      float(prim_rect.x1), float(prim_rect.y1))

                            if not _is_relevant_image(img_bytes, options, img_w, img_h, page_bbox=r_bbox):
                                continue

                            existing_bytes.add(img_bytes)
                            page_text.blocks.append(Block(
                                block_type="image",
                                image_bytes=img_bytes,
                                image_ext=img_ext,
                                image_width=img_w,
                                image_height=img_h,
                                bbox=r_bbox,
                                page_num=page_num
                            ))
                        # Ordenar bloques por posición Y0 en la página para mantener flujo natural
                        page_text.blocks.sort(key=lambda blk: (blk.bbox[1], blk.bbox[0]))
                    except Exception:
                        pass

                # 5. Hipervinculos: se resuelven sobre los bbox originales, antes
                #    de que la transformacion fusione o reordene las lineas.
                if options.extract_links:
                    enlaces = extraer_enlaces(page)
                    if enlaces:
                        aplicar_enlaces(page_text, enlaces)
                        page_text.enlaces_huerfanos = enlaces_sin_ancla(page_text, enlaces)

                if options.detect_footnotes:
                    page_text.rule_y = detectar_raya_de_notas(page)

                pages_text.append(page_text)

            if progress_cb:
                progress_cb(page_num, max_pages)

        if options.detect_text_layer:
            diagnosticar_capa_texto(
                pages_text, metadata, options,
                motor_ocr="ocrmypdf" if ocr_tmp_pdf else "",
                log_cb=log_cb
            )

        return pages_text, metadata
    finally:
        doc.close()
        if ocr_tmp_pdf:
            shutil.rmtree(os.path.dirname(ocr_tmp_pdf), ignore_errors=True)

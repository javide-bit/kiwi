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
    find_ocrmypdf_binary,
    find_tesseract_binary,
    is_tesseract_available,
    leer_url_origen,
    sha256_fichero,
)


def extract_document_metadata(doc: fitz.Document, filename: str,
                              options: Optional[Options] = None) -> DocumentMetadata:
    """Extrae metadatos estándar del PDF y su cadena de procedencia."""
    meta = doc.metadata or {}
    opts = options or Options()

    def _clean(key: str) -> str:
        return (meta.get(key) or "").strip()

    return DocumentMetadata(
        title=_clean("title"),
        author=_clean("author"),
        subject=_clean("subject"),
        keywords=_clean("keywords"),
        creator=_clean("creator"),
        creation_date=_clean("creationDate"),
        mod_date=_clean("modDate"),
        page_count=len(doc),
        filename=os.path.basename(filename),
        url_origen=opts.url_origen or leer_url_origen(filename),
        fecha_captura=opts.fecha_captura or fecha_de_fichero(filename),
        fecha_conversion=datetime.now().strftime("%Y-%m-%d"),
        sha256_pdf=sha256_fichero(filename) if opts.compute_hash else ""
    )


def should_apply_ocr(page: fitz.Page, options: Options) -> bool:
    """Heurística para determinar si una página requiere OCR."""
    if options.ocr_mode in ("off", "ocrmypdf"):
        # El modo ocrmypdf se resuelve en una pasada previa sobre el documento completo.
        return False
    if options.ocr_mode == "tesseract":
        return True

    text = page.get_text("text")
    if len(text.strip()) < 50:
        return True

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
    if (img_area / total_area) > 0.30:
        return True
    return False


def run_ocrmypdf(input_pdf: str, lang: str = "eng",
                 log_cb: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """
    Ejecuta ocrmypdf sobre el documento completo generando una copia temporal con
    capa de texto. Devuelve la ruta del PDF procesado o None si no fue posible.
    El llamante es responsable de eliminar el directorio temporal resultante.
    """
    binary = find_ocrmypdf_binary()
    if not binary:
        if log_cb:
            log_cb("ocrmypdf no está instalado en el sistema: se continúa sin OCR.")
        return None

    tmp_dir = tempfile.mkdtemp(prefix="pdfmd_ocr_")
    out_pdf = os.path.join(tmp_dir, "ocr_" + os.path.basename(input_pdf))
    cmd = [binary, "--skip-text", "--quiet", "-l", lang, input_pdf, out_pdf]

    if log_cb:
        log_cb("Aplicando OCR con ocrmypdf sobre el documento completo...")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=1800)
        if proc.returncode == 0 and os.path.exists(out_pdf):
            return out_pdf
        detalle = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        if log_cb:
            log_cb(f"ocrmypdf devolvió un error ({proc.returncode}): {detalle}")
    except subprocess.TimeoutExpired:
        if log_cb:
            log_cb("ocrmypdf superó el tiempo máximo de espera: se continúa sin OCR.")
    except Exception as ex:
        if log_cb:
            log_cb(f"No se pudo ejecutar ocrmypdf: {ex}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def run_tesseract_on_page(page: fitz.Page, lang: str = "eng") -> Optional[PageText]:
    """Ejecuta OCR sobre el pixmap de una página con pytesseract si está disponible."""
    try:
        import pytesseract
        from PIL import Image
        import io
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        tess_bin = find_tesseract_binary()
        if tess_bin:
            pytesseract.pytesseract.tesseract_cmd = tess_bin
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)

        blocks_map = {}
        for i in range(len(data["text"])):
            txt = data["text"][i].strip()
            if not txt:
                continue
            b_num = data["block_num"][i]
            l_num = data["line_num"][i]
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            span = Span(text=txt + " ", size=float(h), bbox=(x, y, x + w, y + h))

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
            has_images=True
        )
    except Exception:
        return None


def extract_native_tables(page: fitz.Page) -> List[Tuple[Tuple[float, float, float, float], List[List[str]]]]:
    """Extrae tablas nativas usando find_tables de PyMuPDF si están presentes."""
    tables_found = []
    if hasattr(page, "find_tables"):
        try:
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

    hubo_ocr = motor_ocr or any(p.ocr_aplicado for p in pages)
    if hubo_ocr:
        metadata.capa_texto = "ocr"
        metadata.ocr_motor = motor_ocr or "tesseract"
    else:
        metadata.capa_texto = "nativa"
        metadata.ocr_motor = ""

    if metadata.chars_por_pagina < options.min_chars_por_pagina:
        # Aunque haya pasado el OCR: si tras el OCR sigue sin haber texto, lo que
        # el usuario tiene delante no es el documento.
        metadata.capa_texto = "ausente"
        aviso = (f"El PDF no tiene capa de texto utilizable "
                 f"({metadata.chars_por_pagina} caracteres por pagina).")
        if aviso not in metadata.avisos:
            metadata.avisos.append(aviso)
        if log_cb:
            log_cb(f"AVISO: {aviso} Verificar el resultado contra el original.")

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
    """Extrae las páginas del PDF junto con metadatos estructurados y soporte de tablas nativas."""
    if not os.path.exists(input_pdf):
        raise FileNotFoundError(f"No se encontró el archivo PDF: {input_pdf}")

    if options.ocr_mode in ("auto", "tesseract") and not is_tesseract_available():
        if log_cb:
            log_cb("Aviso: Tesseract no está disponible; el OCR se omitirá en las páginas escaneadas.")

    # OCR previo del documento completo (modo ocrmypdf)
    ocr_tmp_pdf = None
    source_pdf = input_pdf
    if options.ocr_mode == "ocrmypdf":
        ocr_tmp_pdf = run_ocrmypdf(input_pdf, lang=options.ocr_lang, log_cb=log_cb)
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
                    log_cb(f"Aplicando OCR en página {page_num}...")
                ocr_res = run_tesseract_on_page(page, lang=options.ocr_lang)
                if ocr_res and ocr_res.blocks:
                    ocr_res.ocr_aplicado = True
                    pages_text.append(ocr_res)
                else:
                    p_dict = page.get_text("dict")
                    pages_text.append(PageText.from_pymupdf_dict(p_dict, page_num=page_num))
            else:
                # 1. Extracción de tablas nativas si la opción está activa
                native_tables = extract_native_tables(page) if options.detect_tables else []
                table_bboxes = [t[0] for t in native_tables]

                p_dict = page.get_text("dict")
                page_text = PageText.from_pymupdf_dict(p_dict, page_num=page_num)

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

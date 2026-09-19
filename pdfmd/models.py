# -*- coding: utf-8 -*-
"""
pdfmd.models — Modelos de datos para el pipeline de conversión de PDF a Markdown.
"""

from dataclasses import dataclass, field

from pdfmd.utils import normalizar_texto_pdf
from typing import Dict, List, Optional, Tuple, Literal, Any


@dataclass
class Span:
    text: str
    size: float = 11.0
    bold: bool = False
    italic: bool = False
    monospace: bool = False
    font: str = ""
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    link: str = ""      # URL de la anotacion /Link que cubre este span, si la hay


@dataclass
class Line:
    spans: List[Span] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def avg_size(self) -> float:
        if not self.spans:
            return 11.0
        total_len = sum(len(s.text) for s in self.spans)
        if total_len == 0:
            return 11.0
        return sum(s.size * len(s.text) for s in self.spans) / total_len

    @property
    def is_bold(self) -> bool:
        if not self.spans:
            return False
        return any(s.bold for s in self.spans)

    @property
    def is_monospace(self) -> bool:
        if not self.spans:
            return False
        return any(s.monospace for s in self.spans)


@dataclass
class Block:
    lines: List[Line] = field(default_factory=list)
    block_type: str = "text"  # "text", "heading", "table", "equation", "image", "code", "list_item", "quote"
    heading_level: int = 0
    table_data: Optional[List[List[str]]] = None
    code_lang: str = ""
    image_bytes: Optional[bytes] = None
    image_ext: str = "png"
    image_name: str = ""
    image_width: int = 0
    image_height: int = 0
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    page_num: int = 1

    def text(self) -> str:
        if self.table_data:
            return "\n".join("\t".join(row) for row in self.table_data)
        return "\n".join(line.text() for line in self.lines)


@dataclass
class Footnote:
    """Nota al pie extraida de la franja inferior de una pagina."""
    numero: int = 0                 # Numeracion global correlativa en todo el documento
    texto: str = ""
    page_num: int = 1
    marcador_original: str = ""     # Lo que ponia en el PDF (puede reiniciarse por pagina)
    anclada: bool = False           # True si se localizo la llamada dentro del cuerpo


@dataclass
class PageText:
    blocks: List[Block] = field(default_factory=list)
    page_num: int = 1
    width: float = 595.0
    height: float = 842.0
    char_count: int = 0
    has_images: bool = False
    footnotes: List["Footnote"] = field(default_factory=list)
    rule_y: float = 0.0             # Y de la raya separadora de notas, si la hay
    enlaces_huerfanos: List[str] = field(default_factory=list)  # URLs sin texto que anclar
    ocr_aplicado: bool = False      # True si el contenido de la pagina viene de OCR
    fin_de_portada: bool = False    # Primera pagina de cuerpo tras los creditos

    @classmethod
    def from_pymupdf_dict(cls, page_dict: dict, page_num: int = 1) -> "PageText":
        blocks_out = []
        total_chars = 0
        has_imgs = False
        width = page_dict.get("width", 595.0)
        height = page_dict.get("height", 842.0)

        for b in page_dict.get("blocks", []):
            b_type = b.get("type", 0)
            bbox = tuple(b.get("bbox", (0, 0, 0, 0)))

            if b_type == 1:  # Imagen
                has_imgs = True
                img_bytes = b.get("image", None)
                ext = b.get("ext", "png")
                blocks_out.append(Block(
                    block_type="image",
                    image_bytes=img_bytes,
                    image_ext=ext,
                    image_width=int(b.get("width", 0) or 0),
                    image_height=int(b.get("height", 0) or 0),
                    bbox=bbox,
                    page_num=page_num
                ))
            elif b_type == 0:  # Texto
                lines_out = []
                for l in b.get("lines", []):
                    spans_out = []
                    line_bbox = tuple(l.get("bbox", (0, 0, 0, 0)))
                    for s in l.get("spans", []):
                        txt = normalizar_texto_pdf(s.get("text", ""))
                        flags = s.get("flags", 0)
                        font = s.get("font", "").lower()
                        bold = bool(flags & 2 or "bold" in font or "black" in font or "heavy" in font)
                        italic = bool(flags & 1 or "italic" in font or "oblique" in font)
                        mono = any(m in font for m in ("courier", "consolas", "menlo", "monaco", "inconsolata", "mono", "code"))
                        size = float(s.get("size", 11.0))
                        span_bbox = tuple(s.get("bbox", (0, 0, 0, 0)))
                        total_chars += len(txt)
                        spans_out.append(Span(
                            text=txt,
                            size=size,
                            bold=bold,
                            italic=italic,
                            monospace=mono,
                            font=font,
                            bbox=span_bbox
                        ))
                    if spans_out:
                        lines_out.append(Line(spans=spans_out, bbox=line_bbox))
                if lines_out:
                    blocks_out.append(Block(
                        lines=lines_out,
                        block_type="text",
                        bbox=bbox,
                        page_num=page_num
                    ))

        return cls(
            blocks=blocks_out,
            page_num=page_num,
            width=width,
            height=height,
            char_count=total_chars,
            has_images=has_imgs
        )


@dataclass
class DocumentMetadata:
    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""
    creator: str = ""
    creation_date: str = ""
    mod_date: str = ""
    page_count: int = 0
    filename: str = ""
    url_origen: str = ""
    fecha_captura: str = ""
    fecha_conversion: str = ""
    sha256_pdf: str = ""
    idioma: str = ""                    # ISO 639-1 detectado del cuerpo ("en", "es"...)
    # Diagnostico de la capa de texto: sin esto un PDF escaneado produce un MD
    # vacio y nada avisa de que lo que se lee no es el documento.
    chars_extraidos: int = 0
    chars_por_pagina: int = 0
    capa_texto: str = "nativa"          # nativa | ocr | ausente
    ocr_motor: str = ""                 # p.ej. "tesseract" si capa_texto == ocr
    enlaces: int = 0                    # Hipervinculos emitidos en el Markdown
    lineas_suprimidas: List[str] = field(default_factory=list)
    avisos: List[str] = field(default_factory=list)


@dataclass
class Options:
    ocr_mode: Literal["off", "auto", "tesseract", "ocrmypdf"] = "off"
    ocr_lang: str = "eng"
    preview_only: bool = False
    preview_pages: int = 3
    caps_to_headings: bool = True
    defragment_short: bool = True
    heading_size_ratio: float = 1.15
    orphan_max_len: int = 45
    remove_headers_footers: bool = True
    drop_page_numbers: bool = True
    insert_page_breaks: bool = False
    export_images: bool = False
    detect_tables: bool = True
    convert_equations: bool = True
    include_frontmatter: bool = True
    detect_code_blocks: bool = True
    normalize_lists: bool = True

    # Notas al pie, trazabilidad y citabilidad
    detect_footnotes: bool = True
    page_markers: bool = True       # Emite <!-- p.N --> al inicio de cada pagina
    mark_cover_end: bool = True     # Emite <!-- fin-de-portada --> si lo detecta con certeza
    compute_hash: bool = True       # SHA-256 del PDF en el frontmatter
    url_origen: str = ""            # Si se deja vacio se intenta leer del propio fichero
    fecha_captura: str = ""         # Si se deja vacio se usa la fecha del PDF en disco

    # Enlaces: forman parte del contenido del documento, no son adorno.
    extract_links: bool = True          # Emite las anotaciones /Link como enlace Markdown
    # Diagnostico de la capa de texto
    detect_text_layer: bool = True      # Calcula chars/pagina y clasifica la capa de texto
    warn_no_text_layer: bool = True     # Inserta un aviso visible en el cuerpo del MD
    min_chars_por_pagina: int = 800     # Por debajo se marca capa_texto: ausente
    detect_language: bool = True        # Idioma del cuerpo para el frontmatter
    join_hyphens: bool = True           # Une palabras partidas por guion de fin de linea

    # Control de exportacion de imagenes
    dedupe_images: bool = True          # Reutiliza el mismo fichero para imagenes identicas
    min_image_side: int = 50            # Descarta iconos/artefactos por debajo de este lado en px
    min_image_bytes: int = 1500         # Descarta imagenes triviales por peso
    max_image_aspect_ratio: float = 6.0 # Descarta barras, separadores y lineas alargadas
    min_image_area_points: float = 400.0 # Descarta elementos con area en pagina menor a 20x20 pt

    # Control de frontmatter
    include_suppressed_lines: bool = False  # Si es False, no vuelca las lineas suprimidas en el YAML

# -*- coding: utf-8 -*-
"""
pdfmd.render — Renderizado de Markdown estructurado con YAML Frontmatter, bloques de código, tablas y assets.
"""

import hashlib
import os
import re
from typing import Callable, Dict, List, Optional
from pdfmd.models import Block, DocumentMetadata, Footnote, Line, Options, PageText, Span
from pdfmd.utils import process_image_bytes

_RE_LEADING_HASH = re.compile(r"^(#{1,6})(\s)")


_AVISO = "AVISO DE CONVERSION"
AVISO_SIN_CAPA_TEXTO = f"""> **{_AVISO}:** este PDF no tiene capa de texto utilizable. El contenido
> siguiente puede estar incompleto o ser incorrecto. Verificar contra el original."""


def render_span(span: Span) -> str:
    txt = span.text
    if not txt:
        return ""
    if txt.strip().startswith("$") and txt.strip().endswith("$"):
        return txt

    is_b = span.bold
    is_i = span.italic
    is_m = span.monospace

    cleaned = txt.strip()
    if not cleaned:
        return txt

    # Se conserva la sangria real, no un unico espacio: la anidacion de una
    # lista se codifica precisamente en cuantos espacios lleva delante, y
    # colapsarlos convertiria una sublista en un item del mismo nivel.
    leading_space = txt[:len(txt) - len(txt.lstrip())]
    trailing_space = txt[len(txt.rstrip()):]

    if is_m:
        cleaned = f"`{cleaned}`"
    if is_b and is_i:
        cleaned = f"***{cleaned}***"
    elif is_b:
        cleaned = f"**{cleaned}**"
    elif is_i:
        cleaned = f"*{cleaned}*"

    return f"{leading_space}{cleaned}{trailing_space}"


def _url_markdown(url: str) -> str:
    """Envuelve entre <> las URLs con parentesis o espacios, que romperian el enlace."""
    if any(c in url for c in "() <>"):
        return "<" + url.replace(">", "%3E") + ">"
    return url


def render_link(texto: str, url: str) -> str:
    """
    Emite un enlace Markdown a partir del texto anclado y su destino.

    Si el ancla no sirve como texto visible (esta vacia o es la propia URL) se
    emite la URL suelta: perder el ancla es aceptable, perder la URL no.
    """
    visible = texto.strip()
    if not visible or visible.rstrip("/") == url.rstrip("/"):
        return f"<{url}>"
    # Los corchetes del ancla cerrarian el enlace antes de tiempo
    visible = visible.replace("[", r"\[").replace("]", r"\]")
    return f"[{visible}]({_url_markdown(url)})"


def render_line(line: Line) -> str:
    """
    Renderiza una linea agrupando los spans consecutivos que comparten destino,
    para que un enlace repartido en varios spans salga como un unico ancla.
    """
    partes: List[str] = []
    grupo: List[Span] = []
    url_actual = ""

    def _volcar():
        if not grupo:
            return
        render = "".join(render_span(sp) for sp in grupo)
        if url_actual:
            izq = " " if render.startswith(" ") else ""
            der = " " if render.endswith(" ") else ""
            partes.append(f"{izq}{render_link(render, url_actual)}{der}")
        else:
            partes.append(render)

    for sp in line.spans:
        url = getattr(sp, "link", "") or ""
        if url != url_actual:
            _volcar()
            grupo = []
            url_actual = url
        grupo.append(sp)
    _volcar()
    return "".join(partes)


def escape_markdown_line(text: str) -> str:
    """Neutraliza caracteres que convertirían una línea de texto en otra construcción."""
    return _RE_LEADING_HASH.sub(r"\\\1\2", text)


def render_table(table_data: List[List[str]]) -> str:
    if not table_data:
        return ""
    cols_count = max(len(r) for r in table_data)
    if cols_count == 0:
        return ""

    # Normalizar columnas y calcular ancho máximo para alineación visual
    norm_rows = []
    col_widths = [4] * cols_count

    for r in table_data:
        norm_r = [c.replace("\n", " ").replace("|", "\\|").strip() for c in r]
        if len(norm_r) < cols_count:
            norm_r.extend([""] * (cols_count - len(norm_r)))
        norm_rows.append(norm_r)
        for i, val in enumerate(norm_r):
            col_widths[i] = max(col_widths[i], len(val))

    header = norm_rows[0]
    body = norm_rows[1:]

    lines = []
    # Fila cabecera
    h_padded = [header[i].ljust(col_widths[i]) for i in range(cols_count)]
    lines.append("| " + " | ".join(h_padded) + " |")

    # Fila separadora
    sep = ["-" * max(3, col_widths[i]) for i in range(cols_count)]
    lines.append("| " + " | ".join(sep) + " |")

    # Filas de datos
    for row in body:
        r_padded = [row[i].ljust(col_widths[i]) for i in range(cols_count)]
        lines.append("| " + " | ".join(r_padded) + " |")

    return "\n".join(lines)


def yaml_quote(value: str) -> str:
    """Escapa un valor para que no rompa el bloque YAML del frontmatter."""
    cleaned = " ".join(str(value).split())
    cleaned = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{cleaned}"'


def yaml_lista(valores: List[str]) -> str:
    """Serializa una lista de cadenas en formato YAML de una linea."""
    if not valores:
        return "[]"
    return "[" + ", ".join(yaml_quote(v) for v in valores) + "]"


def generate_yaml_frontmatter(metadata: Optional[DocumentMetadata], input_pdf_path: str,
                               options: Optional[Options] = None) -> str:
    """Genera el bloque YAML de metadatos para Obsidian/Jekyll/Pandoc."""
    if not metadata:
        return ""

    source_name = metadata.filename or os.path.basename(input_pdf_path)
    title = metadata.title or os.path.splitext(source_name)[0]

    lines = ["---", f"title: {yaml_quote(title)}"]
    if metadata.author:
        lines.append(f"author: {yaml_quote(metadata.author)}")
    if metadata.subject:
        lines.append(f"subject: {yaml_quote(metadata.subject)}")
    if metadata.keywords:
        lines.append(f"keywords: {yaml_quote(metadata.keywords)}")
    if metadata.creation_date:
        lines.append(f"creation_date: {yaml_quote(metadata.creation_date)}")
    if metadata.page_count:
        lines.append(f"pages: {metadata.page_count}")
    lines.append(f"source_file: {yaml_quote(source_name)}")
    # Cadena de procedencia: permite verificar dentro de anos que el .md y el PDF
    # siguen siendo el mismo documento.
    if metadata.url_origen:
        lines.append(f"url_origen: {yaml_quote(metadata.url_origen)}")
    if metadata.fecha_captura:
        lines.append(f"fecha_captura: {yaml_quote(metadata.fecha_captura)}")
    if metadata.fecha_conversion:
        lines.append(f"fecha_conversion: {yaml_quote(metadata.fecha_conversion)}")
    if metadata.sha256_pdf:
        lines.append(f"sha256_pdf: {yaml_quote(metadata.sha256_pdf)}")
    # La URL nunca se inventa: si no consta, consta que no consta.
    if not metadata.url_origen:
        lines.append("url_origen: null")
    if metadata.idioma:
        lines.append(f"idioma: {yaml_quote(metadata.idioma)}")

    # Diagnostico de la conversion: permite decidir de que ficheros fiarse sin
    # abrir el PDF, y auditar despues que se suprimio del cuerpo.
    lines.append(f"chars_extraidos: {metadata.chars_extraidos}")
    lines.append(f"chars_por_pagina: {metadata.chars_por_pagina}")
    lines.append(f"capa_texto: {yaml_quote(metadata.capa_texto or 'nativa')}")
    lines.append(f"ocr_motor: {yaml_quote(metadata.ocr_motor)}" if metadata.ocr_motor
                 else "ocr_motor: null")
    lines.append(f"enlaces: {metadata.enlaces}")
    incluir_suprimidas = options.include_suppressed_lines if options else False
    if incluir_suprimidas and metadata.lineas_suprimidas:
        lines.append(f"lineas_suprimidas: {yaml_lista(metadata.lineas_suprimidas)}")
    if metadata.avisos:
        lines.append(f"avisos: {yaml_lista(metadata.avisos)}")
    lines.append('generator: "KIWI (pdfmd)"')
    lines.append("---\n")
    return "\n".join(lines)


def _export_image(block: Block, options: Options, assets_abs_dir: str, assets_rel_dir: str,
                  cache: Dict[str, str], counter: int,
                  log_cb: Optional[Callable[[str], None]] = None):
    """
    Guarda la imagen de un bloque en la carpeta de assets y devuelve (ruta_relativa, es_nueva).
    Las imágenes repetidas (logotipos, marcas de agua) reutilizan el mismo fichero.
    """
    digest = hashlib.sha1(block.image_bytes).hexdigest()
    if options.dedupe_images and digest in cache:
        return cache[digest], False

    try:
        raw_bytes, ext = process_image_bytes(block.image_bytes, block.image_ext)
        img_filename = f"img_p{block.page_num}_{counter}.{ext}"
        os.makedirs(assets_abs_dir, exist_ok=True)
        with open(os.path.join(assets_abs_dir, img_filename), "wb") as f_img:
            f_img.write(raw_bytes)
    except OSError as ex:
        if log_cb:
            log_cb(f"No se pudo guardar una imagen de la página {block.page_num}: {ex}")
        return None, False

    rel_path = f"{assets_rel_dir}/{img_filename}"
    cache[digest] = rel_path
    return rel_path, True


def render_footnotes(footnotes: List[Footnote]) -> str:
    """
    Emite las definiciones de notas al pie al final del documento.

    Las ancladas usan la sintaxis nativa `[^N]: texto`, que Obsidian, Pandoc y
    GitHub renderizan al pie con enlaces en ambos sentidos. Las que no se pudieron
    enlazar van a un apartado propio en texto plano: una definicion huerfana, sin
    su llamada en el cuerpo, puede no renderizarse y desapareceria en silencio.
    """
    if not footnotes:
        return ""

    partes = []
    ancladas = [n for n in footnotes if n.anclada]
    sueltas = [n for n in footnotes if not n.anclada]

    if ancladas:
        partes.append("\n".join(f"[^{n.numero}]: {n.texto}" for n in ancladas))

    if sueltas:
        bloque = ["## Notas sin ancla", "",
                  "*Notas recogidas del pie del PDF cuya llamada no pudo localizarse en el texto.*", ""]
        bloque.extend(f"{n.numero}. {n.texto} *(pág. {n.page_num})*" for n in sueltas)
        partes.append("\n".join(bloque))

    return "\n\n".join(partes)


def render_document(
    pages: List[PageText],
    output_md_path: str,
    options: Options,
    metadata: Optional[DocumentMetadata] = None,
    log_cb: Optional[Callable[[str], None]] = None
) -> str:
    """Renderiza todas las páginas a un string Markdown estructurado."""
    out_dir = os.path.dirname(os.path.abspath(output_md_path))
    base_name = os.path.splitext(os.path.basename(output_md_path))[0]
    assets_rel_dir = f"{base_name}_assets"
    assets_abs_dir = os.path.join(out_dir, assets_rel_dir)

    md_sections = []
    # Un fallo de capa de texto tiene que ser imposible de pasar por alto: el
    # frontmatter se lee si se busca, esto se ve al abrir el fichero.
    if (options.warn_no_text_layer and metadata is not None
            and metadata.capa_texto == "ausente"):
        md_sections.append(AVISO_SIN_CAPA_TEXTO)

    image_counter = 1      # Índice del siguiente fichero nuevo en _assets
    image_shown = 0        # Imágenes insertadas en el documento (incluye repeticiones)
    image_cache: Dict[str, str] = {}
    reused_images = 0

    # 1. Bloques por página
    for page in pages:
        page_md_blocks = []

        # Marca de corte entre los créditos y el cuerpo del documento
        if options.mark_cover_end and page.fin_de_portada:
            page_md_blocks.append("<!-- fin-de-portada -->")

        # Ancla de citación: el número real de página del PDF, en comentario HTML
        # para que no ensucie el render. Se emite aunque la página venga vacía,
        # o el índice dejaría de corresponderse con el original.
        if options.page_markers:
            page_md_blocks.append(f"<!-- p.{page.page_num} -->")

        for b in page.blocks:
            if b.block_type == "heading":
                prefix = "#" * max(1, min(6, b.heading_level or 1))
                text = b.text().strip()
                page_md_blocks.append(f"{prefix} {text}")

            elif b.block_type == "table":
                t_str = render_table(b.table_data or [])
                if t_str:
                    page_md_blocks.append(t_str)

            elif b.block_type == "code":
                code_lines = [l.text() for l in b.lines]
                code_content = "\n".join(code_lines)
                page_md_blocks.append(f"```{b.code_lang}\n{code_content}\n```")

            elif b.block_type == "quote":
                quote_lines = [f"> {l.text().lstrip('> ').strip()}" for l in b.lines]
                page_md_blocks.append("\n".join(quote_lines))

            elif b.block_type == "image":
                if options.export_images and b.image_bytes:
                    rel_path, is_new = _export_image(b, options, assets_abs_dir, assets_rel_dir,
                                                     image_cache, image_counter, log_cb)
                    if rel_path:
                        image_shown += 1
                        page_md_blocks.append(f"![Imagen {image_shown} (pág. {b.page_num})]({rel_path})")
                        if is_new:
                            image_counter += 1
                        else:
                            reused_images += 1

            else:  # Texto normal o listas
                block_lines_md = [escape_markdown_line(render_line(l)) for l in b.lines]
                if block_lines_md:
                    page_md_blocks.append("\n".join(block_lines_md))

        page_content = "\n\n".join(page_md_blocks).strip()
        if page_content:
            md_sections.append(page_content)

    if reused_images and log_cb:
        log_cb(f"Imágenes repetidas reutilizadas sin duplicar fichero: {reused_images}")

    separator = "\n\n---\n\n" if options.insert_page_breaks else "\n\n"
    final_body = separator.join(md_sections)

    # 2. Todas las notas al pie del documento, al final y numeradas de corrido
    bloque_notas = render_footnotes([n for page in pages for n in page.footnotes])
    if bloque_notas:
        final_body = f"{final_body}\n\n---\n\n{bloque_notas}"

    if options.include_frontmatter:
        fm = generate_yaml_frontmatter(metadata, output_md_path, options=options)
        if fm:
            return f"{fm}\n\n{final_body}\n"

    return final_body + "\n"

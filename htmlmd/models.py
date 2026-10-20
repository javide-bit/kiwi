# -*- coding: utf-8 -*-
"""
htmlmd.models — Metadatos y opciones propias de la conversion de web a Markdown.

Los modelos de contenido (Span, Line, Block, PageText) se reutilizan tal cual de
`pdfmd.models`: son neutros respecto del origen y eso es justo lo que permite que
`pdfmd.render.render_document` sirva sin tocarlo.

Lo que si es distinto son los metadatos. `DocumentMetadata` habla de paginas, de
OCR y de `sha256_pdf`; una pagina web no tiene nada de eso y en cambio tiene URL
final tras redirecciones, codigo HTTP y fecha de publicacion. Forzar una en la
otra produciria un frontmatter que miente.
"""

from dataclasses import dataclass, field
from typing import List

from pdfmd.models import Options


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class WebMetadata:
    """Procedencia y diagnostico de una pagina convertida."""

    titulo: str = ""
    autor: str = ""
    descripcion: str = ""
    keywords: str = ""
    sitio: str = ""                     # Dominio, p.ej. "elpais.com"
    idioma: str = ""                    # De <html lang> o detectado del cuerpo
    seccion: str = ""                   # Sección dentro de una lista estructurada (p.ej. "Recetas")

    # Procedencia: permite comprobar que el .md corresponde a lo que habia
    # en esa URL ese dia.
    url_solicitada: str = ""
    url_final: str = ""                 # Tras redirecciones: puede no ser la pedida
    estado_http: int = 0
    content_type: str = ""
    sha256_html: str = ""               # Huella del HTML crudo recibido
    fecha_publicacion: str = ""         # De metadatos del propio documento
    fecha_captura: str = ""             # Cuando se descargo (vacia si es un fichero local)
    fecha_archivo: str = ""             # mtime del .html local
    ruta_local: str = ""                # Ruta del .html local convertido
    fecha_conversion: str = ""

    # Diagnostico. El equivalente web de `capa_texto`: aqui el modo de fallo no
    # es el PDF escaneado sino la pagina que solo pinta con JavaScript, el muro
    # de pago o el aviso de cookies servido en lugar del articulo.
    chars_extraidos: int = 0
    bloques: int = 0
    enlaces: int = 0
    imagenes: int = 0
    contenido: str = "completo"         # completo | escaso | ausente
    criterio_cuerpo: str = ""           # article | main | role=main | densidad | ...
    motivo_escaso: str = ""             # cookie wall | login/paywall | JS | bloqueo anti-bot
    alternativa_local: str = ""         # Ruta a .html local alternativo si la descarga salio escasa
    elementos_suprimidos: List[str] = field(default_factory=list)
    avisos: List[str] = field(default_factory=list)


@dataclass
class WebOptions:
    """Opciones de conversion. Las de render se derivan en `opciones_render`."""

    # Estructura
    incluir_frontmatter: bool = True
    clean_mode: bool = False            # Modo visualmente limpio (frontmatter simple, sin avisos de diagnóstico)
    detectar_tablas: bool = True
    normalizar_listas: bool = True
    detectar_codigo: bool = True
    conservar_enlaces: bool = True      # Los enlaces son contenido, no adorno
    enlaces_absolutos: bool = True      # Resuelve href relativos contra la URL base
    detectar_idioma: bool = True
    seccion: str = ""                   # Sección por defecto para frontmatter

    # Imagenes
    exportar_imagenes: bool = False     # Descarga a <nombre>_assets/
    dedupe_imagenes: bool = True
    min_bytes_imagen: int = 1024        # Descarta pixeles de seguimiento e iconos
    conservar_alt: bool = True

    # Limpieza
    quitar_cromo: bool = True           # Poda nav/footer/cookies/compartir...
    solo_cuerpo: bool = True            # Se queda con <article>/<main>/mejor candidato

    # Obsidian
    obsidian_callouts: bool = False     # Renderiza notas y advertencias como callouts de Obsidian (> [!NOTE])
    obsidian_wikilinks: bool = False    # Convierte enlaces relativos e internos en wikilinks de Obsidian ([[doc]])

    # Diagnostico
    avisar_contenido_escaso: bool = True
    min_chars_contenido: int = 500      # Por debajo -> "escaso" + aviso visible
    proteger_salida_existente: bool = True  # No sobrescribir una salida previa mas larga con una captura escasa

    # Red
    timeout: float = 15.0
    reintentos: int = 2                 # Reintentos ante timeouts o errores transitorios (429, 5xx)
    espera_reintento: float = 2.0       # Espera base en segundos entre reintentos
    user_agent: str = BROWSER_USER_AGENT
    verificar_tls: bool = True
    max_bytes: int = 20 * 1024 * 1024   # Corta descargas desbocadas

    def opciones_render(self) -> Options:
        """
        Traduce a las `Options` que espera `pdfmd.render.render_document`.

        Se apagan explicitamente las que no significan nada fuera de un PDF:
        marcadores de pagina, fin de portada, saltos de pagina y el aviso de
        capa de texto (htmlmd emite el suyo, que habla de JavaScript, no de OCR).
        """
        return Options(
            include_frontmatter=False,   # htmlmd escribe su propio frontmatter
            clean_mode=self.clean_mode,
            obsidian_callouts=self.obsidian_callouts,
            obsidian_wikilinks=self.obsidian_wikilinks,
            page_markers=False,
            mark_cover_end=False,
            insert_page_breaks=False,
            warn_no_text_layer=False,
            detect_footnotes=False,
            export_images=False,         # htmlmd gestiona sus imagenes (conserva el alt)
            detect_tables=self.detectar_tablas,
            normalize_lists=self.normalizar_listas,
            detect_code_blocks=self.detectar_codigo,
            extract_links=self.conservar_enlaces,
        )

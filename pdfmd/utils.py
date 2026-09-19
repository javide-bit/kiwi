# -*- coding: utf-8 -*-
"""
pdfmd.utils — Funciones transversales de utilidad para detección de binarios e imágenes.
"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import unicodedata
from datetime import datetime
from typing import Optional, Tuple

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def find_tesseract_binary() -> Optional[str]:
    """Busca el ejecutable tesseract en el PATH o en ubicaciones estándar de Windows."""
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
    return None


def is_tesseract_available() -> bool:
    return find_tesseract_binary() is not None


def find_ocrmypdf_binary() -> Optional[str]:
    return shutil.which("ocrmypdf")


def is_ocrmypdf_available() -> bool:
    return find_ocrmypdf_binary() is not None


def process_image_bytes(image_bytes: bytes, ext: str = "png") -> Tuple[bytes, str]:
    """
    Normaliza los bytes de una imagen (ej. convierte CMYK a RGB) para evitar
    fallos de formato al guardar como PNG/JPG en el directorio _assets.
    """
    if not HAS_PIL or not image_bytes:
        return image_bytes, ext
    try:
        bio = io.BytesIO(image_bytes)
        img = Image.open(bio)
        if img.mode in ("CMYK", "P", "RGBA", "LA") and ext.lower() in ("jpg", "jpeg"):
            img = img.convert("RGB")
        elif img.mode == "CMYK":
            img = img.convert("RGB")
        out_bio = io.BytesIO()
        save_format = "PNG" if ext.lower() in ("png", "") else "JPEG"
        img.save(out_bio, format=save_format)
        return out_bio.getvalue(), save_format.lower()
    except Exception:
        return image_bytes, ext


def slugify(texto: str, por_defecto: str = "documento", max_len: int = 80) -> str:
    """
    Normaliza un nombre a ASCII apto para cualquier sistema de ficheros:
    quita acentos y apostrofos, colapsa espacios y los sustituye por guiones.

        "Informe Anual: Situación 2026's"  ->  "informe-anual-situacion-2026s"
    """
    if not texto:
        return por_defecto

    # Descomponer y eliminar los diacriticos (tildes, dieresis, cedillas...)
    plano = unicodedata.normalize("NFKD", texto)
    plano = "".join(c for c in plano if not unicodedata.combining(c))

    # Los apostrofos y comillas desaparecen sin dejar hueco: "2026's" -> "2026s"
    plano = re.sub(r"['\u2018\u2019\u02bc\"\u201c\u201d`]", "", plano)

    # Cualquier otro caracter no admitido pasa a separador
    plano = re.sub(r"[^A-Za-z0-9]+", " ", plano)

    plano = re.sub(r"\s+", " ", plano).strip().lower().replace(" ", "-")
    plano = re.sub(r"-{2,}", "-", plano).strip("-")

    if len(plano) > max_len:
        plano = plano[:max_len].rstrip("-")
    return plano or por_defecto


def sha256_fichero(ruta: str, bloque: int = 1024 * 1024) -> str:
    """Huella SHA-256 del fichero: garantiza que el .md y el PDF siguen siendo el mismo par."""
    h = hashlib.sha256()
    try:
        with open(ruta, "rb") as f:
            for trozo in iter(lambda: f.read(bloque), b""):
                h.update(trozo)
    except OSError:
        return ""
    return h.hexdigest()


def leer_url_origen(ruta: str) -> str:
    """
    Recupera la URL de descarga que Windows guarda en el flujo alternativo
    Zone.Identifier. Devuelve cadena vacia si el fichero no la trae.
    """
    if os.name != "nt":
        return ""
    try:
        with open(f"{ruta}:Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            contenido = f.read()
    except OSError:
        return ""

    # HostUrl es la descarga directa; ReferrerUrl, la pagina desde la que se pidio
    for clave in ("HostUrl=", "ReferrerUrl="):
        for linea in contenido.splitlines():
            if linea.startswith(clave):
                url = linea[len(clave):].strip()
                if url.startswith(("http://", "https://")):
                    return url
    return ""


def fecha_de_fichero(ruta: str) -> str:
    """Fecha de modificacion del fichero en formato ISO (aproxima cuando se descargo)."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%Y-%m-%d")
    except OSError:
        return ""


# Ligaduras tipograficas: los PDFs bien maquetados codifican "fi" como un unico
# caracter. Sin deshacerlas, buscar "afirmacion" en el Markdown no encuentra nada.
LIGADURAS = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}

# Espacios y guiones exoticos que conviene reducir a su equivalente ASCII
_TRADUCCION = {
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "­": "",          # Espacio de ancho cero y guion blando
    "‑": "-",                        # Guion de no separacion
}
_TRADUCCION.update(LIGADURAS)
_TABLA_NORMALIZACION = str.maketrans(_TRADUCCION)


def normalizar_texto_pdf(texto: str) -> str:
    """
    Deshace ligaduras y espacios atipicos para que el Markdown resultante sea
    buscable. Deliberadamente NO usa NFKC, que ademas convertiria los digitos
    volados en normales y haria irreconocibles las llamadas de nota al pie.
    """
    if not texto:
        return texto
    return texto.translate(_TABLA_NORMALIZACION)


# Palabras vacias muy frecuentes y poco ambiguas entre estas lenguas. No se
# pretende un identificador general: solo etiquetar el idioma del cuerpo en el
# frontmatter para poder filtrar el corpus.
_PALABRAS_POR_IDIOMA = {
    "es": {"de", "la", "que", "el", "en", "los", "del", "las", "por", "con",
           "para", "una", "como", "es", "se", "no", "su", "al", "lo", "mas"},
    "en": {"the", "of", "and", "to", "in", "that", "for", "was", "with", "as",
           "on", "by", "at", "from", "this", "which", "were", "has", "been", "not"},
    "fr": {"le", "de", "la", "les", "des", "et", "en", "un", "une", "dans",
           "pour", "que", "qui", "sur", "pas", "plus", "par", "au", "aux", "est"},
    "de": {"der", "die", "und", "den", "des", "das", "von", "mit", "dem", "ist",
           "im", "nicht", "auch", "eine", "einer", "auf", "sich", "als", "wurde", "durch"},
    "pt": {"de", "que", "nao", "para", "com", "uma", "dos", "das", "por", "mais",
           "como", "foi", "pelo", "pela", "sao", "seu", "sua", "isso", "mas", "ja"},
    "it": {"di", "che", "il", "la", "per", "non", "una", "con", "del", "della",
           "sono", "come", "piu", "anche", "nel", "alla", "gli", "questo", "essere", "stato"},
}


def detectar_idioma(texto: str, por_defecto: str = "") -> str:
    """
    Etiqueta ISO 639-1 aproximada del texto, por recuento de palabras vacias.

    Devuelve `por_defecto` si la muestra es corta o ninguna lengua destaca:
    preferimos no poner idioma a poner uno inventado.
    """
    if not texto:
        return por_defecto
    palabras = re.findall(r"[a-zA-Zaaeeiioouunncc]+", texto.lower())
    if len(palabras) < 40:
        return por_defecto

    muestra = palabras[:4000]
    puntuaciones = {
        idioma: sum(1 for p in muestra if p in vocabulario)
        for idioma, vocabulario in _PALABRAS_POR_IDIOMA.items()
    }
    mejor = max(puntuaciones, key=puntuaciones.get)
    if puntuaciones[mejor] < len(muestra) * 0.02:
        return por_defecto

    # "de", "la" y "que" son comunes a varias lenguas romances: si dos empatan
    # casi, no hay senal suficiente para decidir.
    segundo = sorted(puntuaciones.values())[-2]
    if segundo and puntuaciones[mejor] < segundo * 1.25:
        return por_defecto
    return mejor

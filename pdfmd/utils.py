# -*- coding: utf-8 -*-
"""
pdfmd.utils — Funciones transversales de utilidad para detección de binarios e imágenes.
"""

import glob
import hashlib
import io
import os
import re
import shutil
import subprocess
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional, Tuple

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
    found = shutil.which("ocrmypdf")
    if found:
        return found
    # También buscar junto al ejecutable de Python activo (.venv\Scripts\ocrmypdf.exe)
    import sys
    scripts_dir = os.path.dirname(sys.executable)
    for nombre in ("ocrmypdf.exe", "ocrmypdf"):
        cand = os.path.join(scripts_dir, nombre)
        if os.path.isfile(cand):
            return cand
    return None


def is_ocrmypdf_available() -> bool:
    return find_ocrmypdf_binary() is not None


def find_ghostscript_binary() -> Optional[str]:
    """ocrmypdf necesita Ghostscript (gswin64c en Windows) para escribir el PDF."""
    for nombre in ("gswin64c", "gswin32c", "gs"):
        found = shutil.which(nombre)
        if found:
            return found
    if os.name == "nt":
        for patron in (
            r"C:\Program Files\gs\gs*\bin\gswin64c.exe",
            r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe",
        ):
            coincidencias = sorted(glob.glob(patron), reverse=True)
            if coincidencias:
                return coincidencias[0]
    return None


def _evaluar_tessdata_dir(ruta: str) -> Tuple[int, int, int, int]:
    """
    Evalúa la idoneidad de una carpeta tessdata devolviendo:
    (tiene_hocr_y_idiomas, tiene_eng_y_spa, num_traineddata, tiene_hocr)
    """
    if not ruta or not os.path.isdir(ruta):
        return (0, 0, 0, 0)
    try:
        ficheros = os.listdir(ruta)
    except OSError:
        return (0, 0, 0, 0)
    trained = {f[:-12].lower() for f in ficheros if f.lower().endswith(".traineddata")}
    if not trained:
        return (0, 0, 0, 0)
    tiene_hocr = int(os.path.isfile(os.path.join(ruta, "configs", "hocr")))
    tiene_eng_spa = int("eng" in trained and "spa" in trained)
    tiene_hocr_y_multi = int(tiene_hocr and len(trained) >= 2)
    return (tiene_hocr_y_multi, tiene_eng_spa, len(trained), tiene_hocr)


def buscar_mejor_tessdata() -> Optional[str]:
    """
    Localiza la carpeta tessdata más completa disponible en el sistema, incluyendo
    carpetas virtualizadas de paquetes MSIX en Windows (%LOCALAPPDATA%\\Packages\\*\\LocalCache\\Local\\tessdata).
    """
    candidatos: List[str] = []
    actual = os.environ.get("TESSDATA_PREFIX", "").strip()
    if actual:
        candidatos.append(actual)
        # A veces TESSDATA_PREFIX apunta al padre de tessdata
        sub = os.path.join(actual, "tessdata")
        if os.path.isdir(sub):
            candidatos.append(sub)

    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidatos.append(os.path.join(local_app, "tessdata"))
            # Virtualización MSIX (ej. cuando se descargaron traineddata desde un terminal empaquetado)
            patron_msix = os.path.join(local_app, "Packages", "*", "LocalCache", "Local", "tessdata")
            candidatos.extend(glob.glob(patron_msix))
        candidatos.extend([
            r"C:\Program Files\Tesseract-OCR\tessdata",
            r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
        ])

    tess_bin = find_tesseract_binary()
    if tess_bin:
        candidatos.append(os.path.join(os.path.dirname(tess_bin), "tessdata"))

    mejor_ruta = None
    mejor_punt = (0, 0, 0, 0)
    vistos = set()
    for c in candidatos:
        if not c:
            continue
        norm = os.path.normcase(os.path.abspath(c))
        if norm in vistos:
            continue
        vistos.add(norm)
        punt = _evaluar_tessdata_dir(c)
        if punt > mejor_punt:
            mejor_punt = punt
            mejor_ruta = c

    return mejor_ruta


def preparar_entorno_ocr() -> None:
    """
    Si TESSDATA_PREFIX no llega al proceso o apunta a una carpeta vacía/incompleta
    (por ejemplo, tras virtualización MSIX en Windows), selecciona la carpeta
    tessdata más completa en memoria del proceso y asegura que Tesseract y
    Ghostscript estén en el PATH del proceso para que los hereden pytesseract y ocrmypdf.
    """
    global _IDIOMAS_CACHE
    # 1. Asegurar que tesseract y ghostscript estén en el PATH del proceso actual
    dirs_extra = []
    for buscador in (find_tesseract_binary, find_ghostscript_binary, find_ocrmypdf_binary):
        bin_path = buscador()
        if bin_path:
            d = os.path.dirname(bin_path)
            if d and os.path.isdir(d):
                dirs_extra.append(d)
    if dirs_extra:
        path_actual = os.environ.get("PATH", "")
        partes_norm = {os.path.normcase(os.path.abspath(p)) for p in path_actual.split(os.pathsep) if p.strip()}
        nuevos = [d for d in dirs_extra if os.path.normcase(os.path.abspath(d)) not in partes_norm]
        if nuevos:
            os.environ["PATH"] = os.pathsep.join(nuevos + ([path_actual] if path_actual else []))

    # 2. Verificar TESSDATA_PREFIX actual y actualizarlo en memoria del proceso si hay uno mejor
    actual = os.environ.get("TESSDATA_PREFIX", "").strip()
    punt_actual = _evaluar_tessdata_dir(actual)
    mejor = buscar_mejor_tessdata()
    if mejor:
        punt_mejor = _evaluar_tessdata_dir(mejor)
        if punt_mejor > punt_actual:
            os.environ["TESSDATA_PREFIX"] = mejor
            _IDIOMAS_CACHE = None


_IDIOMAS_CACHE: Optional[set] = None
_VERSION_OCR_CACHE: Dict[str, str] = {}


def idiomas_tesseract() -> set:
    """Idiomas instalados en tessdata (vacio si no hay Tesseract)."""
    global _IDIOMAS_CACHE
    if _IDIOMAS_CACHE is not None:
        return _IDIOMAS_CACHE
    preparar_entorno_ocr()
    binario = find_tesseract_binary()
    idiomas = set()
    if binario:
        try:
            proc = subprocess.run([binario, "--list-langs"], capture_output=True, timeout=20,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            salida = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
            idiomas = {l.strip() for l in salida[1:] if l.strip() and not l.strip().startswith("List of")}
        except (OSError, subprocess.SubprocessError):
            pass
    # Fallback directo sobre ficheros .traineddata si tesseract --list-langs no devolvio lista
    if not idiomas:
        td = os.environ.get("TESSDATA_PREFIX", "")
        if td and os.path.isdir(td):
            try:
                idiomas = {
                    f[:-12] for f in os.listdir(td)
                    if f.lower().endswith(".traineddata") and f[:-12].lower() != "osd"
                }
            except OSError:
                pass
    _IDIOMAS_CACHE = idiomas
    return idiomas


def resolver_idioma_ocr(lang: str) -> str:
    """
    Deja en "spa+eng" solo los idiomas instalados: un idioma ausente hace
    fallar a Tesseract entero y la pagina saldria vacia sin aviso.
    """
    disponibles = idiomas_tesseract()
    if not disponibles:
        return lang or "eng"
    pedidos = [p for p in (lang or "eng").split("+") if p]
    validos = [p for p in pedidos if p in disponibles]
    return "+".join(validos) or ("eng" if "eng" in disponibles else sorted(disponibles)[0])


# Mapa de prefijos de código de país/idioma en nombres de archivo
# y códigos ISO 639-1 a códigos de idioma de Tesseract (ISO 639-2/3).
_PREFIJO_A_TESS: Dict[str, str] = {
    "es": "spa", "spa": "spa",
    "en": "eng", "eng": "eng", "uk_gov": "eng", "us": "eng",
    "fr": "fra", "fra": "fra", "fre": "fra",
    "de": "deu", "deu": "deu", "ger": "deu", "at": "deu",
    "it": "ita", "ita": "ita",
    "nl": "nld", "nld": "nld", "dut": "nld",
    "pl": "pol", "pol": "pol",
    "pt": "por", "por": "por",
    "ru": "rus", "rus": "rus",
    "ua": "ukr", "uk": "ukr", "ukr": "ukr",
    "ca": "cat", "cat": "cat",
    "ro": "ron", "ron": "ron",
    "cs": "ces", "cz": "ces", "ces": "ces",
    "sk": "slk", "slk": "slk",
    "hu": "hun", "hun": "hun",
    "bg": "bul", "bul": "bul",
    "hr": "hrv", "hrv": "hrv",
    "et": "est", "ee": "est", "est": "est",
    "sv": "swe", "se": "swe", "swe": "swe",
}

_PALABRAS_CLAVE_NOMBRE_TESS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(?:^|[_\-\s])(disinformazione|elezioni|agcom|relazione)(?:$|[_\-\s])", re.I), "ita"),
    (re.compile(r"(?:^|[_\-\s])(verkiezingen|benedmo|desinformatie|jaarverslag)(?:$|[_\-\s])", re.I), "nld"),
    (re.compile(r"(?:^|[_\-\s])(desinformacion|ciberespacio|estrategia|seguridad|actuacion|injerencia|guia)(?:$|[_\-\s])", re.I), "spa"),
    (re.compile(r"(?:^|[_\-\s])(desinformation|menaces|rapport|securite)(?:$|[_\-\s])", re.I), "fra"),
    (re.compile(r"(?:^|[_\-\s])(dezinformacja|wybory|raport|bezpieczenstwa)(?:$|[_\-\s])", re.I), "pol"),
]


def inferir_idioma_ocr_documento(input_pdf: str, base_lang: str = "spa+eng",
                                 title: str = "", subject: str = "") -> str:
    """
    Elige el idioma de OCR óptimo para el documento combinando pistas del nombre de
    fichero (prefijos `es_`, `it_`, `nl_`, `pl_`, `fr_`, `de_`, `pt_`...) y metadatos
    con `eng`, y filtrando contra los idiomas realmente instalados en `tessdata`.
    """
    base = (base_lang or "spa+eng").strip()
    # Si el usuario fijó explícitamente un idioma distinto del predeterminado spa+eng/eng, respetarlo
    if base not in ("spa+eng", "eng+spa", "eng", "spa", "auto", ""):
        return resolver_idioma_ocr(base)

    nombre = os.path.splitext(os.path.basename(input_pdf or ""))[0].lower()
    partes = [p for p in re.split(r"[_\-\s\.]+", nombre) if p]

    detectados: List[str] = []
    # 1. Prefijo de país/idioma al inicio del nombre (ej. it_2018_..., nl_intl_..., es_gob_...)
    if partes:
        primero = partes[0]
        if primero in _PREFIJO_A_TESS and primero not in ("uk",):
            tess_lang = _PREFIJO_A_TESS[primero]
            if tess_lang not in detectados:
                detectados.append(tess_lang)

    # 2. Términos inequívocos en el nombre del archivo o título de metadatos
    texto_pistas = f"{nombre} {title or ''} {subject or ''}"
    for patron, tess_lang in _PALABRAS_CLAVE_NOMBRE_TESS:
        if patron.search(texto_pistas) and tess_lang not in detectados:
            detectados.append(tess_lang)

    # 3. Detector léxico sobre título + asunto si son suficientemente largos
    if not detectados and (title or subject):
        iso = detectar_idioma(f"{title} {subject}")
        if iso and iso in _PREFIJO_A_TESS:
            detectados.append(_PREFIJO_A_TESS[iso])

    # Construir lista final de idiomas priorizando el detectado + eng (y spa si estaba en base)
    candidatos: List[str] = []
    for d in detectados:
        if d not in candidatos:
            candidatos.append(d)
    for p in base.split("+"):
        p = p.strip()
        if p and p not in candidatos:
            # Si ya detectamos un idioma específico no español (ej. ita, nld, pol),
            # basta con combinarlo con 'eng' para no ralentizar ni confundir a Tesseract
            if detectados and detectados[0] not in ("spa", "eng") and p == "spa":
                continue
            candidatos.append(p)
    if "eng" not in candidatos:
        candidatos.append("eng")

    return resolver_idioma_ocr("+".join(candidatos))


def version_motor_ocr(motor: str) -> str:
    """
    Devuelve el nombre y versión del motor OCR empleado, p. ej.:
    'ocrmypdf 16.10.4 (tesseract 5.4.0)' o 'tesseract 5.4.0'.
    """
    motor_limpio = (motor or "").strip().lower()
    if not motor_limpio:
        return ""
    if motor_limpio in _VERSION_OCR_CACHE:
        return _VERSION_OCR_CACHE[motor_limpio]

    preparar_entorno_ocr()
    tess_ver = ""
    tess_bin = find_tesseract_binary()
    if tess_bin:
        try:
            proc = subprocess.run([tess_bin, "--version"], capture_output=True, timeout=10,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            salida = ((proc.stdout or b"") + b"\n" + (proc.stderr or b"")).decode("utf-8", "replace")
            m = re.search(r"tesseract\s+v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", salida, re.I)
            if m:
                tess_ver = m.group(1)
        except Exception:
            pass

    if "ocrmypdf" in motor_limpio:
        ocr_ver = ""
        try:
            import importlib.metadata
            ocr_ver = importlib.metadata.version("ocrmypdf")
        except Exception:
            ocr_bin = find_ocrmypdf_binary()
            if ocr_bin:
                try:
                    proc = subprocess.run([ocr_bin, "--version"], capture_output=True, timeout=10,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    salida = ((proc.stdout or b"") + b"\n" + (proc.stderr or b"")).decode("utf-8", "replace").strip()
                    m = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)", salida)
                    if m:
                        ocr_ver = m.group(1)
                except Exception:
                    pass
        etiqueta = f"ocrmypdf {ocr_ver}".strip() if ocr_ver else "ocrmypdf"
        if tess_ver:
            etiqueta = f"{etiqueta} (tesseract {tess_ver})"
        _VERSION_OCR_CACHE[motor_limpio] = etiqueta
        return etiqueta

    if "tesseract" in motor_limpio:
        etiqueta = f"tesseract {tess_ver}".strip() if tess_ver else "tesseract"
        _VERSION_OCR_CACHE[motor_limpio] = etiqueta
        return etiqueta

    return motor


def diagnosticar_entorno_ocr(idiomas=("spa", "eng", "fra")) -> Dict[str, object]:
    """
    Inspecciona el entorno de OCR (PyMuPDF, Tesseract, Ghostscript, ocrmypdf,
    TESSDATA_PREFIX y configs/hocr) y devuelve un diccionario detallado.
    """
    preparar_entorno_ocr()
    pymupdf_ver = ""
    try:
        import pymupdf
        pymupdf_ver = getattr(pymupdf, "__version__", "instalado")
    except ImportError:
        pymupdf_ver = ""

    tess_bin = find_tesseract_binary()
    gs_bin = find_ghostscript_binary()
    ocr_bin = find_ocrmypdf_binary()
    tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
    if not tessdata_dir and tess_bin:
        cand = os.path.join(os.path.dirname(tess_bin), "tessdata")
        if os.path.isdir(cand):
            tessdata_dir = cand

    has_hocr = bool(tessdata_dir and os.path.isfile(os.path.join(tessdata_dir, "configs", "hocr")))
    has_pdf_ttf = bool(tessdata_dir and os.path.isfile(os.path.join(tessdata_dir, "pdf.ttf")))
    disp = sorted(idiomas_tesseract())
    avisos = chequear_dependencias_ocr(idiomas=idiomas)

    return {
        "ok": len(avisos) == 0,
        "pymupdf_version": pymupdf_ver,
        "tesseract_bin": tess_bin or "",
        "tesseract_version": version_motor_ocr("tesseract") if tess_bin else "",
        "ghostscript_bin": gs_bin or "",
        "ocrmypdf_bin": ocr_bin or "",
        "ocrmypdf_version": version_motor_ocr("ocrmypdf") if ocr_bin else "",
        "tessdata_prefix": tessdata_dir,
        "has_hocr": has_hocr,
        "has_hocr_config": has_hocr,
        "hocr_config_ok": has_hocr,
        "has_pdf_ttf": has_pdf_ttf,
        "pdf_ttf_ok": has_pdf_ttf,
        "idiomas": disp,
        "langs": disp,
        "tessdata_langs": disp,
        "avisos": avisos,
    }


def chequear_dependencias_ocr(idiomas=("spa", "fra")) -> list:
    """Lista de avisos legibles sobre lo que falta para el OCR (vacia si todo esta)."""
    preparar_entorno_ocr()
    avisos = []
    try:
        import pymupdf as _fitz  # noqa: F401
    except ImportError:
        avisos.append("PyMuPDF (pymupdf) no está instalado: no se pueden abrir archivos PDF.")

    tess_bin = find_tesseract_binary()
    if not tess_bin:
        avisos.append("Tesseract no está instalado o no se encuentra en PATH: los PDF escaneados saldrán sin texto.")
    else:
        disponibles = idiomas_tesseract()
        faltan = [i for i in idiomas if i not in disponibles]
        tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
        if not tessdata_dir:
            cand = os.path.join(os.path.dirname(tess_bin), "tessdata")
            if os.path.isdir(cand):
                tessdata_dir = cand
        if faltan:
            avisos.append(
                f"Faltan idiomas de Tesseract ({', '.join(faltan)}) en tessdata "
                f"(TESSDATA_PREFIX={tessdata_dir or 'sin definir'})."
            )
        if tessdata_dir and os.path.isdir(tessdata_dir):
            hocr_path = os.path.join(tessdata_dir, "configs", "hocr")
            if not os.path.isfile(hocr_path):
                avisos.append(
                    f"Falta 'configs/hocr' en TESSDATA_PREFIX ({tessdata_dir}): "
                    "si se copiaron solo los .traineddata sin la carpeta 'configs' ni 'pdf.ttf' "
                    "de C:\\Program Files\\Tesseract-OCR\\tessdata, ocrmypdf fallará con "
                    "'read_params_file: Can't open hocr'."
                )
    if not is_ocrmypdf_available():
        avisos.append("ocrmypdf no está instalado: no habrá reintento con OCR de documento completo.")
    elif not find_ghostscript_binary():
        avisos.append("Ghostscript (gswin64c) no está en el PATH: ocrmypdf fallará.")
    return avisos


def fecha_pdf_iso(valor: str) -> str:
    """
    "D:20080210075913-05'00'" -> "2008-02-10T07:59:13-05:00". Devuelve el valor
    tal cual si no tiene el formato de fecha PDF.
    """
    m = re.match(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                 r"(Z|[+-]\d{2}'?\d{2}'?)?", (valor or "").strip())
    if not m:
        return (valor or "").strip()
    a, mes, dia, h, mi, s, tz = m.groups()
    fecha = f"{a}-{mes or '01'}-{dia or '01'}"
    if h is None:
        return fecha
    iso = f"{fecha}T{h}:{mi or '00'}:{s or '00'}"
    if tz == "Z":
        iso += "Z"
    elif tz:
        digitos = tz.replace("'", "")
        iso += f"{digitos[:3]}:{digitos[3:5] or '00'}"
    return iso


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
    # Si hay guión blando (\xad / U+00AD) al final de palabra o de línea, convertirlo en '-'
    # para que unir_palabras_partidas lo detecte y recomponga la palabra
    texto = re.sub(r'(\w)\xad(?=\s|$)', r'\1-', texto)
    # Limpiar caracteres de control no imprimibles (ej. \x07 BEL procedente de dingbats)
    texto = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', texto)
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

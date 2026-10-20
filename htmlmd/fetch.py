# -*- coding: utf-8 -*-
"""
htmlmd.fetch — Obtencion del HTML, desde una URL o desde un fichero en disco.

Solo biblioteca estandar (`urllib`). `requests` seria mas comodo, pero KIWI se
distribuye como ejecutable y la comodidad no compensa una dependencia mas en el
binario para lo que aqui son treinta lineas.

La descarga devuelve siempre los *bytes* crudos ademas del texto: el SHA-256 de
procedencia tiene que calcularse sobre lo que vino por el cable, no
sobre el resultado de nuestra decodificacion.
"""

import hashlib
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from htmlmd.models import WebOptions
from pdfmd.utils import leer_url_origen


@dataclass
class Respuesta:
    """Lo recibido de la red o del disco, antes de interpretarlo como HTML."""
    html: str = ""
    crudo: bytes = b""
    url_solicitada: str = ""
    url_final: str = ""
    estado_http: int = 0
    content_type: str = ""
    encoding: str = ""
    sha256: str = ""
    fecha_captura: str = ""
    ruta_local: str = ""         # Fichero .html de disco, si no vino de la red
    fecha_archivo: str = ""      # mtime del fichero local: no es la fecha de captura


class ErrorDeDescarga(Exception):
    """La pagina no se pudo obtener. El mensaje es apto para mostrar al usuario."""


_RE_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE
)


def es_url(cadena: str) -> bool:
    return cadena.lower().startswith(("http://", "https://"))


def _charset_de_content_type(content_type: str) -> str:
    for parte in content_type.split(";"):
        parte = parte.strip()
        if parte.lower().startswith("charset="):
            return parte.split("=", 1)[1].strip().strip('"\'')
    return ""


def decodificar(crudo: bytes, content_type: str = "") -> tuple:
    """
    Devuelve (texto, encoding_usado).

    Orden de preferencia: cabecera HTTP, <meta charset> del propio documento,
    UTF-8, y como ultimo recurso cp1252 —que nunca falla y es lo que de hecho
    sirven muchos sitios que dicen servir otra cosa—. Nunca se lanza excepcion:
    un acento mal decodificado es recuperable, un fallo duro deja al usuario sin
    documento.
    """
    candidatos = []

    de_cabecera = _charset_de_content_type(content_type)
    if de_cabecera:
        candidatos.append(de_cabecera)

    m = _RE_META_CHARSET.search(crudo[:4096])
    if m:
        try:
            candidatos.append(m.group(1).decode("ascii"))
        except UnicodeDecodeError:
            pass

    candidatos.extend(["utf-8", "cp1252"])

    for enc in candidatos:
        if not enc:
            continue
        try:
            return crudo.decode(enc), enc.lower()
        except (UnicodeDecodeError, LookupError):
            continue

    return crudo.decode("utf-8", errors="replace"), "utf-8/replace"


def _contexto_ssl(verificar: bool) -> Optional[ssl.SSLContext]:
    if verificar:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _es_error_transitorio(ex: Exception) -> bool:
    """True si el error es un timeout o un estado HTTP transitorio (429, 500, 502, 503, 504)."""
    if isinstance(ex, (TimeoutError, socket.timeout)):
        return True
    if isinstance(ex, urllib.error.HTTPError):
        return ex.code in (408, 429, 500, 502, 503, 504)
    if isinstance(ex, urllib.error.URLError):
        razon = str(getattr(ex, "reason", ex)).lower()
        if any(p in razon for p in ("timed out", "timeout", "temporarily", "reset by peer", "connection reset")):
            return True
    if isinstance(ex, OSError):
        msg = str(ex).lower()
        if "timed out" in msg or "timeout" in msg:
            return True
    return False


def descargar(url: str, options: Optional[WebOptions] = None) -> Respuesta:
    """Descarga una URL siguiendo redirecciones y reintentando en timeouts. Lanza ErrorDeDescarga si falla."""
    opts = options or WebOptions()
    intentos_max = 1 + max(0, int(getattr(opts, "reintentos", 2) or 0))
    espera_base = max(0.0, float(getattr(opts, "espera_reintento", 2.0) or 0.0))

    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": opts.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
        },
    )

    captura = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ultimo_error: Optional[Exception] = None

    for intento in range(1, intentos_max + 1):
        try:
            with urllib.request.urlopen(peticion, timeout=opts.timeout,
                                        context=_contexto_ssl(opts.verificar_tls)) as r:
                # El limite es una salvaguarda, no una optimizacion: sin el, una URL
                # que sirve un flujo infinito colgaria la conversion por lotes entera.
                crudo = r.read(opts.max_bytes + 1)
                if len(crudo) > opts.max_bytes:
                    crudo = crudo[:opts.max_bytes]
                content_type = r.headers.get("Content-Type", "")
                estado = getattr(r, "status", 0) or r.getcode() or 0
                url_final = r.geturl()
            break
        except Exception as ex:
            ultimo_error = ex
            if intento < intentos_max and _es_error_transitorio(ex):
                if espera_base > 0:
                    time.sleep(espera_base * intento)
                continue
            if isinstance(ex, urllib.error.HTTPError):
                raise ErrorDeDescarga(f"El servidor respondio {ex.code} ({ex.reason}) para {url}") from ex
            if isinstance(ex, urllib.error.URLError):
                raise ErrorDeDescarga(f"No se pudo conectar con {url}: {ex.reason}") from ex
            if isinstance(ex, (ssl.SSLError, OSError, ValueError)):
                raise ErrorDeDescarga(f"Fallo de red al pedir {url}: {ex}") from ex
            raise
    else:
        raise ErrorDeDescarga(f"Fallo de red tras {intentos_max} intentos para {url}: {ultimo_error}")

    texto, encoding = decodificar(crudo, content_type)
    return Respuesta(
        html=texto,
        crudo=crudo,
        url_solicitada=url,
        url_final=url_final,
        estado_http=int(estado),
        content_type=content_type,
        encoding=encoding,
        sha256=hashlib.sha256(crudo).hexdigest(),
        fecha_captura=captura,
    )


def leer_fichero(ruta: str, url_origen: str = "") -> Respuesta:
    """Carga un .html de disco. `url_origen` permite conservar la procedencia real."""
    try:
        with open(ruta, "rb") as f:
            crudo = f.read()
    except OSError as ex:
        raise ErrorDeDescarga(f"No se pudo leer {ruta}: {ex}") from ex

    texto, encoding = decodificar(crudo)
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        mtime = ""

    url_efectiva = (url_origen or "").strip() or leer_url_origen(ruta)

    # Sin URL declarada no se inventa una: la ruta va en ruta_local, no como URL
    return Respuesta(
        html=texto,
        crudo=crudo,
        url_solicitada=url_efectiva,
        url_final=url_efectiva,
        estado_http=0,
        content_type="text/html (fichero local)",
        encoding=encoding,
        sha256=hashlib.sha256(crudo).hexdigest(),
        ruta_local=os.path.abspath(ruta),
        fecha_archivo=mtime,
    )


def obtener(origen: str, options: Optional[WebOptions] = None,
            url_origen: str = "") -> Respuesta:
    """Punto de entrada unico: acepta indistintamente una URL o una ruta local."""
    if es_url(origen):
        return descargar(origen, options)
    return leer_fichero(origen, url_origen)


def resolver_url(base: str, referencia: str) -> str:
    """Convierte un href relativo en absoluto. Devuelve la referencia si no hay base."""
    referencia = (referencia or "").strip()
    if not referencia or not base:
        return referencia
    try:
        return urllib.parse.urljoin(base, referencia)
    except ValueError:
        return referencia


def dominio(url: str) -> str:
    try:
        red = urllib.parse.urlparse(url).netloc
    except ValueError:
        return ""
    return red[4:] if red.lower().startswith("www.") else red

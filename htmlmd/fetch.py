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
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from htmlmd.models import WebOptions


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


def descargar(url: str, options: Optional[WebOptions] = None) -> Respuesta:
    """Descarga una URL siguiendo redirecciones. Lanza ErrorDeDescarga si no hay pagina."""
    opts = options or WebOptions()
    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": opts.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        },
    )

    captura = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    except urllib.error.HTTPError as ex:
        raise ErrorDeDescarga(f"El servidor respondio {ex.code} ({ex.reason}) para {url}") from ex
    except urllib.error.URLError as ex:
        raise ErrorDeDescarga(f"No se pudo conectar con {url}: {ex.reason}") from ex
    except (ssl.SSLError, OSError, ValueError) as ex:
        raise ErrorDeDescarga(f"Fallo de red al pedir {url}: {ex}") from ex

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

    return Respuesta(
        html=texto,
        crudo=crudo,
        url_solicitada=url_origen or ruta,
        url_final=url_origen,
        estado_http=0,
        content_type="text/html (fichero local)",
        encoding=encoding,
        sha256=hashlib.sha256(crudo).hexdigest(),
        fecha_captura=mtime,
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

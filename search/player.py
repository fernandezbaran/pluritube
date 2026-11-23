"""Resolución y firma de streams de vídeo vía yt-dlp.

El backend nunca expone la URL real del proveedor (googlevideo, PeerTube, etc.)
al navegador: se resuelve en el servidor, se firma y el cliente sólo habla con
``stream_view``. Así no llega ninguna cabecera del proveedor a la máquina del
usuario.

Se invoca el ejecutable ``yt-dlp`` (no el paquete Python) porque es la vía
soportada en este entorno; se puede apuntar a otro binario con ``YTDLP_BIN``.
"""

import ipaddress
import json
import os
import shutil
import socket
import subprocess
from urllib.parse import urlparse

from django.core import signing

#: Sal para los tokens de stream (evita manipulación de la URL de medios).
STREAM_SALT = "video-searcher-stream"
#: Caducidad del enlace firmado. Los enlaces de googlevideo caducan solos antes.
STREAM_MAX_AGE = 60 * 60 * 3
#: Tiempo máximo para resolver un vídeo.
EXTRACT_TIMEOUT = 60

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

#: Cliente de YouTube que usa yt-dlp. El cliente por defecto (``tv_samsung``)
#: devuelve URLs de googlevideo que responden 403; ``android`` sí funciona.
#: Se puede cambiar con la variable de entorno ``YTDLP_PLAYER_CLIENT``.
YTDLP_PLAYER_CLIENT = os.environ.get("YTDLP_PLAYER_CLIENT", "android")

# Prioriza un único fichero progresivo http(s) con audio y vídeo para poder
# reproducirlo en un <video> nativo. Excluye HLS/DASH (los navegadores no los
# reproducen en Android/Firefox). YouTube suele dar 18/22; PeerTube, su mp4 web.
FORMAT_SELECTOR = (
    "best[protocol!*=m3u8][protocol!*=dash][height<=720][ext=mp4]/"
    "best[protocol!*=m3u8][protocol!*=dash][height<=720]/"
    "best[protocol!*=m3u8][protocol!*=dash]/"
    "best[height<=720][vcodec!=none][acodec!=none]/"
    "best[vcodec!=none][acodec!=none]/"
    "best"
)

# Selecciona la lista HLS. Se usa como respaldo cuando el único formato
# disponible es un HLS de PeerTube (``-fragmented.mp4``), que los navegadores
# no reproducen como MP4 progresivo. El reproductor lo carga con hls.js.
FORMAT_SELECTOR_HLS = "best[protocol*=m3u8]/best"


class PlayerError(Exception):
    """Error controlado al resolver un vídeo (se muestra al usuario)."""


def ytdlp_bin():
    return os.environ.get("YTDLP_BIN") or shutil.which("yt-dlp") or "/usr/bin/yt-dlp"


def _is_public_host(host):
    """Rechaza hosts que resuelven a rangos privados/reservados (anti-SSRF)."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def validate_source_url(url):
    """Comprueba que la URL de origen sea http(s) y apunte a un host público."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PlayerError("Sólo se admiten URLs http(s).")
    if not parsed.hostname:
        raise PlayerError("URL de vídeo no válida.")
    if not _is_public_host(parsed.hostname):
        raise PlayerError("Host de vídeo no permitido.")
    return url


def _run_ytdlp(url, fmt):
    """Ejecuta yt-dlp con un selector de formato y devuelve el dict de info."""
    command = [
        ytdlp_bin(),
        "-J",  # volcar la información como JSON único
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--socket-timeout", "20",
        "--extractor-args", f"youtube:player_client={YTDLP_PLAYER_CLIENT}",
        "-f", fmt,
        "--", url,
    ]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=EXTRACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlayerError("Se agotó el tiempo al resolver el vídeo.") from exc
    except OSError as exc:
        raise PlayerError(f"No se pudo ejecutar yt-dlp: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        message = detail[-1] if detail else "yt-dlp no pudo procesar el vídeo."
        raise PlayerError(message)

    try:
        info = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PlayerError("Respuesta inválida de yt-dlp.") from exc
    if not isinstance(info, dict):
        raise PlayerError("Respuesta inesperada de yt-dlp.")

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise PlayerError("La URL no contiene ningún vídeo.")
        info = entries[0]
    if not isinstance(info, dict):
        raise PlayerError("Respuesta inesperada de yt-dlp.")
    return info


def _looks_like_fragmented_mp4(url):
    """Detecta los HLS de PeerTube servidos como ``-fragmented.mp4``.

    Esos ficheros no se pueden reproducir como MP4 progresivo en un
    ``<video>`` (su duración vive en los fragmentos), así que se sirven como
    HLS y el reproductor los carga con hls.js.
    """
    if not url:
        return False
    return "-fragmented.mp4" in url or "/streaming-playlists/hls/" in url


def extract_media(url):
    """Resuelve con yt-dlp la URL reproducible de un vídeo (sin descargarlo).

    Devuelve ``kind`` = ``"file"`` (MP4 progresivo) o ``"hls"`` (lista HLS que
    el navegador carga con hls.js).
    """
    info = _run_ytdlp(url, FORMAT_SELECTOR)
    media_url = info.get("url")
    if not media_url:
        raise PlayerError("No se encontró un stream reproducible.")

    kind = "file"
    protocol = info.get("protocol") or ""
    if protocol.startswith("m3u8") or _looks_like_fragmented_mp4(media_url):
        # Si el formato elegido es un HLS/fragmentado, se vuelve a resolver
        # pidiendo explícitamente la lista m3u8.
        if not protocol.startswith("m3u8"):
            try:
                hls_info = _run_ytdlp(url, FORMAT_SELECTOR_HLS)
            except PlayerError:
                hls_info = None
            if hls_info and (hls_info.get("protocol") or "").startswith("m3u8"):
                info = hls_info
                media_url = info.get("url")
        kind = "hls" if (info.get("protocol") or "").startswith("m3u8") else "file"

    headers = dict(info.get("http_headers") or {})
    headers.setdefault("User-Agent", USER_AGENT)

    return {
        "url": media_url,
        "headers": headers,
        "title": info.get("title") or "Vídeo",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "source": url,
        "kind": kind,
    }


def sign_stream(media):
    """Serializa y firma los datos del stream para pasarlos por la URL."""
    return signing.dumps(media, salt=STREAM_SALT, compress=True)


def unsign_stream(token):
    """Recupera los datos del stream, validando firma y caducidad."""
    return signing.loads(token, salt=STREAM_SALT, max_age=STREAM_MAX_AGE)

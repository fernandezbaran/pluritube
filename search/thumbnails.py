"""Proxy y caché temporal de miniaturas.

El navegador no pide las miniaturas directamente a los CDN (``i.ytimg.com``,
PeerTube, etc.): pide ``/api/thumb/?url=<miniatura>`` y el backend la descarga
con las cabeceras oportunas, la guarda en un caché temporal en disco y la
sirve desde ahí. Así las miniaturas se ven aunque la red del cliente bloquee el
acceso directo a esos dominios.

El caché es "temporal": vive en el directorio de temporales del sistema y se
poda solo cuando acumula demasiados ficheros. Se puede ajustar con
``THUMB_CACHE_DIR``, ``THUMB_CACHE_TTL`` (segundos) y ``THUMB_CACHE_MAX_FILES``.
"""

import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .player import PlayerError, validate_source_url

#: Caducidad del caché en segundos (por defecto 3 días).
CACHE_TTL = int(os.environ.get("THUMB_CACHE_TTL", 60 * 60 * 24 * 3))
#: Directorio del caché.
CACHE_DIR = Path(
    os.environ.get("THUMB_CACHE_DIR")
    or (Path(tempfile.gettempdir()) / "video_searcher_thumbs")
)
#: Nº máximo de miniaturas guardadas antes de podar las más antiguas.
CACHE_MAX_FILES = int(os.environ.get("THUMB_CACHE_MAX_FILES", 1000))
#: Tamaño máximo aceptado para una miniatura.
MAX_BYTES = 8 * 1024 * 1024
#: Tiempo máximo de descarga.
TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: GIF transparente 1x1 que se sirve cuando la miniatura no se puede obtener
#: (evita el icono de "imagen rota" sin romper la maquetación).
PLACEHOLDER = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
PLACEHOLDER_TYPE = "image/gif"


def cache_key(url):
    """Clave de caché estable para una URL de miniatura."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _paths(key):
    return CACHE_DIR / key, CACHE_DIR / (key + ".json")


def _read_meta(key):
    _, meta_path = _paths(key)
    try:
        with meta_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _is_fresh(meta):
    return bool(meta) and (time.time() - meta.get("saved_at", 0)) < CACHE_TTL


def _store(key, data, content_type):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_path, meta_path = _paths(key)
    _atomic_write(data_path, data)
    _atomic_write(
        meta_path,
        json.dumps({"content_type": content_type, "saved_at": time.time()}).encode(
            "utf-8"
        ),
    )
    _prune()


def _atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _prune():
    """Elimina las miniaturas más antiguas si se supera el máximo."""
    try:
        files = [
            entry
            for entry in CACHE_DIR.iterdir()
            if entry.is_file() and not entry.name.endswith(".json")
        ]
    except OSError:
        return
    if len(files) <= CACHE_MAX_FILES:
        return
    files.sort(key=lambda path: path.stat().st_mtime)
    for path in files[: len(files) - CACHE_MAX_FILES]:
        for target in (path, path.with_name(path.name + ".json")):
            try:
                target.unlink()
            except OSError:
                pass


def _download(url):
    parsed = urlparse(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        # Algunos CDN exigen un Referer del propio sitio.
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }
    response = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True)
    try:
        response.raise_for_status()
        content_type = (
            (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        )
        if content_type and not content_type.startswith("image/"):
            raise PlayerError("El recurso no es una imagen.")
        chunks = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_BYTES:
                raise PlayerError("La imagen es demasiado grande.")
            chunks.append(chunk)
    finally:
        response.close()
    return b"".join(chunks), (content_type or "image/jpeg")


def get_thumbnail(url):
    """Devuelve ``(bytes, content_type)`` de ``url``, usando el caché temporal.

    Lanza :class:`PlayerError` o :class:`requests.RequestException` si no se
    puede obtener.
    """
    validate_source_url(url)
    key = cache_key(url)
    meta = _read_meta(key)
    data_path, _ = _paths(key)
    if _is_fresh(meta) and data_path.exists():
        return data_path.read_bytes(), meta.get("content_type", "image/jpeg")

    data, content_type = _download(url)
    _store(key, data, content_type)
    return data, content_type


def clear_cache():
    """Vacía el caché (útil en tests o mantenimiento)."""
    try:
        entries = list(CACHE_DIR.iterdir())
    except OSError:
        return 0
    count = 0
    for entry in entries:
        if entry.is_file():
            try:
                entry.unlink()
                count += 1
            except OSError:
                pass
    return count

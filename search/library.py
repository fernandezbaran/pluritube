"""Biblioteca de sesión, playlists y suscripciones RSS.

- Favoritos y suscripciones viven en ``request.session`` (backend de BD por
  defecto), así que no hay que tocar modelos ni hay límite de tamaño de cookie.
- Las playlists se listan con ``yt-dlp --flat-playlist``.
- Las suscripciones se resuelven a un feed: YouTube (Atom) o PeerTube (RSS 2.0).
"""

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from .player import PlayerError, validate_source_url, ytdlp_bin

FAVORITES_KEY = "favorites"
SUBSCRIPTIONS_KEY = "subscriptions"
PLAYLISTS_KEY = "playlists"
MAX_FAVORITES = 200
MAX_SUBSCRIPTIONS = 100
MAX_PLAYLISTS = 100
FEED_TIMEOUT = 20
PLAYLIST_TIMEOUT = 120

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
ATOM = "http://www.w3.org/2005/Atom"
MEDIA_NS = "http://search.yahoo.com/mrss/"
NS = {"a": ATOM, "media": MEDIA_NS}


# --------------------------------------------------------------------------- #
# Sesión
# --------------------------------------------------------------------------- #
def get_favorites(session):
    return list(session.get(FAVORITES_KEY, []))


def add_favorite(session, item):
    link = (item.get("link") or "").strip()
    if not link:
        return
    favorites = get_favorites(session)
    if any(fav.get("link") == link for fav in favorites):
        return
    favorites.append(
        {
            "title": (item.get("title") or "Vídeo")[:300],
            "link": link,
            "thumbnail": item.get("thumbnail") or "",
            "source": item.get("source") or "",
            "color": item.get("color") or "#586e75",
        }
    )
    session[FAVORITES_KEY] = favorites[-MAX_FAVORITES:]


def remove_favorite(session, link):
    session[FAVORITES_KEY] = [
        fav for fav in get_favorites(session) if fav.get("link") != link
    ]


def favorite_links(session):
    return {fav.get("link") for fav in get_favorites(session)}


def get_subscriptions(session):
    return list(session.get(SUBSCRIPTIONS_KEY, []))


def add_subscription(session, subscription):
    feed_url = (subscription.get("feed_url") or "").strip()
    if not feed_url:
        return
    subscriptions = get_subscriptions(session)
    if any(sub.get("feed_url") == feed_url for sub in subscriptions):
        return
    subscriptions.append(subscription)
    session[SUBSCRIPTIONS_KEY] = subscriptions[-MAX_SUBSCRIPTIONS:]


def remove_subscription(session, feed_url):
    session[SUBSCRIPTIONS_KEY] = [
        sub for sub in get_subscriptions(session) if sub.get("feed_url") != feed_url
    ]


def subscription_keys(session):
    """Claves de canal suscritas (para marcar «Suscrito» en las tarjetas)."""
    return {sub.get("channel_key") for sub in get_subscriptions(session) if sub.get("channel_key")}


def channel_key_from_url(url):
    """Clave estable de un canal a partir de su URL.

    Permite comparar el canal de una tarjeta con las suscripciones guardadas
    sin necesidad de resolver el feed.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    channel = re.search(r"/channel/(UC[\w-]+)", path)
    if channel:
        return f"yt:{channel.group(1)}"
    if host.endswith("youtube.com") or host == "youtu.be":
        return f"yt-url:{(parsed.netloc + path).lower()}"
    if host:
        return f"pt:{host}{path}"
    return ""


def import_config(session, payload):
    """Fusiona un export previo en la sesión. Devuelve cuántos ítems se añadieron."""
    before = {
        "favorites": len(get_favorites(session)),
        "subscriptions": len(get_subscriptions(session)),
        "playlists": len(get_playlists(session)),
    }
    for item in payload.get("favorites") or []:
        if isinstance(item, dict) and _is_http_link(item.get("link")):
            add_favorite(session, item)
    for item in payload.get("subscriptions") or []:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        if not item.get("channel_key"):
            item["channel_key"] = channel_key_from_url(
                item.get("channel_url") or ""
            )
        if _is_http_link(item.get("feed_url")):
            add_subscription(session, item)
    for item in payload.get("playlists") or []:
        if isinstance(item, dict) and _is_http_link(item.get("link")):
            add_playlist(session, item)
    return {
        "favorites": len(get_favorites(session)) - before["favorites"],
        "subscriptions": len(get_subscriptions(session)) - before["subscriptions"],
        "playlists": len(get_playlists(session)) - before["playlists"],
    }


def _is_http_link(value):
    return isinstance(value, str) and value.strip().startswith(("http://", "https://"))


def get_playlists(session):
    return list(session.get(PLAYLISTS_KEY, []))


def add_playlist(session, item):
    link = (item.get("link") or "").strip()
    if not link:
        return
    playlists = get_playlists(session)
    if any(saved.get("link") == link for saved in playlists):
        return
    playlists.append(
        {
            "title": (item.get("title") or "Playlist")[:300],
            "link": link,
            "thumbnail": item.get("thumbnail") or "",
            "source": item.get("source") or "YouTube Playlist",
            "color": item.get("color") or "#ff0000",
            "kind": "playlist",
        }
    )
    session[PLAYLISTS_KEY] = playlists[-MAX_PLAYLISTS:]


def remove_playlist(session, link):
    session[PLAYLISTS_KEY] = [
        saved for saved in get_playlists(session) if saved.get("link") != link
    ]


def playlist_links(session):
    return {saved.get("link") for saved in get_playlists(session)}


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #
def _friendly_playlist_error(message):
    """Traduce los errores crudos de yt-dlp a mensajes útiles en español."""
    text = (message or "").strip()
    low = text.lower()
    if "unviewable" in low or "playlist type" in low:
        return (
            "YouTube no permite listar este tipo de playlist (Mix). "
            "Usa «Abrir URL» para verla en YouTube."
        )
    if "does not exist" in low or "not found" in low:
        return "La playlist no existe, es privada o ha sido eliminada."
    if "private" in low:
        return "La playlist es privada."
    if "members-only" in low or "members only" in low:
        return "La playlist es solo para miembros del canal."
    if "confirm your age" in low or "age-restricted" in low:
        return "YouTube pide verificar la edad para esta playlist."
    if "sign in" in low or "not a bot" in low:
        return (
            "YouTube pidió una verificación anti-bot. "
            "Inténtalo de nuevo en unos minutos."
        )
    if "http error" in low or "unable to download" in low:
        return "YouTube rechazó la petición de la playlist. Comprueba la URL."
    if "unsupported url" in low or "no suitable" in low:
        return "Esa URL no parece una playlist compatible."
    if "has no attribute" in low or "traceback" in low:
        return "No se pudo leer la playlist. Comprueba que la URL sea válida."
    # Quita los prefijos de yt-dlp: "ERROR: [youtube:tab] ID: ..."
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"^[\w-]{10,}:\s*", "", text)
    return text or "No se pudo leer la playlist."


def _normalize_playlist_url(url):
    """Los Mix de YouTube sólo se pueden listar desde ``/watch?v=<semilla>``."""
    parsed = urlparse(url)
    if not parsed.path.rstrip("/").endswith("/playlist"):
        return url
    match = re.search(r"(?:^|&)list=(RD[\w-]{11})(?:&|$)", parsed.query or "")
    if not match:
        return url
    video_id = match.group(1)[2:]
    return f"https://www.youtube.com/watch?v={video_id}&list={match.group(1)}"


def extract_playlist(url):
    """Devuelve ``{"title", "entries"}`` con los vídeos de una playlist/URL."""
    url = _normalize_playlist_url(url)
    command = [
        ytdlp_bin(),
        "-J",
        "--flat-playlist",
        "--skip-download",
        "--no-warnings",
        "--socket-timeout", "20",
        "--", url,
    ]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=PLAYLIST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlayerError("Se agotó el tiempo al leer la playlist.") from exc
    except OSError as exc:
        raise PlayerError(f"No se pudo ejecutar yt-dlp: {exc}") from exc

    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise PlayerError(_friendly_playlist_error(lines[-1] if lines else ""))

    try:
        info = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PlayerError("Respuesta inválida de yt-dlp.") from exc
    if not isinstance(info, dict):
        raise PlayerError("Respuesta inesperada de yt-dlp.")

    raw_entries = info.get("entries") or ([info] if info.get("url") else [])
    entries = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        link = entry.get("url") or entry.get("webpage_url") or ""
        if link and not link.startswith("http") and entry.get("id"):
            link = f"https://www.youtube.com/watch?v={entry['id']}"
        if not link.startswith("http"):
            continue
        thumbs = entry.get("thumbnails") or []
        entries.append(
            {
                "title": entry.get("title") or "Vídeo",
                "link": link,
                "thumbnail": thumbs[-1].get("url") if thumbs else "",
                "description": "",
                "source": "Playlist",
                "color": "#6c71c4",
                "duration": entry.get("duration"),
            }
        )

    return {"title": info.get("title") or "Playlist", "entries": entries}


# --------------------------------------------------------------------------- #
# Suscripciones: resolver canal -> feed
# --------------------------------------------------------------------------- #
def _normalize_channel_input(raw):
    value = raw.strip()
    if re.fullmatch(r"UC[\w-]{22}", value):
        return value
    if value.startswith("@"):
        return "https://www.youtube.com/" + value
    if "://" in value:
        return value
    if value.startswith(("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")):
        return "https://" + value
    if "/" not in value and "." not in value:
        return "https://www.youtube.com/@" + value
    return "https://" + value


def _fetch(url, timeout=FEED_TIMEOUT):
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response


def _resolve_youtube_channel_id(raw):
    if re.fullmatch(r"UC[\w-]{22}", raw):
        return raw
    match = re.search(r"/channel/(UC[\w-]{22})", raw) or re.search(
        r"channel_id=(UC[\w-]{22})", raw
    )
    if match:
        return match.group(1)
    page_url = raw if raw.startswith("http") else "https://www.youtube.com/" + raw
    html = _fetch(page_url).text
    match = re.search(r'"(?:externalId|browseId|channelId)":"(UC[\w-]{22})"', html)
    if not match:
        raise PlayerError("No se pudo identificar el canal de YouTube.")
    return match.group(1)


def _resolve_peertube_channel(parsed):
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    channel = re.search(r"/video-channels/([^/]+)$", path)
    if channel:
        name = channel.group(1)
        data = _fetch(f"{base}/api/v1/video-channels/{name}").json()
        channel_id = data.get("id")
        if not channel_id:
            raise PlayerError("Canal de PeerTube no encontrado.")
        return {
            "feed_url": f"{base}/feeds/videos.xml?videoChannelId={channel_id}",
            "title": data.get("displayName") or name,
            "source": "PeerTube",
            "channel_url": f"{base}/video-channels/{name}",
            "channel_key": f"pt:{parsed.hostname.lower()}/video-channels/{name}",
        }
    account = re.search(r"/c/([^/]+)$", path)
    if account:
        name = account.group(1)
        data = _fetch(f"{base}/api/v1/accounts/{name}").json()
        account_id = data.get("id")
        if not account_id:
            raise PlayerError("Cuenta de PeerTube no encontrada.")
        return {
            "feed_url": f"{base}/feeds/videos.xml?accountId={account_id}",
            "title": data.get("displayName") or name,
            "source": "PeerTube",
            "channel_url": f"{base}/c/{name}",
            "channel_key": f"pt:{parsed.hostname.lower()}/c/{name}",
        }
    raise PlayerError("Pega la URL de un canal de PeerTube (/video-channels/...).")


def resolve_subscription(raw):
    """Convierte lo que pega el usuario en ``{feed_url, title, source, ...}``."""
    value = _normalize_channel_input(raw)
    if re.fullmatch(r"UC[\w-]{22}", value):
        return {
            "feed_url": YOUTUBE_FEED.format(value),
            "title": f"YouTube · {value}",
            "source": "YouTube",
            "channel_url": f"https://www.youtube.com/channel/{value}",
            "channel_key": f"yt:{value}",
        }

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PlayerError("URL de canal no válida.")
    validate_source_url(value)

    host = parsed.hostname.lower()
    if host.endswith("youtube.com") or host == "youtu.be":
        channel_id = _resolve_youtube_channel_id(value)
        return {
            "feed_url": YOUTUBE_FEED.format(channel_id),
            "title": f"YouTube · {channel_id}",
            "source": "YouTube",
            "channel_url": f"https://www.youtube.com/channel/{channel_id}",
            "channel_key": f"yt:{channel_id}",
        }
    if "feeds/videos.xml" in value or value.lower().endswith((".xml", ".rss")):
        return {
            "feed_url": value,
            "title": host,
            "source": host,
            "channel_url": value,
            "channel_key": channel_key_from_url(value),
        }
    return _resolve_peertube_channel(parsed)


# --------------------------------------------------------------------------- #
# Feeds RSS/Atom
# --------------------------------------------------------------------------- #
def _parse_atom(root, feed_url, limit):
    feed_title = root.findtext("a:title", default="", namespaces=NS)
    host = urlparse(feed_url).hostname or ""
    color = "#ff0000" if "youtube" in host else "#859900"
    entries = []
    for node in root.findall("a:entry", NS)[:limit]:
        link_el = node.find("a:link[@rel='alternate']", NS)
        if link_el is None:
            link_el = node.find("a:link", NS)
        thumb_el = node.find("media:group/media:thumbnail", NS)
        if thumb_el is None:
            thumb_el = node.find("media:thumbnail", NS)
        entries.append(
            {
                "title": node.findtext("a:title", default="Vídeo", namespaces=NS),
                "link": link_el.get("href") if link_el is not None else "",
                "thumbnail": thumb_el.get("url") if thumb_el is not None else "",
                "description": "",
                "source": feed_title or host,
                "color": color,
                "published": node.findtext("a:published", default="", namespaces=NS),
            }
        )
    return {"title": feed_title, "entries": entries}


def _parse_rss(root, feed_url, limit):
    channel = root.find("channel")
    if channel is None:
        return {"title": "", "entries": []}
    feed_title = channel.findtext("title") or urlparse(feed_url).hostname or ""
    entries = []
    for node in channel.findall("item")[:limit]:
        thumb_el = node.find(f"{{{MEDIA_NS}}}thumbnail")
        entries.append(
            {
                "title": node.findtext("title") or "Vídeo",
                "link": node.findtext("link") or "",
                "thumbnail": thumb_el.get("url") if thumb_el is not None else "",
                "description": node.findtext("description") or "",
                "source": feed_title,
                "color": "#859900",
                "published": node.findtext("pubDate") or "",
            }
        )
    return {"title": feed_title, "entries": entries}


def fetch_feed(feed_url, limit=12):
    response = _fetch(feed_url)
    root = ET.fromstring(response.content)
    tag = root.tag.lower()
    if tag.endswith("feed"):
        return _parse_atom(root, feed_url, limit)
    if tag.endswith("rss"):
        return _parse_rss(root, feed_url, limit)
    raise PlayerError("Formato de feed no reconocido.")


def published_sort_key(entry):
    """Clave de orden tolerante a ISO-8601 (Atom) y RFC-822 (RSS 2.0)."""
    raw = entry.get("published") or ""
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()

import requests
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, quote, urljoin, urlparse

from django.core import signing
from django.core.paginator import Paginator
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods

from . import library, thumbnails
from .player import (
    PlayerError,
    extract_media,
    sign_stream,
    unsign_stream,
    validate_source_url,
)

YT_PLAYLIST_URL = "https://www.youtube.com/playlist?list={}"

#: Cuántos vídeos se leen de cada feed de suscripción al construir la lista.
SUBSCRIPTION_FEED_LIMIT = 12

#: Elementos por página en los listados (búsqueda, favoritos, suscripciones…).
RESULTS_PER_PAGE = 10


def _page_base(request, *exclude):
    """Query GET sin las claves indicadas, listo para anteponer a ``page=``.

    La paginación debe conservar el contexto de la página (los filtros de
    suscripciones, la URL de una playlist…), pero nunca arrastrar su propia
    clave de página. Devuelve, por ejemplo, ``'filter=all&channel=x&'`` o
    ``''`` si no queda ningún parámetro.
    """
    params = request.GET.copy()
    for key in exclude or ('page',):
        params.pop(key, None)
    encoded = params.urlencode()
    return f'{encoded}&' if encoded else ''


def _subscription_filter_chips(subscriptions, active_filters, active_channels):
    """Chips de filtro que se activan/desactivan sin JavaScript.

    Cada chip enlaza a la URL con **ese** filtro alternado, así se pueden
    combinar varias fuentes a la vez (la vista las trata como unión). El chip
    «Todos» limpia la selección.
    """
    def encode(filters, channels):
        pairs = [('filter', value) for value in sorted(filters)]
        pairs += [('channel', value) for value in sorted(channels)]
        return urlencode(pairs)

    chips = [
        {
            'label': 'Todos',
            'active': not active_filters and not active_channels,
            'query': '',
        },
        {
            'label': 'YouTube',
            'active': 'youtube' in active_filters,
            'query': encode(active_filters ^ {'youtube'}, active_channels),
        },
        {
            'label': 'PeerTube',
            'active': 'pep' in active_filters,
            'query': encode(active_filters ^ {'pep'}, active_channels),
        },
    ]
    for subscription in subscriptions:
        feed_url = subscription.get('feed_url') or ''
        if not feed_url:
            continue
        chips.append({
            'label': subscription.get('title') or feed_url,
            'active': feed_url in active_channels,
            'query': encode(active_filters, active_channels ^ {feed_url}),
        })
    return chips


def _extract_initial_data(html):
    """Parsea ``ytInitialData`` con un decodificador JSON real (robusto)."""
    marker = 'var ytInitialData = '
    index = html.find(marker)
    if index == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[index + len(marker):])
        return data
    except ValueError:
        return None


def _walk_youtube_results(node):
    """Recorre los resultados en orden y produce ``(tipo, renderer)``."""
    if isinstance(node, dict):
        if 'videoRenderer' in node:
            yield 'video', node['videoRenderer']
        if 'playlistRenderer' in node:
            yield 'playlist', node['playlistRenderer']
        lockup = node.get('lockupViewModel')
        if lockup and lockup.get('contentType') == 'LOCKUP_CONTENT_TYPE_PLAYLIST':
            yield 'playlist_lockup', lockup
        for value in node.values():
            yield from _walk_youtube_results(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_youtube_results(value)


def _youtube_channel(video):
    """Extrae ``(nombre, enlace, clave)`` del canal de un ``videoRenderer``.

    ``browseId`` es el identificador ``UC...`` del canal: se prefiere para el
    enlace y como clave, porque permite suscribirse sin volver a pedir la
    página del canal.
    """
    for key in ('ownerText', 'longBylineText', 'shortBylineText'):
        runs = (video.get(key) or {}).get('runs') or []
        if not runs:
            continue
        run = runs[0]
        name = run.get('text') or ''
        browse = (run.get('navigationEndpoint') or {}).get('browseEndpoint') or {}
        browse_id = browse.get('browseId') or ''
        canonical = browse.get('canonicalBaseUrl') or ''
        if browse_id:
            return name, f'https://www.youtube.com/channel/{browse_id}', f'yt:{browse_id}'
        if canonical:
            link = 'https://www.youtube.com' + canonical
            return name, link, library.channel_key_from_url(link)
        return name, '', ''
    return '', '', ''


def _youtube_video_item(video):
    channel, channel_link, channel_key = _youtube_channel(video)
    return {
        'title': video['title']['runs'][0]['text'],
        'description': video.get('detailedMetadataSnippets', [{}])[0].get('snippetText', {}).get('runs', [{}])[0].get('text', 'No description.'),
        'thumbnail': video['thumbnail']['thumbnails'][-1]['url'],
        'link': f"https://www.youtube.com/watch?v={video['videoId']}",
        'source': 'YouTube',
        'color': '#ff0000',  # YouTube Red
        'kind': 'video',
        'channel': channel,
        'channel_link': channel_link,
        'channel_key': channel_key,
    }


def _youtube_playlist_item(playlist):
    title_obj = playlist.get('title') or {}
    title = title_obj.get('simpleText') or ''.join(
        run.get('text', '') for run in (title_obj.get('runs') or [])
    )
    thumbnails = (playlist.get('thumbnail') or {}).get('thumbnails') or []
    if not thumbnails:
        collections = playlist.get('thumbnails') or []
        if collections:
            thumbnails = collections[0].get('thumbnails') or []
    count = playlist.get('videoCount')
    return {
        'title': title or 'Playlist de YouTube',
        'description': f"{count} vídeos" if count else 'Playlist de YouTube',
        'thumbnail': thumbnails[-1]['url'] if thumbnails else '',
        'link': YT_PLAYLIST_URL.format(playlist.get('playlistId')),
        'source': 'YouTube Playlist',
        'color': '#ff0000',
        'kind': 'playlist',
    }


VIDEO_COUNT_RE = re.compile(r'\b\d[\d.,]*\s*videos?\b', re.IGNORECASE)


def _iter_strings(node):
    """Recorre cualquier estructura JSON y produce todas sus cadenas."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_strings(value)


def _lockup_description(lockup):
    """Extrae el contador real de vídeos ("343 videos") del lockup."""
    for text in _iter_strings(lockup):
        match = VIDEO_COUNT_RE.search(text)
        if match:
            return match.group(0)
    return 'Playlist de YouTube'


def _youtube_lockup_playlist_item(lockup):
    meta = (lockup.get('metadata') or {}).get('lockupMetadataViewModel') or {}
    title = (meta.get('title') or {}).get('content') or 'Playlist de YouTube'
    collection = (lockup.get('contentImage') or {}).get('collectionThumbnailViewModel') or {}
    thumbnail_vm = (collection.get('primaryThumbnail') or {}).get('thumbnailViewModel') or {}
    sources = (thumbnail_vm.get('image') or {}).get('sources') or []
    return {
        'title': title,
        'description': _lockup_description(lockup),
        'thumbnail': sources[-1]['url'] if sources else '',
        'link': YT_PLAYLIST_URL.format(lockup.get('contentId')),
        'source': 'YouTube Playlist',
        'color': '#ff0000',
        'kind': 'playlist',
    }


def get_youtube_results(query):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
    url = f"https://www.youtube.com/results?search_query={quote(query)}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = _extract_initial_data(response.text)
        if not data:
            return []
        results = []
        seen = set()
        for kind, payload in _walk_youtube_results(data.get('contents', {})):
            if kind == 'video':
                item = _youtube_video_item(payload)
            elif kind == 'playlist':
                item = _youtube_playlist_item(payload)
            else:
                item = _youtube_lockup_playlist_item(payload)
            if item.get('link') and item['link'] not in seen:
                seen.add(item['link'])
                results.append(item)
        return results
    except Exception:
        return []


def _sepia_channel(item):
    """Extrae canal/enlace/clave de un resultado de SepiaSearch (PeerTube)."""
    channel = item.get('channel') or {}
    name = channel.get('displayName') or channel.get('name') or ''
    url = channel.get('url') or ''
    if not url:
        host = channel.get('host') or ''
        handle = channel.get('name') or ''
        if host and handle:
            url = f'https://{host}/video-channels/{handle}'
    return {
        'channel': name,
        'channel_link': url,
        'channel_key': library.channel_key_from_url(url),
    }


def get_sepia_results(query):
    url = f"https://sepiasearch.org/api/v1/search/videos?search={quote(query)}&count=50"
    results = []
    try:
        resp = requests.get(url, timeout=10).json()
        for item in resp.get('data', []):
            video_url = item.get('url', '')
            thumb_path = item.get('thumbnailPath', '')

            # --- THE FIX ---
            # If the thumbnail starts with '/', it's relative. 
            # We extract the domain from the video link and add it.
            if thumb_path.startswith('/'):
                parsed_uri = urlparse(video_url)
                # This creates 'https://domain.com'
                domain = f'{parsed_uri.scheme}://{parsed_uri.netloc}'
                full_thumb_url = f"{domain}{thumb_path}"
            else:
                full_thumb_url = thumb_path

            results.append({
                'title': item['name'],
                'description': item['description'][:150] + "..." if item['description'] else "No description.",
                'thumbnail': full_thumb_url, # Now it's a full URL!
                'link': video_url,
                'source': 'Sepia Search',
                'color': '#859900',
                'kind': 'video',
                **_sepia_channel(item),
            })
    except Exception as e:
        print(f"Sepia Error: {e}")
    return results


def search_view(request):
    query = request.GET.get('q', '')
    page_number = request.GET.get('page', 1)
    combined_results = []

    if query:
        yt = get_youtube_results(query)
        sepia = get_sepia_results(query)

        # INTERLEAVING LOGIC (The Zipper)
        # We take one from each until one list is empty, then add the rest of the other
        i, j = 0, 0
        while i < len(yt) or j < len(sepia):
            if i < len(yt):
                combined_results.append(yt[i])
                i += 1
            if j < len(sepia):
                combined_results.append(sepia[j])
                j += 1

    paginator = Paginator(combined_results, RESULTS_PER_PAGE) # 10 items per page (5 YT, 5 Sepia)
    page_obj = paginator.get_page(page_number)
        
    return render(request, 'search/index.html', {
        'page_obj': page_obj,
        'page_base': _page_base(request),
        'query': query,
        'favorite_links': library.favorite_links(request.session),
        'playlist_links': library.playlist_links(request.session),
        'subscription_keys': library.subscription_keys(request.session),
    })


@require_GET
def resolve_view(request):
    """Resuelve (server-side) la URL reproducible de un vídeo y devuelve JSON."""
    source = request.GET.get('url', '').strip()
    if not source:
        return JsonResponse({'ok': False, 'error': 'Falta la URL del vídeo.'}, status=400)

    try:
        validate_source_url(source)
        media = extract_media(source)
    except PlayerError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    token = sign_stream(media)
    endpoint = 'search:hls' if media.get('kind') == 'hls' else 'search:stream'
    stream_url = f"{reverse(endpoint)}?{urlencode({'t': token})}"
    return JsonResponse({
        'ok': True,
        'title': media['title'],
        'duration': media['duration'],
        'thumbnail': media['thumbnail'],
        'kind': media.get('kind', 'file'),
        'stream': stream_url,
    })


def _normalized_content_type(content_type):
    """Asegura un Content-Type reproducible por ``<video>``.

    Algunos orígenes PeerTube sirven el MP4 como ``application/octet-stream``;
    el elemento ``<video>`` de algunos navegadores lo rechaza, así que se
    normaliza a ``video/mp4``.
    """
    value = (content_type or '').split(';')[0].strip().lower()
    if not value or value in ('application/octet-stream', 'binary/octet-stream'):
        return 'video/mp4'
    return value


def _iter_upstream(response, chunk_size=64 * 1024):
    try:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk
    finally:
        response.close()


def _open_upstream(media, range_header):
    headers = dict(media.get('headers') or {})
    # Muchos orígenes (p. ej. googlevideo) rechazan un GET sin Range con 403/400,
    # así que pedimos siempre un rango: el reproductor nativo envía Range igual.
    headers['Range'] = range_header or 'bytes=0-'
    return requests.get(media['url'], headers=headers, stream=True, timeout=30)


@require_GET
def stream_view(request):
    """Proxy de bytes hacia el proveedor. El navegador nunca toca el origen."""
    token = request.GET.get('t', '')
    try:
        media = unsign_stream(token)
    except signing.BadSignature:
        return HttpResponseBadRequest('Enlace de vídeo no válido o caducado.')

    range_header = request.headers.get('Range')

    def _try_open(current_media):
        try:
            return _open_upstream(current_media, range_header)
        except requests.RequestException:
            return None

    upstream = _try_open(media)

    # El enlace de googlevideo puede venir rechazado; se puede regenerar.
    # Reintenta un par de veces re-resolviendo antes de rendirse.
    for _ in range(2):
        if upstream is not None and upstream.status_code < 400:
            break
        if upstream is None or upstream.status_code not in (401, 403, 410):
            break
        if not media.get('source'):
            break
        upstream.close()
        try:
            validate_source_url(media['source'])
            media = extract_media(media['source'])
        except (PlayerError, requests.RequestException):
            upstream = None
            break
        upstream = _try_open(media)

    if upstream is None or upstream.status_code >= 400:
        if upstream is not None:
            upstream.close()
        return HttpResponseBadRequest('El origen rechazó la petición de vídeo.')

    passthrough = {}
    for header in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
        value = upstream.headers.get(header)
        if value:
            passthrough[header] = value

    response = StreamingHttpResponse(
        _iter_upstream(upstream),
        status=upstream.status_code,
        content_type=_normalized_content_type(passthrough.pop('Content-Type', None)),
    )
    for key, value in passthrough.items():
        response[key] = value
    # Permite al reproductor pedir rangos (seek) aunque el origen no lo anuncie.
    response['Accept-Ranges'] = 'bytes'
    return response


@require_GET
def thumbnail_view(request):
    """Sirve miniaturas descargadas por el backend (caché temporal).

    El navegador nunca toca los CDN: así las miniaturas se ven aunque la red
    del cliente bloquee esos dominios.
    """
    url = request.GET.get('url', '').strip()
    if not url:
        return HttpResponse(thumbnails.PLACEHOLDER, content_type=thumbnails.PLACEHOLDER_TYPE)

    try:
        data, content_type = thumbnails.get_thumbnail(url)
    except (PlayerError, requests.RequestException, OSError):
        data, content_type = thumbnails.PLACEHOLDER, thumbnails.PLACEHOLDER_TYPE

    response = HttpResponse(data, content_type=content_type)
    response['Cache-Control'] = 'public, max-age=86400'
    return response


#: Tipos MIME habituales de una lista HLS.
HLS_PLAYLIST_TYPES = {
    'application/vnd.apple.mpegurl',
    'application/x-mpegurl',
    'audio/mpegurl',
    'application/mpegurl',
}
#: Atributos ``URI="..."`` dentro de etiquetas HLS (EXT-X-MAP, EXT-X-KEY...).
HLS_URI_RE = re.compile(r'URI="([^"]*)"')


def _hls_proxy_uri(uri, base, headers):
    """Firma una URI de la lista y la reescribe hacia el proxy propio."""
    absolute = urljoin(base, uri)
    token = sign_stream({'url': absolute, 'headers': headers})
    return f"{reverse('search:hls')}?{urlencode({'t': token})}"


def _rewrite_hls_playlist(text, base, headers):
    """Reescribe una lista HLS para que todo pase por ``hls_view``.

    Así el navegador nunca contacta el servidor PeerTube (ni el cliente tiene
    que poder alcanzarlo), y se mantiene la privacidad del origen.
    """
    lines = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith('#'):
            if 'URI="' in stripped:
                stripped = HLS_URI_RE.sub(
                    lambda match: 'URI="%s"' % _hls_proxy_uri(
                        match.group(1), base, headers
                    ),
                    stripped,
                )
            lines.append(stripped)
        else:
            lines.append(_hls_proxy_uri(stripped, base, headers))
    return '\n'.join(lines)


def _chain(first, rest):
    """Antepone ``first`` (ya leído) al resto del iterador de bytes."""
    if first:
        yield first
    yield from rest


@require_GET
def hls_view(request):
    """Proxy de HLS: reescribe listas m3u8 y sirve los segmentos con rangos."""
    token = request.GET.get('t', '')
    try:
        payload = unsign_stream(token)
    except signing.BadSignature:
        return HttpResponseBadRequest('Enlace de vídeo no válido o caducado.')
    if not isinstance(payload, dict):
        return HttpResponseBadRequest('Enlace de vídeo no válido.')

    target = payload.get('url')
    headers = dict(payload.get('headers') or {})
    if not target:
        return HttpResponseBadRequest('Falta la URL del segmento.')
    try:
        validate_source_url(target)
    except PlayerError:
        return HttpResponseBadRequest('Origen de vídeo no permitido.')

    range_header = request.headers.get('Range')
    request_headers = dict(headers)
    if range_header:
        request_headers['Range'] = range_header

    try:
        upstream = requests.get(target, headers=request_headers, stream=True, timeout=30)
    except requests.RequestException:
        return HttpResponseBadRequest('No se pudo contactar con el origen de vídeo.')

    if upstream.status_code >= 400:
        upstream.close()
        return HttpResponseBadRequest('El origen rechazó la petición de vídeo.')

    content_type = (upstream.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    chunks = upstream.iter_content(chunk_size=64 * 1024)
    first = next(chunks, b'')

    if content_type in HLS_PLAYLIST_TYPES or first[:7] == b'#EXTM3U':
        body = first + b''.join(chunks)
        upstream.close()
        text = body.decode('utf-8', 'replace')
        rewritten = _rewrite_hls_playlist(text, target, headers)
        response = HttpResponse(rewritten, content_type='application/vnd.apple.mpegurl')
        response['Cache-Control'] = 'no-store'
        return response

    passthrough = {}
    for header in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
        value = upstream.headers.get(header)
        if value:
            passthrough[header] = value

    response = StreamingHttpResponse(
        _chain(first, _iter_upstream(upstream)),
        status=upstream.status_code,
        content_type=passthrough.pop('Content-Type', 'video/mp4'),
    )
    for key, value in passthrough.items():
        response[key] = value
    response['Accept-Ranges'] = 'bytes'
    return response


def _redirect_back(request, default):
    """Redirige a ``next`` sólo si es una ruta local (evita open redirects)."""
    target = request.POST.get('next') or request.GET.get('next')
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(target)
    return redirect(default)


@require_http_methods(['GET', 'POST'])
def favorites_view(request):
    """Lista los favoritos de la sesión y procesa añadir/quitar."""
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            library.add_favorite(request.session, request.POST)
        elif action == 'remove':
            library.remove_favorite(request.session, request.POST.get('link', ''))
        return _redirect_back(request, 'search:favorites')

    favorites = library.get_favorites(request.session)
    page_obj = Paginator(favorites, RESULTS_PER_PAGE).get_page(
        request.GET.get('page', 1)
    )
    return render(request, 'search/favorites.html', {
        'page_obj': page_obj,
        'favorite_links': library.favorite_links(request.session),
        'playlist_links': library.playlist_links(request.session),
        'subscription_keys': library.subscription_keys(request.session),
        'remove_mode': True,
        'next': request.get_full_path(),
        'page_base': _page_base(request),
    })


@require_http_methods(['GET', 'POST'])
def subscriptions_view(request):
    """Gestiona suscripciones RSS: vídeos cronológicos, filtros y playlist."""
    errors = []
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            raw = request.POST.get('channel', '').strip()
            if not raw:
                errors.append('Pega la URL o el nombre de un canal.')
            else:
                try:
                    subscription = library.resolve_subscription(raw)
                    if request.POST.get('title'):
                        subscription['title'] = request.POST['title']
                    else:
                        feed = library.fetch_feed(subscription['feed_url'], limit=1)
                        if feed.get('title'):
                            subscription['title'] = feed['title']
                    library.add_subscription(request.session, subscription)
                except (PlayerError, requests.RequestException) as exc:
                    errors.append(str(exc))
        elif action == 'remove':
            library.remove_subscription(
                request.session, request.POST.get('feed_url', '')
            )
            return _redirect_back(request, 'search:subscriptions')
        if not errors:
            return _redirect_back(request, 'search:subscriptions')

    subscriptions = library.get_subscriptions(request.session)
    entries = []
    for subscription, feed, error in library.fetch_feeds(
        subscriptions, SUBSCRIPTION_FEED_LIMIT
    ):
        if error:
            errors.append(
                f"{subscription.get('title') or subscription.get('feed_url')}: {error}"
            )
            continue
        source_type = 'youtube' if 'youtube' in (subscription.get('source') or '').lower() else 'peer'
        for entry in feed.get('entries', []):
            entry = dict(entry)
            entry['channel'] = entry.get('source') or subscription.get('title') or ''
            entry['channel_link'] = subscription.get('channel_url') or ''
            entry['channel_key'] = subscription.get('channel_key') or ''
            entry['feed_url'] = subscription['feed_url']
            entry['sub_title'] = subscription.get('title') or ''
            entry['source_type'] = source_type
            entries.append(entry)

    # Orden cronológico (lo más reciente primero).
    entries.sort(key=library.published_sort_key, reverse=True)

    # Filtros multi-selección (sin JS): varias fuentes se combinan como unión.
    active_filters = {
        value.lower() for value in request.GET.getlist('filter')
    } & {'youtube', 'pep'}
    active_channels = {value for value in request.GET.getlist('channel') if value}

    platforms = set()
    if 'youtube' in active_filters:
        platforms.add('youtube')
    if 'pep' in active_filters:
        platforms.add('peer')

    if not platforms and not active_channels:
        visible = entries
    else:
        visible = [
            entry for entry in entries
            if entry['source_type'] in platforms
            or entry['feed_url'] in active_channels
        ]

    filter_chips = _subscription_filter_chips(
        subscriptions, active_filters, active_channels
    )

    playlist_data = {
        'title': 'Suscripciones',
        'entries': [
            {'title': entry.get('title') or 'Vídeo', 'link': entry['link']}
            for entry in visible
            if entry.get('link')
        ],
    }

    page_obj = Paginator(visible, RESULTS_PER_PAGE).get_page(
        request.GET.get('page', 1)
    )

    return render(request, 'search/subscriptions.html', {
        'subscriptions': subscriptions,
        'page_obj': page_obj,
        'total_entries': len(entries),
        'visible_count': len(visible),
        'filter_chips': filter_chips,
        'playlist_data': playlist_data,
        'errors': errors,
        'favorite_links': library.favorite_links(request.session),
        'playlist_links': library.playlist_links(request.session),
        'subscription_keys': library.subscription_keys(request.session),
        'next': request.get_full_path(),
        'page_base': _page_base(request),
    })


@require_http_methods(['GET', 'POST'])
def import_view(request):
    """Importa un fichero de configuración exportado antes."""
    message = None
    errors = []
    if request.method == 'POST':
        raw = ''
        upload = request.FILES.get('file')
        if upload is not None:
            try:
                raw = upload.read().decode('utf-8', 'replace')
            except (OSError, UnicodeDecodeError):
                errors.append('No se pudo leer el fichero subido.')
        else:
            raw = request.POST.get('data', '')
        if not errors:
            if not raw.strip():
                errors.append('No se recibió ningún contenido para importar.')
            else:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    errors.append('El contenido no es JSON válido.')
                else:
                    if not isinstance(payload, dict):
                        errors.append('El formato del fichero no es el esperado.')
                    else:
                        counts = library.import_config(request.session, payload)
                        message = (
                            f"Importados {counts['favorites']} favoritos, "
                            f"{counts['subscriptions']} suscripciones y "
                            f"{counts['playlists']} playlists."
                        )

    return render(request, 'search/import.html', {
        'message': message,
        'errors': errors,
        'favorites': library.get_favorites(request.session),
        'subscriptions': library.get_subscriptions(request.session),
        'playlists': library.get_playlists(request.session),
    })


def _playlist_payload(playlist):
    """Reduce la playlist a lo que necesita el reproductor del navegador."""
    return {
        'title': playlist.get('title') or 'Playlist',
        'entries': [
            {'title': entry.get('title') or 'Vídeo', 'link': entry.get('link')}
            for entry in playlist.get('entries', [])
            if entry.get('link')
        ],
    }


@require_GET
def playlist_view(request):
    """Carga una playlist/URL con yt-dlp y lista sus vídeos."""
    url = request.GET.get('url', '').strip()
    playlist = None
    error = None
    if url:
        try:
            validate_source_url(url)
            playlist = library.extract_playlist(url)
        except PlayerError as exc:
            error = str(exc)
        except Exception:
            error = 'No se pudo leer la playlist.'

    entries = playlist.get('entries') if playlist else []
    page_obj = Paginator(entries, RESULTS_PER_PAGE).get_page(
        request.GET.get('page', 1)
    )
    saved_page = Paginator(
        library.get_playlists(request.session), RESULTS_PER_PAGE
    ).get_page(request.GET.get('spage', 1))

    return render(request, 'search/playlist.html', {
        'url': url,
        'playlist': playlist,
        'playlist_data': _playlist_payload(playlist) if playlist else None,
        'error': error,
        'page_obj': page_obj,
        'page_base': _page_base(request, 'page'),
        'saved_page': saved_page,
        'saved_page_base': _page_base(request, 'spage'),
        'playlist_links': library.playlist_links(request.session),
        'saved': bool(url) and url in library.playlist_links(request.session),
        'favorite_links': library.favorite_links(request.session),
        'subscription_keys': library.subscription_keys(request.session),
        'next': request.get_full_path(),
    })


@require_GET
def playlist_api_view(request):
    """Lista los vídeos de una playlist con yt-dlp para el reproductor HTML5."""
    url = request.GET.get('url', '').strip()
    if not url:
        return JsonResponse(
            {'ok': False, 'error': 'Falta la URL de la playlist.'}, status=400
        )
    try:
        validate_source_url(url)
        playlist = library.extract_playlist(url)
    except PlayerError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Exception:
        return JsonResponse(
            {'ok': False, 'error': 'No se pudo leer la playlist.'}, status=400
        )

    payload = _playlist_payload(playlist)
    payload['ok'] = True
    return JsonResponse(payload)


@require_http_methods(['GET', 'POST'])
def save_playlist_view(request):
    """Guarda o quita una playlist de la sesión."""
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            library.add_playlist(request.session, request.POST)
        elif action == 'remove':
            library.remove_playlist(request.session, request.POST.get('link', ''))
    return _redirect_back(request, 'search:playlist')


@require_GET
def export_view(request):
    """Descarga favoritos, suscripciones y playlists como JSON."""
    payload = {
        'app': 'video_searcher',
        'version': 1,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'favorites': library.get_favorites(request.session),
        'subscriptions': library.get_subscriptions(request.session),
        'playlists': library.get_playlists(request.session),
    }
    response = JsonResponse(
        payload, json_dumps_params={'indent': 2, 'ensure_ascii': False}
    )
    response['Content-Disposition'] = 'attachment; filename="video-searcher-config.json"'
    return response






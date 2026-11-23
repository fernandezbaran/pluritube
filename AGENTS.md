# AGENTS.md — video_searcher

Django app that aggregates video **and playlist** search results from YouTube + SepiaSearch into one paginated page, with an in-page proxy player and session-backed favorites, subscriptions (RSS), saved playlists, playlist browsing, and a JSON config **export/import**. Every card shows the channel name and a subscribe button; the subscriptions page lists all feed videos chronologically with server-side filters (all / YouTube / PeerTube / per-channel) and a "play all" queue.

## Stack & setup
- Django 6.0.3 on Python 3.13.5, SQLite (`db.sqlite3`).
- Virtualenv `venv/` already exists and has Django + requests. Use `venv/bin/python ...` or `source venv/bin/activate` first — there is no global install.
- `requirements.txt` exists; install with `venv/bin/pip install -r requirements.txt`.
- In-page playback and playlists call the **`yt-dlp` executable** (not the Python package). It resolves `YTDLP_BIN` env var → `yt-dlp` on `PATH` → `/usr/bin/yt-dlp`.
- Note: in this environment `pip install` reports success but does **not** persist files into `venv/.../site-packages` (the `yt_dlp` module vanishes). The system `/usr/bin/yt-dlp` binary is what actually works; don't rely on `import yt_dlp`. `pip install --target=<dir>` does persist.
- Sessions use Django's default DB backend: `django_session` must be migrated (`manage.py migrate`) or favorites/subscriptions silently fail.

## Commands
```sh
venv/bin/python manage.py runserver     # dev server
venv/bin/python manage.py test          # runs search/tests.py (48 offline unit tests)
venv/bin/python manage.py check         # config sanity check
venv/bin/python manage.py makemigrations
venv/bin/python manage.py migrate
```
- No CI, lint, formatter, or typecheck config. Verification = `manage.py test` (offline unit tests in `search/tests.py`) plus running the server and exercising the pages.

## Routes
All in `search/urls.py` (`app_name = "search"`), mounted at `/` from `core/urls.py`:
- `""` → `search_view`
- `"favorites/"` → `favorites_view`
- `"subscriptions/"` → `subscriptions_view`
- `"playlist/"` → `playlist_view`
- `"playlists/save/"` → `save_playlist_view` (POST, add/remove saved playlist)
- `"export/"` → `export_view` (GET, downloads `video-searcher-config.json`)
- `"import/"` → `import_view` (GET/POST, merge an exported config file/JSON into the session)
- `"api/resolve/"` → `resolve_view`
- `"api/playlist/"` → `playlist_api_view` (GET, JSON list of playlist entries for the player)
- `"api/stream/"` → `stream_view` (progressive MP4)
- `"api/hls/"` → `hls_view` (GET `?t=`, signed proxy that rewrites m3u8 playlists and serves HLS segments with `Range`)
- `"api/thumb/"` → `thumbnail_view` (GET `?url=`, proxied+cached thumbnail bytes)

## Architecture
- `search/views.py`: search + the player/HLS endpoints + favorites/subscriptions/playlist/save/export/import views.
  - `get_youtube_results()` scrapes `ytInitialData` (parsed with `json.JSONDecoder().raw_decode`, not regex) and emits items with `kind: 'video' | 'playlist'`; `get_sepia_results()` calls the SepiaSearch API and only returns videos.
  - Every video item carries `channel`, `channel_link`, `channel_key` (YouTube from `_youtube_channel()` via `browseId`/`canonicalBaseUrl`; Sepia from `_sepia_channel()`, rebuilding the URL from `host` + `name` when needed). `channel_key` is a stable id used to mark "Suscrito" on cards.
  - Playlists come from the legacy `playlistRenderer` (absent in current HTML) and from `lockupViewModel` when `contentType == "LOCKUP_CONTENT_TYPE_PLAYLIST"` (`contentId` starts with `PL`). Their description is the real video count extracted with `VIDEO_COUNT_RE` (`\b\d[\d.,]*\s*videos?\b`) over every string in the lockup.
  - `search_view()` zipper-interleaves the lists, paginates 10/page, and passes `favorite_links` + `playlist_links` + `subscription_keys` so cards show "Guardado" / "Playlist guardada" / "Suscrito".
  - `resolve_view()` resolves a stream server-side and returns JSON with a **signed** stream path (never the provider URL) plus `kind` (`file`|`hls`); for `hls` it signs a `search:hls` URL instead of `search:stream`.
  - `playlist_view()` lists a playlist server-side with yt-dlp and also passes `playlist_data` (`{title, entries}`) so the player can start without re-fetching; `playlist_api_view()` (`GET /api/playlist/?url=`) returns the same shape as JSON for on-demand playback.
  - `stream_view()` proxies bytes with `Range` passthrough and **retries** (re-resolves) up to 2 extra times on upstream 401/403/410 before giving up. `_normalized_content_type()` maps `application/octet-stream` → `video/mp4` so `<video>` accepts it.
  - `hls_view()` proxies HLS: fetches the (signed) target, and if it is an m3u8 it **rewrites every segment/`URI="..."`** into signed `search:hls` links (`_rewrite_hls_playlist`, `_hls_proxy_uri`) so the browser never contacts PeerTube; otherwise it streams the segment with `Range`. `_chain()` prepends the already-read first chunk.
  - `subscriptions_view()` fetches every feed (limit `SUBSCRIPTION_FEED_LIMIT = 12`), tags each entry with `channel`/`channel_key`/`source_type` (`youtube`|`peer`), sorts chronologically (`library.published_sort_key`), applies server-side filters (`?filter=all|youtube|pep` and `?channel=<feed_url>`) and builds `playlist_data` from the visible entries.
  - `import_view()` accepts a multipart file or pasted JSON and merges it via `library.import_config()`.
  - `thumbnail_view()` proxies thumbnails so the browser never touches the CDN (useful when the client's network blocks YouTube/PeerTube image hosts). It is served from `search/thumbnails.py` (disk cache under the system temp dir) and returns a 1×1 transparent GIF on any failure.
  - Favorites/subscriptions/playlists views accept `POST` with `action=add|remove` and redirect back via a validated local `next`.
- `search/player.py`: yt-dlp invocation, SSRF guard (`validate_source_url`), and `django.core.signing` of stream tokens. It forces `--extractor-args youtube:player_client=<YTDLP_PLAYER_CLIENT>` (default `android`): yt-dlp's own default client (`tv_samsung`) yields googlevideo URLs that answer **403**, and `_open_upstream` always sends a `Range` header because googlevideo rejects a bare GET.
  - `extract_media()` runs `_run_ytdlp(FORMAT_SELECTOR)`, then returns `kind: "file"` or `"hls"`. If the chosen format is an m3u8 or `_looks_like_fragmented_mp4()` (PeerTube `-fragmented.mp4` / `/streaming-playlists/hls/`), it re-resolves with `FORMAT_SELECTOR_HLS` and reports `"hls"`.
- `search/thumbnails.py`: downloads thumbnails server-side (`get_thumbnail(url) -> (bytes, content_type)`) with a browser UA + `Referer`, after the same `validate_source_url` SSRF guard. Caches them on disk keyed by `sha256(url)`; tune with `THUMB_CACHE_DIR`, `THUMB_CACHE_TTL` (default 3 days) and `THUMB_CACHE_MAX_FILES` (prunes oldest).
- `search/library.py`: session helpers (`favorites`/`subscriptions`/`playlists` keys), playlist extraction, channel→feed resolution, Atom/RSS-2.0 parsing, plus `channel_key_from_url()`, `subscription_keys()` and `import_config()`.
- `search/static/search/hls.min.js`: vendored hls.js 1.5.20, loaded on demand only for `kind: "hls"` streams.
- Templates: `base.html` (nav, shared CSS, player overlay/JS, HLS support) is extended by `index.html`, `favorites.html`, `subscriptions.html`, `playlist.html`, `import.html`; `_card.html` is the shared card partial.
- `search/` still has no models; only `player.py`, `library.py`, `thumbnails.py`, views, templates, and static.

## Player flow (progressive enhancement)
- Each card renders a `▶ Reproducir aquí` button with the `hidden` attribute. Inline JS in `base.html` removes `hidden` and wires clicks; **without JS the button never shows**, which is intentional.
- Click → `GET /api/resolve/?url=<link>` → JSON `{ok, title, kind, stream}` → JS sets `<video src>` in a modal overlay (or hands the stream to hls.js when `kind === "hls"`).
- The signed token (`STREAM_SALT`, max age 3h) hides the real googlevideo/PeerTube URL and headers from the browser; that is the privacy goal.
- `FORMAT_SELECTOR` prefers a progressive http(s) MP4 ≤720p and **excludes HLS/DASH** because a native `<video>` can't play m3u8. Keep that constraint; YouTube often offers only `18` (360p combined).
- HLS fallback: PeerTube instances with **no progressive MP4** (only HLS) surface a `-fragmented.mp4` that Chrome will not play as a progressive file (it buffers forever, duration stays 0). `extract_media()` detects this and reports `kind: "hls"`; the vendored `hls.min.js` (loaded on demand) plays the rewritten `/api/hls/` playlist. A native-HLS browser (Safari) uses the `<video src>` directly.
- Card thumbnails render as `<img src="/api/thumb/?url=...">` (see UI/theme) so they load through the backend.

## Playlist player (queue in the overlay)
- Buttons with `.playlist-play-btn` open the same overlay but build a queue: either `data-playlist="<playlist url>"` (JS calls `/api/playlist/`, i.e. yt-dlp, then plays entry 0) or `data-embedded="1"` (reads the `<script id="playlist-data" type="application/json">` emitted by `json_script` on `/playlist/` **and** `/subscriptions/`).
- Each entry is resolved on demand through `/api/resolve/` (one yt-dlp call per video); the whole playlist is **not** resolved upfront.
- A `playToken` counter guards against out-of-order resolutions: a late `/api/resolve/` response (or HLS manifest) for an entry the user already skipped past is ignored, and `closePlayer()` bumps it so nothing starts playing after closing. This matters when jumping quickly via the dropdown.
- Overlay has `#player-nav` with `◀ Anterior` / `Siguiente ▶` and a `n / total` counter, plus `#player-list-bar` (a `#player-select` dropdown listing **all** entries), both visible only for queues of 2+. Picking an option jumps straight to that entry (`playIndex`). `video.ended` auto-advances (or loops). Single-video playback reuses the same queue of length 1, so nav/dropdown stay hidden.
- The player closes **only** via the `✕` button (`#player-close`): clicking the backdrop and pressing Escape do nothing (intentional).
- `#player-controls` (Auto / Bucle / Aleatorio) sits under the video for any queue; Auto and Aleatorio hide for a single-video queue. State is persisted in `localStorage` (`truchitube-player-prefs`). **Auto** = advance on end, **Bucle** = wrap around the list (also repeats a single video), **Aleatorio** = shuffle (re-shuffles only the entries after the current one when toggled mid-playback).
- If a video fails (resolve error, a `<video>` media error, or an HLS fatal error), the JS skips to the next entry automatically and says so in `#player-status`; after `failed >= queue.length` consecutive failures it stops. Guarded by `video.dataset.want` so the `error` fired while clearing `src` is ignored. `destroyHls()` is called whenever the source changes.
- On `/playlist/`, each saved playlist row has a plain (no-JS) `Abrir URL` anchor to the real playlist link, `Ver lista` (in-app list) and `Reproducir` (overlay queue).

## UI / theme
- Dark theme lives entirely in `base.html` as CSS variables: `--bg:#0a0a0a` (almost-black), `--panel:#121212`, `--text:#a9b84f` (olive green), `--red:#5e1616` (dark-red border). All corners are square (`border-radius: 0`) by design.
- Site name is **PluriTube 🎥**: `Pluri` uses `--brand-red:#c62828` (a legible variant of the dark-red border, used only for the wordmark) and `Tube` uses `--text-bright`. Brand appears in the nav (`a.brand`) and in the search hero (`h1.brand-title`).
- The search hero tagline is two lines: line 1 `Meta buscador y frontend alternativo para Youtube y PeerTube | Creado y mantenido por Fabricio E. Fernández`; line 2 (`.brand-links`) is `Código fuente | Licencia de uso`, both hyperlinks currently `href="#"` placeholders. The search button is **Buscar** and the input placeholder is `Buscar videos en Youtube y Peertube` (`index.html`).
- Card layout: `_card.html` branches on `item.kind`. `playlist` → "Reproducir playlist" + "Abrir URL" (direct link) + "Ver lista" + "Guardar playlist"; anything else → channel line (`.card-channel`: channel link + subscribe form, or a disabled "✓ Suscrito" when `item.channel_key in subscription_keys`) + "Reproducir aquí" + favorite/remove.
- `item.color` is still set per source (`#ff0000` YouTube, `#859900` SepiaSearch) and used for the card's left border and source badge — do not repurpose it for the theme.
- Nav includes "Exportar" (`search:export`) and "Importar" (`search:import`).
- `subscriptions.html` has a server-side `.filter-bar` (Todos / YouTube / PeerTube / one link per subscribed channel) and a `▶ Reproducir todo como playlist` button backed by `json_script`.

## Session library
- Favorites: `POST /favorites/` with `action=add` + hidden `title/link/thumbnail/source/color`, or `action=remove` + `link`. Deduplicated by `link`.
- Subscriptions: `POST /subscriptions/` with `action=add` + `channel` (YouTube `@handle`, `UC...`, channel URL, or PeerTube `/video-channels/<name>`), or `action=remove` + `feed_url`. Deduplicated by `feed_url`. Each stored subscription also carries `channel_url` + `channel_key`.
  - Cards can subscribe directly: the `_card.html` channel form posts `channel` = the channel URL with the current page as `next`. `library.subscription_keys(session)` returns the set of `channel_key`s for the "Suscrito" state.
  - YouTube channel id is derived from the page HTML via `"externalId"`/`"browseId"` (the old `"channelId"` key is no longer present).
  - YouTube feeds are **Atom** (`feeds/videos.xml?channel_id=`); PeerTube feeds are **RSS 2.0** (`feeds/videos.xml?videoChannelId=`). Both parsers live in `library.py`.
- Subscriptions page: `GET /subscriptions/` reads up to `SUBSCRIPTION_FEED_LIMIT` (12) items per feed, merges + sorts them chronologically, and filters server-side via `?filter=all|youtube|pep` and `?channel=<feed_url>`; the visible list feeds `playlist_data` for the overlay queue.
- Playlist: `GET /playlist/?url=<playlist>` runs `yt-dlp --flat-playlist -J` server-side; the page also lists saved playlists and offers "Guardar esta playlist".
- Saved playlists: `POST /playlists/save/` with `action=add` + hidden `title/link/thumbnail/source/color`, or `action=remove` + `link`. Deduplicated by `link`, stored under session key `playlists` (`library.get_playlists/add_playlist/remove_playlist/playlist_links`).
- Export: `GET /export/` returns JSON `{app, version, exported_at, favorites, subscriptions, playlists}` with `Content-Disposition: attachment; filename="video-searcher-config.json"`.
- Import: `GET/POST /import/` (file upload or pasted JSON) calls `library.import_config(session, payload)`, which **merges/deduplicates** favorites, subscriptions (backfilling `channel_key` from `channel_url`), and playlists; non-`http(s)` links and non-dict items are ignored.
- All of the above lives in `request.session`; no DB models and no cookie-size limit (DB-backed sessions).

## Gotchas
- `get_youtube_results()` depends on YouTube's HTML/JSON shape and its bare `except: return []` swallows every failure silently — a broken scraper looks like "no YouTube results", not an error.
- Playlist detection is query-dependent: YouTube only emits playlist lockups for some searches (e.g. `music mix playlist` returns playlists, `lofi playlist` returns only videos). Zero playlist cards is not necessarily a bug — check `get_youtube_results(query)` and count `kind` values first. Playlist cards may also land on page ≥2 because pagination is 10/page.
- Playlists: `library.extract_playlist()` rewrites classic YouTube Mix URLs (`/playlist?list=RD` + 11-char seed) to `/watch?v=<seed>&list=RD...`, because YouTube refuses to list `playlist?list=RD...` ("This playlist type is unviewable"). Opaque mix ids still fail. `_friendly_playlist_error()` strips yt-dlp `ERROR: [youtube:tab] ...` prefixes and maps common failures (Mix, private, deleted, members-only, anti-bot, HTTP 400) to Spanish messages.
- `playlist_api_view()` returns JSON `{ok:false, error}` (status 400) for **every** failure, not only `PlayerError`; the player shows that text in `#player-status`. `playlist_view()` also catches broad exceptions so a bad playlist can't 500 the page.
- SepiaSearch `thumbnailPath` is often relative (`/...`); the code rebuilds a full URL from the video link's scheme+host via `urlparse`.
- Player client: yt-dlp's default YouTube client (`tv_samsung`) returns googlevideo URLs that answer **403**, so the app forces `android` via `--extractor-args youtube:player_client=` (override with the `YTDLP_PLAYER_CLIENT` env var). `android_vr`/`tv_simply` also work; `ios`/`web`/`web_safari` fail with "Requested format is not available" and `mweb`/`web_embedded` 403. YouTube live streams are HLS and won't play in a native `<video>`.
- Sepia/PeerTube HLS-only instances (`-fragmented.mp4`, `/streaming-playlists/hls/`) can't be played as progressive MP4 even when the proxy serves correct `Range` responses — Chrome buffers to EOF and reports duration 0. This is why `extract_media()` reports `kind: "hls"` and the player uses the vendored `hls.min.js` with the `/api/hls/` m3u8/segment rewriter. Direct playback (no proxy) fails the same way, so it is not a proxy bug.
- Thumbnails go through `/api/thumb/` and are cached in `THUMB_CACHE_DIR` (default `$TMPDIR/video_searcher_thumbs`); any failure returns the 1×1 GIF placeholder, so a "missing" thumbnail usually means the CDN 404'd, not a bug. The endpoint is an image proxy limited by `validate_source_url` (public http(s) hosts only) — no signing, since thumbnails aren't sensitive.
- FFmpeg is **not** used: progressive formats are single files, and HLS is played client-side by hls.js. Requesting a `+`-merged or DASH format would need server-side muxing, which this flow does not do.
- Subscription aggregation fetches every feed on each `GET /subscriptions/` (up to `SUBSCRIPTION_FEED_LIMIT` = 12 items each); with many feeds the page gets slow. There is no cache yet.
- `views.py_backup` and `templates/search/index.html_backup` are stale. Treat live files as truth.
- CSRF behind a proxy: `CSRF_TRUSTED_ORIGINS` has **no global wildcard** (only `https://*.domain`). In `DEBUG`, `core.middleware.DevAnyOriginCsrfMiddleware` rewrites `Origin`/`Referer` to the request host, so any origin passes while the CSRF **token is still verified**. With `DEBUG=False` it does nothing — then set `CSRF_TRUSTED_ORIGINS` to the public origin (and `USE_X_FORWARDED_HOST` / `SECURE_PROXY_SSL_HEADER` if behind TLS).
- `DEBUG = True` and `ALLOWED_HOSTS = ['*']` — dev-only settings, not deployment-ready.

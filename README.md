# PluriTube 🎥

**Meta buscador y frontend alternativo para Youtube y PeerTube | Creado y mantenido por Fabricio E. Fernández**

PluriTube agrega en una sola página los resultados de búsqueda de **YouTube** y **SepiaSearch** (el buscador de PeerTube), tanto vídeos como playlists, y los reproduce en un reproductor propio incrustado sin exponer las URLs reales de los proveedores.

> Código fuente: _pendiente de definir_ · Licencia de uso: _pendiente de definir_

---

## Índice

- [Qué hace](#qué-hace)
- [Características](#características)
- [Stack](#stack)
- [Requisitos](#requisitos)
- [Instalación rápida](#instalación-rápida)
- [Instalación manual](#instalación-manual)
- [Uso](#uso)
- [Rutas](#rutas)
- [Configuración](#configuración)
- [Exportar e importar](#exportar-e-importar)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Tests](#tests)
- [Notas y limitaciones](#notas-y-limitaciones)
- [Créditos](#créditos)

---

## Qué hace

- Busca en **YouTube** (scrapeando `ytInitialData`) y en **SepiaSearch** (API de PeerTube) y mezcla los resultados en una única página paginada.
- Reconoce vídeos y **playlists** de YouTube.
- Muestra el **nombre del canal** en cada tarjeta y permite **suscribirse** a ese canal con un clic.
- Página de **suscripciones** que junta los vídeos de todos los canales seguidos en **orden cronológico**, con filtros por todos / YouTube / PeerTube / canal concreto y un botón para **reproducir todos como playlist**.
- **Reproductor propio** (overlay) con cola, navegación anterior/siguiente, **desplegable con toda la lista** para saltar a cualquier vídeo y controles **Auto / Bucle / Aleatorio**.
- **Favoritos**, **playlists guardadas** y **suscripciones** guardados en la sesión del servidor.
- **Exportación e importación** de toda la configuración en un único JSON.

## Características

- **Sin dependencias de frontend**: HTML, CSS y JavaScript "vanilla" en una sola plantilla base. Los botones de reproducción se activan solo si hay JS (mejora progresiva).
- **Privacidad**: el navegador nunca contacta directamente con YouTube/PeerTube para el vídeo; todo pasa por el backend mediante enlaces firmados (`django.core.signing`).
- **Reproducción de HLS**: las instancias PeerTube que solo ofrecen HLS se reproducen con [hls.js](https://github.com/video-dev/hls.js) vendorizado y un proxy `m3u8`/segmentos propio.
- **Miniaturas proxy**: se sirven a través de `/api/thumb/` con caché en disco, útil si tu red bloquea los CDN de imágenes.
- **Proxy de vídeo con `Range`** y reintentos automáticos ante `401/403/410`.

## Stack

- **Django 6.0.3** sobre **Python 3.12+**
- **SQLite** (base de datos; solo se usa para las sesiones)
- **`requests`** para el scraping/API
- **`yt-dlp`** (ejecutable) para resolver los streams y listar playlists
- **hls.js 1.5.20** (vendorizado en `search/static/search/hls.min.js`)

## Requisitos

- **Python 3.12 o superior** (probado con 3.13.5).
- **`yt-dlp`** como ejecutable en el `PATH` (el instalador lo resuelve por ti).
- **`curl` o `wget`** (para la descarga de respaldo de `yt-dlp`).
- Linux o macOS. En Debian/Ubuntu, si falta `venv`: `sudo apt install python3-venv`.

> No se necesita **FFmpeg**: los formatos reproducidos son ficheros MP4 progresivos o HLS gestionado en el cliente.

## Instalación rápida

El script `install.sh` crea el entorno virtual, instala las dependencias, deja `yt-dlp` disponible y aplica las migraciones:

```bash
chmod +x install.sh
./install.sh
```

Cuando termine:

```bash
source venv/bin/activate
python manage.py runserver
```

Abre <http://127.0.0.1:8000/>.

## Instalación manual

```bash
# 1. Entorno virtual
python3 -m venv venv
source venv/bin/activate

# 2. Dependencias
pip install --upgrade pip
pip install -r requirements.txt

# 3. yt-dlp (la app invoca el ejecutable, no el módulo)
#    Suele quedar instalado por requirements.txt; compruébalo con:
yt-dlp --version
#    Si no aparece, instálalo con:
pip install yt-dlp
#    o descarga el binario oficial y expórtalo con YTDLP_BIN

# 4. Base de datos (sesiones)
python manage.py migrate

# 5. Arrancar
python manage.py runserver
```

## Uso

1. Escribe una búsqueda en la portada y pulsa **Buscar**. Verás resultados de YouTube y PeerTube mezclados.
2. En cada tarjeta puedes:
   - **Reproducir aquí** el vídeo en el reproductor incrustado.
   - **Guardar** el vídeo en favoritos.
   - **Suscribirte** al canal.
   - Si es una playlist: **Reproducir playlist**, **Abrir URL**, **Ver lista** o **Guardar playlist**.
3. En **Suscripciones** verás los vídeos de tus canales en orden cronológico. Filtra por plataforma o canal y pulsa **Reproducir todo como playlist**.
4. En **Playlist** puedes abrir una playlist por URL, ver su lista y guardarla.
5. En **Exportar** descargas tu configuración; en **Importar** la vuelves a cargar (subiendo el fichero o pegando el JSON).

### Reproductor

- Al reproducir una playlist aparece un **desplegable** con todos los vídeos para saltar a cualquiera de ellos.
- **Auto**: avanza al terminar. **Bucle**: repite/da la vuelta a la lista. **Aleatorio**: mezcla el resto de la cola.
- El reproductor se cierra **solo** con la **✕** de la esquina superior derecha.
- Si un vídeo falla, se salta automáticamente al siguiente e informa en el estado.

## Rutas

Todas están definidas en `search/urls.py` (namespace `search`) y montadas en la raíz desde `core/urls.py`.

| Ruta | Método | Descripción |
| --- | --- | --- |
| `/` | GET | Búsqueda y listado paginado (`?q=`) |
| `/favorites/` | GET/POST | Favoritos (`action=add\|remove`) |
| `/subscriptions/` | GET/POST | Suscripciones, listado cronológico, filtros (`?filter=all\|youtube\|pep`, `?channel=`) |
| `/playlist/` | GET | Abrir/ver una playlist (`?url=`) y playlists guardadas |
| `/playlists/save/` | POST | Guardar/quitar playlist guardada |
| `/export/` | GET | Descargar `video-searcher-config.json` |
| `/import/` | GET/POST | Importar configuración (fichero o JSON pegado) |
| `/api/resolve/` | GET | Resolver un vídeo → JSON con stream firmado y `kind` (`file`/`hls`) |
| `/api/playlist/` | GET | Listar entradas de una playlist en JSON para el reproductor |
| `/api/stream/` | GET | Proxy de vídeo progresivo con `Range` y reintentos |
| `/api/hls/` | GET | Proxy de HLS: reescribe `m3u8` y sirve segmentos |
| `/api/thumb/` | GET | Proxy + caché de miniaturas (`?url=`) |
| `/admin/` | — | Administración de Django (no se usa en la app) |

## Configuración

Variables de entorno opcionales:

| Variable | Por defecto | Descripción |
| --- | --- | --- |
| `YTDLP_BIN` | `yt-dlp` en `PATH`, si no `/usr/bin/yt-dlp` | Ruta al ejecutable `yt-dlp` |
| `YTDLP_PLAYER_CLIENT` | `android` | Cliente de YouTube para yt-dlp. El de por defecto (`tv_samsung`) devuelve URLs que dan 403 |
| `THUMB_CACHE_DIR` | `$TMPDIR/video_searcher_thumbs` | Directorio de caché de miniaturas |
| `THUMB_CACHE_TTL` | `259200` (3 días) | Vida de la caché de miniaturas, en segundos |
| `THUMB_CACHE_MAX_FILES` | `1000` | Nº máximo de miniaturas en caché (poda las más antiguas) |

> Ajustes de desarrollo: `DEBUG=True` y `ALLOWED_HOSTS=['*']`. **No es una configuración lista para producción**: para desplegar, define `DEBUG=False`, un `SECRET_KEY` propio, `ALLOWED_HOSTS` y `CSRF_TRUSTED_ORIGINS`. El middleware `DevAnyOriginCsrfMiddleware` solo actúa con `DEBUG=True`.

## Exportar e importar

**Exportar** (`/export/`) genera un JSON con:

```json
{
  "app": "video_searcher",
  "version": 1,
  "exported_at": "…",
  "favorites": [],
  "subscriptions": [],
  "playlists": []
}
```

**Importar** (`/import/`) fusiona ese JSON con lo que ya tengas, deduplicando por enlace/feed. Acepta un fichero `.json` o el contenido pegado. Solo se aceptan enlaces `http(s)`.

## Estructura del proyecto

```
video_searcher/
├── core/                      # Configuración del proyecto Django
│   ├── settings.py
│   ├── urls.py
│   ├── middleware.py          # CSRF para desarrollo con proxy
│   ├── asgi.py / wsgi.py
├── search/                    # Aplicación principal
│   ├── views.py               # Búsqueda, reproductor, HLS, suscripciones, import/export
│   ├── player.py              # yt-dlp, tokens firmados, guardia SSRF
│   ├── library.py             # Sesión: favoritos/suscripciones/playlists, feeds RSS
│   ├── thumbnails.py          # Proxy + caché de miniaturas
│   ├── urls.py
│   ├── tests.py               # Tests offline
│   ├── static/search/
│   │   └── hls.min.js         # hls.js vendorizado
│   └── templates/search/
│       ├── base.html          # Tema, nav, reproductor y JS
│       ├── _card.html         # Tarjeta compartida
│       ├── index.html
│       ├── favorites.html
│       ├── subscriptions.html
│       ├── playlist.html
│       └── import.html
├── install.sh                 # Instalador
├── requirements.txt
├── manage.py
├── AGENTS.md                  # Notas técnicas para agentes/desarrolladores
├── .gitignore
└── README.md
```

## Tests

Los tests son **offline** (no hacen peticiones de red):

```bash
source venv/bin/activate
python manage.py test
```

Actualmente: **48 tests OK**. Verificación adicional de configuración:

```bash
python manage.py check
```

## Notas y limitaciones

- **Sin modelos de base de datos**: favoritos, suscripciones y playlists viven en `request.session`. La base de datos solo guarda sesiones (requiere `migrate`).
- El **scraper de YouTube** depende del HTML/JSON interno de YouTube; si cambia su estructura, los resultados de YouTube pueden aparecer vacíos sin dar error.
- La **detección de playlists** depende de la búsqueda: no todas las consultas devuelven playlists.
- **PeerTube solo-HLS**: algunas instancias entregan un `-fragmented.mp4` que el navegador no reproduce como MP4 progresivo; por eso se usa hls.js y el proxy `/api/hls/`.
- **Sin FFmpeg**: no se mezclan pistas de audio/vídeo separadas ni se soporta DASH.
- La página de suscripciones consulta todos los feeds en cada visita (hasta 12 vídeos por feed); con muchos canales puede tardar.
- Los enlaces **Código fuente** y **Licencia de uso** del sitio son marcadores pendientes de definir.

## Créditos

Creado y mantenido por **Fabricio E. Fernández**.

Reproductor HLS gracias a [hls.js](https://github.com/video-dev/hls.js) (Apache-2.0). Resolución de vídeos gracias a [yt-dlp](https://github.com/yt-dlp/yt-dlp).
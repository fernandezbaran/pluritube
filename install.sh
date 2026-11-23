#!/usr/bin/env bash
#
# PluriTube · script de instalación
#
# Prepara todo lo necesario para ejecutar la aplicación:
#   - entorno virtual de Python (venv/)
#   - dependencias de requirements.txt (Django, requests, yt-dlp, ...)
#   - el ejecutable `yt-dlp` (imprescindible para resolver los vídeos)
#   - la base de datos SQLite con las migraciones aplicadas
#
# Uso:
#   chmod +x install.sh
#   ./install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-venv}"

echo "==> PluriTube · instalación"
echo "    directorio: $SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Comprobar Python
# ---------------------------------------------------------------------------
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Error: no se encontró '$PYTHON'." >&2
    echo "Instala Python 3.12 o superior y vuelve a intentarlo." >&2
    exit 1
fi

if ! "$PYTHON" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
then
    echo "Error: se requiere Python 3.12 o superior (tienes $("$PYTHON" -V 2>&1))." >&2
    exit 1
fi
echo "    Python: $("$PYTHON" -V 2>&1)"

# ---------------------------------------------------------------------------
# 2. Entorno virtual
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creando el entorno virtual en '$VENV_DIR'"
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "==> Reutilizando el entorno virtual existente en '$VENV_DIR'"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ---------------------------------------------------------------------------
# 3. Dependencias de Python
# ---------------------------------------------------------------------------
echo "==> Actualizando pip"
python -m pip install --upgrade pip || true

echo "==> Instalando dependencias (requirements.txt)"
python -m pip install -r requirements.txt

# ---------------------------------------------------------------------------
# 4. Ejecutable yt-dlp
#    La app invoca el binario `yt-dlp`, no el módulo de Python.
# ---------------------------------------------------------------------------
if command -v yt-dlp >/dev/null 2>&1; then
    echo "==> yt-dlp ya disponible: $(command -v yt-dlp)"
else
    echo "==> Instalando yt-dlp en el entorno virtual"
    python -m pip install --upgrade yt-dlp || true
fi

if ! command -v yt-dlp >/dev/null 2>&1; then
    echo "==> Descargando el binario standalone de yt-dlp en '$VENV_DIR/bin'"
    mkdir -p "$VENV_DIR/bin"
    URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
    if command -v curl >/dev/null 2>&1; then
        curl -fL "$URL" -o "$VENV_DIR/bin/yt-dlp"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$VENV_DIR/bin/yt-dlp" "$URL"
    else
        echo "Error: necesitas 'curl' o 'wget' para descargar yt-dlp." >&2
        exit 1
    fi
    chmod +x "$VENV_DIR/bin/yt-dlp"
fi

if ! command -v yt-dlp >/dev/null 2>&1; then
    echo "Error: no se pudo dejar 'yt-dlp' disponible en el PATH." >&2
    echo "Exporta YTDLP_BIN=/ruta/a/yt-dlp y vuelve a intentarlo." >&2
    exit 1
fi
echo "    yt-dlp: $(command -v yt-dlp)"

# ---------------------------------------------------------------------------
# 5. Base de datos y comprobación de Django
# ---------------------------------------------------------------------------
echo "==> Aplicando migraciones"
python manage.py migrate

echo "==> Comprobando la configuración de Django"
python manage.py check

# ---------------------------------------------------------------------------
# 6. Listo
# ---------------------------------------------------------------------------
echo
echo "==> Instalación completada."
echo
echo "Para arrancar la aplicación:"
echo "    source $VENV_DIR/bin/activate"
echo "    python manage.py runserver"
echo
echo "Luego abre http://127.0.0.1:8000/ en el navegador."
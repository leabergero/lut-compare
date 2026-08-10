#!/bin/bash
# Lanzador de LUT Compare.app (CFBundleExecutable).
# La app vive en /Applications (solo lectura): el venv y los presets
# propios del usuario van a ~/Library/Application Support/LUT Compare.
# En el primer arranque crea el venv con las dependencias pip (necesita
# internet).
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
SUP="$HOME/Library/Application Support/LUT Compare"
VENV="$SUP/venv"
mkdir -p "$SUP"

# Python 3.10+: PATH primero, luego rutas tipicas de Homebrew/python.org
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY="$p"
        break
    fi
done
if [ -z "$PY" ]; then
    osascript -e 'display alert "LUT Compare" message "Se necesita Python 3.10 o superior. Se abrirá la página de descarga; instalalo y volvé a abrir LUT Compare." as critical' >/dev/null 2>&1
    open "https://www.python.org/downloads/macos/"
    exit 1
fi

# Qt6 tiene minimo de macOS por version (visto con PyQt6 en market-ticker:
# 6.7->11, 6.8->12, 6.9->13 — la app moria con "Qt requires macOS N or
# later" pese a que el wheel se anuncia como macosx_11_0). PySide6 es el
# mismo Qt6 por debajo asi que hereda el mismo riesgo; esta tabla es la de
# PyQt6, NO verificada especificamente con PySide6 en un Mac real todavia.
OSMAJ="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
QT_PIN=()
case "$OSMAJ" in
    11) QT_PIN=("PySide6>=6.7,<6.8");;
    12) QT_PIN=("PySide6>=6.7,<6.9");;
esac
if [ -n "$OSMAJ" ] && [ "$OSMAJ" -lt 11 ] 2>/dev/null; then
    osascript -e 'display alert "LUT Compare" message "Se necesita macOS 11 (Big Sur) o superior." as critical' >/dev/null 2>&1
    exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
    osascript -e 'display notification "Instalando dependencias (solo la primera vez, puede tardar unos minutos)…" with title "LUT Compare"' >/dev/null 2>&1
    if ! { "$PY" -m venv "$VENV" \
           && "$VENV/bin/pip" install --quiet --upgrade pip \
           && "$VENV/bin/pip" install --quiet -r "$RES/requirements.txt" "${QT_PIN[@]}"; } >>"$SUP/lut-compare.log" 2>&1; then
        rm -rf "$VENV"
        osascript -e 'display alert "LUT Compare" message "Falló la instalación de dependencias. Revisá tu conexión a internet y volvé a abrir la app." as critical' >/dev/null 2>&1
        exit 1
    fi
fi

exec "$VENV/bin/python" "$RES/app.py"

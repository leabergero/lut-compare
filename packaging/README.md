# Empaquetado

Los 3 instaladores se generan desde Linux (no cross-compila Python, así que
en Windows/macOS el venv con las dependencias se crea en el primer
arranque — mismo patrón probado en `market-ticker`):

```bash
./packaging/build-deb.sh   # release/lut-compare_<version>_all.deb
./packaging/build-msi.sh   # release/lut-compare-<version>.msi
./packaging/build-pkg.sh   # release/lut-compare-<version>.pkg
```

La versión sale de `setup.py` (`version="..."`) en los 3 scripts.

## Herramientas que usan

- **build-deb.sh**: `dpkg-deb` (ya en el sistema).
- **build-msi.sh**: `wixl` + `msibuild` (msitools). Si no están en el PATH,
  usa lo extraído en `~/.local/opt/msitools/root` (`apt-get download wixl
  msitools libmsi-1.0-0 libgsf-1-114 libgsf-1-common libgcab-1.0-0` +
  `dpkg -x` + `LD_LIBRARY_PATH`, sin sudo).
- **build-pkg.sh**: `mkbom` (bomutils, compilado desde
  github.com/hogliux/bomutils, persiste en `~/.local/opt/bomutils/bin`) +
  `packaging/lib/mkxar.py` (empaquetador xar propio — no hay paquete xar
  para Linux moderno).

## Sin probar en máquina real

El `.deb` se probó de punta a punta acá (build, instalación del venv,
arranque de la app). El `.msi` y el `.pkg` sólo se pudieron validar
**estructuralmente** (`msidump`, `bsdtar`/`dumpbom`) — nunca instalados en
Windows/macOS reales. Puntos a confirmar cuando haya una máquina real:

- **macOS**: el pin de versión de Qt en `macos/LutCompare.sh` (PySide6 por
  macOS) está copiado de la tabla que se verificó con PyQt6 en
  market-ticker, no con PySide6 — puede necesitar ajuste. El `icon.icns`
  se generó a mano (`lib/make_icns.py`, sin `iconutil`) — nunca se lo vio
  render izado en un Mac real.
- **Windows**: el flujo entero (`launcher.vbs` → `setup.bat` → venv →
  arranque) es el mismo que ya se probó en Mac/Windows reales para
  market-ticker, pero no se repitió la prueba acá.

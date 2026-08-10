#!/bin/bash
# Genera el instalador .pkg de macOS (product archive plano) desde Linux.
# Instala "LUT Compare.app" en /Applications; el venv se crea en el
# primer arranque (Python no cross-compila, ver LutCompare.sh).
#
# Un .pkg plano es un xar con Distribution + <id>.pkg/{Payload,Bom,PackageInfo}:
#  - Payload: cpio odc gzip con la raiz a instalar
#  - Bom: lo genera mkbom (bomutils, github.com/hogliux/bomutils)
#  - xar: no hay paquete para Linux moderno -> packaging/lib/mkxar.py
# Exportar MKBOM si mkbom no esta en el PATH.
set -e

IDENT="com.bergero.lutcompare"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/.*version="\([^"]*\)".*/\1/p' "$PROJECT_DIR/setup.py")"
STAGE="$PROJECT_DIR/installers/.build-pkg"
OUT="$PROJECT_DIR/installers/lut-compare-${VERSION}.pkg"
MKBOM="${MKBOM:-mkbom}"
if ! command -v "$MKBOM" >/dev/null 2>&1 && [ -x "$HOME/.local/opt/bomutils/bin/mkbom" ]; then
    MKBOM="$HOME/.local/opt/bomutils/bin/mkbom"
fi

rm -rf "$STAGE"
ROOT="$STAGE/root"
APP="$ROOT/Applications/LUT Compare.app"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES"

# ---- bundle de la app ----
cp "$PROJECT_DIR/packaging/macos/LutCompare.sh" "$APP/Contents/MacOS/LutCompare"
chmod 755 "$APP/Contents/MacOS/LutCompare"

cat > "$APP/Contents/Info.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>LUT Compare</string>
  <key>CFBundleDisplayName</key><string>LUT Compare</string>
  <key>CFBundleIdentifier</key><string>$IDENT</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>LutCompare</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
EOF

cp "$PROJECT_DIR/app.py" "$PROJECT_DIR/preset_engine.py" "$PROJECT_DIR/requirements.txt" "$RES/"
cp -r "$PROJECT_DIR/presets" "$RES/presets"
cp "$PROJECT_DIR/packaging/assets/icon.icns" "$RES/icon.icns"

# ---- componentes del pkg plano ----
PKGDIR="$STAGE/flat/lut-compare.pkg"
mkdir -p "$PKGDIR"

(cd "$ROOT" && find . | cpio -o --format odc --owner 0:0 2>/dev/null | gzip -9) > "$PKGDIR/Payload"
"$MKBOM" -u 0 -g 0 "$ROOT" "$PKGDIR/Bom"

# postinstall: primer arranque automatico al terminar la instalacion. El
# instalador corre como root -> abrir la app como el usuario logueado; el
# primer arranque de la app crea el venv (LutCompare.sh), asi que aca no
# se duplica esa logica. Nunca abortar la instalacion por esto (exit 0).
SCRIPTS="$STAGE/scripts"
mkdir -p "$SCRIPTS"
cat > "$SCRIPTS/postinstall" << 'EOF'
#!/bin/bash
APP="/Applications/LUT Compare.app"
CUSER="$(stat -f%Su /dev/console 2>/dev/null)"
if [ -n "$CUSER" ] && [ "$CUSER" != "root" ]; then
    CUID="$(id -u "$CUSER" 2>/dev/null)"
    launchctl asuser "$CUID" sudo -u "$CUSER" -H open "$APP" >/dev/null 2>&1 \
        || sudo -u "$CUSER" -H open "$APP" >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 755 "$SCRIPTS/postinstall"
(cd "$SCRIPTS" && find . | cpio -o --format odc --owner 0:0 2>/dev/null | gzip -9) > "$PKGDIR/Scripts"

NFILES=$(find "$ROOT" | wc -l)
KBYTES=$(du -sk "$ROOT" | cut -f1)

cat > "$PKGDIR/PackageInfo" << EOF
<?xml version="1.0" encoding="utf-8"?>
<pkg-info format-version="2" identifier="$IDENT" version="$VERSION" install-location="/" auth="root">
  <payload installKBytes="$KBYTES" numberOfFiles="$NFILES"/>
  <scripts>
    <postinstall file="./postinstall"/>
  </scripts>
  <bundle-version>
    <bundle CFBundleShortVersionString="$VERSION" CFBundleVersion="$VERSION" id="$IDENT" path="./Applications/LUT Compare.app"/>
  </bundle-version>
</pkg-info>
EOF

cat > "$STAGE/flat/Distribution" << EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
  <title>LUT Compare</title>
  <options customize="never" require-scripts="false" rootVolumeOnly="true"/>
  <domains enable_localSystem="true"/>
  <choices-outline>
    <line choice="default">
      <line choice="$IDENT"/>
    </line>
  </choices-outline>
  <choice id="default"/>
  <choice id="$IDENT" visible="false">
    <pkg-ref id="$IDENT"/>
  </choice>
  <pkg-ref id="$IDENT" version="$VERSION" onConclusion="none">#lut-compare.pkg</pkg-ref>
  <pkg-ref id="$IDENT">
    <payload installKBytes="$KBYTES" numberOfFiles="$NFILES"/>
  </pkg-ref>
</installer-gui-script>
EOF

# ---- empaquetar en xar ----
python3 "$PROJECT_DIR/packaging/lib/mkxar.py" "$STAGE/flat" "$OUT"
rm -rf "$STAGE"
echo
echo "PKG generado: $OUT"

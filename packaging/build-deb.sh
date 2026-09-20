#!/bin/bash
# Genera el paquete .deb de LUT Compare para Ubuntu/Debian.
# La app se instala en /opt/lut-compare; el postinst crea el venv con las
# dependencias pip (requiere internet al instalar, como en market-ticker:
# Debian bloquea pip fuera de un venv). Config y presets propios del
# usuario van a su $HOME via el boton "Cambiar carpeta" de la app.
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# La version vive en setup.py (setup(..., version=...)): unica fuente de verdad
VERSION="$(sed -n 's/.*version="\([^"]*\)".*/\1/p' "$PROJECT_DIR/setup.py")"
PKG="$PROJECT_DIR/installers/.build-deb"
OUT="$PROJECT_DIR/installers/lut-compare_${VERSION}_all.deb"

rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" \
         "$PKG/opt/lut-compare" \
         "$PKG/usr/bin" \
         "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/256x256/apps"

# ---- archivos de la app ----
cp "$PROJECT_DIR/app.py" "$PROJECT_DIR/preset_engine.py" "$PKG/opt/lut-compare/"
cp -r "$PROJECT_DIR/presets" "$PKG/opt/lut-compare/presets"
cp "$PROJECT_DIR/requirements.txt" "$PKG/opt/lut-compare/requirements.txt"
cp "$PROJECT_DIR/packaging/assets/icon.png" "$PKG/opt/lut-compare/icon.png"

# ---- lanzador ----
cat > "$PKG/usr/bin/lut-compare" << 'EOF'
#!/bin/bash
# Lanzador de LUT Compare: usa el venv armado por postinst
exec /opt/lut-compare/venv/bin/python /opt/lut-compare/app.py "$@"
EOF
chmod 755 "$PKG/usr/bin/lut-compare"

# ---- entrada de menu + icono ----
cat > "$PKG/usr/share/applications/lut-compare.desktop" << 'EOF'
[Desktop Entry]
Type=Application
Name=LUT Compare
Comment=Compara fotos con presets de Lightroom (.xmp) y LUTs (.cube)
Comment[en]=Compare photos with Lightroom presets (.xmp) and LUTs (.cube)
Exec=lut-compare
Icon=lut-compare
StartupWMClass=lut-compare
Terminal=false
Categories=Graphics;Photography;
Keywords=lut;xmp;lightroom;fotografia;color;raw;
EOF
cp "$PROJECT_DIR/packaging/assets/icon.png" "$PKG/usr/share/icons/hicolor/256x256/apps/lut-compare.png"

# ---- metadatos DEBIAN ----
cat > "$PKG/DEBIAN/control" << EOF
Package: lut-compare
Version: $VERSION
Section: graphics
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-venv, python3-pip, libxcb-cursor0, libxkbcommon-x11-0, libgl1
Maintainer: Leandro R. Bergero <estudiocontablebergero@gmail.com>
Homepage: https://github.com/leabergero/lut-compare
Description: Compara fotos con presets de Lightroom y LUTs
 Vista fundida en tercios entre la foto original y hasta 2 presets
 (.xmp) o LUTs 3D (.cube), zoom sincronizado, soporte RAW, favoritos
 y guardado individual o por lote.
EOF

cat > "$PKG/DEBIAN/postinst" << 'EOF'
#!/bin/bash
set -e
echo "LUT Compare: instalando dependencias Python (puede tardar un minuto)..."
python3 -m venv /opt/lut-compare/venv
/opt/lut-compare/venv/bin/pip install --quiet --upgrade pip
/opt/lut-compare/venv/bin/pip install --quiet -r /opt/lut-compare/requirements.txt
echo "LUT Compare instalado. Buscalo en el menu de aplicaciones."
EOF
chmod 755 "$PKG/DEBIAN/postinst"

cat > "$PKG/DEBIAN/prerm" << 'EOF'
#!/bin/bash
rm -rf /opt/lut-compare/venv
exit 0
EOF
chmod 755 "$PKG/DEBIAN/prerm"

# ---- construir ----
dpkg-deb --build --root-owner-group "$PKG" "$OUT"
rm -rf "$PKG"
echo
echo "Paquete generado: $OUT"
dpkg-deb --info "$OUT" | head -15

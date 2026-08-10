#!/bin/bash
# Genera el instalador .msi de LUT Compare para Windows usando wixl
# (msitools), desde Linux. La app se instala por-usuario (sin UAC) en
# %LocalAppData%\Programs\LUT Compare con acceso directo en el menu
# inicio; el venv con las dependencias pip se crea en el primer arranque
# (setup.bat via launcher.vbs), porque un MSI no puede correr pip de
# forma fiable durante la instalacion y Python no cross-compila.
#
# Requiere wixl + msibuild (msitools) y uuidgen. Si no estan en el PATH,
# exportar WIXL/MSIBUILD, o dejar que use lo ya extraido en
# ~/.local/opt/msitools/root (ver market-ticker/installers/build_msi.sh).
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/.*version="\([^"]*\)".*/\1/p' "$PROJECT_DIR/setup.py")"
STAGE="$PROJECT_DIR/installers/.build-msi"
OUT="$PROJECT_DIR/installers/lut-compare-${VERSION}.msi"
UPGRADE_CODE="AE29EB26-8627-43DC-80FD-8485ECEC3455"   # fijo: identifica el producto entre versiones
NAMESPACE="@url"

MSITOOLS_ROOT="$HOME/.local/opt/msitools/root"
WIXL="${WIXL:-wixl}"
MSIBUILD="${MSIBUILD:-msibuild}"
if ! command -v "$WIXL" >/dev/null 2>&1 && [ -x "$MSITOOLS_ROOT/usr/bin/wixl" ]; then
    export LD_LIBRARY_PATH="$MSITOOLS_ROOT/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"
    WIXL="$MSITOOLS_ROOT/usr/bin/wixl"
    MSIBUILD="$MSITOOLS_ROOT/usr/bin/msibuild"
fi

cguid() { uuidgen --sha1 --namespace "$NAMESPACE" --name "lut-compare/msi/$1" | tr a-z A-Z; }

rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "$PROJECT_DIR/app.py" "$PROJECT_DIR/preset_engine.py" "$PROJECT_DIR/requirements.txt" \
   "$PROJECT_DIR/icon.ico" "$STAGE/"
cp "$PROJECT_DIR"/packaging/windows/{launcher.vbs,setup.bat,INSTRUCCIONES.txt} "$STAGE/"
cp -r "$PROJECT_DIR/presets" "$STAGE/presets"

# ---- arbol de presets (con subcarpetas) generado dinamicamente ----
TREE="$(python3 "$PROJECT_DIR/packaging/lib/wxs_tree.py" "$STAGE/presets" "lut-compare/msi/presets" "$NAMESPACE" "presets")"
PRESETS_XML="${TREE%$'\n'---REFS---*}"
PRESETS_REFS="$(printf '%s\n' "$TREE" | sed -n '/---REFS---/,$p' | tail -n +2)"

# ---- definicion WiX ----
cat > "$STAGE/lut-compare.wxs" << EOF
<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="LUT Compare" Manufacturer="Leandro R. Bergero"
           Version="$VERSION" Language="3082" UpgradeCode="$UPGRADE_CODE">
    <Package Description="Compara fotos con presets de Lightroom y LUTs"
             Comments="github.com/leabergero" InstallerVersion="200"
             Compressed="yes" InstallScope="perUser"/>
    <MajorUpgrade DowngradeErrorMessage="Ya hay una version mas nueva instalada."/>
    <Media Id="1" Cabinet="app.cab" EmbedCab="yes"/>

    <Icon Id="icon.ico" SourceFile="icon.ico"/>
    <Property Id="ARPPRODUCTICON" Value="icon.ico"/>
    <Property Id="ARPURLINFOABOUT" Value="https://github.com/leabergero"/>
    <Property Id="ARPNOMODIFY" Value="1"/>

    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramMenuFolder"/>
      <Directory Id="LocalAppDataFolder">
        <Directory Id="LADPrograms" Name="Programs">
          <Directory Id="INSTALLDIR" Name="LUT Compare">

            <Component Id="AppPy" Guid="$(cguid app.py)">
              <File Id="app.py" Name="app.py" Source="app.py"/>
            </Component>
            <Component Id="PresetEnginePy" Guid="$(cguid preset_engine.py)">
              <File Id="preset_engine.py" Name="preset_engine.py" Source="preset_engine.py"/>
            </Component>
            <Component Id="LauncherVbs" Guid="$(cguid launcher.vbs)">
              <File Id="launcher.vbs" Name="launcher.vbs" Source="launcher.vbs"/>
              <Shortcut Id="StartMenuShortcut" Directory="ProgramMenuFolder"
                        Name="LUT Compare" WorkingDirectory="INSTALLDIR"
                        Target="[INSTALLDIR]launcher.vbs" Icon="icon.ico"
                        Description="Compara fotos con presets de Lightroom y LUTs"/>
            </Component>
            <Component Id="SetupBat" Guid="$(cguid setup.bat)">
              <File Id="setup.bat" Name="setup.bat" Source="setup.bat"/>
            </Component>
            <Component Id="Reqs" Guid="$(cguid requirements.txt)">
              <File Id="requirements.txt" Name="requirements.txt" Source="requirements.txt"/>
            </Component>
            <Component Id="IconIco" Guid="$(cguid icon.ico)">
              <File Id="icon.ico" Name="icon.ico" Source="icon.ico"/>
            </Component>
            <Component Id="Instrucciones" Guid="$(cguid INSTRUCCIONES.txt)">
              <File Id="INSTRUCCIONES.txt" Name="INSTRUCCIONES.txt" Source="INSTRUCCIONES.txt"/>
            </Component>

            <Directory Id="PresetsDir" Name="presets">
$PRESETS_XML
            </Directory>

          </Directory>
        </Directory>
      </Directory>
    </Directory>

    <!-- Primer arranque automatico al terminar la instalacion (solo en
         instalacion nueva, no en upgrades): lanza launcher.vbs, que sin
         venv todavia corre setup.bat en consola visible. OJO: wixl no
         sabe generar custom actions tipo 34 (Directory + ExeCommand), asi
         que aca solo va la secuencia; la fila de CustomAction se importa
         despues con msibuild (ver el final). -->
    <InstallExecuteSequence>
      <Custom Action="LaunchApp" After="InstallFinalize">NOT Installed AND NOT REMOVE AND NOT WIX_UPGRADE_DETECTED</Custom>
    </InstallExecuteSequence>

    <Feature Id="Complete" Level="1">
      <ComponentRef Id="AppPy"/>
      <ComponentRef Id="PresetEnginePy"/>
      <ComponentRef Id="LauncherVbs"/>
      <ComponentRef Id="SetupBat"/>
      <ComponentRef Id="Reqs"/>
      <ComponentRef Id="IconIco"/>
      <ComponentRef Id="Instrucciones"/>
$PRESETS_REFS
    </Feature>
  </Product>
</Wix>
EOF

# ---- construir ----
(cd "$STAGE" && "$WIXL" -v -o "$OUT" lut-compare.wxs)

# ---- custom action LaunchApp (tipo 34+192 = exe en INSTALLDIR, asyncNoWait):
# wixl no la puede generar, se importa la tabla con msibuild (msitools).
printf 'Action\tType\tSource\tTarget\tExtendedType\ns72\ti2\tS72\tS255\tI4\nCustomAction\tAction\nLaunchApp\t226\tINSTALLDIR\twscript.exe "[INSTALLDIR]launcher.vbs"\t\n' \
    > "$STAGE/CustomAction.idt"
(cd "$STAGE" && "$MSIBUILD" "$OUT" -i CustomAction.idt)
rm -rf "$STAGE"
echo
echo "MSI generado: $OUT"

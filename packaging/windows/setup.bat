@echo off
REM Primer arranque de LUT Compare: crea el venv con las dependencias.
REM Lo invoca launcher.vbs cuando todavia no existe venv\.
setlocal
cd /d "%~dp0"
title LUT Compare - instalacion inicial
echo.
echo  LUT Compare: preparando el entorno (solo la primera vez)...
echo.

REM Buscar un Python real (el alias de Microsoft Store falla al ejecutar codigo)
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
    echo  [X] No se encontro Python 3. Se abrira la pagina de descarga.
    echo      IMPORTANTE: al instalarlo, marcar "Add python.exe to PATH".
    echo      Despues volve a abrir LUT Compare desde el menu inicio.
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo  [X] Se necesita Python 3.10 o superior.
    echo      Actualizalo desde https://www.python.org/downloads/
    pause
    exit /b 1
)

echo  Creando entorno virtual...
%PY% -m venv venv
if errorlevel 1 (
    echo  [X] Fallo la creacion del entorno virtual.
    pause
    exit /b 1
)
echo  Instalando dependencias (puede tardar unos minutos)...
venv\Scripts\python.exe -m pip install --upgrade pip --quiet
venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo  [X] Fallo la instalacion de dependencias. Revisa tu conexion a internet,
    echo      cerra esta ventana y volve a abrir LUT Compare para reintentar.
    rmdir /s /q venv 2>nul
    pause
    exit /b 1
)
REM Verificar que todo quedo importable (una instalacion a medias
REM hacia que la app "abra y no haga nada" sin ningun mensaje)
venv\Scripts\python.exe -c "import PySide6.QtWidgets, numpy, PIL, pillow_heif" >nul 2>nul
if errorlevel 1 (
    echo  [X] Las dependencias quedaron incompletas. Se reintentara la
    echo      instalacion la proxima vez que abras LUT Compare.
    rmdir /s /q venv 2>nul
    pause
    exit /b 1
)

echo.
echo  [OK] Listo. Iniciando LUT Compare...
start "" wscript "%~dp0launcher.vbs"
exit /b 0

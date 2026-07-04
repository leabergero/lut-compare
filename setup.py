"""Empaquetado de LUT Compare.

Instalador MSI para Windows:  python setup.py bdist_msi
El .msi queda en dist\. Instala por usuario (sin admin) y crea acceso
directo en el menu Inicio.
"""

from cx_Freeze import Executable, setup

build_exe_options = {
    "includes": ["preset_engine"],
    "packages": ["numpy", "PIL", "pillow_heif", "rawpy"],
    "excludes": [
        "tkinter", "unittest", "pydoc_data", "test", "distutils", "setuptools",
        "matplotlib", "debugpy", "cryptography", "IPython", "ipykernel",
        "jedi", "parso", "pygments", "zmq", "tornado", "psutil", "OpenSSL",
        "jupyter_client", "jupyter_core", "traitlets", "colorama",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQuickWidgets", "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
        "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets", "PySide6.QtRemoteObjects",
        "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtSvgWidgets",
        "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebSockets",
    ],
    "include_files": [
        ("presets", "presets"),
        ("README.md", "README.md"),
    ],
    "include_msvcr": True,
}

bdist_msi_options = {
    "upgrade_code": "{7E4B2C11-52A9-4E86-9C3D-1FDA6B90210E}",
    "all_users": False,
    "add_to_path": False,
    "initial_target_dir": r"[LocalAppDataFolder]\Programs\LUT Compare",
    "install_icon": "icon.ico",
}

setup(
    name="LUT Compare",
    version="1.2.0",
    description="Compara fotos con presets de Lightroom (.xmp) y LUTs (.cube)",
    author="Lea",
    options={"build_exe": build_exe_options, "bdist_msi": bdist_msi_options},
    executables=[
        Executable(
            "app.py",
            base="gui",
            target_name="LUT Compare.exe",
            icon="icon.ico",
            shortcut_name="LUT Compare",
            shortcut_dir="ProgramMenuFolder",
        )
    ],
)

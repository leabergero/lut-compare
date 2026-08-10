#!/usr/bin/env python3
"""Genera un .icns a partir de un .ico, sin depender de iconutil (macOS) ni
de icnsutils (no instalado y sin sudo interactivo aca): el formato icns
moderno son bloques TLV con PNG embebido tal cual, asi que alcanza con
reescalar cada tamano con Pillow y armar el contenedor a mano."""
import struct
import sys
from pathlib import Path

from PIL import Image

# tipo icns -> lado en pixeles (subset suficiente para un icono chico de app)
SIZES = {"icp4": 16, "icp5": 32, "icp6": 64, "ic07": 128, "ic08": 256, "ic09": 512}


def make_icns(src, dest):
    base = Image.open(src).convert("RGBA")
    native = max(base.size)
    entries = []
    for tag, side in SIZES.items():
        if side > native * 2:  # no upscalear mas del doble del original
            continue
        im = base.resize((side, side), Image.LANCZOS)
        import io
        buf = io.BytesIO()
        im.save(buf, "PNG")
        entries.append((tag.encode("ascii"), buf.getvalue()))

    body = b"".join(tag + struct.pack(">I", len(data) + 8) + data for tag, data in entries)
    header = b"icns" + struct.pack(">I", len(body) + 8)
    Path(dest).write_bytes(header + body)


if __name__ == "__main__":
    make_icns(sys.argv[1], sys.argv[2])
    print(f"icns generado: {sys.argv[2]} ({Path(sys.argv[2]).stat().st_size} bytes)")

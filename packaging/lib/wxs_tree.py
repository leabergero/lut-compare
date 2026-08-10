#!/usr/bin/env python3
"""Genera el fragmento WiX (Directory/Component/File anidados) para una
carpeta arbitraria de datos (los presets, que ahora pueden tener
subcarpetas). Evita escribir el .wxs a mano por cada archivo/carpeta.

Uso: wxs_tree.py <carpeta> <id_prefix> <namespace_uuid> <source_prefix>
Imprime dos bloques separados por una linea "---REFS---":
  1) los <Directory>/<Component>/<File> anidados
  2) los <ComponentRef> correspondientes, para pegar en el <Feature>

<source_prefix> se antepone a cada Source: wixl resuelve los Source
relativos al .wxs, no a la carpeta que estamos recorriendo.
"""
import subprocess
import sys
from pathlib import Path


def cguid(namespace, name):
    out = subprocess.run(
        ["uuidgen", "--sha1", "--namespace", namespace, "--name", name],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out.upper()


def safe_id(path_parts):
    return "P_" + "_".join(
        "".join(c if c.isalnum() else "_" for c in part) for part in path_parts
    )


def walk(dir_path, rel_parts, prefix, namespace, source_prefix, refs):
    out = []
    for entry in sorted(dir_path.iterdir(), key=lambda p: p.name.lower()):
        parts = rel_parts + (entry.name,)
        wid = safe_id(parts)
        if entry.is_dir():
            out.append(f'<Directory Id="{wid}" Name="{xml_escape(entry.name)}">')
            out.append(walk(entry, parts, prefix, namespace, source_prefix, refs))
            out.append("</Directory>")
        else:
            comp_id = f"C_{wid}"
            guid = cguid(namespace, f"{prefix}/{'/'.join(parts)}")
            rel_src = "/".join((source_prefix,) + parts) if source_prefix else "/".join(parts)
            out.append(
                f'<Component Id="{comp_id}" Guid="{guid}">'
                f'<File Id="F_{wid}" Name="{xml_escape(entry.name)}" '
                f'Source="{xml_escape(rel_src)}"/></Component>'
            )
            refs.append(comp_id)
    return "\n".join(out)


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


if __name__ == "__main__":
    root, prefix, namespace = sys.argv[1], sys.argv[2], sys.argv[3]
    source_prefix = sys.argv[4] if len(sys.argv) > 4 else ""
    refs = []
    body = walk(Path(root), (), prefix, namespace, source_prefix, refs)
    print(body)
    print("---REFS---")
    for r in refs:
        print(f'<ComponentRef Id="{r}"/>')

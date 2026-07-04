"""Motor de presets: lee .xmp (Lightroom/Camera Raw) y .cube (LUT 3D)
y los aplica sobre arrays numpy float32 RGB en rango 0..1.

El renderizado de .xmp es una aproximacion al pipeline de Adobe:
balance de blancos -> exposicion -> contraste -> tono (altas luces/sombras/
blancos/negros) -> curvas -> claridad -> vibrance/saturacion -> mezcla HSL ->
calibracion -> vinyeta -> grano. Nitidez y reduccion de ruido se omiten
(no cambian el "look" de color).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(e1 - e0, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _luma(rgb):
    return rgb @ LUMA


def rgb_to_hsv(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.max(rgb, axis=-1)
    minc = np.min(rgb, axis=-1)
    c = maxc - minc
    safe_c = np.maximum(c, 1e-8)
    s = np.where(maxc > 1e-8, c / np.maximum(maxc, 1e-8), 0.0)
    rc = (maxc - r) / safe_c
    gc = (maxc - g) / safe_c
    bc = (maxc - b) / safe_c
    h = np.select([maxc == r, maxc == g], [bc - gc, 2.0 + rc - bc], default=4.0 + gc - rc)
    h = (h / 6.0) % 1.0
    h = np.where(c < 1e-8, 0.0, h)
    return np.stack([h, s, maxc], axis=-1)


def hsv_to_rgb(hsv):
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    i = np.floor(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i.astype(np.int32) % 6
    conds = [i == k for k in range(6)]
    r = np.select(conds, [v, q, p, p, t, v])
    g = np.select(conds, [t, v, v, q, p, p])
    b = np.select(conds, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1).astype(np.float32)


def _monotone_curve(points, samples=1024):
    """LUT muestreada de una curva monotona (Fritsch-Carlson) por puntos 0-255."""
    pts = sorted(points)
    xs = np.array([p[0] for p in pts], dtype=np.float64) / 255.0
    ys = np.array([p[1] for p in pts], dtype=np.float64) / 255.0
    if len(xs) < 2:
        return None
    dx = np.diff(xs)
    dy = np.diff(ys)
    m = dy / np.maximum(dx, 1e-9)
    t = np.empty_like(xs)
    t[0] = m[0]
    t[-1] = m[-1]
    if len(xs) > 2:
        t[1:-1] = (m[:-1] + m[1:]) / 2.0
    for k in range(len(m)):
        if abs(m[k]) < 1e-12:
            t[k] = 0.0
            t[k + 1] = 0.0
        else:
            a = t[k] / m[k]
            b = t[k + 1] / m[k]
            h = np.hypot(a, b)
            if h > 3.0:
                s = 3.0 / h
                t[k] = s * a * m[k]
                t[k + 1] = s * b * m[k]
    x_out = np.linspace(0.0, 1.0, samples)
    idx = np.clip(np.searchsorted(xs, x_out) - 1, 0, len(xs) - 2)
    h_seg = np.maximum(dx[idx], 1e-9)
    u = np.clip((x_out - xs[idx]) / h_seg, 0.0, 1.0)
    h00 = 2 * u**3 - 3 * u**2 + 1
    h10 = u**3 - 2 * u**2 + u
    h01 = -2 * u**3 + 3 * u**2
    h11 = u**3 - u**2
    y = h00 * ys[idx] + h10 * h_seg * t[idx] + h01 * ys[idx + 1] + h11 * h_seg * t[idx + 1]
    y = np.where(x_out <= xs[0], ys[0], y)
    y = np.where(x_out >= xs[-1], ys[-1], y)
    return np.clip(y, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------- XMP


def parse_xmp(path):
    tree = ET.parse(path)
    desc = tree.getroot().find(f".//{{{RDF}}}Description")
    if desc is None:
        raise ValueError("XMP sin rdf:Description")
    s = {}
    for k, v in desc.attrib.items():
        if k.startswith("{" + CRS + "}"):
            s[k.split("}")[1]] = v
    for child in desc:
        if child.tag.startswith("{" + CRS + "}") and child.text and child.text.strip():
            s[child.tag.split("}")[1]] = child.text.strip()

    def curve(tag):
        seq = desc.find(f"{{{CRS}}}{tag}/{{{RDF}}}Seq")
        if seq is None:
            return None
        pts = []
        for li in seq.findall(f"{{{RDF}}}li"):
            x, y = li.text.split(",")
            pts.append((float(x), float(y)))
        if pts == [(0.0, 0.0), (255.0, 255.0)] or len(pts) < 2:
            return None
        return pts

    curves = {}
    for tag, key in [
        ("ToneCurvePV2012", "main"),
        ("ToneCurvePV2012Red", "r"),
        ("ToneCurvePV2012Green", "g"),
        ("ToneCurvePV2012Blue", "b"),
    ]:
        pts = curve(tag)
        if pts:
            curves[key] = _monotone_curve(pts)

    name_el = desc.find(f"{{{CRS}}}Name/{{{RDF}}}Alt/{{{RDF}}}li")
    name = name_el.text.strip() if name_el is not None and name_el.text else Path(path).stem
    return {"settings": s, "curves": curves, "name": name}


HSL_BANDS = [
    ("Red", 0.0), ("Orange", 30.0), ("Yellow", 60.0), ("Green", 120.0),
    ("Aqua", 180.0), ("Blue", 240.0), ("Purple", 285.0), ("Magenta", 330.0),
]


def _band_weights(hue_deg):
    """Peso por banda HSL: coseno elevado con ancho hasta la banda vecina."""
    centers = np.array([c for _, c in HSL_BANDS], dtype=np.float32)
    out = []
    n = len(centers)
    for i in range(n):
        prev_c = centers[(i - 1) % n]
        next_c = centers[(i + 1) % n]
        w_left = (centers[i] - prev_c) % 360.0
        w_right = (next_c - centers[i]) % 360.0
        d = (hue_deg - centers[i] + 180.0) % 360.0 - 180.0
        w = np.where(
            d >= 0,
            np.clip(1.0 - d / max(w_right, 1e-6), 0.0, 1.0),
            np.clip(1.0 + d / max(w_left, 1e-6), 0.0, 1.0),
        )
        out.append(w * w * (3.0 - 2.0 * w))
    return out


def apply_xmp_color(rgb, preset):
    """Parte cromatica del pipeline (funcion pura de color, horneable en LUT 3D)."""
    s = preset["settings"]
    curves = preset["curves"]

    def f(key):
        v = s.get(key)
        if v in (None, "", "True", "False"):
            return 0.0
        try:
            return float(v)
        except ValueError:
            return 0.0

    x = np.clip(rgb.astype(np.float32), 0.0, 1.0).copy()

    temp = f("IncrementalTemperature")
    tint = f("IncrementalTint")
    if temp:
        x[..., 0] *= 1.0 + temp * 0.0016
        x[..., 2] *= 1.0 - temp * 0.0016
    if tint:
        x[..., 1] *= 1.0 - tint * 0.0012
    x = np.clip(x, 0.0, 1.0)

    exp = f("Exposure2012")
    if exp:
        x = np.clip(x * (2.0 ** exp), 0.0, 1.0)

    c = f("Contrast2012")
    if c:
        pivot = 0.46
        k = 1.0 + c / 100.0 * 0.9 if c > 0 else 1.0 / (1.0 - c / 100.0 * 0.9)
        x = np.clip(pivot + (x - pivot) * k, 0.0, 1.0)

    hi, sh, wh, bl = f("Highlights2012"), f("Shadows2012"), f("Whites2012"), f("Blacks2012")
    if hi or sh or wh or bl:
        l = _luma(x)
        l2 = l.copy()
        if hi:
            w = _smoothstep(0.45, 1.0, l)
            l2 = l2 + (hi / 100.0) * 0.45 * w * l * (1.0 - l)
        if sh:
            w = 1.0 - _smoothstep(0.05, 0.55, l)
            l2 = l2 + (sh / 100.0) * 0.35 * w * np.sqrt(np.maximum(l, 0.0)) * (1.0 - l)
        if wh:
            w = _smoothstep(0.6, 1.0, l)
            l2 = l2 + (wh / 100.0) * 0.3 * w * (1.0 - l)
        if bl:
            w = 1.0 - _smoothstep(0.0, 0.35, l)
            l2 = l2 + (bl / 100.0) * 0.25 * w * (0.35 - l)
        ratio = (np.clip(l2, 0.0, 1.0) + 1e-6) / (l + 1e-6)
        x = np.clip(x * ratio[..., None], 0.0, 1.0)

    if curves:
        grid = np.linspace(0.0, 1.0, 1024)
        if "main" in curves:
            x = np.interp(x, grid, curves["main"]).astype(np.float32)
        for key, ch in (("r", 0), ("g", 1), ("b", 2)):
            if key in curves:
                x[..., ch] = np.interp(x[..., ch], grid, curves[key]).astype(np.float32)

    sat = f("Saturation")
    vib = f("Vibrance")
    hue_adj = [f(f"HueAdjustment{n}") for n, _ in HSL_BANDS]
    sat_adj = [f(f"SaturationAdjustment{n}") for n, _ in HSL_BANDS]
    lum_adj = [f(f"LuminanceAdjustment{n}") for n, _ in HSL_BANDS]
    cal_hue = (f("RedHue"), f("GreenHue"), f("BlueHue"))
    cal_sat = (f("RedSaturation"), f("GreenSaturation"), f("BlueSaturation"))
    grayscale = s.get("ConvertToGrayscale") == "True"

    if grayscale:
        l = _luma(x)
        x = np.repeat(l[..., None], 3, axis=-1)
    elif sat or vib or any(hue_adj) or any(sat_adj) or any(lum_adj) or any(cal_hue) or any(cal_sat):
        hsv = rgb_to_hsv(x)
        h, s_, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        if vib:
            s_ = s_ + (vib / 100.0) * (1.0 - s_) * s_ * 1.6
        if sat:
            s_ = s_ * (1.0 + sat / 100.0)
        if any(hue_adj) or any(sat_adj) or any(lum_adj):
            mask = _smoothstep(0.04, 0.25, s_)
            weights = _band_weights(h * 360.0)
            h_shift = np.zeros_like(h)
            s_mult = np.zeros_like(h)
            v_mult = np.zeros_like(h)
            for w, ha, sa, la in zip(weights, hue_adj, sat_adj, lum_adj):
                if ha:
                    h_shift += w * (ha / 100.0) * (30.0 / 360.0)
                if sa:
                    s_mult += w * (sa / 100.0)
                if la:
                    v_mult += w * (la / 100.0)
            h = (h + h_shift * mask) % 1.0
            s_ = s_ * (1.0 + s_mult * mask)
            v = np.clip(v * (1.0 + 0.45 * v_mult * mask), 0.0, 1.0)
        if any(cal_hue) or any(cal_sat):
            tot = x[..., 0] + x[..., 1] + x[..., 2] + 1e-6
            wr, wg, wb = x[..., 0] / tot, x[..., 1] / tot, x[..., 2] / tot
            cmask = _smoothstep(0.04, 0.25, s_)
            h_shift = (cal_hue[0] * wr + cal_hue[1] * wg + cal_hue[2] * wb) / 100.0 * (20.0 / 360.0)
            s_mult = (cal_sat[0] * wr + cal_sat[1] * wg + cal_sat[2] * wb) / 100.0 * 0.5
            h = (h + h_shift * cmask) % 1.0
            s_ = s_ * (1.0 + s_mult * cmask)
        hsv = np.stack([h, np.clip(s_, 0.0, 1.0), np.clip(v, 0.0, 1.0)], axis=-1)
        x = hsv_to_rgb(hsv)

    return x.astype(np.float32, copy=False)


def apply_xmp_spatial(rgb, preset, include_grain=True):
    """Efectos que dependen de la posicion (no entran en una LUT):
    claridad, vinyeta y grano."""
    s = preset["settings"]

    def f(key):
        v = s.get(key)
        if v in (None, "", "True", "False"):
            return 0.0
        try:
            return float(v)
        except ValueError:
            return 0.0

    x = np.clip(rgb.astype(np.float32), 0.0, 1.0)
    if x.ndim != 3:
        return x

    cl = f("Clarity2012")
    if cl and min(x.shape[0], x.shape[1]) > 16:
        l = _luma(x)
        radius = max(2.0, min(x.shape[0], x.shape[1]) / 40.0)
        im = Image.fromarray((l * 255.0).astype(np.uint8))
        blurred = np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0
        mid = 1.0 - np.abs(2.0 * l - 1.0)
        l2 = np.clip(l + (cl / 100.0) * 0.4 * mid * (l - blurred), 0.0, 1.0)
        ratio = (l2 + 1e-6) / (l + 1e-6)
        x = np.clip(x * ratio[..., None], 0.0, 1.0)

    va = f("PostCropVignetteAmount")
    if va:
        hgt, wid = x.shape[:2]
        yy = np.linspace(-1.0, 1.0, hgt, dtype=np.float32)[:, None]
        xx = np.linspace(-1.0, 1.0, wid, dtype=np.float32)[None, :]
        r = np.sqrt(xx * xx + yy * yy) * np.float32(0.7071068)
        mid = f("PostCropVignetteMidpoint") or 50.0
        start = 0.25 + 0.5 * (mid / 100.0)
        w = _smoothstep(start, 1.05, r)
        x = np.clip(x * (1.0 + (va / 100.0) * 0.85 * w)[..., None], 0.0, 1.0)

    ga = f("GrainAmount")
    if ga and include_grain:
        rng = np.random.default_rng(20260704)
        noise = rng.standard_normal(x.shape[:2]).astype(np.float32)
        x = np.clip(x + (ga / 100.0 * 0.12 * noise)[..., None], 0.0, 1.0)

    return x.astype(np.float32, copy=False)


def apply_xmp(rgb, preset, include_grain=True):
    """Pipeline completo pixel a pixel (referencia; la app usa la LUT horneada)."""
    return apply_xmp_spatial(apply_xmp_color(rgb, preset), preset, include_grain)


def bake_xmp_lut(preset, size=41):
    """Hornea la parte cromatica del preset en una LUT 3D interna: el pipeline
    entero corre una sola vez sobre size^3 muestras en vez de millones de pixeles."""
    grid = np.linspace(0.0, 1.0, size, dtype=np.float32)
    idx = np.arange(size ** 3)
    rgb = np.stack([grid[idx % size], grid[(idx // size) % size],
                    grid[idx // (size * size)]], axis=-1)
    table = apply_xmp_color(rgb, preset)
    return {"size": size, "table": np.ascontiguousarray(table, dtype=np.float32),
            "dmin": np.zeros(3, dtype=np.float32), "dmax": np.ones(3, dtype=np.float32)}


# ---------------------------------------------------------------- CUBE


def parse_cube(path):
    size = None
    dmin = np.zeros(3, dtype=np.float32)
    dmax = np.ones(3, dtype=np.float32)
    rows = []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            upper = line.upper()
            if upper.startswith("TITLE"):
                continue
            if upper.startswith("LUT_1D_SIZE"):
                raise ValueError("LUT 1D no soportado (usar LUT 3D)")
            if upper.startswith("LUT_3D_SIZE"):
                size = int(line.split()[-1])
                continue
            if upper.startswith("DOMAIN_MIN"):
                dmin = np.array([float(v) for v in line.split()[1:4]], dtype=np.float32)
                continue
            if upper.startswith("DOMAIN_MAX"):
                dmax = np.array([float(v) for v in line.split()[1:4]], dtype=np.float32)
                continue
            if line[0] in "0123456789.+-":
                parts = line.split()
                rows.append((float(parts[0]), float(parts[1]), float(parts[2])))
    if size is None or len(rows) != size ** 3:
        raise ValueError(f"Archivo .cube invalido ({len(rows)} filas, tamano {size})")
    table = np.array(rows, dtype=np.float32)
    return {"size": size, "table": table, "dmin": dmin, "dmax": dmax}


def apply_cube(rgb, cube, chunk=1_000_000):
    """Interpolacion trilineal, procesada en bloques para acotar el pico de memoria."""
    shape = rgb.shape
    flat = rgb.reshape(-1, 3)
    if flat.shape[0] > chunk:
        out = np.empty((flat.shape[0], 3), dtype=np.float32)
        for start in range(0, flat.shape[0], chunk):
            out[start:start + chunk] = _cube_block(flat[start:start + chunk], cube)
        return out.reshape(shape)
    return _cube_block(flat, cube).reshape(shape)


def _cube_block(rgb, cube):
    n = cube["size"]
    table = cube["table"]
    v = (np.clip(rgb, 0.0, 1.0) - cube["dmin"]) / np.maximum(cube["dmax"] - cube["dmin"], 1e-9)
    v = np.clip(v, 0.0, 1.0) * (n - 1)
    i0 = np.minimum(np.floor(v).astype(np.int32), n - 2)
    fr = (v - i0).astype(np.float32)
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fx, fy, fz = fr[..., 0, None], fr[..., 1, None], fr[..., 2, None]

    def gat(ir, ig, ib):
        return table[ir + ig * n + ib * n * n]

    c000, c100 = gat(r0, g0, b0), gat(r0 + 1, g0, b0)
    c010, c110 = gat(r0, g0 + 1, b0), gat(r0 + 1, g0 + 1, b0)
    c001, c101 = gat(r0, g0, b0 + 1), gat(r0 + 1, g0, b0 + 1)
    c011, c111 = gat(r0, g0 + 1, b0 + 1), gat(r0 + 1, g0 + 1, b0 + 1)
    c00 = c000 * (1 - fx) + c100 * fx
    c10 = c010 * (1 - fx) + c110 * fx
    c01 = c001 * (1 - fx) + c101 * fx
    c11 = c011 * (1 - fx) + c111 * fx
    c0 = c00 * (1 - fy) + c10 * fy
    c1 = c01 * (1 - fy) + c11 * fy
    return np.clip(c0 * (1 - fz) + c1 * fz, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------- API


class Preset:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self._lut = None
        suffix = self.path.suffix.lower()
        if suffix == ".xmp":
            self.kind = "xmp"
            self._data = parse_xmp(self.path)
        elif suffix == ".cube":
            self.kind = "cube"
            self._data = parse_cube(self.path)
        else:
            raise ValueError(f"Formato no soportado: {suffix}")

    def apply(self, rgb, include_grain=True):
        if self.kind == "cube":
            return apply_cube(rgb, self._data)
        if self._lut is None:
            self._lut = bake_xmp_lut(self._data)
        out = apply_cube(rgb, self._lut)
        return apply_xmp_spatial(out, self._data, include_grain=include_grain)


def load_presets(folder):
    """Carga todos los .xmp y .cube de la carpeta. Devuelve (presets, errores)."""
    folder = Path(folder)
    presets, errors = [], []
    if not folder.is_dir():
        return presets, errors
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.suffix.lower() not in (".xmp", ".cube"):
            continue
        try:
            presets.append(Preset(path))
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return presets, errors


def blend(original, processed, amount):
    """Mezcla original/procesada segun intensidad 0..1."""
    if amount >= 0.999:
        return processed
    if amount <= 0.001:
        return original
    return original * (1.0 - amount) + processed * amount


def blend_thirds(images, fade):
    """Funde tres renders en tercios horizontales. fade = fraccion del ancho (0 = corte neto)."""
    w = images[0].shape[1]
    xcol = np.linspace(0.0, 1.0, w, dtype=np.float32)
    fw = max(fade, 0.002)
    t1 = _smoothstep(1.0 / 3.0 - fw / 2.0, 1.0 / 3.0 + fw / 2.0, xcol)
    t2 = _smoothstep(2.0 / 3.0 - fw / 2.0, 2.0 / 3.0 + fw / 2.0, xcol)
    w0 = (1.0 - t1)[None, :, None]
    w1 = (t1 * (1.0 - t2))[None, :, None]
    w2 = t2[None, :, None]
    return np.clip(images[0] * w0 + images[1] * w1 + images[2] * w2, 0.0, 1.0)

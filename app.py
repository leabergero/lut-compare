"""LUT compare — compara una foto con 2 presets (.xmp / .cube) + original.

Uso:  python app.py            (interfaz normal)
      python app.py --selftest <carpeta>   (prueba automatica sin interaccion)
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

try:
    import rawpy
except ImportError:
    rawpy = None

from PySide6.QtCore import (
    QEvent, QObject, QRectF, QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QDesktopServices, QImage, QKeySequence, QPainter, QPixmap, QShortcut,
    QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QListView, QMainWindow, QMessageBox,
    QProgressDialog, QPushButton, QSlider, QSizePolicy, QVBoxLayout, QWidget,
)

from preset_engine import Preset, _smoothstep, blend, blend_thirds, load_presets

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent


def _presets_dir():
    """En desarrollo: presets/ junto al codigo. Instalada (MSI): una carpeta
    del usuario que sobrevive reinstalaciones, sembrada con los presets
    incluidos en la instalacion la primera vez."""
    bundled = APP_DIR / "presets"
    if not FROZEN:
        return bundled
    user_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LUT Compare" / "presets"
    user_dir.mkdir(parents=True, exist_ok=True)
    if bundled.is_dir():
        for src in bundled.iterdir():
            dest = user_dir / src.name
            if src.suffix.lower() in (".xmp", ".cube") and not dest.exists():
                try:
                    shutil.copy2(src, dest)
                except OSError:
                    pass
    return user_dir


PRESETS_DIR = _presets_dir()
OUT_DIR_NAME = "editadas"
FAVS_FILE = ".lut_compare.json"
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".raf", ".orf",
            ".rw2", ".pef", ".srw", ".x3f"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
              ".heic", ".heif"} | (RAW_EXTS if rawpy is not None else set())
PREVIEW_MAX = 1300
QUICK_MAX = 480
THUMB_H = 64
STAR_ON, STAR_OFF = "★", "☆"
BIG_FUNDIDO_ID = 3  # id del boton "Fundido" en big_mode_group (-1 esta
                    # reservado por Qt como "sin id" y QButtonGroup lo ignora)


def load_image(path, max_side=None):
    """Devuelve (float32 RGB 0..1, exif_bytes | None).

    Con max_side, los JPEG se decodifican ya reducidos (draft mode) y los RAW
    se revelan a media resolucion: mucho mas rapido para previews."""
    if rawpy is not None and Path(path).suffix.lower() in RAW_EXTS:
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8,
                                  half_size=max_side is not None)
        return np.asarray(rgb, dtype=np.float32) / 255.0, None
    im = Image.open(path)
    if max_side is not None:
        try:
            im.draft("RGB", (max_side, max_side))
        except Exception:
            pass
    im = ImageOps.exif_transpose(im)
    exif = im.info.get("exif")
    arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return arr, exif


def downscale(arr, max_side):
    h, w = arr.shape[:2]
    scale = max_side / max(h, w)
    if scale >= 1.0:
        return arr
    im = Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8))
    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return np.asarray(im, dtype=np.float32) / 255.0


def downscale_u8(arr, max_side):
    h, w = arr.shape[:2]
    scale = max_side / max(h, w)
    if scale >= 1.0:
        return arr
    im = Image.fromarray(arr)
    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
    return np.asarray(im)


def to_u8(arr):
    return (np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def blend_thirds_u8(imgs, fade):
    """blend_thirds sobre arrays uint8 a resolucion completa, por bloques de
    columnas para no materializar copias float de toda la imagen."""
    h, w = imgs[0].shape[:2]
    out = np.empty_like(imgs[0])
    fw = max(fade, 0.002)
    x = np.linspace(0.0, 1.0, w, dtype=np.float32)
    t1 = _smoothstep(1.0 / 3.0 - fw / 2.0, 1.0 / 3.0 + fw / 2.0, x)
    t2 = _smoothstep(2.0 / 3.0 - fw / 2.0, 2.0 / 3.0 + fw / 2.0, x)
    weights = [1.0 - t1, t1 * (1.0 - t2), t2]
    step = max(1, 2_000_000 // max(h, 1))
    for c0 in range(0, w, step):
        c1 = min(w, c0 + step)
        acc = np.zeros((h, c1 - c0, 3), dtype=np.float32)
        for img, wgt in zip(imgs, weights):
            acc += img[:, c0:c1].astype(np.float32) * wgt[c0:c1][None, :, None]
        out[:, c0:c1] = np.clip(acc + 0.5, 0.0, 255.0).astype(np.uint8)
    return out


def u8_to_qimage(arr):
    h, w, _ = arr.shape
    return QImage(arr.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def raw_thumbnail(photo):
    """Miniatura de un RAW: usa el JPEG embebido si existe (instantaneo),
    si no revela a media resolucion."""
    with rawpy.imread(str(photo)) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                im = Image.open(io.BytesIO(thumb.data))
                return ImageOps.exif_transpose(im)
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                return Image.fromarray(thumb.data)
        except Exception:
            pass
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8, half_size=True)
        return Image.fromarray(rgb)


def sanitize(name):
    return re.sub(r"[^\w\-]+", "-", name).strip("-") or "preset"


def out_path(folder, src, label):
    out_dir = Path(folder) / OUT_DIR_NAME
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return out_dir / f"{Path(src).stem}_{sanitize(label)}_{ts}.jpg"


def save_render(arr_u8, dest, exif):
    im = Image.fromarray(arr_u8)
    kwargs = {"quality": 95}
    if exif:
        kwargs["exif"] = exif
    im.save(dest, "JPEG", **kwargs)


def render_full(photo, presets, amounts, fade):
    """Original + los 2 presets a resolucion completa, y el fundido en tercios
    de esos tres paneles. Comparte codigo entre el zoom 100% y el guardado de
    la vista fundida (misma composicion, con o sin persistirla a disco)."""
    full, exif = load_image(photo)
    panels = [to_u8(full)]
    for preset, amount in zip(presets, amounts):
        render = preset.apply(full) if preset else full
        panels.append(to_u8(blend(full, render, amount)))
    del full
    big = blend_thirds_u8(panels, fade)
    return panels, big, exif


# ---------------------------------------------------------------- workers


class PreviewWorker(QThread):
    """Un solo hilo de previews; siempre procesa el ultimo pedido pendiente."""

    ready = Signal(dict)

    def __init__(self):
        super().__init__()
        self._job = None
        self._stop = False

    def request(self, job):
        self._job = dict(job)
        if not self.isRunning():
            self.start()

    def stop(self):
        self._stop = True

    def run(self):
        pool = ThreadPoolExecutor(max_workers=2)
        bases = OrderedDict()      # foto -> base uint8 (LRU)
        renders = OrderedDict()    # (foto, preset) -> render uint8 (LRU)

        def get_base(photo):
            if photo in bases:
                bases.move_to_end(photo)
            else:
                full, _ = load_image(photo, max_side=PREVIEW_MAX)
                bases[photo] = to_u8(downscale(full, PREVIEW_MAX))
                while len(bases) > 8:
                    bases.popitem(last=False)
            return bases[photo]

        def get_renders(photo, presets):
            needed = {}
            for preset in presets:
                if preset is not None and (photo, preset.name) not in renders:
                    needed[preset.name] = preset
            if needed:
                base_f = get_base(photo).astype(np.float32) / 255.0
                futures = {
                    name: pool.submit(lambda pr=preset: to_u8(pr.apply(base_f)))
                    for name, preset in needed.items()
                }
                for name, future in futures.items():
                    renders[(photo, name)] = future.result()
                while len(renders) > 16:
                    renders.popitem(last=False)
            outs = []
            for preset in presets:
                if preset is None:
                    outs.append(get_base(photo))
                else:
                    key = (photo, preset.name)
                    renders.move_to_end(key)
                    outs.append(renders[key])
            return outs

        def emit_blend(job, base, r1, r2):
            blended = [
                base,
                blend(base, r1, job["amounts"][0]),
                blend(base, r2, job["amounts"][1]),
            ]
            big = blend_thirds(blended, job["fade"])
            self.ready.emit({
                "gen": job["gen"],
                "photo": job["photo"],
                "panels": [to_u8(b) for b in blended],
                "big": to_u8(big),
            })

        while not self._stop:
            job = self._job
            if job is None:
                self.msleep(20)
                continue
            self._job = None
            try:
                photo, presets = job["photo"], job["presets"]
                cached = all(p is None or (photo, p.name) in renders
                             for p in presets)
                if not cached:
                    # pasada rapida a baja resolucion: feedback inmediato
                    small = downscale_u8(get_base(photo), QUICK_MAX)
                    small = small.astype(np.float32) / 255.0
                    futures = [None if p is None else
                               pool.submit(lambda pr=p: pr.apply(small))
                               for p in presets]
                    quick = [small if f is None else f.result() for f in futures]
                    emit_blend(job, small, quick[0], quick[1])
                    if self._job is not None:
                        continue
                r1_u8, r2_u8 = get_renders(photo, presets)
                base = get_base(photo).astype(np.float32) / 255.0
                emit_blend(job, base,
                           r1_u8.astype(np.float32) / 255.0,
                           r2_u8.astype(np.float32) / 255.0)
            except Exception:
                self.ready.emit({"gen": job["gen"], "photo": job["photo"],
                                 "error": traceback.format_exc(limit=3)})
                continue
            try:
                # precarga: procesa las fotos vecinas mientras no haya pedidos
                for neighbor in job.get("neighbors", []):
                    if self._stop or self._job is not None:
                        break
                    get_renders(neighbor, job["presets"])
            except Exception:
                pass
        pool.shutdown(wait=False)


class FullResWorker(QThread):
    """Procesa la foto a resolucion completa (guardar / zoom / lote)."""

    progress = Signal(int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task
        self.cancelled = False

    def run(self):
        t = self.task
        try:
            if t["kind"] == "batch":
                workers = min(4, max(2, (os.cpu_count() or 4) // 2))
                done = []

                def process(photo):
                    if self.cancelled:
                        return None
                    try:
                        full, exif = load_image(photo)
                        render = t["preset"].apply(full) if t["preset"] else full
                        render = blend(full, render, t["amount"])
                        dest = out_path(t["folder"], photo, t["label"])
                        save_render(to_u8(render), dest, exif)
                        return str(dest)
                    except Exception:
                        print(f"lote: error con {photo}", file=sys.stderr)
                        traceback.print_exc(limit=2)
                        return None

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(process, photo): photo
                               for photo in t["photos"]}
                    count = 0
                    for future in as_completed(futures):
                        count += 1
                        result = future.result()
                        if result:
                            done.append(result)
                        self.progress.emit(count, Path(futures[future]).name)
                self.finished_ok.emit({"kind": "batch", "saved": done,
                                       "cancelled": self.cancelled})
            elif t["kind"] == "save":
                full, exif = load_image(t["photo"])
                render = t["preset"].apply(full) if t["preset"] else full
                render = blend(full, render, t["amount"])
                dest = out_path(t["folder"], t["photo"], t["label"])
                save_render(to_u8(render), dest, exif)
                self.finished_ok.emit({"kind": "save", "dest": str(dest)})
            elif t["kind"] == "zoom":
                panels, big, _ = render_full(t["photo"], t["presets"], t["amounts"], t["fade"])
                self.finished_ok.emit({"kind": "zoom", "photo": t["photo"],
                                       "key": t["key"], "panels": panels,
                                       "big": big})
            elif t["kind"] == "blend_save":
                _, big, exif = render_full(t["photo"], t["presets"], t["amounts"], t["fade"])
                dest = out_path(t["folder"], t["photo"], t["label"])
                save_render(big, dest, exif)
                self.finished_ok.emit({"kind": "blend_save", "dest": str(dest)})
        except Exception:
            self.failed.emit(traceback.format_exc(limit=3))


class ThumbWorker(QThread):
    thumb = Signal(str, QImage)

    def __init__(self, photos):
        super().__init__()
        self.photos = list(photos)
        self.cancelled = False

    def run(self):
        for photo in self.photos:
            if self.cancelled:
                return
            try:
                if rawpy is not None and Path(photo).suffix.lower() in RAW_EXTS:
                    im = raw_thumbnail(photo)
                else:
                    im = Image.open(photo)
                    try:
                        im.draft("RGB", (THUMB_H * 3, THUMB_H * 2))
                    except Exception:
                        pass
                    im = ImageOps.exif_transpose(im)
                im.thumbnail((THUMB_H * 3, THUMB_H * 2))
                arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
                self.thumb.emit(str(photo), u8_to_qimage(arr))
            except Exception:
                continue


# ---------------------------------------------------------------- vistas


class ViewState(QObject):
    """Zoom/paneo continuos, compartidos por las 4 vistas (3 paneles + grande)."""

    changed = Signal()
    MIN_ZOOM = 1.0
    MAX_ZOOM = 8.0

    def __init__(self):
        super().__init__()
        self.zoom = self.MIN_ZOOM
        self.cx = 0.5
        self.cy = 0.5

    def set(self, zoom=None, cx=None, cy=None):
        if zoom is not None:
            self.zoom = min(max(zoom, self.MIN_ZOOM), self.MAX_ZOOM)
        if cx is not None:
            self.cx = min(max(cx, 0.0), 1.0)
        if cy is not None:
            self.cy = min(max(cy, 0.0), 1.0)
        self.changed.emit()


class SyncView(QWidget):
    """Panel de imagen con zoom continuo (rueda del mouse) y paneo (arrastre)
    sincronizados entre las 4 vistas a traves del ViewState compartido."""

    zoom_requested = Signal()
    CLICK_ZOOM = 3.0    # nivel al que salta un click simple (sin arrastre)
    WHEEL_STEP = 1.0015  # multiplicador de zoom por unidad de angleDelta

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.preview = None      # QImage ajustada
        self.full = None         # QImage resolucion completa (para el detalle)
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)
        self._press = None
        self._dragged = False
        state.changed.connect(self.update)

    def set_preview(self, qimg):
        self.preview = qimg
        self.update()

    def set_full(self, qimg):
        self.full = qimg
        self.update()

    def _image(self):
        return self.full if self.full is not None else self.preview

    @staticmethod
    def _window(widget_dim, array_dim, scale, center):
        """Ventana visible en pixeles del array + rect destino en el widget
        (con bandas si la imagen no llena ese eje a este zoom)."""
        visible = min(array_dim, widget_dim / scale)
        start = min(max(center * array_dim - visible / 2, 0.0), array_dim - visible)
        dest_len = min(widget_dim, array_dim * scale)
        dest_start = (widget_dim - dest_len) / 2
        return start, visible, dest_start, dest_len

    def _geometry(self):
        """(rect origen en el array actual, rect destino en el widget), o
        None si todavia no hay imagen. Una sola formula para "ajustada"
        (zoom=1) y cualquier nivel de zoom/paneo: no hace falta un camino
        de codigo aparte para cada caso."""
        img = self._image()
        if img is None or not self.width() or not self.height():
            return None
        fit_scale = min(self.width() / img.width(), self.height() / img.height())
        scale = fit_scale * self.state.zoom
        sx, sw, dx, dw = self._window(self.width(), img.width(), scale, self.state.cx)
        sy, sh, dy, dh = self._window(self.height(), img.height(), scale, self.state.cy)
        return QRectF(sx, sy, sw, sh), QRectF(dx, dy, dw, dh)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), Qt.transparent)
        geo = self._geometry()
        if geo is None:
            p.setPen(Qt.gray)
            p.drawText(self.rect(), Qt.AlignCenter, "sin imagen")
            return
        source, dest = geo
        p.drawImage(dest, self._image(), source)
        if self.full is None and self.state.zoom > 1.0:
            p.setPen(Qt.white)
            p.drawText(self.rect().adjusted(6, 6, -6, -6),
                       Qt.AlignTop | Qt.AlignLeft, "cargando resolucion completa...")

    def wheelEvent(self, event):
        geo = self._geometry()
        if geo is None:
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        source, dest = geo
        pos = event.position()
        fx = min(max((pos.x() - dest.x()) / dest.width(), 0.0), 1.0) if dest.width() else 0.5
        fy = min(max((pos.y() - dest.y()) / dest.height(), 0.0), 1.0) if dest.height() else 0.5
        image_x = source.x() + fx * source.width()
        image_y = source.y() + fy * source.height()

        img = self._image()
        new_zoom = self.state.zoom * (self.WHEEL_STEP ** delta)
        new_zoom = min(max(new_zoom, ViewState.MIN_ZOOM), ViewState.MAX_ZOOM)
        fit_scale = min(self.width() / img.width(), self.height() / img.height())
        new_scale = fit_scale * new_zoom
        new_vw = min(img.width(), self.width() / new_scale)
        new_vh = min(img.height(), self.height() / new_scale)
        # mantiene el punto bajo el cursor fijo en pantalla al cambiar el zoom
        new_cx = (image_x - fx * new_vw + new_vw / 2) / img.width()
        new_cy = (image_y - fy * new_vh + new_vh / 2) / img.height()
        self.state.set(zoom=new_zoom, cx=new_cx, cy=new_cy)
        if new_zoom > 1.0:
            self.zoom_requested.emit()
        event.accept()

    def mousePressEvent(self, event):
        self._press = event.position()
        self._dragged = False

    def mouseMoveEvent(self, event):
        if self._press is None or self.state.zoom <= 1.0:
            return
        delta = event.position() - self._press
        if delta.manhattanLength() > 3:
            self._dragged = True
            geo = self._geometry()
            img = self._image()
            if geo is None or img is None:
                return
            source, dest = geo
            dx_img = delta.x() * (source.width() / max(dest.width(), 1.0))
            dy_img = delta.y() * (source.height() / max(dest.height(), 1.0))
            self.state.set(
                cx=self.state.cx - dx_img / img.width(),
                cy=self.state.cy - dy_img / img.height(),
            )
            self._press = event.position()

    def mouseReleaseEvent(self, _event):
        if not self._dragged:
            if self.state.zoom > 1.0:
                self.state.set(zoom=1.0)
            else:
                self.state.set(zoom=self.CLICK_ZOOM, cx=0.5, cy=0.5)
                self.zoom_requested.emit()
        self._press = None


class FilmStrip(QListWidget):
    """Tira de miniaturas: la rueda del mouse desplaza horizontalmente."""

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - delta)
        event.accept()


class PresetCombo(QComboBox):
    """Combo de presets: se reconstruye (agrupado, con favoritos arriba)
    justo antes de abrirse, asi siempre refleja la ultima estrella tocada."""

    def __init__(self, rebuild_fn):
        super().__init__()
        self._rebuild_fn = rebuild_fn

    def showPopup(self):
        self._rebuild_fn()
        super().showPopup()


# ---------------------------------------------------------------- ventana


class MainWindow(QMainWindow):
    def __init__(self, selftest_folder=None):
        super().__init__()
        self.setWindowTitle("LUT compare")
        self.resize(1280, 860)
        self.settings = QSettings("LeaB", "LutCompare")
        raw_favs = self.settings.value("fav_presets", [])
        if raw_favs is None:  # lista vacia guardada antes: QSettings la devuelve como None
            raw_favs = []
        elif isinstance(raw_favs, str):  # y una lista de 1 elemento, como str suelto
            raw_favs = [raw_favs] if raw_favs else []
        self.fav_presets = set(raw_favs)
        self.presets_dir = Path(self.settings.value("presets_dir", str(PRESETS_DIR)))
        if not self.presets_dir.is_dir():
            self.presets_dir = PRESETS_DIR

        self.presets = []
        self.photos = []
        self.visible = []
        self.current = None
        self.favorites = set()
        self.folder = None
        self.gen = 0
        self.zoom_cache_key = None
        self.workers = []
        self.thumb_worker = None
        self.big_solo = None  # None = fundido; 0/1/2 = agrandar solo ese panel
        self._preview_panels = self._preview_big = None
        self._full_panels = self._full_big = None

        self.view_state = ViewState()
        self.preview_worker = PreviewWorker()
        self.preview_worker.ready.connect(self.on_preview_ready)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(80)
        self._debounce.timeout.connect(self._request_preview)

        self._build_ui()
        self._shortcuts()
        self.reload_presets(startup=True)

        last = self.settings.value("folder", "")
        if selftest_folder:
            QTimer.singleShot(400, lambda: self.set_folder(selftest_folder))
            QTimer.singleShot(2500, self._selftest_step2)
        elif last and Path(last).is_dir():
            QTimer.singleShot(100, lambda: self.set_folder(last))

    # -------------------------------------------------- construccion UI

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("LUT compare")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.file_label = QLabel("")
        self.file_label.setStyleSheet("color: gray;")
        self.open_presets_btn = QPushButton("Abrir carpeta")
        self.open_presets_btn.clicked.connect(self.open_presets_folder)
        choose_presets_btn = QPushButton("Cambiar carpeta...")
        choose_presets_btn.clicked.connect(self.choose_presets_folder)
        refresh = QPushButton("Releer presets")
        refresh.clicked.connect(self.reload_presets)
        top.addWidget(title)
        top.addWidget(self.file_label)
        top.addStretch(1)
        top.addWidget(self.open_presets_btn)
        top.addWidget(choose_presets_btn)
        top.addWidget(refresh)
        layout.addLayout(top)
        self._update_presets_tooltip()

        # nombres de los 3 tercios del fundido, ahora clickeables: eligen
        # que se ve agrandado en la vista grande (fundido, o solo uno)
        names = QHBoxLayout()
        self.big_mode_group = QButtonGroup(self)
        self.big_mode_group.setExclusive(True)
        fundido_btn = QPushButton("Fundido")
        fundido_btn.setCheckable(True)
        fundido_btn.setChecked(True)
        fundido_btn.setFlat(True)
        fundido_btn.setStyleSheet("font-size: 12px;")
        self.big_mode_group.addButton(fundido_btn, BIG_FUNDIDO_ID)
        names.addWidget(fundido_btn, 1)
        self.zone_buttons = []
        for i in range(3):
            btn = QPushButton("")
            btn.setCheckable(True)
            btn.setFlat(True)
            btn.setStyleSheet("font-size: 12px;")
            self.big_mode_group.addButton(btn, i)
            names.addWidget(btn, 1)
            self.zone_buttons.append(btn)
        self.big_mode_group.idClicked.connect(self.on_big_mode_changed)
        layout.addLayout(names)

        self.big_view = SyncView(self.view_state)
        self.big_view.setMinimumHeight(220)
        self.big_view.zoom_requested.connect(self.request_zoom)
        layout.addWidget(self.big_view, 5)

        fade_row = QHBoxLayout()
        fade_row.addWidget(QLabel("Fundido"))
        self.fade_slider = QSlider(Qt.Horizontal)
        self.fade_slider.setRange(0, 30)
        self.fade_slider.setValue(int(self.settings.value("fade", 12)))
        self.fade_slider.setFixedWidth(180)
        self.fade_slider.valueChanged.connect(self.on_fade_changed)
        self.hard_cut = QCheckBox("Corte neto")
        self.hard_cut.toggled.connect(self.on_fade_changed)
        save_blend_btn = QPushButton("Guardar fundido")
        save_blend_btn.clicked.connect(self.save_blend)
        fade_row.addWidget(self.fade_slider)
        fade_row.addWidget(self.hard_cut)
        fade_row.addWidget(save_blend_btn)
        fade_row.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray;")
        fade_row.addWidget(self.status_label)
        layout.addLayout(fade_row)

        panels_row = QHBoxLayout()
        panels_row.setSpacing(8)
        self.panel_views = []
        self.panel_combos = []
        self.panel_sliders = []
        self.panel_slider_labels = []
        for i in range(3):
            frame = QFrame()
            frame.setFrameShape(QFrame.StyledPanel)
            col = QVBoxLayout(frame)
            col.setContentsMargins(6, 6, 6, 6)
            col.setSpacing(5)
            view = SyncView(self.view_state)
            view.zoom_requested.connect(self.request_zoom)
            col.addWidget(view, 1)
            self.panel_views.append(view)
            if i == 0:
                # combo deshabilitado en vez de una simple etiqueta: un
                # QLabel mide bastante menos de alto que un QComboBox (17px
                # vs 25px), y esa diferencia dejaba a este panel mas bajo
                # que los otros dos -> la imagen de arriba se veia mas
                # grande. Con el mismo tipo de widget, el alto calza.
                original_combo = QComboBox()
                original_combo.addItem("Original")
                original_combo.setEnabled(False)
                col.addWidget(original_combo)
                self.panel_combos.append(None)
                self.panel_sliders.append(None)
                self.panel_slider_labels.append(None)
                # fila fantasma (oculta, pero reserva su alto): sin ella a
                # este panel le falta la fila "Intensidad" que tienen los
                # otros dos, queda mas bajo, y la imagen ahi arriba (que
                # llena el resto) se ve mas grande que las demas.
                ghost = QWidget()
                ghost_row = QHBoxLayout(ghost)
                ghost_row.setContentsMargins(0, 0, 0, 0)
                ghost_row.addWidget(QLabel("Intensidad"))
                ghost_row.addWidget(QSlider(Qt.Horizontal), 1)
                ghost_val = QLabel("100%")
                ghost_val.setFixedWidth(38)
                ghost_row.addWidget(ghost_val)
                policy = ghost.sizePolicy()
                policy.setRetainSizeWhenHidden(True)
                ghost.setSizePolicy(policy)
                ghost.hide()
                col.addWidget(ghost)
            else:
                combo = PresetCombo(lambda i=i: self._rebuild_preset_model(i))
                combo.currentIndexChanged.connect(self.on_preset_changed)
                combo.view().viewport().installEventFilter(self)
                col.addWidget(combo)
                self.panel_combos.append(combo)
                srow = QHBoxLayout()
                srow.addWidget(QLabel("Intensidad"))
                slider = QSlider(Qt.Horizontal)
                slider.setRange(0, 100)
                slider.setValue(100)
                slider.valueChanged.connect(self.on_amount_changed)
                sval = QLabel("100%")
                sval.setFixedWidth(38)
                srow.addWidget(slider, 1)
                srow.addWidget(sval)
                col.addLayout(srow)
                self.panel_sliders.append(slider)
                self.panel_slider_labels.append(sval)
            brow = QHBoxLayout()
            save_btn = QPushButton(f"Guardar ({i + 1})")
            save_btn.clicked.connect(lambda _=False, k=i: self.save_panel(k))
            brow.addWidget(save_btn)
            if i > 0:
                batch_btn = QPushButton("Aplicar a todas")
                batch_btn.clicked.connect(lambda _=False, k=i: self.batch_panel(k))
                brow.addWidget(batch_btn)
            col.addLayout(brow)
            panels_row.addWidget(frame, 1)
        layout.addLayout(panels_row, 4)

        strip_frame = QFrame()
        strip_frame.setFrameShape(QFrame.StyledPanel)
        strip_col = QVBoxLayout(strip_frame)
        strip_col.setContentsMargins(6, 6, 6, 6)
        strip_col.setSpacing(5)
        browse_row = QHBoxLayout()
        browse = QPushButton("Examinar...")
        browse.clicked.connect(self.browse_folder)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.count_label = QLabel("")
        self.fav_button = QPushButton("Favorita (F)")
        self.fav_button.clicked.connect(self.toggle_favorite)
        self.fav_filter = QCheckBox("Solo favoritas")
        self.fav_filter.toggled.connect(self.rebuild_strip)
        browse_row.addWidget(browse)
        browse_row.addWidget(self.path_edit, 1)
        browse_row.addWidget(self.count_label)
        browse_row.addWidget(self.fav_button)
        browse_row.addWidget(self.fav_filter)
        strip_col.addLayout(browse_row)
        self.strip = FilmStrip()
        self.strip.setViewMode(QListView.IconMode)
        self.strip.setFlow(QListView.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setIconSize(QSize(THUMB_H * 3 // 2, THUMB_H))
        self.strip.setFixedHeight(THUMB_H + 34)
        self.strip.setHorizontalScrollMode(QListView.ScrollPerPixel)
        self.strip.currentRowChanged.connect(self.on_strip_row)
        strip_col.addWidget(self.strip)
        layout.addWidget(strip_frame)

        self.setCentralWidget(root)

    def _shortcuts(self):
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self.step_photo(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self.step_photo(1))
        QShortcut(QKeySequence(Qt.Key_F), self, activated=self.toggle_favorite)
        for k, key in ((0, Qt.Key_1), (1, Qt.Key_2), (2, Qt.Key_3)):
            QShortcut(QKeySequence(key), self, activated=lambda k=k: self.save_panel(k))

    # -------------------------------------------------- presets

    def open_presets_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.presets_dir)))

    def choose_presets_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Elegir carpeta de presets", str(self.presets_dir))
        if folder:
            self.presets_dir = Path(folder)
            self.settings.setValue("presets_dir", str(self.presets_dir))
            self._update_presets_tooltip()
            self.reload_presets()

    def _update_presets_tooltip(self):
        self.open_presets_btn.setToolTip(str(self.presets_dir))

    def reload_presets(self, startup=False):
        self.presets, errors = load_presets(self.presets_dir)
        for i in (1, 2):
            self._rebuild_preset_model(i)
        if startup:
            for i in (1, 2):
                combo = self.panel_combos[i]
                if combo.currentData() is None and len(self.presets) >= i:
                    idx = combo.findData(self.presets[i - 1].key)  # preset 1 y 2 de la lista
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
        if errors:
            self.status_label.setText("Presets con error: " + "; ".join(errors))
        self.on_preset_changed()

    def _rebuild_preset_model(self, i):
        """Reconstruye el modelo del combo i: (ninguno), Favoritos arriba,
        despues cada carpeta con su subtitulo. Se llama al recargar presets
        y de nuevo justo antes de abrir el desplegable (PresetCombo)."""
        combo = self.panel_combos[i]
        prev_key = combo.currentData() if combo.count() else self.settings.value(f"preset{i}", "")
        model = QStandardItemModel(combo)

        def add(text, key, header=False):
            item = QStandardItem(text)
            item.setData(key, Qt.UserRole)
            if header:
                item.setFlags(item.flags() & ~(Qt.ItemIsEnabled | Qt.ItemIsSelectable))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            model.appendRow(item)

        add("(ninguno)", None)
        favorites = [p for p in self.presets if p.key in self.fav_presets]
        if favorites:
            add("Favoritos", None, header=True)
            for p in favorites:
                add(f"{STAR_ON} {p.name}  [{p.kind}]", p.key)
        last_group = None
        for p in self.presets:
            if p.group and p.group != last_group:
                add(p.group, None, header=True)
                last_group = p.group
            star = STAR_ON if p.key in self.fav_presets else STAR_OFF
            add(f"{star} {p.name}  [{p.kind}]", p.key)

        combo.blockSignals(True)
        combo.setModel(model)
        idx = combo.findData(prev_key) if prev_key else -1
        if idx < 0 and prev_key:
            # compat con configs de una version anterior: guardaban el texto
            # completo del combo ("nombre  [tipo]"), no la ruta relativa
            legacy_name = str(prev_key).split("  [")[0]
            old = next((p.key for p in self.presets if p.name == legacy_name), None)
            idx = combo.findData(old) if old else -1
        combo.setCurrentIndex(max(idx, 0))
        combo.blockSignals(False)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            for i in (1, 2):
                combo = self.panel_combos[i]
                if combo is not None and obj is combo.view().viewport():
                    index = combo.view().indexAt(event.position().toPoint())
                    key = index.data(Qt.UserRole) if index.isValid() else None
                    if key and event.position().x() < self._star_zone_width(combo):
                        self._toggle_fav_preset(key)
                        return True
        return super().eventFilter(obj, event)

    def _star_zone_width(self, combo):
        return combo.view().fontMetrics().horizontalAdvance(STAR_ON + " ")

    def _toggle_fav_preset(self, key):
        if key in self.fav_presets:
            self.fav_presets.discard(key)
        else:
            self.fav_presets.add(key)
        self.settings.setValue("fav_presets", sorted(self.fav_presets))
        # feedback inmediato en el desplegable abierto: solo cambia el icono,
        # sin reordenar (eso pasa recien la proxima vez que se abre, ver
        # PresetCombo.showPopup) para no reconstruir el modelo a mitad de un click
        star = STAR_ON if key in self.fav_presets else STAR_OFF
        for i in (1, 2):
            model = self.panel_combos[i].model()
            for row in range(model.rowCount()):
                item = model.item(row)
                if item.data(Qt.UserRole) == key:
                    item.setText(star + item.text()[1:])

    def panel_preset(self, i):
        if i == 0:
            return None
        combo = self.panel_combos[i]
        key = combo.currentData()
        if not key:
            return None
        for preset in self.presets:
            if preset.key == key:
                return preset
        return None

    def panel_amount(self, i):
        slider = self.panel_sliders[i]
        return 1.0 if slider is None else slider.value() / 100.0

    def panel_label(self, i):
        preset = self.panel_preset(i)
        if preset is None:
            return "Original"
        amount = self.panel_amount(i)
        return preset.name if amount >= 0.999 else f"{preset.name}@{int(amount * 100)}"

    # -------------------------------------------------- carpeta y fotos

    def browse_folder(self):
        start = self.folder or str(Path.home() / "Pictures")
        folder = QFileDialog.getExistingDirectory(self, "Elegir carpeta de fotos", start)
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder):
        folder = Path(folder)
        self.folder = str(folder)
        self.path_edit.setText(self.folder)
        self.settings.setValue("folder", self.folder)
        self.photos = sorted(
            [str(p) for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS],
            key=lambda p: Path(p).name.lower(),
        )
        favs_path = folder / FAVS_FILE
        self.favorites = set()
        if favs_path.is_file():
            try:
                self.favorites = set(json.loads(favs_path.read_text(encoding="utf-8"))
                                     .get("favorites", []))
            except Exception:
                pass
        self.rebuild_strip(select_first=True)

    def save_favorites(self):
        if not self.folder:
            return
        try:
            (Path(self.folder) / FAVS_FILE).write_text(
                json.dumps({"favorites": sorted(self.favorites)}, indent=1),
                encoding="utf-8")
        except Exception:
            pass

    def rebuild_strip(self, _checked=False, select_first=False):
        if self.fav_filter.isChecked():
            self.visible = [p for p in self.photos if Path(p).name in self.favorites]
        else:
            self.visible = list(self.photos)
        current = self.current
        self.strip.blockSignals(True)
        self.strip.clear()
        for photo in self.visible:
            name = Path(photo).name
            star = "* " if name in self.favorites else ""
            item = QListWidgetItem(star + name)
            item.setToolTip(name)
            item.setSizeHint(QSize(THUMB_H * 3 // 2 + 8, THUMB_H + 26))
            self.strip.addItem(item)
        self.strip.blockSignals(False)
        self.count_label.setText(f"{len(self.visible)} fotos")
        if self.thumb_worker is not None:
            self.thumb_worker.cancelled = True
            self.thumb_worker.wait(10000)
        self.thumb_worker = ThumbWorker(self.visible)
        self.thumb_worker.thumb.connect(self.on_thumb)
        self.thumb_worker.start()
        if select_first and self.visible:
            self.strip.setCurrentRow(0)
        elif current in self.visible:
            self.strip.setCurrentRow(self.visible.index(current))
        elif self.visible:
            self.strip.setCurrentRow(0)

    def on_thumb(self, photo, qimg):
        if photo in self.visible:
            row = self.visible.index(photo)
            item = self.strip.item(row)
            if item is not None:
                from PySide6.QtGui import QIcon

                item.setIcon(QIcon(QPixmap.fromImage(qimg)))

    def on_strip_row(self, row):
        if 0 <= row < len(self.visible):
            self.set_photo(self.visible[row])

    def step_photo(self, delta):
        if not self.visible or self.current not in self.visible:
            return
        row = self.visible.index(self.current) + delta
        if 0 <= row < len(self.visible):
            self.strip.setCurrentRow(row)

    def toggle_favorite(self):
        if not self.current:
            return
        name = Path(self.current).name
        if name in self.favorites:
            self.favorites.discard(name)
        else:
            self.favorites.add(name)
        self.save_favorites()
        self.rebuild_strip()

    def set_photo(self, photo):
        self.current = photo
        self.view_state.set(zoom=1.0)
        self.zoom_cache_key = None
        self._full_panels = self._full_big = None
        for view in (*self.panel_views, self.big_view):
            view.set_full(None)
        total = len(self.visible)
        pos = self.visible.index(photo) + 1 if photo in self.visible else 0
        self.file_label.setText(f"{Path(photo).name} - {pos} de {total}")
        self.schedule_preview()

    # -------------------------------------------------- previews

    def schedule_preview(self, *_args):
        for i in (1, 2):
            label = self.panel_slider_labels[i]
            if label is not None:
                label.setText(f"{self.panel_sliders[i].value()}%")
        self._debounce.start()

    def on_amount_changed(self, *_args):
        self.zoom_cache_key = None
        for view in (*self.panel_views, self.big_view):
            view.set_full(None)
        self.schedule_preview()

    def on_fade_changed(self, *_args):
        self.zoom_cache_key = None
        self.big_view.set_full(None)
        if self.view_state.zoom > 1.0:
            self.request_zoom()
        self.schedule_preview()

    def on_preset_changed(self, *_args):
        for i in (1, 2):
            combo = self.panel_combos[i]
            self.settings.setValue(f"preset{i}", combo.currentData())
        self.zone_buttons[0].setText("Original")
        self.zone_buttons[1].setText(self.panel_label(1))
        self.zone_buttons[2].setText(self.panel_label(2))
        self.zoom_cache_key = None
        for view in (*self.panel_views, self.big_view):
            view.set_full(None)
        self.schedule_preview()

    def _request_preview(self):
        if not self.current:
            return
        self.gen += 1
        self.settings.setValue("fade", self.fade_slider.value())
        fade = 0.0 if self.hard_cut.isChecked() else self.fade_slider.value() / 100.0
        self.zone_buttons[1].setText(self.panel_label(1))
        self.zone_buttons[2].setText(self.panel_label(2))
        self.status_label.setText("procesando...")
        neighbors = []
        if self.current in self.visible:
            pos = self.visible.index(self.current)
            for j in (pos + 1, pos - 1):
                if 0 <= j < len(self.visible):
                    neighbors.append(self.visible[j])
        self.preview_worker.request({
            "gen": self.gen,
            "photo": self.current,
            "presets": [self.panel_preset(1), self.panel_preset(2)],
            "amounts": [self.panel_amount(1), self.panel_amount(2)],
            "fade": fade,
            "neighbors": neighbors,
        })

    def on_preview_ready(self, result):
        if result["gen"] != self.gen or result["photo"] != self.current:
            return
        if "error" in result:
            self.status_label.setText("error al procesar (ver consola)")
            print(result["error"], file=sys.stderr)
            return
        for view, arr in zip(self.panel_views, result["panels"]):
            view.set_preview(u8_to_qimage(arr))
        self._preview_panels, self._preview_big = result["panels"], result["big"]
        self.big_view.set_preview(u8_to_qimage(self._big_source(full=False)))
        self.status_label.setText("")

    def on_big_mode_changed(self, mode_id):
        """Elegido en la fila de nombres: -1 = fundido, 0/1/2 = agrandar
        solo ese panel (mas facil de juzgar el resultado que en el tercio
        chico). No recalcula nada: reusa lo que ya se tenia renderizado."""
        self.big_solo = None if mode_id == BIG_FUNDIDO_ID else mode_id
        if self._preview_panels is not None:
            self.big_view.set_preview(u8_to_qimage(self._big_source(full=False)))
        if self._full_panels is not None:
            self.big_view.set_full(u8_to_qimage(self._big_source(full=True)))

    def _big_source(self, full):
        panels = self._full_panels if full else self._preview_panels
        big = self._full_big if full else self._preview_big
        if self.big_solo is not None and panels is not None:
            return panels[self.big_solo]
        return big

    # -------------------------------------------------- zoom 100%

    def request_zoom(self):
        if not self.current:
            return
        fade = 0.0 if self.hard_cut.isChecked() else self.fade_slider.value() / 100.0
        key = (self.current, self.panel_label(1), self.panel_label(2), round(fade, 3))
        if key == self.zoom_cache_key:
            return
        self.zoom_cache_key = key
        self.status_label.setText("procesando zoom 100%...")
        worker = FullResWorker({
            "kind": "zoom", "photo": self.current, "key": key,
            "presets": [self.panel_preset(1), self.panel_preset(2)],
            "amounts": [self.panel_amount(1), self.panel_amount(2)],
            "fade": fade,
        })
        worker.finished_ok.connect(self.on_zoom_ready)
        worker.failed.connect(self.on_worker_failed)
        self._track(worker)
        worker.start()

    def on_zoom_ready(self, result):
        if result["key"] != self.zoom_cache_key or result["photo"] != self.current:
            return
        for view, arr in zip(self.panel_views, result["panels"]):
            view.set_full(u8_to_qimage(arr))
        self._full_panels, self._full_big = result["panels"], result["big"]
        self.big_view.set_full(u8_to_qimage(self._big_source(full=True)))
        self.status_label.setText("")

    # -------------------------------------------------- guardar / lote

    def save_panel(self, i):
        if not self.current or not self.folder:
            return
        label = self.panel_label(i)
        self.status_label.setText(f"guardando {label}...")
        worker = FullResWorker({
            "kind": "save", "photo": self.current, "folder": self.folder,
            "preset": self.panel_preset(i), "amount": self.panel_amount(i),
            "label": label,
        })
        worker.finished_ok.connect(
            lambda r: self.status_label.setText(f"guardada: {Path(r['dest']).name}"))
        worker.failed.connect(self.on_worker_failed)
        self._track(worker)
        worker.start()

    def save_blend(self):
        """Guarda la vista grande tal cual se ve: el fundido en tercios entre
        original y los 2 presets, a resolucion completa."""
        if not self.current or not self.folder:
            return
        label = f"{self.panel_label(1)}+{self.panel_label(2)}_fundido"
        self.status_label.setText("guardando fundido...")
        worker = FullResWorker({
            "kind": "blend_save", "photo": self.current, "folder": self.folder,
            "presets": [self.panel_preset(1), self.panel_preset(2)],
            "amounts": [self.panel_amount(1), self.panel_amount(2)],
            "fade": 0.0 if self.hard_cut.isChecked() else self.fade_slider.value() / 100.0,
            "label": label,
        })
        worker.finished_ok.connect(
            lambda r: self.status_label.setText(f"guardado: {Path(r['dest']).name}"))
        worker.failed.connect(self.on_worker_failed)
        self._track(worker)
        worker.start()

    def batch_panel(self, i):
        if not self.visible or not self.folder:
            return
        preset = self.panel_preset(i)
        label = self.panel_label(i)
        count = len(self.visible)
        answer = QMessageBox.question(
            self, "Aplicar a todas",
            f"Aplicar '{label}' a las {count} fotos visibles y guardarlas en "
            f"{OUT_DIR_NAME}\\ ?")
        if answer != QMessageBox.Yes:
            return
        dialog = QProgressDialog(f"Procesando con {label}...", "Cancelar", 0, count, self)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        worker = FullResWorker({
            "kind": "batch", "photos": list(self.visible), "folder": self.folder,
            "preset": preset, "amount": self.panel_amount(i), "label": label,
        })
        worker.progress.connect(
            lambda n, name: (dialog.setValue(n), dialog.setLabelText(name)))
        worker.finished_ok.connect(
            lambda r: (dialog.setValue(count), self.status_label.setText(
                f"lote: {len(r['saved'])} guardadas"
                + (" (cancelado)" if r["cancelled"] else ""))))
        worker.failed.connect(lambda m: (dialog.cancel(), self.on_worker_failed(m)))
        dialog.canceled.connect(lambda: setattr(worker, "cancelled", True))
        self._track(worker)
        worker.start()

    def on_worker_failed(self, message):
        self.status_label.setText("error (ver consola)")
        print(message, file=sys.stderr)

    def _track(self, worker):
        self.workers.append(worker)
        worker.finished.connect(
            lambda: self.workers.remove(worker) if worker in self.workers else None)

    # -------------------------------------------------- selftest

    def _selftest_step2(self):
        combos_ok = all(self.panel_combos[i].count() > 1 for i in (1, 2))
        if combos_ok:
            self.panel_combos[1].setCurrentIndex(1)
            self.panel_combos[2].setCurrentIndex(min(2, self.panel_combos[2].count() - 1))
        QTimer.singleShot(4000, self._selftest_step3)

    def _selftest_step3(self):
        self.save_panel(1)
        self.save_panel(2)
        QTimer.singleShot(5000, self._selftest_check)

    def _selftest_check(self):
        out_dir = Path(self.folder) / OUT_DIR_NAME
        files = list(out_dir.glob("*.jpg")) if out_dir.is_dir() else []
        ok = len(files) >= 2 and self.big_view.preview is not None
        print(f"SELFTEST {'OK' if ok else 'FAIL'}: {len(files)} guardadas, "
              f"preview={'si' if self.big_view.preview is not None else 'no'}")
        self.close()
        QApplication.instance().exit(0 if ok else 1)

    def closeEvent(self, event):
        # los hilos pueden estar en medio de un revelado RAW o un render:
        # esperas generosas para no destruir QThreads vivos (crash al salir)
        self.preview_worker.stop()
        self.preview_worker.wait(15000)
        for worker in list(self.workers):
            worker.cancelled = True
            worker.wait(15000)
        if self.thumb_worker is not None:
            self.thumb_worker.cancelled = True
            self.thumb_worker.wait(10000)
        super().closeEvent(event)


def main():
    args = sys.argv[1:]
    selftest = None
    if "--selftest" in args:
        idx = args.index("--selftest")
        selftest = args[idx + 1] if idx + 1 < len(args) else None
    app = QApplication(sys.argv)
    window = MainWindow(selftest_folder=selftest)
    window.show()
    if "--smoke" in args:
        QTimer.singleShot(1500, window.close)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

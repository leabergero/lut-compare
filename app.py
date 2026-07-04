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

from PySide6.QtCore import QObject, QRect, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QListView, QMainWindow, QMessageBox,
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
                full, _ = load_image(t["photo"])
                panels = [to_u8(full)]
                for preset, amount in zip(t["presets"], t["amounts"]):
                    render = preset.apply(full) if preset else full
                    panels.append(to_u8(blend(full, render, amount)))
                del full
                big = blend_thirds_u8(panels, t["fade"])
                self.finished_ok.emit({"kind": "zoom", "photo": t["photo"],
                                       "key": t["key"], "panels": panels,
                                       "big": big})
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
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.zoomed = False
        self.cx = 0.5
        self.cy = 0.5

    def set(self, zoomed=None, cx=None, cy=None):
        if zoomed is not None:
            self.zoomed = zoomed
        if cx is not None:
            self.cx = min(max(cx, 0.0), 1.0)
        if cy is not None:
            self.cy = min(max(cy, 0.0), 1.0)
        self.changed.emit()


class SyncView(QWidget):
    """Panel de imagen con zoom 100% sincronizado entre los tres paneles."""

    zoom_requested = Signal()

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.preview = None      # QImage ajustada
        self.full = None         # QImage resolucion completa (para zoom)
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

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), Qt.transparent)
        if self.preview is None:
            p.setPen(Qt.gray)
            p.drawText(self.rect(), Qt.AlignCenter, "sin imagen")
            return
        if not self.state.zoomed:
            img = self.preview
            scale = min(self.width() / img.width(), self.height() / img.height())
            w, h = int(img.width() * scale), int(img.height() * scale)
            target = QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)
            p.drawImage(target, img)
        else:
            img = self.full if self.full is not None else self.preview
            ratio = 1.0 if self.full is not None else 0.35
            vw, vh = int(self.width() / ratio), int(self.height() / ratio)
            sx = int(self.state.cx * img.width() - vw / 2)
            sy = int(self.state.cy * img.height() - vh / 2)
            sx = max(0, min(sx, img.width() - vw))
            sy = max(0, min(sy, img.height() - vh))
            src = QRect(sx, sy, min(vw, img.width()), min(vh, img.height()))
            p.drawImage(self.rect(), img, src)
            if self.full is None:
                p.setPen(Qt.white)
                p.drawText(self.rect().adjusted(6, 6, -6, -6),
                           Qt.AlignTop | Qt.AlignLeft, "procesando 100%...")

    def mousePressEvent(self, event):
        self._press = event.position()
        self._dragged = False

    def mouseMoveEvent(self, event):
        if self._press is None or not self.state.zoomed:
            return
        delta = event.position() - self._press
        if delta.manhattanLength() > 3:
            self._dragged = True
            img = self.full if self.full is not None else self.preview
            if img is None:
                return
            self.state.set(
                cx=self.state.cx - delta.x() / img.width(),
                cy=self.state.cy - delta.y() / img.height(),
            )
            self._press = event.position()

    def mouseReleaseEvent(self, _event):
        if not self._dragged:
            if self.state.zoomed:
                self.state.set(zoomed=False)
            else:
                self.state.set(zoomed=True, cx=0.5, cy=0.5)
                self.zoom_requested.emit()
        self._press = None


class FilmStrip(QListWidget):
    """Tira de miniaturas: la rueda del mouse desplaza horizontalmente."""

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - delta)
        event.accept()


# ---------------------------------------------------------------- ventana


class MainWindow(QMainWindow):
    def __init__(self, selftest_folder=None):
        super().__init__()
        self.setWindowTitle("LUT compare")
        self.resize(1280, 860)
        self.settings = QSettings("LeaB", "LutCompare")

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
        refresh = QPushButton("Releer presets")
        refresh.clicked.connect(self.reload_presets)
        top.addWidget(title)
        top.addWidget(self.file_label)
        top.addStretch(1)
        top.addWidget(refresh)
        layout.addLayout(top)

        names = QHBoxLayout()
        self.zone_labels = []
        for _ in range(3):
            lab = QLabel("")
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("color: gray; font-size: 12px;")
            names.addWidget(lab, 1)
            self.zone_labels.append(lab)
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
        fade_row.addWidget(self.fade_slider)
        fade_row.addWidget(self.hard_cut)
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
                lab = QLabel("Original")
                lab.setAlignment(Qt.AlignCenter)
                col.addWidget(lab)
                self.panel_combos.append(None)
                self.panel_sliders.append(None)
                self.panel_slider_labels.append(None)
            else:
                combo = QComboBox()
                combo.currentIndexChanged.connect(self.on_preset_changed)
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

    def reload_presets(self, startup=False):
        self.presets, errors = load_presets(PRESETS_DIR)
        for i in (1, 2):
            combo = self.panel_combos[i]
            prev = combo.currentText() if combo.count() else self.settings.value(f"preset{i}", "")
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(ninguno)")
            for preset in self.presets:
                combo.addItem(f"{preset.name}  [{preset.kind}]", preset.name)
            idx = combo.findText(prev) if prev else -1
            if idx < 0 and startup and len(self.presets) >= i:
                idx = i  # por defecto: preset 1 y 2 de la lista
            combo.setCurrentIndex(max(idx, 0))
            combo.blockSignals(False)
        if errors:
            self.status_label.setText("Presets con error: " + "; ".join(errors))
        self.on_preset_changed()

    def panel_preset(self, i):
        if i == 0:
            return None
        combo = self.panel_combos[i]
        name = combo.currentData()
        if not name:
            return None
        for preset in self.presets:
            if preset.name == name:
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
        self.view_state.set(zoomed=False)
        self.zoom_cache_key = None
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
        if self.view_state.zoomed:
            self.request_zoom()
        self.schedule_preview()

    def on_preset_changed(self, *_args):
        for i in (1, 2):
            combo = self.panel_combos[i]
            self.settings.setValue(f"preset{i}", combo.currentText())
        self.zone_labels[0].setText("Original")
        self.zone_labels[1].setText(self.panel_label(1))
        self.zone_labels[2].setText(self.panel_label(2))
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
        self.zone_labels[1].setText(self.panel_label(1))
        self.zone_labels[2].setText(self.panel_label(2))
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
        self.big_view.set_preview(u8_to_qimage(result["big"]))
        self.status_label.setText("")

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
        self.big_view.set_full(u8_to_qimage(result["big"]))
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

#!/usr/bin/env python3
"""Crop2Draw: manual crop UI — image / white-text / colored-text modes → draw.io."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw
from io import BytesIO

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import baidu_ocr

MODE_IMAGE = "image"
MODE_TEXT_WHITE = "text_white"
MODE_TEXT_COLOR = "text_color"

PROJECT_EXT = ".c2d"
PROJECT_FORMAT = "crop2draw-project"
PROJECT_VERSION = 1


@dataclass
class CropItem:
    id: str
    source_bbox: list[int]  # x, y, w, h
    file: str = ""
    mode: str = MODE_IMAGE
    medium: str = "crop"  # crop | text
    text: str = ""
    font_size: int = 14
    font_color: str = "#222222"
    font_bold: bool = False
    font_italic: bool = False
    font_underline: bool = False
    fill_rgba: list[int] = field(default_factory=lambda: [255, 255, 255, 255])

    @staticmethod
    def from_dict(d: dict) -> "CropItem":
        known = set(CropItem.__dataclass_fields__.keys())
        data = {k: v for k, v in d.items() if k in known}
        item = CropItem(id=str(data.get("id", "item")), source_bbox=list(data.get("source_bbox", [0, 0, 1, 1])))
        for k, v in data.items():
            if k in ("id", "source_bbox"):
                continue
            setattr(item, k, v)
        if item.mode in (MODE_TEXT_WHITE, MODE_TEXT_COLOR) or item.medium == "text":
            item.medium = "text"
            if item.mode == MODE_IMAGE:
                item.mode = MODE_TEXT_WHITE
        return item


def pil_to_qpixmap(im: Image.Image) -> QPixmap:
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


def qimage_to_pil(qimg: QImage) -> Image.Image:
    qimg = qimg.convertToFormat(QImage.Format.Format_RGBA8888)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    qimg.save(buf, "PNG")
    buf.close()
    return Image.open(BytesIO(bytes(ba))).convert("RGBA")


def fit_image_contain(
    src: Image.Image,
    target_w: int,
    target_h: int,
    bg: tuple[int, int, int, int] = (255, 255, 255, 0),
) -> Image.Image:
    """Scale src with aspect ratio preserved to fit inside target_w×target_h, then pad."""
    src = src.convert("RGBA")
    tw, th = max(1, int(target_w)), max(1, int(target_h))
    sw, sh = src.size
    scale = min(tw / sw, th / sh)
    nw = max(1, int(round(sw * scale)))
    nh = max(1, int(round(sh * scale)))
    # avoid 1px overshoot from rounding
    if nw > tw:
        nw = tw
    if nh > th:
        nh = th
    resized = src.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (tw, th), bg)
    ox = (tw - nw) // 2
    oy = (th - nh) // 2
    canvas.paste(resized, (ox, oy), resized)
    return canvas


def sanitize_id(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", name)
    return name.strip("_") or "crop"


DRAWIO_TEXT_FONT = "Times New Roman"


def find_drawio_exe() -> Optional[Path]:
    """Locate draw.io Desktop for opening exported files."""
    env = os.environ.get("DRAWIO_PATH", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.extend(
        [
            Path(r"F:\drawio\draw.io\draw.io.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "draw.io" / "draw.io.exe",
            Path(r"C:\Program Files\draw.io\draw.io.exe"),
            Path(r"C:\Program Files (x86)\draw.io\draw.io.exe"),
        ]
    )
    for p in candidates:
        if p and p.is_file():
            return p
    return None


def open_drawio_file(path: Path) -> None:
    """Open a .drawio file with Desktop app, else system association."""
    exe = find_drawio_exe()
    if exe is not None:
        subprocess.Popen(
            [str(exe), str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    open_in_file_manager(path)


def open_in_file_manager(path: Path) -> None:
    """Reveal a file/folder in the OS file manager."""
    path = path.resolve()
    if sys.platform.startswith("win"):
        if path.is_dir():
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        if path.is_dir():
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["open", "-R", str(path)])
    else:
        target = path if path.is_dir() else path.parent
        subprocess.Popen(["xdg-open", str(target)])


def text_width_em(s: str) -> float:
    """Approximate advance width in em (CJK≈1, ASCII≈0.5 for Times New Roman)."""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if ch.isspace():
            w += 0.33
        elif o > 0x2E7F:  # CJK / fullwidth-ish
            w += 1.0
        else:
            w += 0.5
    return max(w, 0.01)


def normalize_ocr_text(text: str, box_w: int, box_h: int) -> str:
    """Keep OCR lines, but join fragments when the crop looks like one visual line."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    # Wide & short box → single-line label; Baidu often splits into many rows
    if len(lines) > 1 and box_h > 0 and box_h <= max(28, int(box_w * 0.42)):
        return " ".join(lines)
    return "\n".join(lines)


def fit_font_size(text: str, box_w: int, box_h: int, *, bold: bool = False) -> int:
    """Choose font size so text fits inside the OCR crop box (avoids odd wraps)."""
    lines = (text or "").split("\n") or [""]
    n_lines = max(1, len(lines))
    max_em = max(text_width_em(ln) for ln in lines)
    if bold:
        max_em *= 1.08
    # small padding so draw.io doesn't wrap at the last glyph
    size_w = (box_w * 0.94) / max_em
    size_h = (box_h * 0.88) / n_lines
    return max(8, min(96, int(min(size_w, size_h))))


def drawio_font_style(bold: bool = False, italic: bool = False, underline: bool = False) -> int:
    """draw.io fontStyle bitflags: 1=bold, 2=italic, 4=underline."""
    style = 0
    if bold:
        style |= 1
    if italic:
        style |= 2
    if underline:
        style |= 4
    return style


def estimate_font_color(im: Image.Image) -> str:
    """Pick ink color from crop (dark / saturated pixels), matching original text."""
    rgb = im.convert("RGB")
    # slightly upsample tiny crops for stabler stats
    sample = rgb.resize((max(24, rgb.width // 2), max(24, rgb.height // 2)))
    pixels = list(sample.getdata())
    if not pixels:
        return "#222222"

    # background ≈ lightest / most frequent pale tone
    by_lum = sorted(pixels, key=lambda p: 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2])
    bg = by_lum[int(len(by_lum) * 0.9)]
    bg_lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]

    ink: list[tuple[int, int, int]] = []
    for r, g, b in pixels:
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        sat = max(r, g, b) - min(r, g, b)
        # darker than bg, or colored accent on pale bg
        if lum < bg_lum - 35 or (sat > 40 and lum < bg_lum - 10):
            ink.append((r, g, b))
    if not ink:
        # fallback: darkest quartile
        ink = by_lum[: max(1, len(by_lum) // 4)]

    ink.sort(key=lambda p: 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2])
    r, g, b = ink[len(ink) // 3]
    return f"#{r:02x}{g:02x}{b:02x}"


def auto_neighbor_fill(work: Image.Image, x: int, y: int, w: int, h: int, ring: int = 6) -> tuple[int, int, int, int]:
    """Sample colors just outside the box and return median RGBA."""
    W, H = work.size
    samples: list[tuple[int, int, int]] = []
    for yy in range(max(0, y - ring), min(H, y + h + ring)):
        for xx in range(max(0, x - ring), min(W, x + w + ring)):
            inside = x <= xx < x + w and y <= yy < y + h
            if inside:
                continue
            # only ring band
            near = (
                xx < x
                or xx >= x + w
                or yy < y
                or yy >= y + h
            ) and (
                xx >= x - ring
                and xx < x + w + ring
                and yy >= y - ring
                and yy < y + h + ring
            )
            if not near:
                continue
            r, g, b, *_ = work.getpixel((xx, yy))
            samples.append((r, g, b))
    if not samples:
        return (255, 255, 255, 255)
    samples.sort(key=lambda p: p[0] + p[1] + p[2])
    mid = samples[len(samples) // 2]
    return (mid[0], mid[1], mid[2], 255)


def xml_esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", "&#xa;")
    )


class ModeFlyout(QFrame):
    """Floating mode picker shown to the right of the cursor after drawing a box."""

    mode_chosen = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            """
            ModeFlyout {
                background: #2b2b2e;
                border: 1px solid #6a6a70;
                border-radius: 8px;
            }
            QPushButton {
                text-align: left;
                padding: 8px 12px;
                border: none;
                border-radius: 6px;
                color: #f0f0f0;
                background: transparent;
                font-size: 13px;
            }
            QPushButton:hover { background: #3d5a80; }
            QLabel { color: #bbb; padding: 4px 8px 0 8px; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        layout.addWidget(QLabel("选择处理模式"))
        self._buttons: list[QPushButton] = []
        options = [
            (MODE_IMAGE, "1  图片模式（挖空白）", "#00c878"),
            (MODE_TEXT_WHITE, "2  白底文字（OCR）", "#46a0ff"),
            (MODE_TEXT_COLOR, "3  有色底文字（OCR+填色）", "#ffaa28"),
        ]
        for mode, label, color in options:
            btn = QPushButton(label)
            btn.setStyleSheet(
                f"QPushButton{{border-left: 3px solid {color};}}"
                f"QPushButton:hover{{background:#3d5a80; border-left: 3px solid {color};}}"
            )
            btn.clicked.connect(lambda _=False, m=mode: self.mode_chosen.emit(m))
            layout.addWidget(btn)
            self._buttons.append(btn)
        self.adjustSize()

    def popup_near(self, global_pos: QPoint) -> None:
        self.adjustSize()
        # appear to the right of the cursor
        pos = QPoint(global_pos.x() + 16, global_pos.y() - 20)
        screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            if pos.x() + self.width() > geo.right():
                pos.setX(global_pos.x() - self.width() - 16)
            if pos.y() + self.height() > geo.bottom():
                pos.setY(geo.bottom() - self.height() - 8)
            if pos.y() < geo.top():
                pos.setY(geo.top() + 8)
        self.move(pos)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        mapping = {
            Qt.Key.Key_1: MODE_IMAGE,
            Qt.Key.Key_2: MODE_TEXT_WHITE,
            Qt.Key.Key_3: MODE_TEXT_COLOR,
        }
        mode = mapping.get(event.key())
        if mode is not None:
            self.mode_chosen.emit(mode)
            return
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)


class ImageCanvas(QWidget):
    crop_finished = Signal(int, int, int, int, str)  # x,y,w,h,mode
    hover_moved = Signal(int, int)
    color_picked = Signal(int, int, int)  # RGB
    pending_changed = Signal(str)  # status hint
    selection_ready = Signal(QPoint)  # global cursor pos for mode flyout

    HANDLE_NAMES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

    def __init__(self) -> None:
        super().__init__()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pixmap = QPixmap()
        self._img_w = 0
        self._img_h = 0
        self._scale = 1.0
        self._offset = QPointF(20, 20)
        self._panning = False
        self._drawing = False
        self._pan_start = QPoint()
        self._offset_start = QPointF()
        self._draw_start_img = QPointF()
        self._draw_curr_img = QPointF()
        self._boxes: list[tuple[str, QRectF, str]] = []  # id, rect, mode
        self._draw_mode = True
        self._space_pan = False
        self._pick_color_mode = False
        self._source_for_pick: Optional[Image.Image] = None
        # adjustable selection (confirmed by mode 1/2/3)
        self._pending: Optional[QRectF] = None
        self._drag_kind: str = ""  # "", "move", or handle name
        self._drag_origin_img = QPointF()
        self._drag_rect0 = QRectF()
        self._handle_px = 8

    def clear_pending(self) -> None:
        self._pending = None
        self._drag_kind = ""
        self._drawing = False
        self.update()
        self.pending_changed.emit("")

    def has_pending(self) -> bool:
        return self._pending is not None and self._pending.width() >= 4 and self._pending.height() >= 4

    def confirm_pending(self, mode: str) -> None:
        if not self.has_pending() or self._pending is None:
            return
        r = self._pending.normalized()
        x, y, w, h = int(r.x()), int(r.y()), int(r.width()), int(r.height())
        self.clear_pending()
        self.crop_finished.emit(x, y, w, h, mode)

    def set_pick_color_mode(self, enabled: bool, source: Optional[Image.Image] = None) -> None:
        self._pick_color_mode = enabled
        self._source_for_pick = source
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.set_draw_mode(self._draw_mode)

    def set_image(self, pixmap: QPixmap, width: int, height: int, *, reset_view: bool = True) -> None:
        self._pixmap = pixmap
        self._img_w = width
        self._img_h = height
        if reset_view:
            self._boxes.clear()
            self.clear_pending()
            self.fit_to_view()
        self.update()

    def update_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def set_boxes(self, boxes: list[tuple[str, QRectF, str]]) -> None:
        self._boxes = boxes
        self.update()

    def set_draw_mode(self, enabled: bool) -> None:
        self._draw_mode = enabled
        if not self._pick_color_mode:
            self.setCursor(
                Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.OpenHandCursor
            )

    def fit_to_view(self) -> None:
        if self._img_w <= 0 or self._img_h <= 0:
            return
        margin = 40
        sx = (self.width() - margin) / self._img_w
        sy = (self.height() - margin) / self._img_h
        self._scale = max(0.05, min(sx, sy, 2.0))
        self._offset = QPointF(
            (self.width() - self._img_w * self._scale) / 2,
            (self.height() - self._img_h * self._scale) / 2,
        )
        self.update()

    def zoom_at(self, factor: float, anchor: QPointF) -> None:
        old = self._scale
        new = max(0.05, min(8.0, old * factor))
        if abs(new - old) < 1e-6:
            return
        img_pt = self.widget_to_image(anchor)
        self._scale = new
        after = self.image_to_widget(img_pt)
        self._offset += anchor - after
        self.update()

    def widget_to_image(self, pt: QPointF) -> QPointF:
        return QPointF(
            (pt.x() - self._offset.x()) / self._scale,
            (pt.y() - self._offset.y()) / self._scale,
        )

    def image_to_widget(self, pt: QPointF) -> QPointF:
        return QPointF(
            pt.x() * self._scale + self._offset.x(),
            pt.y() * self._scale + self._offset.y(),
        )

    def clamp_img_rect(self, x0: float, y0: float, x1: float, y1: float) -> QRectF:
        xa, xb = sorted([x0, x1])
        ya, yb = sorted([y0, y1])
        xa = max(0.0, min(float(self._img_w), xa))
        xb = max(0.0, min(float(self._img_w), xb))
        ya = max(0.0, min(float(self._img_h), ya))
        yb = max(0.0, min(float(self._img_h), yb))
        return QRectF(xa, ya, max(0.0, xb - xa), max(0.0, yb - ya))

    def _normalize_pending(self, rect: QRectF) -> QRectF:
        r = rect.normalized()
        if r.width() < 4:
            r.setWidth(4)
        if r.height() < 4:
            r.setHeight(4)
        x2 = min(float(self._img_w), r.x() + r.width())
        y2 = min(float(self._img_h), r.y() + r.height())
        x1 = max(0.0, x2 - r.width())
        y1 = max(0.0, y2 - r.height())
        return QRectF(x1, y1, x2 - x1, y2 - y1)

    def _pending_widget_rect(self) -> Optional[QRectF]:
        if self._pending is None:
            return None
        r = self._pending.normalized()
        tl = self.image_to_widget(r.topLeft())
        br = self.image_to_widget(r.bottomRight())
        return QRectF(tl, br).normalized()

    def _handle_rects(self) -> dict[str, QRectF]:
        wr = self._pending_widget_rect()
        if wr is None:
            return {}
        s = float(self._handle_px)
        cx, cy = wr.center().x(), wr.center().y()
        pts = {
            "nw": wr.topLeft(),
            "n": QPointF(cx, wr.top()),
            "ne": wr.topRight(),
            "e": QPointF(wr.right(), cy),
            "se": wr.bottomRight(),
            "s": QPointF(cx, wr.bottom()),
            "sw": wr.bottomLeft(),
            "w": QPointF(wr.left(), cy),
        }
        out = {}
        for name, pt in pts.items():
            out[name] = QRectF(pt.x() - s / 2, pt.y() - s / 2, s, s)
        return out

    def _hit_handle(self, pos: QPointF) -> str:
        for name, hr in self._handle_rects().items():
            if hr.contains(pos):
                return name
        return ""

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(45, 45, 48))
        if self._pixmap.isNull():
            p.setPen(QColor(200, 200, 200))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "打开图片开始裁切")
            return

        target = QRectF(
            self._offset.x(),
            self._offset.y(),
            self._img_w * self._scale,
            self._img_h * self._scale,
        )
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._scale < 1.5)
        p.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))

        mode_colors = {
            MODE_IMAGE: QColor(0, 200, 120),
            MODE_TEXT_WHITE: QColor(70, 160, 255),
            MODE_TEXT_COLOR: QColor(255, 170, 40),
        }
        for cid, rect, mode in self._boxes:
            tl = self.image_to_widget(rect.topLeft())
            br = self.image_to_widget(rect.bottomRight())
            wr = QRectF(tl, br).normalized()
            color = mode_colors.get(mode, QColor(0, 200, 120))
            p.setPen(QPen(color, 2))
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 40))
            p.drawRect(wr)
            p.setPen(QColor(255, 255, 255))
            p.drawText(wr.adjusted(4, 2, -4, -2), cid)

        # live drawing rubber-band
        if self._drawing:
            rect = self.clamp_img_rect(
                self._draw_start_img.x(),
                self._draw_start_img.y(),
                self._draw_curr_img.x(),
                self._draw_curr_img.y(),
            )
            tl = self.image_to_widget(rect.topLeft())
            br = self.image_to_widget(rect.bottomRight())
            wr = QRectF(tl, br).normalized()
            p.setPen(QPen(QColor(255, 80, 80), 2, Qt.PenStyle.DashLine))
            p.setBrush(QColor(255, 80, 80, 50))
            p.drawRect(wr)

        # pending adjustable selection
        if self._pending is not None:
            wr = self._pending_widget_rect()
            if wr is not None:
                p.setPen(QPen(QColor(255, 64, 64), 2))
                p.setBrush(QColor(255, 64, 64, 45))
                p.drawRect(wr)
                p.setBrush(QColor(255, 255, 255))
                p.setPen(QPen(QColor(255, 64, 64), 1))
                for hr in self._handle_rects().values():
                    p.drawRect(hr)
                r = self._pending.normalized()
                p.setPen(QColor(255, 230, 230))
                p.drawText(
                    wr.adjusted(4, 2, -4, -2),
                    f"{int(r.width())}×{int(r.height())}  |  拖拽微调 · 按 1/2/3 选模式 · Esc取消",
                )

        if self._pick_color_mode:
            p.setPen(QColor(255, 220, 80))
            p.drawText(12, 24, "取色模式：点击画布选取填充颜色（Esc 取消）")
        elif self._pending is not None:
            p.setPen(QColor(255, 220, 80))
            p.drawText(12, 24, "选区未确认：右侧菜单或按 1/2/3 选择模式，Esc 取消")

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.15 if delta > 0 else 1 / 1.15
        self.zoom_at(factor, event.position())

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._pick_color_mode:
                self.set_pick_color_mode(False)
                return
            if self._pending is not None or self._drawing:
                self.clear_pending()
                self.pending_changed.emit("已取消选区")
                return
        mode_keys = {
            Qt.Key.Key_1: MODE_IMAGE,
            Qt.Key.Key_2: MODE_TEXT_WHITE,
            Qt.Key.Key_3: MODE_TEXT_COLOR,
        }
        if self.has_pending() and event.key() in mode_keys:
            self.confirm_pending(mode_keys[event.key()])
            return
        if self._pending is not None and event.key() in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            dx = dy = 0
            if event.key() == Qt.Key.Key_Left:
                dx = -step
            elif event.key() == Qt.Key.Key_Right:
                dx = step
            elif event.key() == Qt.Key.Key_Up:
                dy = -step
            elif event.key() == Qt.Key.Key_Down:
                dy = step
            r = self._pending.translated(dx, dy)
            self._pending = self._normalize_pending(r)
            self.update()
            return
        if event.key() == Qt.Key.Key_Space:
            self._space_pan = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_pan = False
            if self._pick_color_mode:
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.set_draw_mode(self._draw_mode)
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event) -> None:
        if self._pixmap.isNull():
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._pick_color_mode and event.button() == Qt.MouseButton.LeftButton:
            img = self.widget_to_image(event.position())
            x, y = int(img.x()), int(img.y())
            src = self._source_for_pick
            if src is not None and 0 <= x < src.width and 0 <= y < src.height:
                r, g, b, *_ = src.getpixel((x, y))
                self.color_picked.emit(r, g, b)
            return
        if (
            event.button() == Qt.MouseButton.RightButton
            or event.button() == Qt.MouseButton.MiddleButton
            or (
                event.button() == Qt.MouseButton.LeftButton
                and (self._space_pan or not self._draw_mode)
            )
        ):
            # right/middle/space+left: pan canvas
            self._drawing = False
            self._drag_kind = ""
            self._panning = True
            self._pan_start = event.position().toPoint()
            self._offset_start = QPointF(self._offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() != Qt.MouseButton.LeftButton or not self._draw_mode:
            return

        # adjust existing pending selection
        if self._pending is not None:
            handle = self._hit_handle(event.position())
            wr = self._pending_widget_rect()
            if handle:
                self._drag_kind = handle
                self._drag_origin_img = self.widget_to_image(event.position())
                self._drag_rect0 = QRectF(self._pending)
                return
            if wr is not None and wr.contains(event.position()):
                self._drag_kind = "move"
                self._drag_origin_img = self.widget_to_image(event.position())
                self._drag_rect0 = QRectF(self._pending)
                return

        # start a new rubber-band
        self._drawing = True
        if self._pending is not None:
            self._pending = None
            self.pending_changed.emit("")
        self._draw_start_img = self.widget_to_image(event.position())
        self._draw_curr_img = QPointF(self._draw_start_img)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if not self._pixmap.isNull():
            img = self.widget_to_image(event.position())
            self.hover_moved.emit(int(img.x()), int(img.y()))
        if self._panning:
            delta = event.position().toPoint() - self._pan_start
            self._offset = self._offset_start + QPointF(delta)
            self.update()
            return
        if self._drawing:
            self._draw_curr_img = self.widget_to_image(event.position())
            self.update()
            return
        if self._drag_kind and self._pending is not None:
            cur = self.widget_to_image(event.position())
            dx = cur.x() - self._drag_origin_img.x()
            dy = cur.y() - self._drag_origin_img.y()
            r0 = self._drag_rect0
            l, t, ri, b = r0.left(), r0.top(), r0.right(), r0.bottom()
            kind = self._drag_kind
            if kind == "move":
                r = r0.translated(dx, dy)
            else:
                if "w" in kind:
                    l = r0.left() + dx
                if "e" in kind:
                    ri = r0.right() + dx
                if "n" in kind:
                    t = r0.top() + dy
                if "s" in kind:
                    b = r0.bottom() + dy
                r = QRectF(QPointF(l, t), QPointF(ri, b))
            self._pending = self._normalize_pending(r)
            self.update()
            return
        # cursor hint over handles
        if self._pending is not None and not self._pick_color_mode:
            handle = self._hit_handle(event.position())
            cursors = {
                "nw": Qt.CursorShape.SizeFDiagCursor,
                "se": Qt.CursorShape.SizeFDiagCursor,
                "ne": Qt.CursorShape.SizeBDiagCursor,
                "sw": Qt.CursorShape.SizeBDiagCursor,
                "n": Qt.CursorShape.SizeVerCursor,
                "s": Qt.CursorShape.SizeVerCursor,
                "e": Qt.CursorShape.SizeHorCursor,
                "w": Qt.CursorShape.SizeHorCursor,
            }
            if handle:
                self.setCursor(cursors.get(handle, Qt.CursorShape.ArrowCursor))
            else:
                wr = self._pending_widget_rect()
                if wr is not None and wr.contains(event.position()):
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event) -> None:
        if self._panning and event.button() in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.RightButton,
        ):
            self._panning = False
            if self._pick_color_mode:
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.set_draw_mode(self._draw_mode)
            return
        if self._drag_kind and event.button() == Qt.MouseButton.LeftButton:
            self._drag_kind = ""
            self.pending_changed.emit(
                f"选区 {int(self._pending.width())}×{int(self._pending.height())} · 按 1/2/3 选模式"
                if self._pending
                else ""
            )
            return
        if self._drawing and event.button() == Qt.MouseButton.LeftButton:
            self._drawing = False
            self._draw_curr_img = self.widget_to_image(event.position())
            rect = self.clamp_img_rect(
                self._draw_start_img.x(),
                self._draw_start_img.y(),
                self._draw_curr_img.x(),
                self._draw_curr_img.y(),
            )
            if rect.width() >= 4 and rect.height() >= 4:
                self._pending = self._normalize_pending(rect)
                self.pending_changed.emit(
                    f"选区已建立 {int(rect.width())}×{int(rect.height())} · 按 1/2/3 或点右侧菜单"
                )
                self.selection_ready.emit(event.globalPosition().toPoint())
            else:
                self._pending = None
                self.pending_changed.emit("")
            self.update()


class TextReviewDialog(QDialog):
    def __init__(
        self,
        parent,
        preview: QPixmap,
        ocr_text: str,
        mode: str,
        font_color: str,
        box_w: int,
        box_h: int,
        auto_fill_rgba: tuple[int, int, int, int],
        crop_im: Image.Image,
        ocr_profile: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("OCR 结果审核（可修改）")
        self.resize(540, 560)
        self.picked_rgba = list(auto_fill_rgba)
        self.font_color = font_color
        self.box_w = box_w
        self.box_h = box_h
        self.crop_im = crop_im
        self.ocr_profile = ocr_profile

        layout = QVBoxLayout(self)
        self.tip = QLabel()
        self.tip.setWordWrap(True)
        layout.addWidget(self.tip)
        self._refresh_tip()

        pv = QLabel()
        pv.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pv.setMinimumHeight(120)
        pv.setStyleSheet("background:#222; border:1px solid #444;")
        pv.setPixmap(
            preview.scaled(480, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )
        layout.addWidget(pv)

        form = QFormLayout()
        self.text_edit = QTextEdit()
        self.text_edit.setPlainText(ocr_text)
        form.addRow("识别文字（可改）", self.text_edit)
        sw = QLabel()
        sw.setFixedSize(48, 22)
        sw.setStyleSheet(f"background:{font_color}; border:1px solid #888;")
        form.addRow("检测文字色", sw)
        layout.addLayout(form)

        style_row = QHBoxLayout()
        self.chk_bold = QCheckBox("加粗")
        self.chk_italic = QCheckBox("斜体")
        self.chk_underline = QCheckBox("下划线")
        style_row.addWidget(QLabel("文字样式"))
        style_row.addWidget(self.chk_bold)
        style_row.addWidget(self.chk_italic)
        style_row.addWidget(self.chk_underline)
        style_row.addStretch()
        layout.addLayout(style_row)

        if ocr_profile == "standard":
            self.btn_rerun_accurate = QPushButton("用高精度版重新识别")
            self.btn_rerun_accurate.setStyleSheet(
                "QPushButton{background:#6a1b9a;color:white;padding:6px 10px;}"
            )
            self.btn_rerun_accurate.clicked.connect(self.rerun_accurate_ocr)
            layout.addWidget(self.btn_rerun_accurate)

        if mode == MODE_TEXT_COLOR:
            fill_row = QHBoxLayout()
            self.radio_auto = QRadioButton("自动邻域填色")
            self.radio_pick = QRadioButton("取色笔（确认后点画布）")
            self.radio_auto.setChecked(True)
            grp = QButtonGroup(self)
            grp.addButton(self.radio_auto)
            grp.addButton(self.radio_pick)
            self.fill_swatch = QLabel()
            self.fill_swatch.setFixedSize(36, 24)
            self._set_swatch(self.picked_rgba)
            fill_row.addWidget(self.radio_auto)
            fill_row.addWidget(self.radio_pick)
            fill_row.addWidget(QLabel("预览"))
            fill_row.addWidget(self.fill_swatch)
            fill_row.addStretch()
            layout.addLayout(fill_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_tip(self) -> None:
        self.tip.setText(
            f"已用百度 OCR（{baidu_ocr.profile_label(self.ocr_profile)}）预填，请检查文字后确认。\n"
            f"导出为 Times New Roman · 颜色 {self.font_color} · "
            f"字号按裁切框 {self.box_w}×{self.box_h} 自适应。"
        )

    def rerun_accurate_ocr(self) -> None:
        btn = getattr(self, "btn_rerun_accurate", None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("高精度识别中…")
        QApplication.processEvents()
        try:
            text = baidu_ocr.ocr_image(self.crop_im, profile="accurate")
            text = normalize_ocr_text(text or "", self.box_w, self.box_h)
            self.text_edit.setPlainText(text)
            self.ocr_profile = "accurate"
            self._refresh_tip()
            if btn is not None:
                btn.setText("已用高精度版识别（可再点）")
                btn.setEnabled(True)
        except Exception as e:
            QMessageBox.critical(self, "高精度 OCR 失败", str(e))
            if btn is not None:
                btn.setText("用高精度版重新识别")
                btn.setEnabled(True)

    def _set_swatch(self, rgba: list[int]) -> None:
        self.fill_swatch.setStyleSheet(
            f"background: rgb({rgba[0]},{rgba[1]},{rgba[2]}); border:1px solid #888;"
        )

    def result_data(self) -> dict:
        raw = self.text_edit.toPlainText()
        text = normalize_ocr_text(raw, self.box_w, self.box_h)
        bold = self.chk_bold.isChecked()
        data = {
            "text": text,
            "font_size": fit_font_size(text, self.box_w, self.box_h, bold=bold),
            "font_color": self.font_color,
            "font_bold": bold,
            "font_italic": self.chk_italic.isChecked(),
            "font_underline": self.chk_underline.isChecked(),
            "fill_rgba": list(self.picked_rgba),
            "use_eyedropper": bool(
                getattr(self, "radio_pick", None) and self.radio_pick.isChecked()
            ),
        }
        return data


class DropPasteZone(QLabel):
    """Drop / paste image intake area."""

    image_ready = Signal(object)  # PIL.Image

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(160)
        self.setWordWrap(True)
        self.setStyleSheet(
            "QLabel{background:#1e1e22;color:#ccc;border:2px dashed #666;border-radius:8px;padding:12px;}"
        )
        self.setText("将替换图片拖到此处\n或点击后 Ctrl+V 粘贴剪切板\n也可点下方「选择文件」")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        md = event.mimeData()
        if md.hasUrls() or md.hasImage():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        md = event.mimeData()
        im: Optional[Image.Image] = None
        if md.hasUrls():
            for url in md.urls():
                local = url.toLocalFile()
                if local and Path(local).is_file():
                    try:
                        im = Image.open(local).convert("RGBA")
                        break
                    except Exception:
                        continue
        if im is None and md.hasImage():
            qimg = QImage(md.imageData())
            if not qimg.isNull():
                im = qimage_to_pil(qimg)
        if im is not None:
            self.image_ready.emit(im)
            event.acceptProposedAction()
        else:
            event.ignore()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Paste) or (
            event.key() == Qt.Key.Key_V
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.paste_clipboard()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, _event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def paste_clipboard(self) -> bool:
        cb = QApplication.clipboard()
        md = cb.mimeData()
        if md is not None and md.hasImage():
            qimg = cb.image()
            if not qimg.isNull():
                self.image_ready.emit(qimage_to_pil(qimg))
                return True
        # sometimes Windows clipboard stores file paths
        if md is not None and md.hasUrls():
            for url in md.urls():
                local = url.toLocalFile()
                if local and Path(local).is_file():
                    try:
                        self.image_ready.emit(Image.open(local).convert("RGBA"))
                        return True
                    except Exception:
                        continue
        return False


class ImageReplaceDialog(QDialog):
    """Pick a target crop image, intake a replacement, scale contain, overwrite file."""

    def __init__(self, parent, crops: list[CropItem], crops_dir: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("图片替换处理器")
        self.resize(720, 620)
        self.crops = crops
        self.crops_dir = crops_dir
        self.src_im: Optional[Image.Image] = None
        self.fitted_im: Optional[Image.Image] = None

        layout = QVBoxLayout(self)
        tip = QLabel(
            "选择要被替换的目标裁切图 → 拖入/粘贴新图 → 按原比例缩放到目标框内"
            "（不拉伸，居中留边）→ 覆盖保存。draw.io 几何尺寸保持不变。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        form = QFormLayout()
        self.target_combo = QComboBox()
        self._image_indices: list[int] = []
        for i, c in enumerate(crops):
            if c.medium != "crop" and c.mode != MODE_IMAGE:
                continue
            if not c.file:
                continue
            x, y, w, h = c.source_bbox
            self.target_combo.addItem(f"{c.id}  ({w}×{h})  [{x},{y}]", i)
            self._image_indices.append(i)
        form.addRow("目标图片", self.target_combo)
        layout.addLayout(form)

        self.zone = DropPasteZone()
        self.zone.image_ready.connect(self.on_source_image)
        layout.addWidget(self.zone)

        row = QHBoxLayout()
        self.btn_browse = QPushButton("选择文件…")
        self.btn_paste = QPushButton("从剪切板粘贴")
        self.btn_browse.clicked.connect(self.browse_file)
        self.btn_paste.clicked.connect(self.paste_clip)
        row.addWidget(self.btn_browse)
        row.addWidget(self.btn_paste)
        layout.addLayout(row)

        prev = QHBoxLayout()
        self.lbl_target = QLabel("目标预览")
        self.lbl_fitted = QLabel("缩放后预览")
        for lab in (self.lbl_target, self.lbl_fitted):
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setMinimumHeight(140)
            lab.setStyleSheet("background:#222;border:1px solid #444;color:#aaa;")
        prev.addWidget(self.lbl_target)
        prev.addWidget(self.lbl_fitted)
        layout.addLayout(prev)

        self.info = QLabel("")
        self.info.setStyleSheet("color:#9cf;")
        layout.addWidget(self.info)

        buttons = QDialogButtonBox()
        self.btn_replace = buttons.addButton("缩放并替换", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.btn_replace.setEnabled(False)
        buttons.accepted.connect(self.do_replace)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.target_combo.currentIndexChanged.connect(self.refresh_previews)
        if self.target_combo.count() == 0:
            self.zone.setEnabled(False)
            self.btn_browse.setEnabled(False)
            self.btn_paste.setEnabled(False)
            self.info.setText("当前没有可替换的图片元素（仅图片模式裁切可替换）")
        else:
            self.refresh_previews()

    def current_item(self) -> Optional[CropItem]:
        idx = self.target_combo.currentData()
        if idx is None:
            return None
        return self.crops[int(idx)]

    def target_size(self) -> tuple[int, int]:
        item = self.current_item()
        if item is None:
            return (1, 1)
        _, _, w, h = item.source_bbox
        return (max(1, int(w)), max(1, int(h)))

    def browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择替换图片",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif)",
        )
        if not path:
            return
        try:
            self.on_source_image(Image.open(path).convert("RGBA"))
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))

    def paste_clip(self) -> None:
        if not self.zone.paste_clipboard():
            QMessageBox.information(self, "提示", "剪切板里没有图片")

    def on_source_image(self, im: Image.Image) -> None:
        self.src_im = im.convert("RGBA")
        self.zone.setText(f"已载入替换图 {im.width}×{im.height}\n可继续拖入/粘贴更换")
        self.recompute_fit()

    def recompute_fit(self) -> None:
        if self.src_im is None:
            self.fitted_im = None
            self.btn_replace.setEnabled(False)
            self.refresh_previews()
            return
        tw, th = self.target_size()
        self.fitted_im = fit_image_contain(self.src_im, tw, th)
        self.btn_replace.setEnabled(True)
        sw, sh = self.src_im.size
        scale = min(tw / sw, th / sh)
        self.info.setText(
            f"原图 {sw}×{sh} → 比例缩放 ×{scale:.3f} → 画布 {tw}×{th}（居中，不拉伸）"
        )
        self.refresh_previews()

    def refresh_previews(self) -> None:
        item = self.current_item()
        if item is not None:
            path = self.crops_dir / item.file
            if path.exists():
                pix = QPixmap(str(path))
                self.lbl_target.setPixmap(
                    pix.scaled(
                        300,
                        140,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                self.lbl_target.setText("目标文件缺失")
        if self.src_im is not None:
            # recompute when target changes
            tw, th = self.target_size()
            self.fitted_im = fit_image_contain(self.src_im, tw, th)
            self.btn_replace.setEnabled(True)
            sw, sh = self.src_im.size
            scale = min(tw / max(1, sw), th / max(1, sh))
            self.info.setText(
                f"原图 {sw}×{sh} → 比例缩放 ×{scale:.3f} → 画布 {tw}×{th}（居中，不拉伸）"
            )
        if self.fitted_im is not None:
            self.lbl_fitted.setPixmap(
                pil_to_qpixmap(self.fitted_im).scaled(
                    300,
                    140,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        elif self.src_im is None:
            self.lbl_fitted.setText("缩放后预览")

    def do_replace(self) -> None:
        item = self.current_item()
        if item is None or self.fitted_im is None:
            return
        path = self.crops_dir / item.file
        try:
            self.fitted_im.save(path, format="PNG")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return
        QMessageBox.information(
            self,
            "替换完成",
            f"已覆盖：\n{path}\n尺寸 {self.fitted_im.width}×{self.fitted_im.height}\n"
            "重新导出 Draw.io 即可看到效果。",
        )
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Crop2Draw — 手动裁切 → Draw.io（图片 / 文字 OCR）")
        self.resize(1460, 920)

        self.image_path: Path | None = None
        self.original_image: Image.Image | None = None
        self.work_image: Image.Image | None = None
        self.output_dir: Path | None = None
        self.crops: list[CropItem] = []
        self.crop_mode = MODE_IMAGE
        self._pending_color_item: Optional[dict] = None

        self.canvas = ImageCanvas()
        self.canvas.crop_finished.connect(self.on_crop_finished)
        self.canvas.hover_moved.connect(self.on_hover)
        self.canvas.color_picked.connect(self.on_color_picked)
        self.canvas.pending_changed.connect(self.on_pending_changed)
        self.canvas.selection_ready.connect(self.show_mode_flyout)

        self.mode_flyout = ModeFlyout(self)
        self.mode_flyout.mode_chosen.connect(self.on_mode_flyout_chosen)
        self.mode_flyout.hide()

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.currentRowChanged.connect(self.on_select_crop)

        self.preview = QLabel("裁切预览")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(160)
        self.preview.setStyleSheet("background:#222; color:#ddd; border:1px solid #444;")

        side = QWidget()
        side_layout = QVBoxLayout(side)

        side_layout.addWidget(QLabel("OCR 接口（文字模式，默认标准版）"))
        self.ocr_combo = QComboBox()
        self.ocr_combo.addItem("标准版 general_basic", "standard")
        self.ocr_combo.addItem("高精度版 accurate_basic", "accurate")
        self.ocr_combo.currentIndexChanged.connect(self.on_ocr_profile_changed)
        side_layout.addWidget(self.ocr_combo)
        self._init_ocr_combo()

        self.btn_show_modes = QPushButton("显示模式菜单 (1/2/3)")
        self.btn_show_modes.setEnabled(False)
        self.btn_show_modes.setStyleSheet(
            "QPushButton{background:#1565c0;color:white;font-weight:bold;padding:8px;}"
        )
        self.btn_show_modes.clicked.connect(self.reshow_mode_flyout)
        self.btn_cancel_sel = QPushButton("取消选区 (Esc)")
        self.btn_cancel_sel.setEnabled(False)
        self.btn_cancel_sel.clicked.connect(self.cancel_selection)
        conf_row = QHBoxLayout()
        conf_row.addWidget(self.btn_show_modes)
        conf_row.addWidget(self.btn_cancel_sel)
        side_layout.addLayout(conf_row)

        side_layout.addWidget(QLabel("已裁切元素"))
        side_layout.addWidget(self.list, stretch=1)
        side_layout.addWidget(self.preview)

        btn_row = QHBoxLayout()
        self.btn_rename = QPushButton("重命名")
        self.btn_delete = QPushButton("删除选中（可多选）")
        self.btn_export = QPushButton("导出 Draw.io")
        self.btn_export_open = QPushButton("一键导出并打开")
        self.btn_open_crops = QPushButton("打开裁切图片文件夹")
        self.btn_replace = QPushButton("图片替换处理器")
        self.btn_export_project = QPushButton("导出工程文件 (.c2d)")
        self.btn_open_project = QPushButton("打开工程文件 (.c2d)")
        self.btn_rename.clicked.connect(self.rename_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_export.clicked.connect(lambda: self.export_drawio(open_after=False))
        self.btn_export_open.clicked.connect(lambda: self.export_drawio(open_after=True))
        self.btn_open_crops.clicked.connect(self.open_crops_folder)
        self.btn_replace.clicked.connect(self.open_replace_processor)
        self.btn_export_project.clicked.connect(self.export_project)
        self.btn_open_project.clicked.connect(self.open_project_dialog)
        self.btn_export.setStyleSheet(
            "QPushButton{background:#2e7d32;color:white;font-weight:bold;padding:8px;}"
        )
        self.btn_export_open.setStyleSheet(
            "QPushButton{background:#1565c0;color:white;font-weight:bold;padding:8px;}"
        )
        self.btn_replace.setStyleSheet(
            "QPushButton{background:#ef6c00;color:white;font-weight:bold;padding:8px;}"
        )
        self.btn_export_project.setStyleSheet(
            "QPushButton{background:#6a1b9a;color:white;font-weight:bold;padding:8px;}"
        )
        self.btn_open_project.setStyleSheet(
            "QPushButton{background:#4527a0;color:white;font-weight:bold;padding:8px;}"
        )
        btn_row.addWidget(self.btn_rename)
        btn_row.addWidget(self.btn_delete)
        side_layout.addLayout(btn_row)
        side_layout.addWidget(self.btn_export)
        side_layout.addWidget(self.btn_export_open)
        side_layout.addWidget(self.btn_open_crops)
        side_layout.addWidget(self.btn_replace)
        side_layout.addWidget(self.btn_export_project)
        side_layout.addWidget(self.btn_open_project)

        tip = QLabel(
            "① 拖拽画框 ② 鼠标右侧出现模式菜单\n"
            "③ 点选或按 1/2/3：图片 / 白底文字 / 有色底文字\n"
            "④ 可拖角落微调后再按快捷键\n"
            "导出层级：先裁的在上，后裁的在下\n"
            "右键拖拽 / 中键 / 空格+左键 平移画布\n"
            ".c2d 工程可发给他人继续裁切\n"
            "列表可 Ctrl/Shift 多选后删除 · Esc取消选区"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#aaa;")
        side_layout.addWidget(tip)

        splitter = QSplitter()
        splitter.addWidget(self.canvas)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        tb = QToolBar()
        self.addToolBar(tb)
        act_open = QAction("打开图片", self)
        act_open.triggered.connect(self.open_image)
        act_open_proj = QAction("打开工程", self)
        act_open_proj.triggered.connect(self.open_project_dialog)
        act_save_proj = QAction("导出工程", self)
        act_save_proj.triggered.connect(self.export_project)
        act_fit = QAction("适应窗口", self)
        act_fit.triggered.connect(self.canvas.fit_to_view)
        act_out = QAction("选择输出目录", self)
        act_out.triggered.connect(self.choose_output_dir)
        act_test = QAction("测试 OCR 连通", self)
        act_test.triggered.connect(self.test_ocr)
        tb.addAction(act_open)
        tb.addAction(act_open_proj)
        tb.addAction(act_save_proj)
        tb.addAction(act_fit)
        tb.addAction(act_out)
        tb.addAction(act_test)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._update_ocr_status_hint()

    def _init_ocr_combo(self) -> None:
        profile = "standard"
        try:
            secrets = baidu_ocr.load_secrets()
            profile = secrets.get("ocr_profile", "standard")
            if profile not in ("accurate", "standard"):
                profile = "standard"
        except Exception:
            profile = "standard"
        try:
            baidu_ocr.set_profile(profile)
        except Exception:
            pass
        idx = self.ocr_combo.findData(profile)
        if idx < 0:
            idx = self.ocr_combo.findData("standard")
        if idx >= 0:
            self.ocr_combo.blockSignals(True)
            self.ocr_combo.setCurrentIndex(idx)
            self.ocr_combo.blockSignals(False)

    def _update_ocr_status_hint(self) -> None:
        try:
            baidu_ocr.load_secrets()
            self.status.showMessage(
                f"百度 OCR 已加载 · 当前 {baidu_ocr.profile_label()} · 请打开图片开始"
            )
        except Exception as e:
            self.status.showMessage(f"OCR 未就绪: {e}")

    def on_ocr_profile_changed(self) -> None:
        profile = self.ocr_combo.currentData()
        if not profile:
            return
        try:
            baidu_ocr.set_profile(profile)
            self.status.showMessage(f"OCR 接口：{baidu_ocr.profile_label()}")
        except Exception as e:
            QMessageBox.warning(self, "OCR 配置", str(e))

    def show_mode_flyout(self, global_pos: QPoint) -> None:
        if not self.canvas.has_pending():
            return
        self.mode_flyout.popup_near(global_pos)

    def reshow_mode_flyout(self) -> None:
        if not self.canvas.has_pending():
            return
        self.mode_flyout.popup_near(QCursor.pos())

    def on_mode_flyout_chosen(self, mode: str) -> None:
        self.mode_flyout.hide()
        if self.canvas.has_pending():
            self.canvas.confirm_pending(mode)

    def on_pending_changed(self, msg: str) -> None:
        has = self.canvas.has_pending()
        self.btn_show_modes.setEnabled(has)
        self.btn_cancel_sel.setEnabled(has)
        if not has:
            self.mode_flyout.hide()
        if msg:
            self.status.showMessage(msg)

    def cancel_selection(self) -> None:
        self.mode_flyout.hide()
        self.canvas.clear_pending()
        self.status.showMessage("已取消选区")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        mode_keys = {
            Qt.Key.Key_1: MODE_IMAGE,
            Qt.Key.Key_2: MODE_TEXT_WHITE,
            Qt.Key.Key_3: MODE_TEXT_COLOR,
        }
        if self.canvas.has_pending() and event.key() in mode_keys:
            self.mode_flyout.hide()
            self.canvas.confirm_pending(mode_keys[event.key()])
            return
        if event.key() == Qt.Key.Key_Escape and self.canvas.has_pending():
            self.cancel_selection()
            return
        super().keyPressEvent(event)

    def ensure_output_dir(self) -> Path | None:
        if self.output_dir is not None:
            return self.output_dir
        if self.image_path is None:
            return None
        default = self.image_path.parent / f"{self.image_path.stem}_manual_crops"
        self.output_dir = default
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "crops").mkdir(exist_ok=True)
        return self.output_dir

    def choose_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if not path:
            return
        self.output_dir = Path(path)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "crops").mkdir(exist_ok=True)
        self.status.showMessage(f"输出目录: {self.output_dir}")

    def open_crops_folder(self) -> None:
        """Open the folder that stores cropped PNGs (for manual replace)."""
        out = self.ensure_output_dir()
        if out is None:
            QMessageBox.information(self, "提示", "请先打开一张图片")
            return
        crops_dir = out / "crops"
        crops_dir.mkdir(parents=True, exist_ok=True)
        try:
            open_in_file_manager(crops_dir)
            self.status.showMessage(f"已打开裁切文件夹: {crops_dir}")
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"{crops_dir}\n\n{e}")

    def open_replace_processor(self) -> None:
        out = self.ensure_output_dir()
        if out is None:
            QMessageBox.information(self, "提示", "请先打开一张图片")
            return
        crops_dir = out / "crops"
        crops_dir.mkdir(parents=True, exist_ok=True)
        dlg = ImageReplaceDialog(self, self.crops, crops_dir)
        # preselect current list item if it is an image crop
        row = self.list.currentRow()
        if 0 <= row < len(self.crops):
            for i in range(dlg.target_combo.count()):
                if dlg.target_combo.itemData(i) == row:
                    dlg.target_combo.setCurrentIndex(i)
                    break
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # refresh preview for current selection
            row = self.list.currentRow()
            self.refresh_list()
            if 0 <= row < len(self.crops):
                self.list.setCurrentRow(row)
                self.on_select_crop(row)
            self.status.showMessage("图片替换完成 · 请重新导出 Draw.io")

    def test_ocr(self) -> None:
        try:
            profile = self.ocr_combo.currentData() or baidu_ocr.get_profile()
            baidu_ocr.set_profile(profile)
            token = baidu_ocr.get_access_token(force=True, profile=profile)
            endpoint = baidu_ocr.PROFILES[profile]["endpoint"].rsplit("/", 1)[-1]
            QMessageBox.information(
                self,
                "OCR 连通成功",
                f"已拿到 access_token（前 12 位）：{token[:12]}...\n"
                f"当前：{baidu_ocr.profile_label(profile)}（{endpoint}）",
            )
        except Exception as e:
            QMessageBox.critical(self, "OCR 失败", str(e))

    def load_image_path(self, path: Path) -> None:
        self.image_path = path
        self.original_image = Image.open(path).convert("RGBA")
        self.work_image = self.original_image.copy()
        self.crops.clear()
        self.canvas.set_image(
            pil_to_qpixmap(self.work_image),
            self.work_image.width,
            self.work_image.height,
            reset_view=True,
        )
        self.refresh_list()
        out = self.ensure_output_dir()
        manifest = out / "icons.json" if out else None
        if manifest and manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                self.crops = [CropItem.from_dict(d) for d in data]
                self.rebuild_work_image()
                self.refresh_list()
                self.status.showMessage(
                    f"已加载 {len(self.crops)} 个裁切 · {path.name}"
                )
                return
            except Exception:
                pass
        self.status.showMessage(
            f"已打开 {path.name} ({self.original_image.width}×{self.original_image.height})"
        )

    def open_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开图片",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if path:
            self.load_image_path(Path(path))

    def export_project(self) -> None:
        """Pack source image + crops + manifest into a transferable .c2d project file."""
        if self.original_image is None or self.image_path is None:
            QMessageBox.information(self, "提示", "请先打开图片或工程")
            return
        out = self.ensure_output_dir()
        if out is None:
            return
        self.save_manifest()

        default_name = f"{self.image_path.stem}.c2d"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出工程文件",
            str(self.image_path.parent / default_name),
            f"Crop2Draw Project (*{PROJECT_EXT})",
        )
        if not path:
            return
        dest = Path(path)
        if dest.suffix.lower() != PROJECT_EXT:
            dest = dest.with_suffix(PROJECT_EXT)

        # Prefer PNG for lossless handoff; keep original suffix hint in metadata
        src_suffix = self.image_path.suffix.lower() or ".png"
        if src_suffix not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            src_suffix = ".png"
        source_name = f"source{src_suffix}"

        try:
            with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                # source image bytes
                buf = BytesIO()
                save_fmt = "PNG" if src_suffix == ".png" else ("JPEG" if src_suffix in (".jpg", ".jpeg") else "PNG")
                if save_fmt == "JPEG":
                    self.original_image.convert("RGB").save(buf, format="JPEG", quality=95)
                    source_name = "source.jpg"
                else:
                    self.original_image.save(buf, format="PNG")
                    source_name = "source.png"
                zf.writestr(source_name, buf.getvalue())

                crops_meta = []
                for item in self.crops:
                    d = asdict(item)
                    crops_meta.append(d)
                    if item.file:
                        crop_path = out / "crops" / item.file
                        if crop_path.exists():
                            zf.write(crop_path, arcname=f"crops/{item.file}")

                project = {
                    "format": PROJECT_FORMAT,
                    "version": PROJECT_VERSION,
                    "app": "Crop2Draw",
                    "source": source_name,
                    "source_name": self.image_path.name,
                    "image_size": [self.original_image.width, self.original_image.height],
                    "crops": crops_meta,
                }
                zf.writestr(
                    "project.json",
                    json.dumps(project, indent=2, ensure_ascii=False),
                )
                # also embed icons.json for compatibility with folder workflow
                zf.writestr(
                    "icons.json",
                    json.dumps(crops_meta, indent=2, ensure_ascii=False),
                )
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
            return

        QMessageBox.information(
            self,
            "工程已导出",
            f"已保存工程文件：\n{dest}\n\n"
            f"包含原图 + {len(self.crops)} 个裁切。\n"
            "发给对方后，用「打开工程文件」即可继续编辑。",
        )
        self.status.showMessage(f"已导出工程 {dest.name} · {len(self.crops)} 个裁切")

    def open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开工程文件",
            str(Path.home()),
            f"Crop2Draw Project (*{PROJECT_EXT});;Zip (*.zip)",
        )
        if path:
            self.open_project_path(Path(path))

    def open_project_path(self, package: Path) -> None:
        """Unpack a .c2d project and resume editing."""
        if not package.exists():
            QMessageBox.warning(self, "打开失败", f"文件不存在：\n{package}")
            return
        # Workspace next to the package so B can keep working and re-export
        work_dir = package.parent / f"{package.stem}_work"
        try:
            if work_dir.exists():
                # refresh extract (overwrite) so received updates apply
                shutil.rmtree(work_dir)
            work_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(package, "r") as zf:
                # basic safety: reject path traversal
                for name in zf.namelist():
                    target = (work_dir / name).resolve()
                    if not str(target).startswith(str(work_dir.resolve())):
                        raise ValueError(f"非法路径: {name}")
                zf.extractall(work_dir)
        except Exception as e:
            QMessageBox.critical(self, "打开工程失败", str(e))
            return

        project_path = work_dir / "project.json"
        icons_path = work_dir / "icons.json"
        try:
            if project_path.exists():
                project = json.loads(project_path.read_text(encoding="utf-8"))
            elif icons_path.exists():
                project = {
                    "format": PROJECT_FORMAT,
                    "version": 1,
                    "source": "source.png",
                    "crops": json.loads(icons_path.read_text(encoding="utf-8")),
                }
            else:
                raise FileNotFoundError("工程内缺少 project.json / icons.json")
        except Exception as e:
            QMessageBox.critical(self, "工程损坏", str(e))
            return

        source_rel = project.get("source") or "source.png"
        source_path = work_dir / source_rel
        if not source_path.exists():
            # fallback: first image-like file at root
            candidates = list(work_dir.glob("source.*")) + list(work_dir.glob("*.png"))
            candidates = [p for p in candidates if p.is_file() and p.name != "icons.json"]
            if not candidates:
                QMessageBox.critical(self, "工程损坏", "找不到原图 source.*")
                return
            source_path = candidates[0]

        crops_dir = work_dir / "crops"
        crops_dir.mkdir(exist_ok=True)

        # Persist icons.json for subsequent saves
        crops_data = project.get("crops") or []
        icons_path.write_text(
            json.dumps(crops_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        self.output_dir = work_dir
        self.image_path = source_path
        self.original_image = Image.open(source_path).convert("RGBA")
        self.work_image = self.original_image.copy()
        self.crops = [CropItem.from_dict(d) for d in crops_data]
        self.rebuild_work_image()
        self.canvas.set_image(
            pil_to_qpixmap(self.work_image),
            self.work_image.width,
            self.work_image.height,
            reset_view=True,
        )
        self.refresh_list()
        self.status.showMessage(
            f"已打开工程 {package.name} · {len(self.crops)} 个裁切 · 工作目录 {work_dir.name}"
        )
        QMessageBox.information(
            self,
            "工程已打开",
            f"已恢复 {len(self.crops)} 个裁切。\n\n"
            f"工作目录：\n{work_dir}\n\n"
            "可继续裁切；完成后可再「导出工程文件」发回。",
        )

    def rebuild_work_image(self) -> None:
        if self.original_image is None:
            return
        self.work_image = self.original_image.copy()
        draw = ImageDraw.Draw(self.work_image)
        for c in self.crops:
            x, y, w, h = c.source_bbox
            fill = tuple(c.fill_rgba[:4]) if c.fill_rgba else (255, 255, 255, 255)
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=fill)
        self.canvas.update_pixmap(pil_to_qpixmap(self.work_image))

    def punch_region(self, x: int, y: int, w: int, h: int, fill_rgba: list[int]) -> None:
        if self.work_image is None:
            return
        draw = ImageDraw.Draw(self.work_image)
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=tuple(fill_rgba[:4]))
        self.canvas.update_pixmap(pil_to_qpixmap(self.work_image))

    def on_hover(self, x: int, y: int) -> None:
        if self.work_image is None:
            return
        mode_name = {
            MODE_IMAGE: "图片",
            MODE_TEXT_WHITE: "白底文字",
            MODE_TEXT_COLOR: "有色底文字",
        }.get(self.crop_mode, self.crop_mode)
        self.status.showMessage(
            f"{self.image_path.name if self.image_path else ''}  "
            f"模式={mode_name}  ({x},{y})  zoom={self.canvas._scale:.2f}  已裁={len(self.crops)}"
        )

    def unique_id(self, base: str) -> str:
        cid = sanitize_id(base)
        existing = {c.id for c in self.crops}
        root = cid
        n = 2
        while cid in existing:
            cid = f"{root}_{n}"
            n += 1
        return cid

    def on_crop_finished(self, x: int, y: int, w: int, h: int, mode: str) -> None:
        """Called after mode 1/2/3 is chosen for the pending selection."""
        if self.work_image is None:
            return
        self.mode_flyout.hide()
        if mode not in (MODE_IMAGE, MODE_TEXT_WHITE, MODE_TEXT_COLOR):
            self.status.showMessage("未知模式，已取消")
            return
        self.crop_mode = mode
        if mode == MODE_IMAGE:
            self._save_image_crop(x, y, w, h)
        else:
            self._save_text_crop(x, y, w, h, mode)

    def _save_image_crop(self, x: int, y: int, w: int, h: int) -> None:
        cid = self.unique_id(f"img_{len(self.crops)+1:03d}")
        out = self.ensure_output_dir()
        if out is None:
            return
        crop_dir = out / "crops"
        crop_dir.mkdir(exist_ok=True)
        crop_im = self.work_image.crop((x, y, x + w, y + h))
        filename = f"{cid}.png"
        crop_im.save(crop_dir / filename)
        item = CropItem(
            id=cid,
            source_bbox=[x, y, w, h],
            file=filename,
            mode=MODE_IMAGE,
            medium="crop",
            fill_rgba=[255, 255, 255, 255],
        )
        self.crops.append(item)
        self.punch_region(x, y, w, h, item.fill_rgba)
        self.save_manifest()
        self.refresh_list()
        self.list.setCurrentRow(len(self.crops) - 1)
        self.status.showMessage(f"已保存图片 {cid}（{w}×{h}）")

    def _save_text_crop(self, x: int, y: int, w: int, h: int, mode: str) -> None:
        assert self.work_image is not None
        crop_im = self.work_image.crop((x, y, x + w, y + h))
        self.status.showMessage("正在 OCR…")
        QApplication.processEvents()
        try:
            ocr_text = baidu_ocr.ocr_image(crop_im)
        except Exception as e:
            QMessageBox.critical(self, "OCR 失败", str(e))
            self.status.showMessage("OCR 失败")
            return

        ocr_text = normalize_ocr_text(ocr_text or "", w, h)
        font_color = estimate_font_color(crop_im)
        auto_fill = (
            (255, 255, 255, 255)
            if mode == MODE_TEXT_WHITE
            else auto_neighbor_fill(self.work_image, x, y, w, h)
        )
        dlg = TextReviewDialog(
            self,
            pil_to_qpixmap(crop_im),
            ocr_text,
            mode,
            font_color,
            w,
            h,
            auto_fill,
            crop_im,
            baidu_ocr.get_profile(),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.status.showMessage("已取消文字裁切")
            return
        data = dlg.result_data()
        if not data["text"].strip():
            QMessageBox.warning(self, "提示", "文字为空，已取消")
            return

        pending = {
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "mode": mode,
            "crop_im": crop_im,
            "name": f"text_{len(self.crops)+1:03d}",
            **data,
        }
        if mode == MODE_TEXT_COLOR and data["use_eyedropper"]:
            self._pending_color_item = pending
            self.canvas.set_pick_color_mode(True, self.work_image)
            QMessageBox.information(
                self,
                "取色笔",
                "请在画布上点击要填充的颜色。\n按 Esc 可取消取色。",
            )
            self.status.showMessage("取色模式：点击画布选色")
            return

        self._commit_text_item(pending)

    def on_color_picked(self, r: int, g: int, b: int) -> None:
        if not self._pending_color_item:
            return
        self.canvas.set_pick_color_mode(False)
        pending = self._pending_color_item
        self._pending_color_item = None
        pending["fill_rgba"] = [r, g, b, 255]
        self._commit_text_item(pending)

    def _commit_text_item(self, pending: dict) -> None:
        out = self.ensure_output_dir()
        if out is None:
            return
        cid = self.unique_id(pending["name"])
        crop_dir = out / "crops"
        crop_dir.mkdir(exist_ok=True)
        # keep a reference snapshot for preview (before punch)
        filename = f"{cid}.png"
        pending["crop_im"].save(crop_dir / filename)

        fill = list(pending.get("fill_rgba") or [255, 255, 255, 255])
        bw, bh = int(pending["w"]), int(pending["h"])
        text = pending["text"]
        bold = bool(pending.get("font_bold", False))
        # Always re-fit to the OCR crop box so draw.io geometry matches selection
        font_size = fit_font_size(text, bw, bh, bold=bold)
        item = CropItem(
            id=cid,
            source_bbox=[pending["x"], pending["y"], bw, bh],
            file=filename,
            mode=pending["mode"],
            medium="text",
            text=text,
            font_size=font_size,
            font_color=pending["font_color"],
            font_bold=bold,
            font_italic=bool(pending.get("font_italic", False)),
            font_underline=bool(pending.get("font_underline", False)),
            fill_rgba=fill,
        )
        self.crops.append(item)
        self.punch_region(pending["x"], pending["y"], pending["w"], pending["h"], fill)
        self.save_manifest()
        self.refresh_list()
        self.list.setCurrentRow(len(self.crops) - 1)
        self.status.showMessage(f"已保存文字层 {cid}")

    def save_manifest(self) -> None:
        out = self.ensure_output_dir()
        if out is None:
            return
        data = [asdict(c) for c in self.crops]
        # don't serialize PIL
        (out / "icons.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def refresh_list(self) -> None:
        self.list.clear()
        boxes: list[tuple[str, QRectF, str]] = []
        for c in self.crops:
            x, y, w, h = c.source_bbox
            tag = {"image": "图", "text_white": "白字", "text_color": "色字"}.get(c.mode, c.mode)
            label = f"[{tag}] {c.id}  [{x},{y},{w},{h}]"
            if c.medium == "text" and c.text:
                one = c.text.replace("\n", " ")[:24]
                label += f"  “{one}”"
            self.list.addItem(QListWidgetItem(label))
            boxes.append((c.id, QRectF(x, y, w, h), c.mode))
        self.canvas.set_boxes(boxes)

    def on_select_crop(self, row: int) -> None:
        if row < 0 or row >= len(self.crops):
            self.preview.setText("裁切预览")
            self.preview.setPixmap(QPixmap())
            return
        out = self.ensure_output_dir()
        if out is None:
            return
        item = self.crops[row]
        path = out / "crops" / item.file
        if item.file and path.exists():
            pix = QPixmap(str(path))
            self.preview.setPixmap(
                pix.scaled(
                    self.preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.preview.setText(item.text or item.id)

    def rename_selected(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        old = self.crops[row]
        name, ok = QInputDialog.getText(self, "重命名", "新名称：", text=old.id)
        if not ok or not name.strip():
            return
        new_id = self.unique_id(name) if sanitize_id(name) != old.id else old.id
        # if unique_id added suffix unnecessarily when same after sanitize
        new_id = sanitize_id(name)
        existing = {c.id for i, c in enumerate(self.crops) if i != row}
        base = new_id
        n = 2
        while new_id in existing:
            new_id = f"{base}_{n}"
            n += 1
        out = self.ensure_output_dir()
        if out is None:
            return
        if old.file:
            old_path = out / "crops" / old.file
            new_file = f"{new_id}.png"
            new_path = out / "crops" / new_file
            if old_path.exists():
                if new_path.exists() and new_file != old.file:
                    QMessageBox.warning(self, "冲突", f"{new_file} 已存在")
                    return
                old_path.rename(new_path)
                old.file = new_file
        old.id = new_id
        self.save_manifest()
        self.refresh_list()
        self.list.setCurrentRow(row)

    def delete_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.list.selectedIndexes()}, reverse=True)
        if not rows:
            return
        names = [self.crops[r].id for r in rows if 0 <= r < len(self.crops)]
        if not names:
            return
        msg = f"删除选中的 {len(names)} 项？\n" + "、".join(names[:12])
        if len(names) > 12:
            msg += "…"
        if QMessageBox.question(self, "确认删除", msg) != QMessageBox.StandardButton.Yes:
            return
        out = self.ensure_output_dir()
        for row in rows:
            if row < 0 or row >= len(self.crops):
                continue
            item = self.crops[row]
            if out is not None and item.file:
                path = out / "crops" / item.file
                if path.exists():
                    path.unlink()
            del self.crops[row]
        self.save_manifest()
        self.rebuild_work_image()
        self.refresh_list()

    def export_drawio(self, *, open_after: bool = False) -> Optional[Path]:
        if not self.crops:
            QMessageBox.information(self, "提示", "还没有裁切任何元素")
            return None
        if self.original_image is None or self.image_path is None:
            return None
        out = self.ensure_output_dir()
        if out is None:
            return None

        page_w, page_h = self.original_image.size
        cells: list[str] = []
        cid = 2

        if open_after:
            # one-click: auto include translucent base when there are text layers
            include_base = any(c.medium == "text" for c in self.crops)
        else:
            include_base = (
                QMessageBox.question(
                    self,
                    "导出选项",
                    "是否在底层放一张半透明原图方便对照？\n（文字层建议选“是”，便于核对）",
                )
                == QMessageBox.StandardButton.Yes
            )

        def image_cell(cell_id: int, x: int, y: int, w: int, h: int, png_path: Path, opacity: float = 1.0) -> str:
            b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
            op = f"opacity={int(opacity*100)};" if opacity < 0.999 else ""
            style = (
                f"shape=image;verticalLabelPosition=bottom;labelBackgroundColor=none;"
                f"verticalAlign=top;aspect=fixed;imageAspect=0;{op}"
                f"image=data:image/png%3Bbase64,{b64};"
            )
            return (
                f'<mxCell id="{cell_id}" value="" style="{style}" vertex="1" parent="1">'
                f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>'
                f"</mxCell>"
            )

        def text_cell(cell_id: int, item: CropItem) -> str:
            # Geometry is exactly the OCR crop box (source_bbox)
            x, y, w, h = item.source_bbox
            text = item.text or ""
            bold = bool(getattr(item, "font_bold", False))
            italic = bool(getattr(item, "font_italic", False))
            underline = bool(getattr(item, "font_underline", False))
            font_size = fit_font_size(text, w, h, bold=bold)
            n_lines = max(1, text.count("\n") + 1) if text else 1
            # Single-line: no wrap (avoids mid-word breaks). Multi-line: wrap inside box.
            wrap = "whiteSpace=wrap;" if n_lines > 1 else "whiteSpace=nowrap;"
            fstyle = drawio_font_style(bold, italic, underline)
            font_style_attr = f"fontStyle={fstyle};" if fstyle else ""
            style = (
                f"text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;"
                f"{wrap}rounded=0;fontFamily={DRAWIO_TEXT_FONT};fontSize={font_size};"
                f"fontColor={item.font_color};{font_style_attr}"
                f"spacing=0;spacingTop=0;spacingBottom=0;"
                f"spacingLeft=0;spacingRight=0;overflow=hidden;"
            )
            return (
                f'<mxCell id="{cell_id}" value="{xml_esc(text)}" style="{style}" '
                f'vertex="1" parent="1">'
                f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>'
                f"</mxCell>"
            )

        if include_base:
            cells.append(image_cell(cid, 0, 0, page_w, page_h, self.image_path, opacity=0.22))
            cid += 1

        missing = []
        # draw.io: later cells paint above earlier ones → export reverse so first crop is on top
        for item in reversed(self.crops):
            x, y, w, h = item.source_bbox
            if item.medium == "text":
                cells.append(text_cell(cid, item))
                cid += 1
            else:
                png = out / "crops" / item.file
                if not png.exists():
                    missing.append(item.id)
                    continue
                cells.append(image_cell(cid, x, y, w, h, png))
                cid += 1

        if missing:
            QMessageBox.warning(self, "缺失文件", "以下图片裁切丢失：\n" + "\n".join(missing))

        xml = f"""<mxfile host="app.diagrams.net" agent="Crop2Draw" version="22.1.0" type="device">
  <diagram id="manual" name="{self.image_path.stem}">
    <mxGraphModel dx="1400" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{page_w}" pageHeight="{page_h}" math="0" shadow="0">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        {''.join(cells)}
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""
        drawio_path = out / f"{self.image_path.stem}_manual.drawio"
        drawio_path.write_text(xml, encoding="utf-8")
        self.save_manifest()
        n_text = sum(1 for c in self.crops if c.medium == "text")
        n_img = len(self.crops) - n_text

        if open_after:
            try:
                open_drawio_file(drawio_path)
                self.status.showMessage(
                    f"已导出并打开 · 图片{n_img} · 文字{n_text} · {drawio_path}"
                )
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "已导出，但打开失败",
                    f"文件已保存：\n{drawio_path}\n\n打开失败：{e}\n"
                    "可设置环境变量 DRAWIO_PATH 指向 draw.io.exe",
                )
                self.status.showMessage(f"已导出 {drawio_path}（打开失败）")
        else:
            QMessageBox.information(
                self,
                "导出完成",
                f"已生成：\n{drawio_path}\n\n图片层 {n_img} · 可编辑文字层 {n_text}",
            )
            self.status.showMessage(f"已导出 {drawio_path}")
        return drawio_path


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if path.exists():
            if path.suffix.lower() in (PROJECT_EXT, ".zip") and zipfile.is_zipfile(path):
                # .c2d is a zip package; plain zips may also be projects
                try:
                    with zipfile.ZipFile(path, "r") as zf:
                        names = set(zf.namelist())
                    if "project.json" in names or "icons.json" in names:
                        win.open_project_path(path)
                    else:
                        win.load_image_path(path)
                except Exception:
                    win.open_project_path(path)
            else:
                win.load_image_path(path)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

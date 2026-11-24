"""Рамка обрезки поверх картинки: свободная или с фиксированным соотношением сторон."""
import json
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsObject

RATIOS_FILE = Path(__file__).resolve().parent.parent / "ratios.json"

_FALLBACK = {
    "standard": ["1:1", "3:2", "4:3", "16:9", "9:16"],
    "gemini": {"1K": {"1:1": [1024, 1024], "3:2": [1264, 848], "4:3": [1200, 896], "16:9": [1376, 768],
                      "9:16": [768, 1376]},
               "2K": {"1:1": [2048, 2048], "3:2": [2528, 1696], "4:3": [2400, 1792], "16:9": [2752, 1536],
                      "9:16": [1536, 2752]}},
}


def load_ratios():
    try:
        data = json.loads(RATIOS_FILE.read_text(encoding="utf-8"))
        if "standard" in data and "gemini" in data:
            return data
    except (OSError, ValueError):
        pass
    return _FALLBACK


def parse_ratio(text: str) -> float:
    a, b = text.split(":")
    return float(a) / float(b)


class CropItem(QGraphicsObject):
    rect_changed = Signal()
    MIN = 8.0
    HANDLE_CURSORS = {0: Qt.SizeFDiagCursor, 4: Qt.SizeFDiagCursor, 2: Qt.SizeBDiagCursor,
                      6: Qt.SizeBDiagCursor, 1: Qt.SizeVerCursor, 5: Qt.SizeVerCursor,
                      3: Qt.SizeHorCursor, 7: Qt.SizeHorCursor}

    def __init__(self, image_item):
        super().__init__(image_item)
        self.img = QRectF(0, 0, image_item.w, image_item.h)
        self.rect = QRectF(self.img)
        self.ratio = None
        self._mode = None
        self._anchor = None
        self._press = None
        self._press_rect = None
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setZValue(20)

    # ---- масштаб экрана
    def _kxy(self):
        """Экранный масштаб по осям (картинка может быть растянута неравномерно)."""
        sc = self.scene()
        z = sc.views()[0].transform().m11() if sc and sc.views() else 1.0
        p = self.parentItem()
        return max(1e-6, z * p.sx), max(1e-6, z * p.sy)

    def refresh(self):
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        kx, ky = self._kxy()
        mx, my = 14 / kx, 14 / ky
        return self.img.adjusted(-mx, -my, mx, my)

    # ---- публичный API
    def set_ratio(self, ratio, refit=True, size=None):
        """size=(w, h) в пикселях оригинала: рамка появляется ровно такого размера (если помещается)."""
        self.ratio = ratio
        if ratio and refit:
            self._fit_ratio(size)
        self.update()
        self.rect_changed.emit()

    def crop_rect(self) -> QRect:
        """Целочисленная рамка в пикселях оригинала. При фиксированном соотношении высота
        выводится из ширины, чтобы пропорция в пикселях была максимально строгой."""
        r = self.rect
        iw, ih = int(self.img.width()), int(self.img.height())
        x = min(max(0, round(r.left())), iw - 1)
        y = min(max(0, round(r.top())), ih - 1)
        w = max(1, min(round(r.width()), iw - x))
        if self.ratio:
            h = max(1, round(w / self.ratio))
            if y + h > ih:
                h = ih - y
                w = max(1, round(h * self.ratio))
        else:
            h = max(1, min(round(r.height()), ih - y))
        return QRect(x, y, w, h)

    def set_pixel_size(self, w, h, driver="w"):
        """Задаёт размер рамки в пикселях. При фиксированном соотношении ведущая сторона — driver."""
        img, r = self.img, self.rect
        if self.ratio:
            if driver == "w":
                h = w / self.ratio
            else:
                w = h * self.ratio
            k = min(1.0, img.width() / max(w, 1), img.height() / max(h, 1))
            w, h = w * k, h * k
        else:
            w, h = min(w, img.width()), min(h, img.height())
        w, h = max(w, 1.0), max(h, 1.0)
        x = min(max(r.left(), img.left()), img.right() - w)
        y = min(max(r.top(), img.top()), img.bottom() - h)
        self.rect = QRectF(x, y, w, h)
        self.update()
        self.rect_changed.emit()

    def _fit_ratio(self, size=None):
        c = self.rect.center()
        img, r = self.img, self.ratio
        if size:
            w, h = float(size[0]), float(size[1])
            k = min(1.0, img.width() / w, img.height() / h)
            w, h = w * k, h * k
        else:
            w = img.width()
            h = w / r
            if h > img.height():
                h = img.height()
                w = h * r
            w *= 0.8
            h *= 0.8
        x = min(max(c.x() - w / 2, img.left()), img.right() - w)
        y = min(max(c.y() - h / 2, img.top()), img.bottom() - h)
        self.rect = QRectF(x, y, w, h)

    # ---- ручки
    def _handles(self):
        r = self.rect
        cx, cy = r.center().x(), r.center().y()
        return [r.topLeft(), QPointF(cx, r.top()), r.topRight(), QPointF(r.right(), cy),
                r.bottomRight(), QPointF(cx, r.bottom()), r.bottomLeft(), QPointF(r.left(), cy)]

    def _active(self):
        return (0, 2, 4, 6) if self.ratio else range(8)

    def _hit(self, pos):
        kx, ky = self._kxy()
        tx, ty = 10 / kx, 10 / ky
        hs = self._handles()
        for i in self._active():
            if abs(pos.x() - hs[i].x()) <= tx and abs(pos.y() - hs[i].y()) <= ty:
                return i
        return "move" if self.rect.contains(pos) else "new"

    def _clamp_pt(self, pos):
        return QPointF(min(max(pos.x(), self.img.left()), self.img.right()),
                       min(max(pos.y(), self.img.top()), self.img.bottom()))

    def _rect_from_anchor(self, anchor, pos):
        m = self._clamp_pt(pos)
        dx, dy = m.x() - anchor.x(), m.y() - anchor.y()
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        w, h = abs(dx), abs(dy)
        if self.ratio:
            r = self.ratio
            if w / r > h:
                h = w / r
            else:
                w = h * r
            maxw = (self.img.right() - anchor.x()) if sx > 0 else (anchor.x() - self.img.left())
            maxh = (self.img.bottom() - anchor.y()) if sy > 0 else (anchor.y() - self.img.top())
            f = min(1.0, maxw / w if w else 1.0, maxh / h if h else 1.0)
            w, h = w * f, h * f
        x0 = anchor.x() if sx > 0 else anchor.x() - w
        y0 = anchor.y() if sy > 0 else anchor.y() - h
        return QRectF(x0, y0, w, h)

    # ---- мышь
    def cursor_shape(self, scene_pos):
        """Форма курсора над рамкой. Курсором управляет холст (а не сама рамка): сцена Qt запоминает
        «прежний» курсор и после смены инструмента возвращает устаревший."""
        h = self._hit(self.mapFromScene(scene_pos))
        if isinstance(h, int):
            return self.HANDLE_CURSORS[h]
        return Qt.SizeAllCursor if h == "move" else Qt.CrossCursor

    def mousePressEvent(self, e):
        h = self._hit(e.pos())
        self._mode = h
        self._press = e.pos()
        self._press_rect = QRectF(self.rect)
        r = self.rect
        if h in (0, 2, 4, 6):
            self._anchor = {0: r.bottomRight(), 2: r.bottomLeft(), 4: r.topLeft(), 6: r.topRight()}[h]
        elif h == "new":
            self._anchor = self._clamp_pt(e.pos())
        e.accept()

    def mouseMoveEvent(self, e):
        m, pos = self._mode, e.pos()
        if m == "move":
            d = pos - self._press
            r = QRectF(self._press_rect).translated(d)
            dx = max(self.img.left() - r.left(), min(0.0, self.img.right() - r.right()))
            dy = max(self.img.top() - r.top(), min(0.0, self.img.bottom() - r.bottom()))
            self.rect = r.translated(dx, dy)
        elif m in (0, 2, 4, 6, "new"):
            self.rect = self._rect_from_anchor(self._anchor, pos)
        elif isinstance(m, int):
            r = QRectF(self._press_rect)
            img, mn = self.img, self.MIN
            if m == 1:
                r.setTop(min(max(pos.y(), img.top()), r.bottom() - mn))
            elif m == 3:
                r.setRight(min(max(pos.x(), r.left() + mn), img.right()))
            elif m == 5:
                r.setBottom(min(max(pos.y(), r.top() + mn), img.bottom()))
            elif m == 7:
                r.setLeft(max(min(pos.x(), r.right() - mn), img.left()))
            self.rect = r
        self.update()
        self.rect_changed.emit()
        e.accept()

    def mouseReleaseEvent(self, e):
        if self.rect.width() < self.MIN or self.rect.height() < self.MIN:
            self.rect = QRectF(self._press_rect)
            self.update()
            self.rect_changed.emit()
        self._mode = None
        e.accept()

    # ---- отрисовка
    def paint(self, p, opt, widget=None):
        kx, ky = self._kxy()
        dim = QPainterPath()
        dim.addRect(self.img)
        dim.addRect(self.rect)
        p.fillPath(dim, QColor(12, 14, 24, 140))

        pen = QPen(QColor("white"), 1.6)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(self.rect)

        thin = QPen(QColor(255, 255, 255, 110), 1)
        thin.setCosmetic(True)
        p.setPen(thin)
        r = self.rect
        for i in (1, 2):
            x = r.left() + r.width() * i / 3
            y = r.top() + r.height() * i / 3
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        hsx, hsy = 5 / kx, 5 / ky
        hp = QPen(QColor("#8391FF"), 1.5)
        hp.setCosmetic(True)
        p.setPen(hp)
        p.setBrush(QColor("#15171D"))
        pts = self._handles()
        for i in self._active():
            c = pts[i]
            p.drawRect(QRectF(c.x() - hsx, c.y() - hsy, hsx * 2, hsy * 2))

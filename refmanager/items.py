"""Объекты холста: картинка (полное разрешение, LOD-уровни, 8 маркеров растяжения), штрих кисти."""
import math
import uuid

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QApplication, QGraphicsItem, QGraphicsPathItem

ACCENT = QColor("#8391FF")
STROKE_Z = 1_000_000
MIN_SIZE = 8.0
MAGNET_PX = 8.0  # на таком экранном расстоянии от линии сетки край картинки прилипает к ней


GRID_STEP = 40  # единственный шаг сетки: мелкий шаг заметно дороже в отрисовке, а гибкость не нужна


class GridSettings:
    def __init__(self):
        self.size = GRID_STEP  # не меняется
        self.show = True
        self.snap = True
        self.keep_ratio = False  # «привязка в соотношении»: общий замок пропорций для маркеров и полей W×H


def load_image(path):
    """Читает картинку целиком, без уменьшения. Возвращает QImage или None."""
    try:
        QImageReader.setAllocationLimit(0)
    except (AttributeError, TypeError):
        pass
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    img = reader.read()
    if img.isNull():
        return None
    fmt = QImage.Format_ARGB32_Premultiplied if img.hasAlphaChannel() else QImage.Format_RGB32
    if img.format() != fmt:
        img = img.convertToFormat(fmt)
    return img


HANDLE_CURSORS = {0: Qt.SizeFDiagCursor, 4: Qt.SizeFDiagCursor, 2: Qt.SizeBDiagCursor,
                  6: Qt.SizeBDiagCursor, 1: Qt.SizeVerCursor, 5: Qt.SizeVerCursor,
                  3: Qt.SizeHorCursor, 7: Qt.SizeHorCursor}


class ResizeHandle(QGraphicsItem):
    """Маркер растяжения (0..7 = углы и рёбра по часовой стрелке от левого верхнего), экранный размер постоянный."""

    def __init__(self, parent, idx):
        super().__init__(parent)
        self.idx = idx
        w, h = parent.w, parent.h
        pos = [(0, 0), (w / 2, 0), (w, 0), (w, h / 2), (w, h), (w / 2, h), (0, h), (0, h / 2)][idx]
        self.setPos(*pos)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setZValue(10)
        self._press = None

    def boundingRect(self):
        return QRectF(-8, -8, 16, 16)

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(ACCENT, 2))
        p.setBrush(QColor("#15171D"))
        p.drawRoundedRect(QRectF(-5.5, -5.5, 11, 11), 3, 3)

    def mousePressEvent(self, e):
        it = self.parentItem()
        r0 = it.display_rect()
        corner = [r0.topLeft(), QPointF(r0.center().x(), r0.top()), r0.topRight(),
                  QPointF(r0.right(), r0.center().y()), r0.bottomRight(), QPointF(r0.center().x(), r0.bottom()),
                  r0.bottomLeft(), QPointF(r0.left(), r0.center().y())][self.idx]
        self._press = (e.scenePos(), r0, corner)
        e.accept()

    def mouseMoveEvent(self, e):
        if self._press is None:
            return
        start, r0, corner = self._press
        target = corner + (e.scenePos() - start)
        mods = QApplication.keyboardModifiers()
        it = self.parentItem()
        keep = it.grid.keep_ratio != bool(mods & Qt.ShiftModifier)   # Shift инвертирует замок пропорций
        it.drag_handle(self.idx, r0, target, keep_ratio=keep, from_center=bool(mods & Qt.AltModifier),
                       snap=it.grid.snap and not (mods & Qt.ControlModifier))
        e.accept()

    def mouseReleaseEvent(self, e):
        self._press = None
        e.accept()


class PatchMarker(QGraphicsItem):
    """Подсветка выбранного участка на картинке (в пикселях оригинала). Мышь не перехватывает."""

    def __init__(self, parent, rect: QRectF, label: str):
        super().__init__(parent)
        self._r = QRectF(rect)
        self.label = label
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.setZValue(15)

    def boundingRect(self):
        p = self.parentItem()
        m = 6.0 / (p._view_zoom * min(p.sx, p.sy))
        return self._r.adjusted(-m, -m, m, m)

    def shape(self):
        return QPainterPath()   # не участвует в попадании мыши

    def paint(self, p, opt, widget=None):
        p.setBrush(QColor(131, 145, 255, 46))
        pen = QPen(QColor("#FFD166"), 2, Qt.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.drawRect(self._r)


class ImageItem(QGraphicsItem):
    def __init__(self, image: QImage, file: str, name: str, grid: GridSettings, uid=None):
        super().__init__()
        self.image = image
        self.fmt = image.format()   # формат пикселей картинки (не меняется при замене основы)
        self.file = file
        self.name = name
        self.grid = grid
        self.uid = uid or uuid.uuid4().hex[:10]   # стабильный id: на него ссылаются «участки»
        self.w = image.width()
        self.h = image.height()
        self.sx = 1.0
        self.sy = 1.0
        self._view_zoom = 1.0
        # замена основной картинки из буфера: file — оригинал, iters — до 3 последних замен (как у участков)
        self.base = {"iters": [], "active": None, "visible": True, "iter_seq": 0}
        self.cropping = False
        self._raw = False
        self._levels = [image]
        self._max_level = max(0, int(math.log2(max(1.0, min(self.w, self.h) / 16.0))))
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.handles = [ResizeHandle(self, i) for i in range(8)]
        for h in self.handles:
            h.setVisible(False)

    # ---- геометрия
    def rect(self):
        return QRectF(0, 0, self.w, self.h)

    def boundingRect(self):
        # запас под рамку выделения: ~6 экранных px независимо от масштаба картинки и вида,
        # иначе рамка «вылезает» за область перерисовки и оставляет хвосты
        m = 6.0 / (self._view_zoom * min(self.sx, self.sy))
        return self.rect().adjusted(-m, -m, m, m)

    def set_view_zoom(self, z):
        if abs(z - self._view_zoom) > 1e-9:
            self.prepareGeometryChange()
            self._view_zoom = max(z, 1e-6)
            for ch in self.childItems():
                if isinstance(ch, PatchMarker):
                    ch.prepareGeometryChange()

    def set_pixels(self, img: QImage):
        """Подменяет все пиксели картинки (замена основы). Размер тот же; уменьшенные копии пересчитаются при показе."""
        self.image = img
        self._levels = [img]
        self.update()

    def paste_region(self, rect, region: QImage):
        """Вставляет готовый кусок в картинку на месте (пиксель в пиксель, без смешивания с фоном).
        Уменьшенные копии для показа обновляются только в области участка, а не пересчитываются целиком."""
        p = QPainter(self.image)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.drawImage(rect.topLeft(), region)
        p.end()
        self._levels[0] = self.image
        x0, y0, x1, y1 = rect.left(), rect.top(), rect.right() + 1, rect.bottom() + 1
        for k in range(1, len(self._levels)):
            prev, cur = self._levels[k - 1], self._levels[k]
            x0, y0, x1, y1 = x0 // 2, y0 // 2, (x1 + 1) // 2, (y1 + 1) // 2
            src = QRect(x0 * 2, y0 * 2, (x1 - x0) * 2, (y1 - y0) * 2).intersected(prev.rect())
            if src.isEmpty():
                continue
            tw, th = max(1, (src.width() + 1) // 2), max(1, (src.height() + 1) // 2)
            part = prev.copy(src).scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            q = QPainter(cur)
            q.setCompositionMode(QPainter.CompositionMode_Source)
            q.drawImage(src.x() // 2, src.y() // 2, part)
            q.end()
        self.update(QRectF(rect).adjusted(-1, -1, 1, 1))   # перерисовываем только участок

    def shape(self):
        p = QPainterPath()
        p.addRect(self.rect())
        return p

    def display_size(self):
        return self.w * self.sx, self.h * self.sy

    def display_rect(self):
        dw, dh = self.display_size()
        return QRectF(self.pos().x(), self.pos().y(), dw, dh)

    def set_scale(self, sx, sy=None):
        self.prepareGeometryChange()
        self.sx = min(max(sx, 0.001), 500.0)
        self.sy = min(max(self.sx if sy is None else sy, 0.001), 500.0)
        self.setTransform(QTransform.fromScale(self.sx, self.sy))

    def set_geometry(self, x, y, w, h):
        """Задаёт позицию и размер в пикселях сцены (без привязки к сетке)."""
        self._raw = True
        self.setPos(x, y)
        self.set_scale(w / self.w, h / self.h)
        self._raw = False

    # ---- состояние для undo/сохранения
    def get_state(self):
        return (self.pos().x(), self.pos().y(), self.sx, self.sy, self.zValue())

    def set_state(self, st):
        self._raw = True
        self.setPos(st[0], st[1])
        self.set_scale(st[2], st[3])
        self.setZValue(st[4])
        self._raw = False

    # ---- привязка
    def _snapping(self):
        return (self.grid.snap and not self._raw and self.scene() is not None
                and not (QApplication.keyboardModifiers() & Qt.AltModifier))

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self._snapping():
            g = self.grid.size
            w, h = self.display_size()
            thr = MAGNET_PX / self._view_zoom

            def snap1(v, size):
                """Магнит: левый/верхний или правый/нижний край прилипает к линии, только если она рядом."""
                a = round(v / g) * g                 # левый/верхний край на линию сетки
                b = round((v + size) / g) * g - size  # правый/нижний край на линию сетки
                best = a if abs(a - v) <= abs(b - v) else b
                return best if abs(best - v) <= thr else v

            return QPointF(snap1(value.x(), w), snap1(value.y(), h))
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self._update_handles(bool(value))
        return super().itemChange(change, value)

    def _update_handles(self, selected):
        for h in self.handles:
            h.setVisible(selected and not self.cropping)

    def set_cropping(self, on):
        self.cropping = on
        self._update_handles(self.isSelected())

    def drag_handle(self, idx, r0, m, keep_ratio, from_center, snap):
        """Растяжение как в Photoshop. r0 — прямоугольник в момент нажатия, m — целевая позиция маркера."""
        ml, mr = idx in (0, 6, 7), idx in (2, 3, 4)
        mt, mb = idx in (0, 1, 2), idx in (4, 5, 6)
        mx_, my_ = ml or mr, mt or mb
        c = r0.center()
        g = self.grid.size

        thr = MAGNET_PX / self._view_zoom

        def sn(v):
            """Магнит к линии сетки: только в пределах порога."""
            if not snap:
                return v
            line = round(v / g) * g
            return line if abs(line - v) <= thr else v

        mx, my = sn(m.x()), sn(m.y())
        w, h = r0.width(), r0.height()
        if mx_:
            if from_center:
                w = 2 * max(MIN_SIZE / 2, (mx - c.x()) if mr else (c.x() - mx))
            else:
                w = max(MIN_SIZE, (mx - r0.left()) if mr else (r0.right() - mx))
        if my_:
            if from_center:
                h = 2 * max(MIN_SIZE / 2, (my - c.y()) if mb else (c.y() - my))
            else:
                h = max(MIN_SIZE, (my - r0.top()) if mb else (r0.bottom() - my))
        if keep_ratio:
            if mx_ and my_:
                s = max(w / r0.width(), h / r0.height())
                w, h = r0.width() * s, r0.height() * s
            elif mx_:
                h = w * r0.height() / r0.width()
            elif my_:
                w = h * r0.width() / r0.height()
        # позиция: фиксируем противоположную сторону (или центр при Alt / по неизменяемой оси)
        if from_center or not mx_:
            x = c.x() - w / 2
        else:
            x = r0.left() if mr else r0.right() - w
        if from_center or not my_:
            y = c.y() - h / 2
        else:
            y = r0.top() if mb else r0.bottom() - h
        self.set_geometry(x, y, max(w, 1.0), max(h, 1.0))

    # ---- отрисовка (LOD: для сильного уменьшения берём заранее уменьшенные копии)
    def _level(self, k):
        while len(self._levels) <= k:
            prev = self._levels[-1]
            self._levels.append(prev.scaled(max(1, prev.width() // 2), max(1, prev.height() // 2),
                                            Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        return self._levels[k]

    def paint(self, p, opt, widget=None):
        t = p.worldTransform()
        lod = max(math.hypot(t.m11(), t.m12()), math.hypot(t.m21(), t.m22()))
        k = 0
        if 0 < lod < 0.5:
            k = min(self._max_level, int(math.floor(math.log2(1.0 / lod))))
        img = self._level(k)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(self.rect(), img, QRectF(img.rect()))
        if self.isSelected():
            pen = QPen(ACCENT, 2)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.rect())


class StrokeItem(QGraphicsPathItem):
    def __init__(self, color, width, points=None):
        super().__init__()
        self.color = QColor(color)
        self.width = float(width)
        self.points = []
        self._path = QPainterPath()
        self.setPen(QPen(self.color, self.width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        self.setBrush(Qt.NoBrush)
        self.setZValue(STROKE_Z)
        if points:
            for x, y in points:
                self.add_point(QPointF(x, y))

    def add_point(self, pt: QPointF):
        n = len(self.points)
        self.points.append(pt)
        if n == 0:
            self._path.moveTo(pt)
        elif n == 1:
            self._path.lineTo((self.points[0] + pt) / 2)
        else:
            prev = self.points[-2]
            self._path.quadTo(prev, (prev + pt) / 2)
        shown = QPainterPath(self._path)
        shown.lineTo(pt + QPointF(0.01, 0) if n == 0 else pt)
        self.setPath(shown)

    def to_dict(self):
        return {"type": "stroke", "color": self.color.name(), "width": round(self.width, 3),
                "points": [[round(p.x(), 2), round(p.y(), 2)] for p in self.points]}

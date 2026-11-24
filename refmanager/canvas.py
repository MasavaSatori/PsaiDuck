"""Холст: бесконечная сцена с сеткой, инструменты (выбор/кисть/ластик/обрезка), undo."""
import copy
import math
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap,
                           QTransform, QUndoCommand)
from PySide6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView, QMenu

from . import cutout, psd, theme
from .crop import CropItem, load_ratios, parse_ratio
from .items import HANDLE_CURSORS, STROKE_Z, ImageItem, PatchMarker, ResizeHandle, StrokeItem, load_image

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".ico"}
SCENE_HALF = 100_000
PATCH_MAX_ITERS = 3  # сколько последних вставок хранится у участка
MOVE_INTERVAL = 0.016  # при перетаскивании обрабатываем движения мыши не чаще ~60 раз в секунду


# --------------------------------------------------------------------------- undo
class AddItemsCmd(QUndoCommand):
    def __init__(self, canvas, items, text="Добавить"):
        super().__init__(text)
        self.c, self.items = canvas, items

    def redo(self):
        for it in self.items:
            if it.scene() is None:
                self.c.scene().addItem(it)
            if isinstance(it, ImageItem):
                it.set_view_zoom(self.c.zoom)
        self.c.items_changed()

    def undo(self):
        for it in self.items:
            if it.scene() is not None:
                self.c.scene().removeItem(it)
        self.c.items_changed()


class RemoveItemsCmd(AddItemsCmd):
    def __init__(self, canvas, items, text="Удалить"):
        super().__init__(canvas, items, text)

    def redo(self):
        AddItemsCmd.undo(self)

    def undo(self):
        AddItemsCmd.redo(self)


class TransformCmd(QUndoCommand):
    def __init__(self, canvas, changes, text="Изменить"):
        super().__init__(text)
        self.c, self.changes = canvas, changes  # [(item, old_state, new_state)]

    def redo(self):
        for it, _, new in self.changes:
            it.set_state(new)
        self.c.items_changed()

    def undo(self):
        for it, old, _ in self.changes:
            it.set_state(old)
        self.c.items_changed()


def _ring_cursor(diameter):
    d = int(max(6, min(diameter, 124))) + 6
    pm = QPixmap(d, d)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(3.5, 3.5, d - 7, d - 7)
    p.setPen(QPen(QColor(255, 255, 255, 235), 3))
    p.drawEllipse(rect)
    p.setPen(QPen(QColor(20, 24, 40, 235), 1.2))
    p.drawEllipse(rect)
    p.end()
    return QCursor(pm, d // 2, d // 2)


class RegionCache:
    """Кэш пикселей участков (исходные и вставки): глазик и смена итераций не читают PNG с диска заново.
    Вставки с прозрачностью хранятся с альфа-каналом, а на картинку накладываются поверх исходного участка."""
    LIMIT = 192 * 1024 * 1024

    def __init__(self):
        self._d = OrderedDict()
        self._bytes = 0

    def clear(self):
        self._d.clear()
        self._bytes = 0

    def _store(self, key, img):
        if key in self._d:
            self._bytes -= self._d.pop(key).sizeInBytes()
        self._d[key] = img
        self._bytes += img.sizeInBytes()
        while self._bytes > self.LIMIT and len(self._d) > 1:
            _, old = self._d.popitem(last=False)
            self._bytes -= old.sizeInBytes()

    @staticmethod
    def _key(project, fname, fmt, size, alpha):
        return (project.id if project else "", fname, fmt, size.width(), size.height(), alpha)

    def put(self, project, fname, fmt, size, img, alpha=False):
        if fname:
            self._store(self._key(project, fname, fmt, size, alpha), img)

    def get(self, project, fname, fmt, size, alpha=False):
        """alpha=True — вернуть с прозрачностью (ARGB32 premultiplied), иначе в формате картинки fmt."""
        if not fname or project is None:
            return None
        key = self._key(project, fname, fmt, size, alpha)
        img = self._d.get(key)
        if img is not None:
            self._d.move_to_end(key)
            return img
        src = load_image(project.asset_path(fname))
        if src is None:
            return None
        target = QImage.Format_ARGB32_Premultiplied if alpha else fmt
        img = src.copy(0, 0, size.width(), size.height()).convertToFormat(target)
        self._store(key, img)
        return img

    def compose(self, project, iter_file, orig_file, fmt, size):
        """Вставка с прозрачностью поверх исходного участка: прозрачные места показывают исходные пиксели."""
        key = ("compose", project.id if project else "", iter_file, orig_file, fmt, size.width(), size.height())
        img = self._d.get(key)
        if img is not None:
            self._d.move_to_end(key)
            return img
        top = self.get(project, iter_file, fmt, size, alpha=True)
        if top is None:
            return None
        bottom = self.get(project, orig_file, fmt, size) if orig_file else None
        if bottom is None:
            img = top.convertToFormat(fmt)   # исходника нет — подложить нечего
        else:
            img = bottom.copy()
            p = QPainter(img)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.drawImage(0, 0, top)
            p.end()
        self._store(key, img)
        return img


class BaseCache:
    """Кэш полных картинок (оригинал и замены основы): переключение глазика/итерации не декодирует файл заново.
    Хранит не больше LIMIT байт, лишнее вытесняется (самое давнее — первым). Объекты из кэша менять нельзя."""
    LIMIT = 768 * 1024 * 1024

    def __init__(self):
        self._d = OrderedDict()
        self._bytes = 0

    def clear(self):
        self._d.clear()
        self._bytes = 0

    def put(self, project, fname, fmt, img):
        key = (project.id if project else "", fname, fmt)
        if key in self._d:
            self._bytes -= self._d.pop(key).sizeInBytes()
        self._d[key] = img
        self._bytes += img.sizeInBytes()
        while self._bytes > self.LIMIT and len(self._d) > 1:
            _, old = self._d.popitem(last=False)
            self._bytes -= old.sizeInBytes()

    def get(self, project, fname, fmt):
        if not fname or project is None:
            return None
        key = (project.id, fname, fmt)
        img = self._d.get(key)
        if img is not None:
            self._d.move_to_end(key)
            return img
        img = load_image(project.asset_path(fname))
        if img is None:
            return None
        if img.format() != fmt:
            img = img.convertToFormat(fmt)
        self.put(project, fname, fmt, img)
        return img


class BaseStateCmd(QUndoCommand):
    """Смена состояния основной картинки (итерации, активная, видимость) вместе с её пикселями.
    Пиксели берутся из кэша по описанию состояния; поверх накладываются текущие вставки участков."""

    def __init__(self, canvas, item, old_meta, new_meta, text):
        super().__init__(text)
        self.c, self.item = canvas, item
        self.old_meta, self.new_meta = old_meta, new_meta

    def _apply(self, meta):
        img = self.c.base_image_for(self.item, meta)
        if img is None:
            self.c.message.emit("Файл основной картинки не найден")
            return
        self.item.base = copy.deepcopy(meta)
        self.item.set_pixels(img.copy())            # кэш не трогаем: поверх рисуются вставки участков
        self.c._apply_patch_states(self.item.uid)
        self.c.base_updated.emit(self.item.uid)
        self.c.modified.emit()

    def redo(self):
        self._apply(self.new_meta)

    def undo(self):
        self._apply(self.old_meta)


class PatchStateCmd(QUndoCommand):
    """Смена состояния участка (итерации, активная, видимость) вместе с его пикселями.
    Пиксели не хранятся в команде: берутся из кэша регионов по описанию состояния — команды лёгкие,
    сколько бы раз ни нажимали глазик."""

    def __init__(self, canvas, item, rect, pid, orig, old_meta, new_meta, text):
        super().__init__(text)
        self.c, self.item, self.rect, self.pid, self.orig = canvas, item, rect, pid, orig
        self.old_meta, self.new_meta = old_meta, new_meta

    def _apply(self, meta):
        region = self.c.region_for(self.item, self.rect, meta, self.orig)
        if region is None:
            self.c.message.emit("Файл с пикселями участка не найден")
            return
        self.item.paste_region(self.rect, region)
        p = self.c.get_patch(self.pid)
        if p is not None:
            p.update(copy.deepcopy(meta))
        self.c.patch_updated.emit(self.pid)
        self.c.modified.emit()

    def redo(self):
        self._apply(self.new_meta)

    def undo(self):
        self._apply(self.old_meta)


class Canvas(QGraphicsView):
    patches_changed = Signal()   # структура списка участков изменилась
    base_updated = Signal(str)    # у основной картинки (по uid) сменилось состояние
    patch_updated = Signal(int)  # у одного участка сменилось состояние (глазик, итерация)
    patch_focus = Signal(int)
    empty_clicked = Signal()   # клик левой кнопкой в пустое место холста (не по картинке)
    message = Signal(str)
    modified = Signal()
    zoom_changed = Signal(float)
    tool_changed = Signal(str)
    selection_info = Signal(object)  # ImageItem | None
    crop_info = Signal(str)
    crop_size = Signal(int, int)
    crop_state = Signal(bool)

    def __init__(self, grid, undo):
        super().__init__()
        self.grid = grid
        self.undo = undo
        self.project = None
        self.tool = "select"
        self.ratios = load_ratios()
        self.crop = None
        self.crop_target = None
        self.crop_mode = "free"
        self.crop_ratio = "1:1"
        self.crop_res = "1K"
        self.brush_color = QColor("#E5484D")
        self.brush_size = 4
        self._z = 0
        self._loading = False
        self._pending_view = None
        self._pan = None
        self._space = False
        self._stroke = None
        self._erased = None
        self._snap = None
        self._ring_cache = {}
        self.patches = []        # вырезанные участки проекта (см. register_patch)
        self._patch_seq = 0
        self._marker = None
        self.regions = RegionCache()
        self.base_cache = BaseCache()
        self.remove_white = True    # у вставки белый фон по краям становится прозрачным
        self.source_note = ""       # откуда пришла вставка (для сообщения): формат из буфера обмена

        sc = QGraphicsScene(-SCENE_HALF, -SCENE_HALF, SCENE_HALF * 2, SCENE_HALF * 2, self)
        self.setScene(sc)
        sc.selectionChanged.connect(self._emit_selection)

        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # поле рисует фон само (drawBackground): системную заливку выключаем
        self.viewport().setAutoFillBackground(False)
        self.viewport().setMouseTracking(True)
        self.setBackgroundBrush(Qt.NoBrush)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self._last_move = 0.0
        self._pending_move = None
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush_pending_move)

    # ------------------------------------------------------------- вспомогательное
    @property
    def zoom(self):
        return self.transform().m11()

    def snap_enabled(self):
        return self.grid.snap

    def next_z(self):
        self._z += 1
        return self._z

    def image_items(self):
        return [i for i in self.scene().items() if isinstance(i, ImageItem)]

    def selected_images(self):
        return [i for i in self.scene().selectedItems() if isinstance(i, ImageItem)]

    def items_changed(self):
        if self.crop_target is not None and self.crop_target.scene() is None:
            self.crop = None
            self.crop_target = None
            self.crop_state.emit(False)
            self.crop_info.emit("")
        self._emit_selection()
        self.modified.emit()

    def _emit_selection(self):
        sel = self.selected_images()
        self.selection_info.emit(sel[0] if len(sel) == 1 else None)

    def refresh_grid(self):
        self.viewport().update()

    # ------------------------------------------------------------- фон и сетка
    def drawBackground(self, p, rect):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, False)   # ровные линии сетки без размытия
        p.fillRect(rect, QColor(*theme.CANVAS_BG))
        if not self.grid.show:
            p.restore()
            return
        z = self.zoom
        step = float(self.grid.size)
        while step * z < 9:
            step *= 2
        minor, major = [], []
        i0 = int(math.floor(rect.left() / step))
        i1 = int(math.ceil(rect.right() / step))
        for i in range(i0, i1 + 1):
            (major if i % 5 == 0 else minor).append(QLineF(i * step, rect.top(), i * step, rect.bottom()))
        j0 = int(math.floor(rect.top() / step))
        j1 = int(math.ceil(rect.bottom() / step))
        for j in range(j0, j1 + 1):
            (major if j % 5 == 0 else minor).append(QLineF(rect.left(), j * step, rect.right(), j * step))
        pen = QPen(QColor(*theme.GRID_MINOR, 30), 0)
        p.setPen(pen)
        p.drawLines(minor)
        pen.setColor(QColor(*theme.GRID_MAJOR, 62))
        p.setPen(pen)
        p.drawLines(major)
        p.restore()

    # ------------------------------------------------------------- вид
    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._apply_pending_view)

    def _apply_pending_view(self):
        if self._pending_view is not None:
            v, self._pending_view = self._pending_view, None
            self._apply_view(v)

    def _apply_view(self, v):
        if v:
            z = min(max(float(v.get("zoom", 1.0)), 0.02), 40)
            self.setTransform(QTransform.fromScale(z, z))
            self.centerOn(float(v.get("cx", 0)), float(v.get("cy", 0)))
        elif self.image_items():
            self.fit_all()
        else:
            self.setTransform(QTransform())
            self.centerOn(0, 0)
        self._on_zoom()

    def _on_zoom(self):
        z = self.zoom
        for it in self.image_items():
            it.set_view_zoom(z)
        if self.crop:
            self.crop.refresh()
        self.zoom_changed.emit(self.zoom)
        self.viewport().update()

    def fit_all(self):
        items = [i for i in self.scene().items() if isinstance(i, (ImageItem, StrokeItem))]
        if not items:
            self.setTransform(QTransform())
            self.centerOn(0, 0)
        else:
            r = QRectF()
            for it in items:
                r = r.united(it.sceneBoundingRect())
            self.fitInView(r.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)
            z = self.zoom
            if z > 4:
                self.setTransform(QTransform.fromScale(4, 4))
                self.centerOn(r.center())
        self._on_zoom()

    def set_zoom_1(self):
        c = self.mapToScene(self.viewport().rect().center())
        self.setTransform(QTransform())
        self.centerOn(c)
        self._on_zoom()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if dy == 0:
            return
        z = self.zoom
        nz = min(max(z * 1.15 ** (dy / 120.0), 0.02), 40.0)
        f = nz / z
        self.scale(f, f)
        self._on_zoom()

    # ------------------------------------------------------------- инструменты
    def set_tool(self, name):
        if name == self.tool and name != "crop":
            return
        self.tool = name
        if name != "crop":
            self.end_crop()
        self.setDragMode(QGraphicsView.RubberBandDrag if name == "select" else QGraphicsView.NoDrag)
        self.update_cursor()
        self.tool_changed.emit(name)
        if name == "crop":
            target = self._pick_crop_target()
            if target is None:
                self.message.emit("Нет картинки для обрезки: добавьте или выберите картинку")
            else:
                self.begin_crop(target)

    # Курсором управляет только холст. Курсоры у элементов сцены не используем: QGraphicsScene запоминает
    # «прежний» курсор вьюпорта и после смены инструмента возвращает устаревший (плюсик вместо стрелки).
    def _ring(self, diameter):
        key = int(diameter)
        if key not in self._ring_cache:
            self._ring_cache[key] = _ring_cursor(diameter)
        return self._ring_cache[key]

    def _tool_cursor(self):
        if self.tool == "brush":
            return self._ring(self.brush_size)
        if self.tool == "eraser":
            return self._ring(self._eraser_radius_px() * 2)
        if self.tool == "crop":
            return QCursor(Qt.CrossCursor)
        return QCursor(Qt.ArrowCursor)

    def _cursor_at(self, vpos):
        if self._space:
            return QCursor(Qt.OpenHandCursor)
        if self.tool in ("select", "crop"):
            item = self.itemAt(vpos)
            if isinstance(item, ResizeHandle):
                return QCursor(HANDLE_CURSORS[item.idx])
            if isinstance(item, CropItem):
                return QCursor(item.cursor_shape(self.mapToScene(vpos)))
        return self._tool_cursor()

    def refresh_cursor(self):
        """Ставит правильный курсор прямо сейчас, без ожидания движения мыши."""
        vp = self.viewport()
        pos = vp.mapFromGlobal(QCursor.pos())
        if vp.rect().contains(pos) and not QApplication.mouseButtons():
            vp.setCursor(self._cursor_at(pos))
        else:
            vp.setCursor(QCursor(Qt.OpenHandCursor) if self._space else self._tool_cursor())

    def update_cursor(self):
        self.refresh_cursor()

    def set_brush(self, color=None, size=None):
        if color is not None:
            self.brush_color = QColor(color)
        if size is not None:
            self.brush_size = int(size)
        self.update_cursor()

    def _eraser_radius_px(self):
        return self.brush_size / 2 + 6

    # ------------------------------------------------------------- обрезка
    def _pick_crop_target(self):
        sel = self.selected_images()
        if sel:
            return sel[0]
        imgs = self.image_items()
        if not imgs:
            return None
        center = self.mapToScene(self.viewport().rect().center())
        under = [i for i in imgs if i.sceneBoundingRect().contains(center)]
        pool = under or imgs
        return max(pool, key=lambda i: i.zValue())

    def begin_crop(self, item):
        self.end_crop()
        self.crop_target = item
        self.scene().clearSelection()
        item.setSelected(True)
        item.set_cropping(True)
        self.crop = CropItem(item)
        self.crop.rect_changed.connect(self._emit_crop_info)
        self.crop_state.emit(True)
        self.apply_crop_config()
        self.refresh_cursor()
        if abs(item.sx - item.sy) > 1e-3 * max(item.sx, item.sy):
            self.message.emit("Картинка растянута неравномерно: рамка обрезки работает в пикселях оригинала")

    def end_crop(self):
        if self.crop is not None:
            if self.crop.scene() is not None:
                self.crop.scene().removeItem(self.crop)
            if self.crop_target is not None and self.crop_target.scene() is not None:
                self.crop_target.set_cropping(False)
            self.crop = None
            self.crop_target = None
            self.crop_state.emit(False)
            self.crop_info.emit("")
            self.refresh_cursor()

    def set_crop_config(self, mode=None, ratio=None, res=None):
        if mode is not None:
            self.crop_mode = mode
        if ratio is not None:
            self.crop_ratio = ratio
        if res is not None:
            self.crop_res = res
        self.apply_crop_config(refit=True)
        self._emit_crop_info()

    def gemini_size(self):
        table = self.ratios["gemini"].get(self.crop_res, {})
        if self.crop_ratio in table:
            return tuple(table[self.crop_ratio])
        base = 1024 if self.crop_res == "1K" else 2048
        r = parse_ratio(self.crop_ratio)
        return (round(base * math.sqrt(r)), round(base / math.sqrt(r)))

    def apply_crop_config(self, refit=False):
        """Gemini: рамка появляется ровно в пикселях Gemini (1K/2K) и дальше масштабируется в строгом
        соотношении сторон. Стандарт: рамка нужного соотношения. Свободная: без ограничений."""
        if not self.crop:
            return
        size = None
        if self.crop_mode == "free":
            ratio = None
        elif self.crop_mode == "standard":
            ratio = parse_ratio(self.crop_ratio)
        else:
            tw, th = self.gemini_size()
            ratio = tw / th
            size = (tw, th)
        changed = (ratio is None) != (self.crop.ratio is None) or (
            ratio is not None and abs(ratio - (self.crop.ratio or 0)) > 1e-6)
        self.crop.set_ratio(ratio, refit=changed or refit, size=size)

    def set_crop_size(self, w, h, driver="w"):
        if self.crop:
            self.crop.set_pixel_size(float(w), float(h), driver)

    def _emit_crop_info(self):
        if not self.crop:
            self.crop_info.emit("")
            return
        r = self.crop.crop_rect()
        self.crop_size.emit(r.width(), r.height())
        txt = f"Выделено: {r.width()}×{r.height()} px"
        if self.crop_mode == "gemini":
            tw, th = self.gemini_size()
            txt += f"\nЭталон Gemini {self.crop_res} · {self.crop_ratio}: {tw}×{th} px"
            if self.crop_target is not None and (self.crop_target.w < tw or self.crop_target.h < th):
                txt += "\n⚠ Картинка меньше эталона: рамка уменьшена"
        elif self.crop_mode == "standard":
            txt += f"  ({self.crop_ratio})"
        txt += "\nКопируется как выделено (пиксель в пиксель)"
        self.crop_info.emit(txt)

    def crop_result(self):
        """QImage вырезанного куска: ровно выделенные пиксели оригинала (во всех режимах)."""
        if not (self.crop and self.crop_target):
            return None
        return self.crop_target.image.copy(self.crop.crop_rect())

    def copy_crop(self):
        img = self.crop_result()
        if img is None:
            return False
        QApplication.clipboard().setImage(img)
        pid, is_new = self.register_patch(self.crop.crop_rect(), img)
        self.message.emit(f"Скопировано в буфер обмена: {img.width()}×{img.height()} px · "
                          f"участок #{pid}{'' if is_new else ' (уже в списке)'}")
        return True

    # ------------------------------------------------------------- участки (вырезанные области)
    def get_patch(self, pid):
        return next((p for p in self.patches if p["id"] == pid), None)

    def find_item(self, uid):
        return next((i for i in self.image_items() if i.uid == uid), None)

    def register_patch(self, rect, exact):
        """Записывает вырезанный участок: где он на картинке и его исходные пиксели (для «вернуть исходный»)."""
        item = self.crop_target
        key = (rect.x(), rect.y(), rect.width(), rect.height())
        for p in self.patches:
            if p["uid"] == item.uid and (p["x"], p["y"], p["w"], p["h"]) == key:
                self.patch_focus.emit(p["id"])
                return p["id"], False
        orig = None
        if self.project is not None:
            src = exact
            if self.base_active_iter(item) is not None:   # сейчас показана замена основы: под участком — оригинал
                full0 = self.base_cache.get(self.project, item.file, item.fmt)
                if full0 is not None:
                    src = full0.copy(QRect(*key))
            try:
                orig = self.project.import_qimage(src)
            except OSError:
                orig = None
        self._patch_seq += 1
        p = {"id": self._patch_seq, "uid": item.uid, "name": item.name,
             "x": key[0], "y": key[1], "w": key[2], "h": key[3],
             "mode": self.crop_mode,
             "ratio": self.crop_ratio if self.crop_mode != "free" else None,
             "res": self.crop_res if self.crop_mode == "gemini" else None,
             "orig": orig, "created": time.time(),
             "iters": [], "active": None, "visible": True, "iter_seq": 0}
        self.patches.append(p)
        self.patches_changed.emit()
        self.patch_focus.emit(p["id"])
        self.modified.emit()
        return p["id"], True

    def copy_patch(self, pid):
        """Копирует в буфер текущее содержимое участка: его пиксели на картинке (с учётом замен)."""
        p = self.get_patch(pid)
        item = self.find_item(p["uid"]) if p else None
        if item is None:
            self.message.emit("Картинки этого участка нет на холсте")
            return False
        rect = QRect(p["x"], p["y"], p["w"], p["h"]).intersected(QRect(0, 0, item.w, item.h))
        img = item.image.copy(rect)
        QApplication.clipboard().setImage(img)
        self.message.emit(f"Участок #{pid} скопирован в буфер обмена: {img.width()}×{img.height()} px")
        return True

    def delete_patch(self, pid):
        p = self.get_patch(pid)
        if p is None:
            return
        self.clear_marker()
        self.patches.remove(p)
        self.patches_changed.emit()
        self.modified.emit()

    def clear_marker(self):
        if self._marker is not None and self._marker.scene() is not None:
            self._marker.scene().removeItem(self._marker)
        self._marker = None

    def show_patch(self, pid, center=False):
        """Подсвечивает участок на картинке; center — выделить картинку и показать участок в центре вида."""
        self.clear_marker()
        p = self.get_patch(pid)
        item = self.find_item(p["uid"]) if p else None
        if item is None:
            return False
        self._marker = PatchMarker(item, QRectF(p["x"], p["y"], p["w"], p["h"]), f"#{pid}")
        if center and self.tool != "crop":
            self.scene().clearSelection()
            item.setSelected(True)
            self.centerOn(item.mapToScene(QRectF(p["x"], p["y"], p["w"], p["h"]).center()))
            self._on_zoom()
        self.viewport().update()
        return True

    def patch_ratio_diff(self, pid, img):
        p = self.get_patch(pid)
        return abs((img.width() / img.height()) / (p["w"] / p["h"]) - 1.0)

    # ---- состояние вставок участка
    def _patch_meta(self, p):
        return {"iters": copy.deepcopy(p.get("iters", [])), "active": p.get("active"),
                "visible": p.get("visible", True), "iter_seq": p.get("iter_seq", 0)}

    def patch_active_iter(self, p):
        a, its = p.get("active"), p.get("iters", [])
        return its[a] if a is not None and 0 <= a < len(its) else None

    def patch_replaced(self, p):
        """На картинке сейчас показана вставка (а не исходные пиксели)."""
        return self.patch_active_iter(p) is not None and p.get("visible", True)

    def _patch_context(self, pid):
        p = self.get_patch(pid)
        item = self.find_item(p["uid"]) if p else None
        if item is None:
            self.message.emit("Картинки этого участка нет на холсте")
            return None
        rect = QRect(p["x"], p["y"], p["w"], p["h"]).intersected(QRect(0, 0, item.w, item.h))
        return p, item, rect

    def region_for(self, item, rect, meta, orig):
        """Пиксели, которые должны быть на месте участка при данном состоянии: активная вставка, если она
        показана (с прозрачностью — поверх того, что под ней), иначе то, что под участком."""
        a, its = meta.get("active"), meta.get("iters", [])
        it = its[a] if meta.get("visible", True) and a is not None and 0 <= a < len(its) else None
        fmt, size = item.image.format(), rect.size()
        if it is None:
            return self._hidden_region(item, rect, orig)
        if it.get("alpha"):
            top = self.regions.get(self.project, it["file"], fmt, size, alpha=True)
            if top is None:
                return None
            if self.base_active_iter(item) is None:
                return self.regions.compose(self.project, it["file"], orig, fmt, size)   # с кэшем
            bottom = self._hidden_region(item, rect, orig)
            if bottom is None:
                return top.convertToFormat(fmt)
            out = bottom.copy()
            q = QPainter(out)
            q.setCompositionMode(QPainter.CompositionMode_SourceOver)
            q.drawImage(0, 0, top)
            q.end()
            return out
        return self.regions.get(self.project, it["file"], fmt, size)

    def _hidden_region(self, item, rect, orig):
        """Что лежит под участком: если показана замена основной картинки — её пиксели, иначе сохранённый исходник."""
        it = self.base_active_iter(item)
        if it is not None:
            full = self.base_cache.get(self.project, it["file"], item.fmt)
            if full is not None:
                return full.copy(rect)
        return self.regions.get(self.project, orig, item.image.format(), rect.size())

    @staticmethod
    def _has_transparency(img):
        """Есть ли в картинке действительно прозрачные пиксели (а не просто формат с альфа-каналом)."""
        if not img.hasAlphaChannel():
            return False
        a = img.convertToFormat(QImage.Format_Alpha8)
        mv, bpl, w = memoryview(a.constBits()), a.bytesPerLine(), a.width()
        for y in range(a.height()):
            if mv[y * bpl:y * bpl + w].tobytes().count(255) != w:
                return True
        return False

    def clipboard_image(self):
        """Картинка из буфера обмена. Если есть формат PNG — берём его: в нём сохраняется прозрачность
        (обычная «картинка из буфера» на Windows часто приходит уже без альфа-канала)."""
        cb = QApplication.clipboard()
        md = cb.mimeData()
        if md is None:
            return None
        if md.hasFormat("image/png"):
            img = QImage.fromData(bytes(md.data("image/png")), "PNG")
            if not img.isNull():
                self.source_note = "из буфера: PNG " + ("с прозрачностью" if self._has_transparency(img)
                                                       else "без прозрачных пикселей")
                return img
        img = cb.image() if md.hasImage() else None
        if img is not None and not img.isNull():
            # обычная картинка из буфера Windows приходит без альфа-канала: прозрачность источником не передана
            self.source_note = "из буфера: обычная картинка, прозрачность не передана"
        return img

    def _push_patch_state(self, p, item, rect, new_meta, text):
        self.undo.push(PatchStateCmd(self, item, rect, p["id"], p.get("orig"), self._patch_meta(p), new_meta, text))

    def replace_patch(self, pid, img, fit="crop"):
        """Вставляет другой кусок на место участка: кусок подгоняется по пикселям к размеру участка.
        fit: 'crop' — сначала обрезать по центру до нужного соотношения, 'stretch' — растянуть.
        Новая вставка становится активной; хранятся последние PATCH_MAX_ITERS вставок."""
        ctx = self._patch_context(pid)
        if ctx is None or img is None or img.isNull():
            return False
        p, item, rect = ctx
        w, h = rect.width(), rect.height()
        src_note, self.source_note = self.source_note, ""
        cut_share = None
        if self.remove_white and cutout.available() and not self._has_transparency(img):
            cut = cutout.white_to_alpha(img)   # белый фон у вставки (связанный с краями) -> прозрачный
            if cut is not None:
                img, cut_share = cut
        if img.hasAlphaChannel():
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)   # без тёмных каёмок при масштабировании
        src, mode = img, "exact"
        pr, nr = w / h, img.width() / img.height()
        if abs(nr / pr - 1.0) > 1e-3:
            mode = fit
            if fit == "crop":
                if nr > pr:
                    cw = max(1, round(img.height() * pr))
                    src = img.copy((img.width() - cw) // 2, 0, cw, img.height())
                else:
                    ch = max(1, round(img.width() / pr))
                    src = img.copy(0, (img.height() - ch) // 2, img.width(), ch)
        sw, sh = src.width(), src.height()
        fitted = src if (sw, sh) == (w, h) else src.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        has_alpha = self._has_transparency(fitted)   # прозрачные места сохраняются и накладываются на исходный участок
        region = fitted.convertToFormat(QImage.Format_ARGB32_Premultiplied if has_alpha else item.image.format())
        fname = None
        if self.project is not None:
            try:
                fname = self.project.import_qimage(region)   # пиксели вставки хранятся, чтобы её можно было скрыть/вернуть
            except OSError as ex:
                self.message.emit(f"Не удалось сохранить вставку: {ex}")
                return False
            self.regions.put(self.project, fname, item.image.format(), rect.size(), region, alpha=has_alpha)
        meta = self._patch_meta(p)
        meta["iter_seq"] += 1
        meta["iters"].append({"n": meta["iter_seq"], "file": fname, "src_w": img.width(), "src_h": img.height(),
                              "used_w": sw, "used_h": sh, "scale_x": w / sw, "scale_y": h / sh,
                              "fit": mode, "alpha": has_alpha, "cutout": cut_share, "ts": time.time()})
        meta["iters"] = meta["iters"][-PATCH_MAX_ITERS:]
        meta["active"] = len(meta["iters"]) - 1
        meta["visible"] = True
        self._push_patch_state(p, item, rect, meta, f"Заменить участок #{pid}")
        notes = []
        if cut_share is not None:
            notes.append(f"белый фон убран ({cut_share * 100:.0f}%)")
        elif has_alpha:
            notes.append("с прозрачностью")
        if src_note:
            notes.append(src_note)
        self.message.emit(f"Участок #{pid} заменён (итерация {meta['iter_seq']}): "
                          f"{img.width()}×{img.height()} → {w}×{h} px" + "".join(f" · {n}" for n in notes))
        return True

    def toggle_patch_visible(self, pid):
        """Глазик: скрыть вставку (показать исходные пиксели участка) или показать её снова."""
        ctx = self._patch_context(pid)
        if ctx is None:
            return False
        p, item, rect = ctx
        it = self.patch_active_iter(p)
        if it is None:
            self.message.emit("У участка пока нет вставленного куска")
            return False
        meta = self._patch_meta(p)
        meta["visible"] = not p.get("visible", True)
        if self.region_for(item, rect, meta, p.get("orig")) is None:
            self.message.emit("Файл с пикселями участка не найден")
            return False
        self._push_patch_state(p, item, rect, meta, f"{'Показать' if meta['visible'] else 'Скрыть'} вставку #{pid}")
        self.message.emit(f"Участок #{pid}: вставка {'показана' if meta['visible'] else 'скрыта (исходный участок)'}")
        return True

    def set_patch_iteration(self, pid, index):
        """Делает активной одну из сохранённых вставок и показывает её."""
        ctx = self._patch_context(pid)
        if ctx is None:
            return False
        p, item, rect = ctx
        iters = p.get("iters", [])
        if not 0 <= index < len(iters):
            return False
        meta = self._patch_meta(p)
        meta["active"], meta["visible"] = index, True
        if self.region_for(item, rect, meta, p.get("orig")) is None:
            self.message.emit("Файл с пикселями участка не найден")
            return False
        self._push_patch_state(p, item, rect, meta, f"Итерация {iters[index]['n']} участка #{pid}")
        self.message.emit(f"Участок #{pid}: показана итерация {iters[index]['n']}")
        return True

    def patch_details(self, pid):
        """Отладочные сведения об участке для боковой панели."""
        p = self.get_patch(pid)
        if p is None:
            return ""
        item = self.find_item(p["uid"])
        ratio = p["ratio"] or f"{p['w'] / p['h']:.3f}:1"
        lines = [f"Участок #{p['id']}",
                 f"Картинка: {item.name if item else p.get('name', '?') + '  (удалена с холста)'}"]
        if item:
            lines.append(f"Оригинал: {item.w}×{item.h} px")
        lines += [f"Область: x={p['x']}  y={p['y']}",
                  f"Размер: {p['w']}×{p['h']} px",
                  f"Соотношение: {ratio}  ({ {'free': 'свободная', 'standard': 'стандарт', 'gemini': 'Gemini'}[p['mode']] })"]
        if p.get("res"):
            tw, th = self.ratios["gemini"].get(p["res"], {}).get(p["ratio"], (0, 0))
            lines.append(f"Эталон Gemini {p['res']}: {tw}×{th} px")
        it = self.patch_active_iter(p)
        if it is None:
            lines.append("Статус: исходный")
        else:
            shown = p.get("visible", True)
            lines += [f"Статус: {'вставка показана' if shown else 'вставка скрыта (виден исходный участок)'}",
                      f"Итерация: {it['n']}  (сохранено {len(p['iters'])} из {PATCH_MAX_ITERS})",
                      f"Вставлено: {it['src_w']}×{it['src_h']} px"
                      f"{' · белый фон убран (' + format(it['cutout'] * 100, '.0f') + '%)' if it.get('cutout') is not None else (' · с прозрачностью' if it.get('alpha') else '')}",
                      f"Подгонка: {it['used_w']}×{it['used_h']} → {p['w']}×{p['h']}"
                      f" (×{it['scale_x']:.3f} / ×{it['scale_y']:.3f}, "
                      f"{ {'exact': 'без изменения пропорций', 'crop': 'с обрезкой по центру', 'stretch': 'растянуто'}[it['fit']] })"]
        return "\n".join(lines)

    def export_psd(self, uid, path, progress=None):
        """Экспорт картинки в слоёный PSD для Photoshop: нижний слой — картинка с исходными пикселями,
        выше — актуальные вставки участков (только активные итерации, история не нужна).
        Штрихи кисти не экспортируются. Возвращает число слоёв-вставок."""
        item = self.find_item(uid)
        if item is None:
            raise ValueError("Картинки нет на холсте")
        fmt = item.image.format()
        base = item.image.copy()
        live = []
        for p in sorted(self.patches, key=lambda q: q["id"]):
            it = self.patch_active_iter(p) if p["uid"] == uid else None
            if it is None:
                continue
            rect = QRect(p["x"], p["y"], p["w"], p["h"]).intersected(QRect(0, 0, item.w, item.h))
            live.append((p, it, rect))
            orig = self._hidden_region(item, rect, p.get("orig"))
            if orig is not None:   # на нижнем слое должно быть то, что под вставкой: вставка лежит отдельным слоем
                q = QPainter(base)
                q.setCompositionMode(QPainter.CompositionMode_Source)
                q.drawImage(rect.topLeft(), orig)
                q.end()
        layers = [{"name": item.name, "x": 0, "y": 0, "image": base, "visible": True}]
        for p, it, rect in live:
            region = self.regions.get(self.project, it["file"], fmt, rect.size(), alpha=bool(it.get("alpha")))
            if region is None:
                raise ValueError(f"Не найден файл вставки участка #{p['id']}")
            layers.append({"name": f"У{p['id']} · {item.name}", "x": rect.x(), "y": rect.y(),
                           "image": region, "visible": bool(p.get("visible", True))})
        psd.write_psd(path, item.w, item.h, item.image, layers, progress)
        return len(live)

    def _migrate_patches(self):
        """Участки старого формата (одна «rep» без хранения пикселей) превращаем в первую итерацию:
        её пиксели берём с картинки, где замена уже сделана."""
        changed = False
        for p in self.patches:
            if "iters" in p:
                p.pop("rep", None)
                continue
            rep = p.pop("rep", None)
            p.update({"iters": [], "active": None, "visible": True, "iter_seq": 0})
            changed = True
            item = self.find_item(p["uid"])
            if rep and item is not None and self.project is not None:
                rect = QRect(p["x"], p["y"], p["w"], p["h"]).intersected(QRect(0, 0, item.w, item.h))
                try:
                    fname = self.project.import_qimage(item.image.copy(rect))
                except OSError:
                    continue
                p["iters"].append({"n": 1, "file": fname, "src_w": rep.get("src_w", p["w"]),
                                   "src_h": rep.get("src_h", p["h"]), "used_w": rep.get("used_w", p["w"]),
                                   "used_h": rep.get("used_h", p["h"]), "scale_x": rep.get("scale_x", 1.0),
                                   "scale_y": rep.get("scale_y", 1.0), "fit": rep.get("fit", "exact"),
                                   "ts": rep.get("ts", time.time())})
                p["active"], p["iter_seq"] = 0, 1
        if changed:
            self.modified.emit()

    def _apply_base_states(self):
        """При открытии окна: если у картинки показана замена основы, подставляем её пиксели (файл оригинала цел)."""
        for item in self.image_items():
            if self.base_active_iter(item) is not None:
                img = self.base_image_for(item, item.base)
                if img is not None:
                    item.set_pixels(img.copy())

    def _apply_patch_states(self, uid=None):
        """Файл картинки не переписывается при каждой смене вставки: при открытии окна на картинку
        накладывается то, что должно быть видно (активная вставка или то, что под участком)."""
        for p in sorted(self.patches, key=lambda q: q["id"]):
            if not p.get("iters") or (uid is not None and p["uid"] != uid):
                continue
            item = self.find_item(p["uid"])
            if item is None:
                continue
            rect = QRect(p["x"], p["y"], p["w"], p["h"]).intersected(QRect(0, 0, item.w, item.h))
            region = self.region_for(item, rect, p, p.get("orig"))
            if region is not None:
                item.paste_region(rect, region)

    # ------------------------------------------------------------- замена основной картинки (с историей)
    def base_active_iter(self, item):
        """Активная замена основы, если она сейчас показана (иначе None — виден оригинал)."""
        m = item.base
        a, its = m.get("active"), m.get("iters", [])
        return its[a] if m.get("visible", True) and a is not None and 0 <= a < len(its) else None

    @staticmethod
    def _base_meta(item):
        return copy.deepcopy(item.base)

    def base_image_for(self, item, meta):
        a, its = meta.get("active"), meta.get("iters", [])
        it = its[a] if meta.get("visible", True) and a is not None and 0 <= a < len(its) else None
        return self.base_cache.get(self.project, it["file"] if it else item.file, item.fmt)

    def base_ratio_diff(self, uid, img):
        item = self.find_item(uid)
        return abs((img.width() / img.height()) / (item.w / item.h) - 1.0)

    def replace_base(self, uid, img, fit="crop"):
        """Заменяет основную картинку другой: новая подгоняется по пикселям к размеру оригинала (размер и положение
        на холсте не меняются, участки остаются на своих местах). Хранятся последние PATCH_MAX_ITERS замен."""
        item = self.find_item(uid)
        if item is None or img is None or img.isNull() or self.project is None:
            self.message.emit("Картинки нет на холсте")
            return False
        w, h = item.w, item.h
        if img.hasAlphaChannel():
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        src, mode = img, "exact"
        pr, nr = w / h, img.width() / img.height()
        if abs(nr / pr - 1.0) > 1e-3:
            mode = fit
            if fit == "crop":
                if nr > pr:
                    cw = max(1, round(img.height() * pr))
                    src = img.copy((img.width() - cw) // 2, 0, cw, img.height())
                else:
                    ch = max(1, round(img.width() / pr))
                    src = img.copy(0, (img.height() - ch) // 2, img.width(), ch)
        sw, sh = src.width(), src.height()
        fitted = src if (sw, sh) == (w, h) else src.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        has_alpha = self._has_transparency(fitted)
        if has_alpha:   # прозрачные места заменяемой картинки показывают оригинал
            base0 = self.base_cache.get(self.project, item.file, item.fmt)
            region = base0.copy() if base0 is not None else QImage(w, h, item.fmt)
            if base0 is None:
                region.fill(0xFF000000)
            q = QPainter(region)
            q.setCompositionMode(QPainter.CompositionMode_SourceOver)
            q.drawImage(0, 0, fitted)
            q.end()
        else:
            region = fitted.convertToFormat(item.fmt)
        try:
            fname = self.project.import_qimage(region)
        except OSError as ex:
            self.message.emit(f"Не удалось сохранить замену: {ex}")
            return False
        self.base_cache.put(self.project, fname, item.fmt, region)
        meta = self._base_meta(item)
        meta["iter_seq"] += 1
        meta["iters"].append({"n": meta["iter_seq"], "file": fname, "src_w": img.width(), "src_h": img.height(),
                              "used_w": sw, "used_h": sh, "scale_x": w / sw, "scale_y": h / sh,
                              "fit": mode, "alpha": has_alpha, "ts": time.time()})
        meta["iters"] = meta["iters"][-PATCH_MAX_ITERS:]
        meta["active"] = len(meta["iters"]) - 1
        meta["visible"] = True
        src_note, self.source_note = self.source_note, ""
        self.undo.push(BaseStateCmd(self, item, self._base_meta(item), meta, f"Заменить картинку «{item.name}»"))
        self.message.emit(f"Картинка «{item.name}» заменена (итерация {meta['iter_seq']}): "
                          f"{img.width()}×{img.height()} → {w}×{h} px" + (f" · {src_note}" if src_note else ""))
        return True

    def toggle_base_visible(self, uid):
        """Глазик: скрыть замену (показать оригинал картинки) или показать её снова."""
        item = self.find_item(uid)
        if item is None:
            return False
        if not item.base["iters"] or item.base["active"] is None:
            self.message.emit("У картинки пока нет замены")
            return False
        meta = self._base_meta(item)
        meta["visible"] = not item.base.get("visible", True)
        if self.base_image_for(item, meta) is None:
            self.message.emit("Файл основной картинки не найден")
            return False
        self.undo.push(BaseStateCmd(self, item, self._base_meta(item), meta,
                                    f"{'Показать' if meta['visible'] else 'Скрыть'} замену «{item.name}»"))
        self.message.emit(f"«{item.name}»: замена {'показана' if meta['visible'] else 'скрыта (виден оригинал)'}")
        return True

    def set_base_iteration(self, uid, index):
        item = self.find_item(uid)
        if item is None or not 0 <= index < len(item.base["iters"]):
            return False
        meta = self._base_meta(item)
        meta["active"], meta["visible"] = index, True
        if self.base_image_for(item, meta) is None:
            self.message.emit("Файл основной картинки не найден")
            return False
        n = item.base["iters"][index]["n"]
        self.undo.push(BaseStateCmd(self, item, self._base_meta(item), meta, f"Итерация {n} картинки «{item.name}»"))
        self.message.emit(f"«{item.name}»: показана итерация {n}")
        return True

    def copy_image(self, uid):
        item = self.find_item(uid)
        if item is None:
            return False
        QApplication.clipboard().setImage(item.image)
        self.message.emit(f"Картинка скопирована: {item.w}×{item.h} px")
        return True

    def image_details(self, uid):
        """Сведения об основной картинке для боковой панели."""
        item = self.find_item(uid)
        if item is None:
            return ""
        n = sum(1 for p in self.patches if p["uid"] == uid)
        lines = [f"Картинка: {item.name}", f"Оригинал: {item.w}×{item.h} px", f"Участков: {n}"]
        it = self.base_active_iter(item)
        if not item.base["iters"]:
            lines.append("Статус: исходная")
        else:
            m = item.base
            cur = m["iters"][m["active"]] if m["active"] is not None else None
            shown = m.get("visible", True)
            lines.append("Статус: " + ("замена показана" if shown else "замена скрыта (виден оригинал)"))
            if cur:
                lines += [f"Итерация: {cur['n']}  (сохранено {len(m['iters'])} из {PATCH_MAX_ITERS})",
                          f"Вставлено: {cur['src_w']}×{cur['src_h']} px{' · с прозрачностью' if cur.get('alpha') else ''}",
                          f"Подгонка: {cur['used_w']}×{cur['used_h']} → {item.w}×{item.h}"
                          f" (×{cur['scale_x']:.3f} / ×{cur['scale_y']:.3f}, "
                          f"{ {'exact': 'без изменения пропорций', 'crop': 'с обрезкой по центру', 'stretch': 'растянуто'}[cur['fit']] })"]
        return "\n".join(lines)

    def show_image(self, uid):
        item = self.find_item(uid)
        if item is None:
            return False
        self.scene().clearSelection()
        item.setSelected(True)
        self.centerOn(item.mapToScene(item.rect().center()))
        self._on_zoom()
        return True

    def copy_selected_image(self):
        sel = self.selected_images()
        if not sel:
            return False
        QApplication.clipboard().setImage(sel[0].image)
        self.message.emit(f"Картинка скопирована: {sel[0].w}×{sel[0].h} px")
        return True

    def copy_current(self):
        if self.crop:
            return self.copy_crop()
        return self.copy_selected_image()

    # ------------------------------------------------------------- добавление картинок
    def add_image_files(self, paths, scene_pos=None):
        if self.project is None:
            return
        new_items = []
        for pth in paths:
            pth = Path(pth)
            try:
                fname = self.project.import_file(pth)
            except OSError as ex:
                self.message.emit(f"Не удалось скопировать {pth.name}: {ex}")
                continue
            img = load_image(self.project.asset_path(fname))
            if img is None:
                self.project.remove_asset(fname)
                self.message.emit(f"Не удалось открыть изображение: {pth.name}")
                continue
            new_items.append(ImageItem(img, fname, pth.name, self.grid))
        self._place_new(new_items, scene_pos)

    def add_qimage(self, qimage, name="вставка"):
        if self.project is None or qimage is None or qimage.isNull():
            return
        try:
            fname = self.project.import_qimage(qimage)
        except OSError as ex:
            self.message.emit(f"Не удалось сохранить: {ex}")
            return
        img = load_image(self.project.asset_path(fname))
        if img is None:
            return
        self._place_new([ImageItem(img, fname, name, self.grid)], None)

    def _place_new(self, items, scene_pos):
        if not items:
            return
        vp = self.viewport().rect()
        z = self.zoom
        center = scene_pos or self.mapToScene(vp.center())
        total_w, scales = 0.0, []
        for it in items:
            s = min(1.0, vp.width() * 0.5 / z / it.w, vp.height() * 0.5 / z / it.h)
            scales.append(s)
            total_w += it.w * s
        total_w += 24 * (len(items) - 1)
        x = center.x() - total_w / 2
        self.scene().clearSelection()
        for it, s in zip(items, scales):
            it.set_scale(s)
            self.scene().addItem(it)
            it.set_view_zoom(z)
            it.setZValue(self.next_z())
            it.setPos(x, center.y() - it.h * s / 2)
            x = it.pos().x() + it.w * s + max(24, self.grid.size)  # учитываем сдвиг от привязки
            it.setSelected(True)
        self.undo.push(AddItemsCmd(self, items, "Добавить картинку"))
        n = len(items)
        self.message.emit(f"Добавлено: {n} шт. · оригинал {items[0].w}×{items[0].h} px сохранён без потерь"
                          if n == 1 else f"Добавлено картинок: {n}")

    def paste_clipboard(self):
        md = QApplication.clipboard().mimeData()
        if md is None:
            return
        if md.hasUrls():
            files = [u.toLocalFile() for u in md.urls()
                     if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in IMAGE_EXT]
            if files:
                self.add_image_files(files)
                return
        if md.hasImage() or md.hasFormat("image/png"):
            self.add_qimage(self.clipboard_image())

    # drag & drop
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasImage():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        pos = self.mapToScene(e.position().toPoint())
        if md.hasUrls():
            files = [u.toLocalFile() for u in md.urls()
                     if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in IMAGE_EXT]
            if files:
                self.add_image_files(files, pos)
                e.acceptProposedAction()
                return
        if md.hasImage():
            self.add_qimage(md.imageData() if not hasattr(md.imageData(), "toImage") else md.imageData().toImage())
            e.acceptProposedAction()

    # ------------------------------------------------------------- действия над выбранными
    def delete_selected(self):
        items = self.selected_images()
        if not items:
            return
        self.undo.push(RemoveItemsCmd(self, items, "Удалить"))

    def _z_change(self, front):
        items = self.selected_images()
        if not items:
            return
        zs = [i.zValue() for i in self.image_items()]
        base = (max(zs) + 1) if front else (min(zs) - 1)
        changes = []
        for k, it in enumerate(sorted(items, key=lambda i: i.zValue())):
            old = it.get_state()
            new = old[:4] + (base + k if front else base - len(items) + k + 1,)
            changes.append((it, old, new))
        self.undo.push(TransformCmd(self, changes, "Порядок слоёв"))

    def bring_to_front(self):
        self._z_change(True)

    def send_to_back(self):
        self._z_change(False)

    def set_selected_scale(self, scale):
        """Масштаб в долях оригинала (1.0 = 100%), пропорции восстанавливаются."""
        self._push_states(lambda it, old: (old[0], old[1], scale, scale, old[4]), "Масштаб")

    def reset_proportions(self):
        """Возвращает оригинальные пропорции, сохраняя ширину."""
        self._push_states(lambda it, old: (old[0], old[1], old[2], old[2], old[4]), "Пропорции")

    def set_selected_size(self, w, h):
        """Размер в пикселях холста (для одной выбранной картинки)."""
        sel = self.selected_images()
        if len(sel) != 1:
            return
        it = sel[0]
        w, h = max(1.0, float(w)), max(1.0, float(h))
        self._push_states(lambda it, old: (old[0], old[1], w / it.w, h / it.h, old[4]), "Размер")

    def _push_states(self, fn, text):
        changes = []
        for it in self.selected_images():
            old = it.get_state()
            new = fn(it, old)
            if new != old:
                changes.append((it, old, new))
        if changes:
            self.undo.push(TransformCmd(self, changes, text))
        self._emit_selection()

    def contextMenuEvent(self, e):
        item = self.itemAt(e.pos())
        while item is not None and not isinstance(item, ImageItem):
            item = item.parentItem()
        menu = QMenu(self)
        if item is not None:
            if not item.isSelected():
                self.scene().clearSelection()
                item.setSelected(True)
            menu.addAction("Обрезать…", lambda: (self.set_tool("crop"), None))
            menu.addAction("Показать 1:1 (100%)", lambda: self.set_selected_scale(1.0))
            menu.addAction("Копировать картинку", self.copy_selected_image)
            menu.addSeparator()
            menu.addAction("На передний план", self.bring_to_front)
            menu.addAction("На задний план", self.send_to_back)
            menu.addSeparator()
            menu.addAction("Удалить", self.delete_selected)
        else:
            menu.addAction("Вставить", self.paste_clipboard)
            menu.addAction("Показать всё", self.fit_all)
        menu.exec(e.globalPos())

    # ------------------------------------------------------------- мышь
    def _scene_pt(self, e):
        return self.mapToScene(e.position().toPoint())

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and self._space):
            self._pan = e.position().toPoint()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton:
            if self.tool == "brush" and self.project is not None:
                self._start_stroke(self._scene_pt(e))
                return
            if self.tool == "eraser":
                self._erased = []
                self._erase_at(self._scene_pt(e))
                return
            if self.tool in ("select", "crop") and self.itemAt(e.position().toPoint()) is None:
                self.empty_clicked.emit()
            if self.tool == "crop":
                item = self.itemAt(e.position().toPoint())
                if isinstance(item, CropItem):
                    super().mousePressEvent(e)
                    return
                while item is not None and not isinstance(item, ImageItem):
                    item = item.parentItem()
                if item is not None and item is not self.crop_target:
                    self.begin_crop(item)
                return
            if self.tool == "select":
                self._snap = {i: i.get_state() for i in self.image_items()}
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            d = e.position().toPoint() - self._pan
            self._pan = e.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            self.viewport().update()
            return
        if self._stroke is not None:
            pt = self._scene_pt(e)
            last = self._stroke.points[-1]
            if (pt - last).manhattanLength() * self.zoom >= 1.5:
                self._stroke.add_point(pt)
            return
        if self._erased is not None:
            self._erase_at(self._scene_pt(e))
            return
        if not e.buttons():
            self.viewport().setCursor(self._cursor_at(e.position().toPoint()))
        if (e.buttons() & Qt.LeftButton) and self.tool in ("select", "crop"):
            now = time.perf_counter()
            if now - self._last_move < MOVE_INTERVAL:   # слишком часто: запоминаем и обработаем по таймеру
                self._pending_move = QMouseEvent(e)
                if not self._flush_timer.isActive():
                    self._flush_timer.start(int(MOVE_INTERVAL * 1000))
                return
            self._last_move = now
            self._pending_move = None
        self._process_move(e)

    def _process_move(self, e):
        super().mouseMoveEvent(e)
        if self._snap is not None and (e.buttons() & Qt.LeftButton):
            self._emit_selection()

    def _flush_pending_move(self):
        ev, self._pending_move = self._pending_move, None
        if ev is not None:
            self._last_move = time.perf_counter()
            self._process_move(ev)

    def mouseReleaseEvent(self, e):
        QTimer.singleShot(0, self.refresh_cursor)
        if self._pan is not None:
            self._pan = None
            self.update_cursor()
            return
        if self._stroke is not None:
            s, self._stroke = self._stroke, None
            if len(s.points) == 1:
                s.add_point(s.points[0] + QPointF(0.01, 0))
            self.undo.push(AddItemsCmd(self, [s], "Штрих"))
            return
        if self._erased is not None:
            gone, self._erased = self._erased, None
            if gone:
                self.undo.push(RemoveItemsCmd(self, gone, "Ластик"))
            return
        self._flush_timer.stop()
        self._flush_pending_move()   # последнее положение мыши применяем до отпускания
        super().mouseReleaseEvent(e)
        if self._snap is not None:
            snap, self._snap = self._snap, None
            changes = []
            for it, old in snap.items():
                if it.scene() is not None and it.get_state() != old:
                    changes.append((it, old, it.get_state()))
            if changes:
                self.undo.push(TransformCmd(self, changes, "Переместить"))
            self._emit_selection()

    def _start_stroke(self, pt):
        self._stroke = StrokeItem(self.brush_color, max(0.2, self.brush_size / self.zoom))
        self.scene().addItem(self._stroke)
        self._stroke.add_point(pt)

    def _erase_at(self, pt):
        r = self._eraser_radius_px() / self.zoom
        path = QPainterPath()
        path.addEllipse(pt, r, r)
        for it in self.scene().items(path, Qt.IntersectsItemShape):
            if isinstance(it, StrokeItem):
                self.scene().removeItem(it)
                self._erased.append(it)

    # ------------------------------------------------------------- клавиши
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = True
            self.update_cursor()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.crop:
            self.copy_crop()
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = False
            self.update_cursor()
            return
        super().keyReleaseEvent(e)

    # ------------------------------------------------------------- сохранение / загрузка
    def to_dict(self):
        items = []
        for it in sorted(self.scene().items(), key=lambda i: i.zValue()):
            if isinstance(it, ImageItem):
                x, y, sx, sy, z = it.get_state()
                d = {"type": "image", "uid": it.uid, "file": it.file, "name": it.name,
                     "x": round(x, 3), "y": round(y, 3), "sx": sx, "sy": sy, "z": z}
                if it.base["iters"]:
                    d["base"] = it.base
                items.append(d)
            elif isinstance(it, StrokeItem):
                items.append(it.to_dict())
        c = self.mapToScene(self.viewport().rect().center())
        return {"version": 1,
                "grid": {"show": self.grid.show, "snap": self.grid.snap},
                "view": {"zoom": self.zoom, "cx": c.x(), "cy": c.y()},
                "items": items,
                "patches": self.patches}

    def referenced_files(self):
        files = {i.file for i in self.image_items()}
        for i in self.image_items():
            files.update(it["file"] for it in i.base["iters"] if it.get("file"))
        for p in self.patches:
            if p.get("orig"):
                files.add(p["orig"])
            files.update(it["file"] for it in p.get("iters", []) if it.get("file"))
        return files

    def clear_all(self):
        self.end_crop()
        self._marker = None
        self.regions.clear()
        self.base_cache.clear()
        self.scene().clearSelection()
        self.scene().clear()
        self.patches = []
        self._patch_seq = 0
        self.patches_changed.emit()
        self._z = 0
        self._stroke = None
        self._erased = None
        self._snap = None

    def load_dict(self, data):
        self.clear_all()
        g = data.get("grid") or {}
        self.grid.show = bool(g.get("show", True))
        self.grid.snap = bool(g.get("snap", True))
        missing = 0
        for d in data.get("items", []):
            try:
                if d.get("type") == "image":
                    img = load_image(self.project.asset_path(d["file"]))
                    if img is None:
                        missing += 1
                        continue
                    it = ImageItem(img, d["file"], d.get("name", d["file"]), self.grid, d.get("uid"))
                    old_scale = float(d.get("scale", 1.0))   # формат до неравномерного растяжения
                    sx = float(d.get("sx", old_scale))
                    sy = float(d.get("sy", old_scale))
                    b = d.get("base")
                    if isinstance(b, dict) and b.get("iters"):
                        it.base = {"iters": b["iters"], "active": b.get("active"),
                                   "visible": b.get("visible", True), "iter_seq": b.get("iter_seq", 0)}
                    self.scene().addItem(it)
                    it.set_view_zoom(self.zoom)
                    it.set_state((float(d["x"]), float(d["y"]), sx, sy, float(d.get("z", 0))))
                    self._z = max(self._z, int(it.zValue()))
                elif d.get("type") == "stroke":
                    self.scene().addItem(StrokeItem(d["color"], d["width"], d["points"]))
            except (KeyError, ValueError, TypeError):
                continue
        if missing:
            self.message.emit(f"Не найдено файлов картинок: {missing}")
        self.patches = [p for p in data.get("patches", []) if isinstance(p, dict) and "id" in p]
        self._patch_seq = max([p["id"] for p in self.patches] or [0])
        self._migrate_patches()
        self._apply_base_states()
        self._apply_patch_states()
        self.patches_changed.emit()
        view = data.get("view")
        if self.isVisible():
            self._apply_view(view)
        else:
            self._pending_view = view or {}
            if not view:
                self._pending_view = None
                QTimer.singleShot(0, lambda: self._apply_view(None))
        self.set_tool("select")
        self._emit_selection()

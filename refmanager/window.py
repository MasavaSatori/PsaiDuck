"""Главное окно: панель проектов, верхняя панель, холст, инспектор."""
import sys
import time
from pathlib import Path

from PySide6.QtCore import (QByteArray, QEvent, QPoint, QRect, QRectF, QSize, QStandardPaths, Qt,
                            QTimer)
from PySide6.QtGui import (QColor, QCursor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap,
                           QShortcut, QUndoStack)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QColorDialog, QFileDialog,
                               QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressDialog, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import cutout, theme
from .canvas import IMAGE_EXT, Canvas
from .items import GridSettings, load_image
from .project import FolderStore, ProjectStore, Settings
from .sidebar import ROLE_ID, ROLE_KIND, ROLE_PARENT, ClearableTree, SectionBox, small_button

SWATCHES = ["#E5484D", "#F76B15", "#FFC53D", "#30A46C", "#12A594", "#3E63DD", "#8E4EC6",
            "#E93D82", "#8B92A5", "#FFFFFF"]
PIN_SWP = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
EDGE = 6  # ширина зоны растягивания окна за край, px


def tool_button(icon_name, tip, checkable=False, size=22):
    b = QToolButton()
    b.setIcon(theme.icon(icon_name, size))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.NoFocus)
    return b


def section(text):
    lab = QLabel(text.upper())
    lab.setObjectName("Section")
    return lab


class Segmented(QWidget):
    """Сегментированный переключатель."""

    def __init__(self, options, on_change):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for i, (key, label) in enumerate(options):
            b = QPushButton(label)
            b.setObjectName("Seg")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setProperty("pos", "first" if i == 0 else ("last" if i == len(options) - 1 else "middle"))
            lay.addWidget(b)
            self.group.addButton(b)
            self.buttons[key] = b
            b.clicked.connect(lambda _=False, k=key: on_change(k))

    def set(self, key):
        if key in self.buttons:
            self.buttons[key].setChecked(True)


class Swatch(QToolButton):
    def __init__(self, color):
        super().__init__()
        self.color = QColor(color)
        self.setCheckable(True)
        self.setFixedSize(26, 26)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(color)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        p.setPen(QPen(QColor(theme.DISABLED), 1))
        p.setBrush(self.color)
        p.drawEllipse(r)
        if self.isChecked():
            p.setPen(QPen(QColor(theme.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))


class TopBar(QWidget):
    """Верхняя панель, она же заголовок безрамочного окна (перетаскивание, двойной клик — развернуть)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("TopBar")
        self.setAttribute(Qt.WA_StyledBackground, True)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self.win.isMaximized():
            h = self.win.windowHandle()
            if h is not None:
                h.startSystemMove()
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.win.toggle_maximize()
        e.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Previewer")
        self.setWindowIcon(theme.icon("pin", 32))
        # безрамочное окно: заголовок и кнопки окна рисуем сами
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1360, 840)
        self.setMinimumSize(1120, 620)
        self._edge_cursor = False
        self._patch_icons = {}
        QApplication.instance().installEventFilter(self)

        self.settings = Settings()
        self.store = ProjectStore()
        self.folders = FolderStore()
        self.project = None
        self.ws_id = None
        self._filling = False
        self._group_open = {}
        self.grid = GridSettings()
        self.undo = QUndoStack(self)
        self.undo.setUndoLimit(200)   # история не растёт бесконечно при многократных действиях
        self.canvas = Canvas(self.grid, self.undo)
        self._pinned = False
        self._loading_ui = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self.save_now)

        self._build_ui()
        self._build_shortcuts()
        self._connect()
        self._restore_settings()

        if not self.store.all():
            self.store.create("Проект 1")
        self._fill_projects()
        last = self.store.get(self.settings.get("last_project")) or self.store.all()[0]
        ws = self.settings.get("last_ws")
        if ws not in {w["id"] for w in last.workspaces}:
            ws = last.active_ws
        self.open_workspace(last, ws)

    # ================================================================= UI
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("Root")
        root.setMouseTracking(True)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- верхняя панель
        top = TopBar(self)
        top.setMouseTracking(True)
        top.setFixedHeight(54)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(10, 0, 12, 0)
        tl.setSpacing(4)

        self.btn_sidebar = tool_button("sidebar", "Панель проектов", True)
        tl.addWidget(self.btn_sidebar)
        self.lbl_project = QLabel("")
        self.lbl_project.setObjectName("Title")
        self.lbl_project.setMinimumWidth(120)
        tl.addWidget(self.lbl_project)
        tl.addStretch(1)

        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_btns = {}
        for key, ic, tip in (("select", "select", "Выбор и перемещение (V)"),
                             ("brush", "brush", "Кисть (B)"),
                             ("eraser", "eraser", "Ластик (E)"),
                             ("crop", "crop", "Обрезка (C)")):
            b = tool_button(ic, tip, True, 24)
            b.setFixedSize(40, 36)
            self.tool_group.addButton(b)
            self.tool_btns[key] = b
            tl.addWidget(b)
            b.clicked.connect(lambda _=False, k=key: self.canvas.set_tool(k))
        self.tool_btns["select"].setChecked(True)
        tl.addSpacing(10)

        self.btn_add = tool_button("image", "Добавить картинки (Ctrl+O). Можно перетащить файлы в окно или вставить Ctrl+V")
        self.btn_undo = tool_button("undo", "Отменить (Ctrl+Z)")
        self.btn_redo = tool_button("redo", "Повторить (Ctrl+Y)")
        self.btn_fit = tool_button("fit", "Показать всё (F)")
        for b in (self.btn_add, self.btn_undo, self.btn_redo, self.btn_fit):
            tl.addWidget(b)
        tl.addStretch(1)

        self.btn_pin = tool_button("pin", "Поверх всех окон", True)
        tl.addWidget(self.btn_pin)
        tl.addSpacing(6)
        self.btn_inspector = tool_button("panel", "Панель настроек", True)
        tl.addWidget(self.btn_inspector)
        tl.addSpacing(8)
        for name, ic, tip in (("min", "win_min", "Свернуть"), ("max", "win_max", "Развернуть"),
                              ("close", "win_close", "Закрыть")):
            b = tool_button(ic, tip, size=20)
            b.setObjectName("WinClose" if name == "close" else "WinBtn")
            b.setFixedSize(38, 32)
            tl.addWidget(b)
            setattr(self, f"btn_win_{name}", b)
        self.btn_win_min.clicked.connect(self.showMinimized)
        self.btn_win_max.clicked.connect(self.toggle_maximize)
        self.btn_win_close.clicked.connect(self.close)
        outer.addWidget(top)

        # ---- тело
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)

        self.side = self._build_side()
        self.side.setMouseTracking(True)
        body.addWidget(self.side)
        body.addWidget(self.canvas, 1)
        self.inspector = self._build_inspector()
        self.inspector.setMouseTracking(True)
        body.addWidget(self.inspector)

        # ---- статус
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.lbl_zoom = QLabel("100%")
        sb.addPermanentWidget(self.lbl_zoom)

    def _build_side(self):
        w = QWidget()
        w.setAttribute(Qt.WA_StyledBackground, True)
        w.setObjectName("SidePanel")
        w.setFixedWidth(272)
        self.side_lay = QVBoxLayout(w)
        self.side_lay.setContentsMargins(0, 10, 0, 8)
        self.side_lay.setSpacing(6)

        # ---- проекты: папки → проекты → рабочие окна
        self.proj_tree = QTreeWidget()
        self.proj_tree.setHeaderHidden(True)
        self.proj_tree.setIndentation(14)
        self.proj_tree.setIconSize(QSize(18, 18))
        self.proj_tree.setMinimumHeight(110)
        self.proj_tree.setMouseTracking(True)
        self.proj_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.btn_add_menu = tool_button("plus", "Создать папку, проект или рабочее окно", size=18)
        add_menu = QMenu(self.btn_add_menu)
        add_menu.addAction("Новая папка проектов", self.new_folder)
        add_menu.addAction("Новый проект", self.new_project)
        add_menu.addAction("Новое рабочее окно", self.new_workspace)
        self.btn_add_menu.setMenu(add_menu)
        self.btn_add_menu.setPopupMode(QToolButton.InstantPopup)
        self.btn_ren = tool_button("brush", "Переименовать выбранное (F2)", size=18)
        self.btn_del = tool_button("trash", "Удалить выбранное", size=18)
        for b in (self.btn_add_menu, self.btn_ren, self.btn_del):
            b.setFixedSize(26, 26)
        self.sec_projects = SectionBox("Проекты", self.proj_tree, [self.btn_add_menu, self.btn_ren, self.btn_del])

        # ---- участки: разделы по картинкам, отладочные сведения и замена куска
        pbody = QWidget()
        pl = QVBoxLayout(pbody)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        self.patch_tree = ClearableTree()
        self.patch_tree.setHeaderHidden(True)
        self.patch_tree.setIndentation(12)
        self.patch_tree.setIconSize(QSize(36, 36))
        self.patch_tree.setUniformRowHeights(False)
        self.patch_tree.setMinimumHeight(110)
        self.patch_tree.setToolTip("Каждое копирование рамки обрезки записывается сюда, по разделам-картинкам.\n"
                                   "Выберите участок, чтобы увидеть его на картинке и заменить кусок.\n"
                                   "Клик в пустое место снимает выделение.")
        pl.addWidget(self.patch_tree, 1)
        pbox = QVBoxLayout()
        pbox.setContentsMargins(12, 0, 12, 0)
        pbox.setSpacing(6)
        self.lbl_patch = QLabel("Вырежьте участок рамкой обрезки и скопируйте его (Ctrl+C): он появится здесь.")
        self.lbl_patch.setObjectName("Info")
        self.lbl_patch.setWordWrap(True)
        self.lbl_patch.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_patch.setStyleSheet("font-size: 12px;")
        self.patch_scroll = QScrollArea()
        self.patch_scroll.setWidgetResizable(True)
        self.patch_scroll.setFrameShape(QScrollArea.NoFrame)
        self.patch_scroll.setMaximumHeight(150)
        self.patch_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.patch_scroll.setWidget(self.lbl_patch)
        self.patch_scroll.setVisible(False)
        pbox.addWidget(self.patch_scroll)
        self.lbl_patch_hint = QLabel("Вырежьте участок рамкой обрезки и скопируйте его (Ctrl+C): "
                                     "он появится здесь.")
        self.lbl_patch_hint.setObjectName("Muted")
        self.lbl_patch_hint.setWordWrap(True)
        pbox.addWidget(self.lbl_patch_hint)
        self.btn_p_clip = QPushButton("Заменить куском из буфера")
        self.btn_p_clip.setObjectName("Primary")
        self.btn_p_clip.setToolTip("Вставляет картинку из буфера обмена на место участка (Ctrl+Shift+V).\n"
                                   "Кусок подгоняется по пикселям к размеру участка.")
        pbox.addWidget(self.btn_p_clip)
        self.chk_cutout = QCheckBox("Убирать белый фон у вставки")
        self.chk_cutout.setToolTip("Если края вставки белые, белый фон вокруг объекта станет прозрачным: сквозь него будет "
                                   "виден исходный участок картинки, а в PSD слой получит прозрачность. "
                                   "Белое внутри объекта остаётся. Если края не белые — вставка не меняется.")
        pbox.addWidget(self.chk_cutout)
        prow1 = QHBoxLayout()
        self.btn_p_copy = QPushButton("Копировать")
        self.btn_p_copy.setToolTip("Копирует текущее содержимое участка (его пиксели на картинке) в буфер обмена.\n"
                                   "Ctrl+C, когда выбран участок в списке.")
        self.btn_p_show = QPushButton("Показать")
        prow1.addWidget(self.btn_p_copy)
        prow1.addWidget(self.btn_p_show)
        pbox.addLayout(prow1)
        prow2 = QHBoxLayout()
        self.btn_p_eye = QPushButton()
        self.btn_p_eye.setCheckable(True)
        self.btn_p_eye.setIcon(theme.icon("eye", 20))
        self.btn_p_eye.setToolTip("Скрыть вставленный кусок (показать исходный участок) / показать его снова")
        self.btn_p_open = QPushButton()
        self.btn_p_open.setIcon(theme.icon("folder", 20))
        self.btn_p_open.setToolTip("Заменить участок куском из файла…")
        self.btn_p_hist = QPushButton()
        self.btn_p_hist.setIcon(theme.icon("history", 20))
        self.btn_p_hist.setToolTip("Выбрать одну из последних трёх вставленных итераций")
        for b in (self.btn_p_eye, self.btn_p_open, self.btn_p_hist):
            b.setIconSize(QSize(20, 20))
            prow2.addWidget(b)
        pbox.addLayout(prow2)
        self.btn_p_del = small_button("trash", "Удалить запись об участке")
        for b in (self.btn_p_clip, self.btn_p_copy, self.btn_p_eye, self.btn_p_open, self.btn_p_hist,
                  self.btn_p_show, self.btn_p_del):
            b.setFocusPolicy(Qt.NoFocus)
            b.setEnabled(False)
        pl.addLayout(pbox)
        self.btn_export = small_button("export", "Экспорт выбранной картинки в Photoshop (PSD) со слоями")
        self.sec_patches = SectionBox("Участки", pbody, [self.btn_export, self.btn_p_del])

        # ---- разделы можно сворачивать и переставлять
        self._sections = {"projects": self.sec_projects, "patches": self.sec_patches}
        self.sec_projects.set_open(bool(self.settings.get("projects_open")))
        self.sec_patches.set_open(bool(self.settings.get("patches_open")))
        for key, sec in self._sections.items():
            sec.move_requested.connect(lambda d, k=key: self._move_section(k, d))
            sec.toggled.connect(lambda on, k=key: self._on_section_toggled(k, on))
        self._apply_sections()
        return w

    def _section_order(self):
        order = [k for k in self.settings.get("sections_order") if k in self._sections]
        order += [k for k in self._sections if k not in order]
        return order

    def _apply_sections(self):
        order = self._section_order()
        lay = self.side_lay
        while lay.count():
            lay.takeAt(0)
        any_open = False
        for i, key in enumerate(order):
            sec = self._sections[key]
            lay.addWidget(sec, 1 if sec.is_open() else 0)
            sec.set_move_enabled(i > 0, i < len(order) - 1)
            any_open = any_open or sec.is_open()
        lay.addStretch(0 if any_open else 1)

    def _move_section(self, key, direction):
        order = self._section_order()
        i = order.index(key)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            self.settings.set("sections_order", order)
            self._apply_sections()

    def _on_section_toggled(self, key, on):
        self.settings.set(f"{key}_open", bool(on))
        self._apply_sections()

    def _build_inspector(self):
        w = QWidget()
        w.setAttribute(Qt.WA_StyledBackground, True)
        w.setObjectName("Inspector")
        w.setFixedWidth(300)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)

        # --- сетка
        lay.addWidget(section("Сетка"))
        self.chk_grid = QCheckBox("Показывать сетку  (G)")
        self.chk_snap = QCheckBox("Привязка к сетке")
        self.chk_snap.setToolTip("Край картинки прилипает к линии сетки, когда подходит к ней ближе ~8 px\n"
                                 "(при перемещении и растягивании). Alt — отключить на время перемещения,\n"
                                 "Ctrl — на время растягивания.")
        lay.addWidget(self.chk_grid)
        lay.addWidget(self.chk_snap)
        step = QLabel(f"Шаг сетки: {self.grid.size} px")
        step.setObjectName("Muted")
        lay.addWidget(step)

        lay.addSpacing(6)
        # --- кисть
        self.box_brush = QWidget()
        bl = QVBoxLayout(self.box_brush)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        bl.addWidget(section("Кисть / ластик"))
        self.sw_wrap = QWidget()
        sw = QHBoxLayout(self.sw_wrap)
        sw.setContentsMargins(0, 0, 0, 0)
        sw.setSpacing(0)
        self.sw_group = QButtonGroup(self)
        self.swatches = []
        for c in SWATCHES[:5]:
            self._add_swatch(c, sw)
        sw2 = QHBoxLayout()
        sw2.setContentsMargins(0, 0, 0, 0)
        sw2.setSpacing(0)
        for c in SWATCHES[5:]:
            self._add_swatch(c, sw2)
        self.btn_color = QPushButton("Цвет…")
        self.btn_color.setFocusPolicy(Qt.NoFocus)
        sw2.addSpacing(4)
        sw2.addWidget(self.btn_color)
        sw_all = QVBoxLayout()
        sw_all.setSpacing(2)
        sw_all.addLayout(sw)
        sw_all.addLayout(sw2)
        self.sw_wrap.setLayout(sw_all)
        bl.addWidget(self.sw_wrap)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Толщина"))
        self.sld_size = QSlider(Qt.Horizontal)
        self.sld_size.setRange(1, 64)
        srow.addWidget(self.sld_size, 1)
        self.lbl_size = QLabel("4")
        self.lbl_size.setFixedWidth(24)
        self.lbl_size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        srow.addWidget(self.lbl_size)
        bl.addLayout(srow)
        hint = QLabel("Ластик удаляет штрих целиком. Средняя кнопка мыши или Пробел — двигать холст.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        bl.addWidget(hint)
        lay.addWidget(self.box_brush)

        # --- обрезка
        self.box_crop = QWidget()
        cl = QVBoxLayout(self.box_crop)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        cl.addWidget(section("Обрезка"))
        self.seg_crop = Segmented([("free", "Свободная"), ("standard", "Стандарт"), ("gemini", "Gemini")],
                                  self.on_crop_mode)
        cl.addWidget(self.seg_crop)
        self.ratio_wrap = QWidget()
        rl = QVBoxLayout(self.ratio_wrap)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self.ratio_group = QButtonGroup(self)
        self.ratio_btns = {}
        ratios = self.canvas.ratios["standard"]
        for i in range(0, len(ratios), 5):
            rr = QHBoxLayout()
            rr.setSpacing(4)
            for r in ratios[i:i + 5]:
                b = QPushButton(r)
                b.setObjectName("Ratio")
                b.setCheckable(True)
                b.setFocusPolicy(Qt.NoFocus)
                self.ratio_group.addButton(b)
                self.ratio_btns[r] = b
                rr.addWidget(b)
                b.clicked.connect(lambda _=False, k=r: self.on_crop_ratio(k))
            rl.addLayout(rr)
        cl.addWidget(self.ratio_wrap)
        self.res_wrap = QWidget()
        rw = QHBoxLayout(self.res_wrap)
        rw.setContentsMargins(0, 0, 0, 0)
        rw.addWidget(QLabel("Разрешение"))
        self.seg_res = Segmented([("1K", "1K"), ("2K", "2K")], self.on_crop_res)
        rw.addWidget(self.seg_res)
        rw.addStretch(1)
        cl.addWidget(self.res_wrap)
        # размер рамки в пикселях оригинала; при фиксированном соотношении вторая сторона считается сама
        self.size_wrap = QWidget()
        sz = QHBoxLayout(self.size_wrap)
        sz.setContentsMargins(0, 0, 0, 0)
        sz.setSpacing(6)
        self.spin_cw = QSpinBox()
        self.spin_ch = QSpinBox()
        for sp in (self.spin_cw, self.spin_ch):
            sp.setRange(1, 100000)
            sp.setKeyboardTracking(False)
            sp.setSuffix(" px")
            sp.setSingleStep(8)
            sp.setToolTip("Размер рамки в пикселях оригинала. Enter — применить.\n"
                          "В режимах «Стандарт» и Gemini вторая сторона меняется в строгом соотношении.")
        sz.addWidget(QLabel("Ш"))
        sz.addWidget(self.spin_cw, 1)
        sz.addWidget(QLabel("В"))
        sz.addWidget(self.spin_ch, 1)
        cl.addWidget(self.size_wrap)
        self.lbl_crop = QLabel("Потяните рамку мышью. Клик вне рамки — новая рамка.")
        self.lbl_crop.setObjectName("Info")
        self.lbl_crop.setWordWrap(True)
        cl.addWidget(self.lbl_crop)
        self.btn_copy_crop = QPushButton("Копировать в буфер  (Ctrl+C)")
        self.btn_copy_crop.setObjectName("Primary")
        self.btn_copy_crop.setFocusPolicy(Qt.NoFocus)
        cl.addWidget(self.btn_copy_crop)
        hint2 = QLabel("Файл не меняется. Результат — в буфере обмена, а сам участок записывается "
                       "в список «Участки» слева.")
        hint2.setObjectName("Muted")
        hint2.setWordWrap(True)
        cl.addWidget(hint2)
        lay.addWidget(self.box_crop)

        # --- картинка
        self.box_image = QWidget()
        il = QVBoxLayout(self.box_image)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(8)
        il.addWidget(section("Картинка"))
        self.lbl_image = QLabel("")
        self.lbl_image.setObjectName("Info")
        self.lbl_image.setWordWrap(True)
        il.addWidget(self.lbl_image)
        szrow = QHBoxLayout()
        szrow.setSpacing(6)
        self.spin_w = QSpinBox()
        self.spin_h = QSpinBox()
        for sp, tip in ((self.spin_w, "Ширина на холсте, px"), (self.spin_h, "Высота на холсте, px")):
            sp.setRange(1, 100000)
            sp.setKeyboardTracking(False)
            sp.setSuffix(" px")
            sp.setToolTip(tip + "\nEnter — применить. Колесо мыши — шаг 10 px.")
            sp.setSingleStep(10)
        szrow.addWidget(QLabel("Ш"))
        szrow.addWidget(self.spin_w, 1)
        szrow.addWidget(QLabel("В"))
        szrow.addWidget(self.spin_h, 1)
        il.addLayout(szrow)
        self.chk_ratio = QCheckBox("Привязка в соотношении")
        self.chk_ratio.setToolTip("Включено: при изменении одной стороны (в полях и маркерами)\n"
                                  "вторая меняется пропорционально. Shift при растягивании — инвертирует.")
        il.addWidget(self.chk_ratio)
        r1 = QHBoxLayout()
        self.btn_100 = QPushButton("100%")
        self.btn_reset_ratio = QPushButton("Пропорции оригинала")
        self.btn_reset_ratio.setToolTip("Вернуть пропорции оригинала, сохранив ширину")
        for b in (self.btn_100, self.btn_reset_ratio):
            b.setFocusPolicy(Qt.NoFocus)
            r1.addWidget(b)
        il.addLayout(r1)
        r1b = QHBoxLayout()
        self.btn_front = QPushButton("Вперёд")
        self.btn_back = QPushButton("Назад")
        for b in (self.btn_front, self.btn_back):
            b.setFocusPolicy(Qt.NoFocus)
            r1b.addWidget(b)
        il.addLayout(r1b)
        r2 = QHBoxLayout()
        self.btn_img_crop = QPushButton("Обрезать")
        self.btn_img_copy = QPushButton("Копировать")
        for b in (self.btn_img_crop, self.btn_img_copy):
            b.setFocusPolicy(Qt.NoFocus)
            r2.addWidget(b)
        il.addLayout(r2)
        hint_t = QLabel("Тяните углы и рёбра рамки. Shift — переключить сохранение пропорций, "
                        "Alt — от центра, Ctrl — без привязки к сетке.")
        hint_t.setObjectName("Muted")
        hint_t.setWordWrap(True)
        il.addWidget(hint_t)
        lay.addWidget(self.box_image)

        self.lbl_empty = QLabel("Перетащите картинки в окно, нажмите Ctrl+V или кнопку добавления сверху.")
        self.lbl_empty.setObjectName("Muted")
        self.lbl_empty.setWordWrap(True)
        lay.addWidget(self.lbl_empty)
        lay.addStretch(1)
        return w

    def _add_swatch(self, color, layout):
        s = Swatch(color)
        self.sw_group.addButton(s)
        self.swatches.append(s)
        layout.addWidget(s)
        s.clicked.connect(lambda _=False, c=color: self.on_color(c))

    def _build_shortcuts(self):
        def sc(keys, fn):
            for k in keys if isinstance(keys, (list, tuple)) else [keys]:
                QShortcut(QKeySequence(k), self, activated=fn)

        sc("V", lambda: self.canvas.set_tool("select"))
        sc("B", lambda: self.canvas.set_tool("brush"))
        sc("E", lambda: self.canvas.set_tool("eraser"))
        sc("C", lambda: self.canvas.set_tool("crop"))
        sc("G", lambda: self.chk_grid.toggle())
        sc("F", self.canvas.fit_all)
        sc("1", lambda: self.canvas.set_selected_scale(1.0))
        sc("Ctrl+Z", self.undo.undo)
        sc(["Ctrl+Y", "Ctrl+Shift+Z"], self.undo.redo)
        sc("Ctrl+V", self.canvas.paste_clipboard)
        sc("Ctrl+Shift+V", self.replace_patch_from_clipboard)
        sc("Ctrl+C", self.copy_action)
        sc("Ctrl+O", self.add_images_dialog)
        sc("Delete", self.canvas.delete_selected)
        sc("]", self.canvas.bring_to_front)
        sc("[", self.canvas.send_to_back)
        sc("Esc", self.on_escape)
        sc("F2", self.rename_current)

    def _connect(self):
        c = self.canvas
        c.message.connect(lambda t: self.statusBar().showMessage(t, 5000))
        c.modified.connect(self._schedule_save)
        c.zoom_changed.connect(lambda z: self.lbl_zoom.setText(f"{z * 100:.0f}%"))
        c.tool_changed.connect(self._on_tool_changed)
        c.selection_info.connect(self._on_selection)
        c.crop_info.connect(lambda t: self.lbl_crop.setText(t or "Потяните рамку мышью. Клик вне рамки — новая рамка."))
        c.crop_state.connect(lambda on: None)
        c.crop_size.connect(self._on_crop_size)
        c.patches_changed.connect(self._fill_patches)
        c.patch_updated.connect(self._on_patch_updated)
        c.base_updated.connect(self._on_base_updated)
        c.patch_focus.connect(self._focus_patch)
        self.spin_cw.valueChanged.connect(lambda _: self._on_crop_size_edit("w"))
        self.spin_ch.valueChanged.connect(lambda _: self._on_crop_size_edit("h"))
        self.patch_tree.currentItemChanged.connect(self._on_patch_selected)
        self.patch_tree.itemDoubleClicked.connect(self._on_patch_double)
        self.patch_tree.itemExpanded.connect(lambda it: self._on_group_toggle(it, True))
        self.patch_tree.itemCollapsed.connect(lambda it: self._on_group_toggle(it, False))
        c.empty_clicked.connect(self._clear_patch_selection)
        self.btn_p_copy.clicked.connect(self.copy_patch)
        self.chk_cutout.toggled.connect(self.on_cutout_toggled)
        self.btn_export.clicked.connect(self.export_psd)
        self.patch_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.patch_tree.customContextMenuRequested.connect(self._patch_menu)
        self.btn_p_clip.clicked.connect(self.replace_patch_from_clipboard)
        self.btn_p_open.clicked.connect(self.replace_patch_from_file)
        self.btn_p_eye.clicked.connect(self.toggle_patch_eye)
        self.btn_p_hist.clicked.connect(self.show_iteration_menu)
        self.btn_p_show.clicked.connect(self.show_patch)
        self.btn_p_del.clicked.connect(self.delete_patch)
        self.undo.canUndoChanged.connect(self.btn_undo.setEnabled)
        self.undo.canRedoChanged.connect(self.btn_redo.setEnabled)
        self.undo.indexChanged.connect(lambda _: self._schedule_save())

        self.btn_undo.clicked.connect(self.undo.undo)
        self.btn_redo.clicked.connect(self.undo.redo)
        self.btn_add.clicked.connect(self.add_images_dialog)
        self.btn_fit.clicked.connect(c.fit_all)
        self.btn_pin.toggled.connect(self.set_pinned)
        self.btn_sidebar.toggled.connect(self.side.setVisible)
        self.btn_inspector.toggled.connect(self.inspector.setVisible)

        self.chk_grid.toggled.connect(self.on_grid_changed)
        self.chk_snap.toggled.connect(self.on_grid_changed)

        self.btn_color.clicked.connect(self.pick_color)
        self.sld_size.valueChanged.connect(self.on_size)
        self.btn_copy_crop.clicked.connect(c.copy_crop)
        self.btn_100.clicked.connect(lambda: c.set_selected_scale(1.0))
        self.btn_reset_ratio.clicked.connect(c.reset_proportions)
        self.spin_w.valueChanged.connect(lambda _: self._on_size_edit("w"))
        self.spin_h.valueChanged.connect(lambda _: self._on_size_edit("h"))
        self.chk_ratio.toggled.connect(self.on_keep_ratio)
        self.btn_front.clicked.connect(c.bring_to_front)
        self.btn_back.clicked.connect(c.send_to_back)
        self.btn_img_crop.clicked.connect(lambda: c.set_tool("crop"))
        self.btn_img_copy.clicked.connect(c.copy_selected_image)

        self.btn_ren.clicked.connect(self.rename_current)
        self.btn_del.clicked.connect(self.delete_current)
        self.proj_tree.itemClicked.connect(self._on_tree_click)
        self.proj_tree.itemActivated.connect(self._on_tree_click)
        self.proj_tree.itemExpanded.connect(lambda it: self._on_expand(it, True))
        self.proj_tree.itemCollapsed.connect(lambda it: self._on_expand(it, False))
        self.proj_tree.customContextMenuRequested.connect(self._tree_menu)

    def _restore_settings(self):
        s = self.settings
        self._loading_ui = True
        geo = s.get("geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromHex(geo.encode()))
        self.btn_sidebar.setChecked(bool(s.get("sidebar")))
        self.side.setVisible(bool(s.get("sidebar")))
        self.btn_inspector.setChecked(bool(s.get("inspector")))
        self.inspector.setVisible(bool(s.get("inspector")))
        col = s.get("brush_color")
        self.canvas.set_brush(col, int(s.get("brush_size")))
        self.sld_size.setValue(int(s.get("brush_size")))
        self.lbl_size.setText(str(self.sld_size.value()))
        self._mark_swatch(col)
        self.btn_pin.setChecked(bool(s.get("pinned")))
        self.grid.keep_ratio = bool(s.get("keep_ratio"))
        self.chk_cutout.setChecked(bool(s.get("remove_white")))
        self.canvas.remove_white = self.chk_cutout.isChecked()
        if not cutout.available():
            self.chk_cutout.setEnabled(False)
            self.canvas.remove_white = False
            self.chk_cutout.setToolTip("Нужен пакет numpy (pip install numpy): без него функция недоступна.")
        self.chk_ratio.setChecked(self.grid.keep_ratio)
        mode = s.get("crop_mode")
        ratio = s.get("crop_ratio")
        res = s.get("crop_res")
        if ratio not in self.ratio_btns:
            ratio = next(iter(self.ratio_btns))
        self.canvas.set_crop_config(mode, ratio, res)
        self.seg_crop.set(mode)
        self.seg_res.set(res)
        self.ratio_btns[ratio].setChecked(True)
        self._loading_ui = False
        self._update_inspector()
        self.btn_undo.setEnabled(False)
        self.btn_redo.setEnabled(False)

    # ================================================================= закрепление поверх окон
    def set_pinned(self, on):
        self._pinned = on
        self.settings.set("pinned", bool(on))
        self._apply_pin()

    def _apply_pin(self):
        if not self.isVisible():
            return
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            after = wintypes.HWND(-1 if self._pinned else -2)  # HWND_TOPMOST / HWND_NOTOPMOST
            user32.SetWindowPos(wintypes.HWND(int(self.winId())), after, 0, 0, 0, 0, PIN_SWP)
        else:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, self._pinned)
            self.show()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._apply_pin)

    # ================================================================= безрамочное окно
    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self.btn_win_max.setIcon(theme.icon("win_max", 20))
            self.btn_win_max.setToolTip("Развернуть")
        else:
            self.showMaximized()
            self.btn_win_max.setIcon(theme.icon("win_restore", 20))
            self.btn_win_max.setToolTip("Восстановить")

    def _edges_at(self, gp):
        g = self.geometry()
        e = Qt.Edges()
        if gp.x() - g.left() < EDGE:
            e |= Qt.LeftEdge
        elif g.right() - gp.x() < EDGE:
            e |= Qt.RightEdge
        if gp.y() - g.top() < EDGE:
            e |= Qt.TopEdge
        elif g.bottom() - gp.y() < EDGE:
            e |= Qt.BottomEdge
        return e

    def _set_edge_cursor(self, edges):
        if not edges:
            if self._edge_cursor:
                QApplication.restoreOverrideCursor()
                self._edge_cursor = False
            return
        horiz = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        vert = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        if horiz and vert:
            diag_main = (bool(edges & Qt.LeftEdge) == bool(edges & Qt.TopEdge))
            shape = Qt.SizeFDiagCursor if diag_main else Qt.SizeBDiagCursor
        else:
            shape = Qt.SizeHorCursor if horiz else Qt.SizeVerCursor
        if self._edge_cursor:
            QApplication.changeOverrideCursor(QCursor(shape))
        else:
            QApplication.setOverrideCursor(QCursor(shape))
            self._edge_cursor = True

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.MouseMove, QEvent.MouseButtonPress) and not self.isMaximized():
            if isinstance(obj, QWidget) and obj.window() is self:
                edges = self._edges_at(ev.globalPosition().toPoint())
                if t == QEvent.MouseButtonPress and edges and ev.button() == Qt.LeftButton:
                    self._set_edge_cursor(Qt.Edges())
                    h = self.windowHandle()
                    if h is not None:
                        h.startSystemResize(edges)
                    return True
                if t == QEvent.MouseMove and not (ev.buttons() & Qt.LeftButton):
                    self._set_edge_cursor(edges)
            elif self._edge_cursor:
                self._set_edge_cursor(Qt.Edges())
        elif t == QEvent.Leave and obj is self:
            self._set_edge_cursor(Qt.Edges())
        return False

    # ================================================================= инспектор
    def _on_tool_changed(self, tool):
        b = self.tool_btns.get(tool)
        if b and not b.isChecked():
            b.setChecked(True)
        self._update_inspector()

    def _on_selection(self, item):
        self._loading_ui = True
        if item is None:
            self.lbl_image.setText("")
            multi = bool(self.canvas.selected_images())
            self.lbl_image.setText(f"Выбрано картинок: {len(self.canvas.selected_images())}" if multi else "")
        else:
            dw, dh = item.display_size()
            self.lbl_image.setText(f"{item.name}\nОригинал: {item.w}×{item.h} px\n"
                                   f"Показано: {dw / item.w * 100:.0f}% × {dh / item.h * 100:.0f}%")
            self.spin_w.setValue(max(1, round(dw)))
            self.spin_h.setValue(max(1, round(dh)))
        for w in (self.spin_w, self.spin_h):
            w.setEnabled(item is not None)
        self._loading_ui = False
        self._update_inspector()

    def _on_size_edit(self, which):
        if self._loading_ui:
            return
        sel = self.canvas.selected_images()
        if len(sel) != 1:
            return
        it = sel[0]
        cw, ch = it.display_size()
        w, h = float(self.spin_w.value()), float(self.spin_h.value())
        if self.grid.keep_ratio:
            if which == "w":
                h = w * ch / cw
            else:
                w = h * cw / ch
        self.canvas.set_selected_size(w, h)

    def on_cutout_toggled(self, on):
        self.canvas.remove_white = bool(on)
        self.settings.set("remove_white", bool(on))

    def on_keep_ratio(self, on):
        self.grid.keep_ratio = bool(on)
        self.settings.set("keep_ratio", bool(on))

    def _update_inspector(self):
        tool = self.canvas.tool
        self.box_brush.setVisible(tool in ("brush", "eraser"))
        self.sw_wrap.setVisible(tool == "brush")
        self.box_crop.setVisible(tool == "crop")
        mode = self.canvas.crop_mode
        self.ratio_wrap.setVisible(mode in ("standard", "gemini"))
        self.res_wrap.setVisible(mode == "gemini")
        has_img = bool(self.canvas.selected_images()) and tool == "select"
        self.box_image.setVisible(has_img)
        self.lbl_empty.setVisible(tool == "select" and not has_img)

    def on_grid_changed(self, *_):
        if self._loading_ui:
            return
        self.grid.show = self.chk_grid.isChecked()
        self.grid.snap = self.chk_snap.isChecked()
        self.canvas.refresh_grid()
        self._schedule_save()

    def _sync_grid_ui(self):
        self._loading_ui = True
        self.chk_grid.setChecked(self.grid.show)
        self.chk_snap.setChecked(self.grid.snap)
        self._loading_ui = False

    def on_color(self, color):
        self.canvas.set_brush(color=color)
        self.settings.set("brush_color", color)

    def _mark_swatch(self, color):
        found = False
        for s in self.swatches:
            on = s.color.name().lower() == QColor(color).name().lower()
            s.setChecked(on)
            found = found or on
        if not found:
            self.sw_group.setExclusive(False)
            for s in self.swatches:
                s.setChecked(False)
            self.sw_group.setExclusive(True)

    def pick_color(self):
        col = QColorDialog.getColor(self.canvas.brush_color, self, "Цвет кисти")
        if col.isValid():
            self._mark_swatch(col.name())
            self.on_color(col.name())

    def on_size(self, v):
        self.lbl_size.setText(str(v))
        self.canvas.set_brush(size=v)
        self.settings.set("brush_size", v)

    def on_crop_mode(self, mode):
        self.settings.set("crop_mode", mode)
        self.canvas.set_crop_config(mode=mode)
        self._update_inspector()

    def on_crop_ratio(self, ratio):
        self.settings.set("crop_ratio", ratio)
        self.canvas.set_crop_config(ratio=ratio)

    def on_crop_res(self, res):
        self.settings.set("crop_res", res)
        self.canvas.set_crop_config(res=res)

    def _on_crop_size(self, w, h):
        self._loading_ui = True
        self.spin_cw.setValue(w)
        self.spin_ch.setValue(h)
        self._loading_ui = False

    def _on_crop_size_edit(self, which):
        if not self._loading_ui:
            self.canvas.set_crop_size(self.spin_cw.value(), self.spin_ch.value(), which)

    # ================================================================= участки
    def _current_pid(self):
        it = self.patch_tree.currentItem()
        if it is not None and it.data(0, ROLE_KIND) == "patch":
            return it.data(0, ROLE_ID)
        return None

    def _current_target(self, use_canvas=False):
        """Что выбрано в панели: ("patch", id участка) или ("image", uid основной картинки — выбран её раздел).
        use_canvas — если в списке ничего не выбрано, взять единственную картинку, выделенную на холсте."""
        it = self.patch_tree.currentItem()
        if it is not None:
            kind = it.data(0, ROLE_KIND)
            if kind == "patch":
                return "patch", it.data(0, ROLE_ID)
            if kind == "group":
                return "image", it.data(0, ROLE_ID)
        if use_canvas:
            sel = self.canvas.selected_images()
            if len(sel) == 1:
                return "image", sel[0].uid
        return None, None

    def _group_text(self, uid, count, name):
        item = self.canvas.find_item(uid)
        tag = ""
        if item is not None and item.base["iters"]:
            tag = "   ·   основа заменена" if self.canvas.base_active_iter(item) else "   ·   замена скрыта"
        return f"{name}{f'   ·   {count}' if count else ''}{tag}"

    def _patch_icon(self, p):
        """Миниатюра: текущие пиксели участка с холста, а если картинки нет — сохранённый оригинал."""
        it = self.canvas.patch_active_iter(p)
        owner = self.canvas.find_item(p["uid"])
        base_state = ((owner.base.get("active"), owner.base.get("visible", True), owner.base.get("iter_seq", 0))
                      if owner is not None else None)
        key = (self.project.id if self.project else "", self.ws_id, p["id"],
               (it or {}).get("ts", 0), p.get("visible", True), base_state, p["x"], p["y"], p["w"], p["h"])
        if key in self._patch_icons:
            return self._patch_icons[key]
        img = None
        item = self.canvas.find_item(p["uid"])
        if item is not None:
            img = item.image.copy(QRect(p["x"], p["y"], p["w"], p["h"]))
        elif p.get("orig") and self.project is not None:
            img = load_image(self.project.asset_path(p["orig"]))
        icon = QIcon()
        if img is not None and not img.isNull():
            icon = QIcon(QPixmap.fromImage(img.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
        self._patch_icons[key] = icon
        return icon

    def _patch_title(self, p, image_name):
        """Название участка: префикс «У<номер>», затем название картинки; второй строкой — размер и соотношение."""
        ratio = p["ratio"] or f"{p['w'] / p['h']:.2f}:1"
        if self.canvas.patch_replaced(p):
            state = " · заменён"
        elif self.canvas.patch_active_iter(p) is not None:
            state = " · вставка скрыта"
        else:
            state = ""
        return f"У{p['id']} · {image_name}\n{p['w']}×{p['h']} · {ratio}{state}"

    def _on_patch_updated(self, pid):
        """У одного участка сменилось состояние: обновляем только его строку, а не пересобираем весь список."""
        p = self.canvas.get_patch(pid)
        if p is None:
            return
        img = self.canvas.find_item(p["uid"])
        name = img.name if img else f"{p.get('name') or 'картинка'} (удалена)"
        tree = self.patch_tree
        for i in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(i)
            for j in range(g.childCount()):
                ch = g.child(j)
                if ch.data(0, ROLE_ID) == pid:
                    ch.setText(0, self._patch_title(p, name))
                    ch.setIcon(0, self._patch_icon(p))
                    if pid == self._current_pid():
                        self._refresh_patch_panel()
                    return
        self._fill_patches()

    def _fill_patches(self):
        """Список «Участки»: раздел для каждой картинки окна (даже без участков — чтобы можно было заменить основу),
        под разделом — участки, созданные на этой картинке."""
        cur = self._current_pid()
        cur_target = self._current_target()
        tree = self.patch_tree
        tree.blockSignals(True)
        tree.clear()
        groups, order = {}, []
        for p in self.canvas.patches:
            if p["uid"] not in groups:
                groups[p["uid"]] = []
                order.append(p["uid"])
            groups[p["uid"]].append(p)
        for img in sorted(self.canvas.image_items(), key=lambda i: i.zValue()):
            if img.uid not in groups:
                groups[img.uid] = []
                order.append(img.uid)
        cur_item = None
        bold = QFont()
        bold.setBold(True)
        for uid in order:
            plist = groups[uid]
            img = self.canvas.find_item(uid)
            name = img.name if img else f"{plist[0].get('name') or 'картинка'} (удалена)"
            g = QTreeWidgetItem([self._group_text(uid, len(plist), name)])
            g.setFont(0, bold)
            g.setData(0, ROLE_KIND, "group")
            g.setData(0, ROLE_ID, uid)
            tree.addTopLevelItem(g)
            if cur_target == ("image", uid):
                cur_item = g
            for p in reversed(plist):   # новые сверху внутри раздела
                it = QTreeWidgetItem([self._patch_title(p, name)])
                it.setIcon(0, self._patch_icon(p))
                it.setData(0, ROLE_KIND, "patch")
                it.setData(0, ROLE_ID, p["id"])
                g.addChild(it)
                if p["id"] == cur:
                    cur_item = it
            g.setExpanded(self._group_open.get(uid, True))
        if cur_item is not None:
            tree.setCurrentItem(cur_item)
        tree.blockSignals(False)
        n = len(self.canvas.patches)
        self.sec_patches.set_count(str(n) if n else "")
        self._refresh_patch_panel()

    def _on_base_updated(self, uid):
        """У основной картинки сменилось состояние: обновляем её раздел и значки участков под ней."""
        tree = self.patch_tree
        for i in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(i)
            if g.data(0, ROLE_KIND) == "group" and g.data(0, ROLE_ID) == uid:
                img = self.canvas.find_item(uid)
                g.setText(0, self._group_text(uid, g.childCount(), img.name if img else "картинка"))
                for j in range(g.childCount()):
                    ch = g.child(j)
                    p = self.canvas.get_patch(ch.data(0, ROLE_ID))
                    if p is not None:
                        ch.setText(0, self._patch_title(p, img.name if img else "картинка"))
                        ch.setIcon(0, self._patch_icon(p))
                break
        self._refresh_patch_panel()

    def _refresh_patch_panel(self):
        kind, ident = self._current_target()
        c = self.canvas
        p = c.get_patch(ident) if kind == "patch" else None
        item = c.find_item(p["uid"]) if p is not None else (c.find_item(ident) if kind == "image" else None)
        text = None
        if p is not None:
            text = c.patch_details(ident)
        elif kind == "image" and item is not None:
            text = c.image_details(ident)
        if text is not None:
            self.lbl_patch.setText(text)
        self.patch_scroll.setVisible(text is not None)            # сведения — только у выбранного
        self.lbl_patch_hint.setVisible(not c.patches)             # подсказка — пока участков нет
        has_img = item is not None
        if p is not None:
            has_iter = has_img and c.patch_active_iter(p) is not None
            visible, can_hide = p.get("visible", True), bool(p.get("orig"))
        elif kind == "image" and item is not None:
            has_iter = bool(item.base["iters"]) and item.base["active"] is not None
            visible, can_hide = item.base.get("visible", True), True
        else:
            has_iter, visible, can_hide = False, True, False
        self.btn_p_clip.setEnabled(has_img)
        self.btn_p_open.setEnabled(has_img)
        self.btn_p_show.setEnabled(has_img)
        self.btn_p_copy.setEnabled(has_img)
        self.btn_p_hist.setEnabled(has_iter)
        # глазик: нажат (глаз открыт) — вставка/замена показана; закрыт — виден оригинал
        shown = bool(has_iter and visible)
        self.btn_p_eye.setEnabled(has_iter and can_hide)
        self.btn_p_eye.setChecked(shown)
        self.btn_p_eye.setIcon(theme.icon("eye" if shown or not has_iter else "eye_off", 20))
        self.btn_p_del.setEnabled(p is not None)
        self.chk_cutout.setEnabled(cutout.available() and kind != "image")   # вырезание фона — только для участков

    def _on_patch_selected(self, cur, prev):
        kind = cur.data(0, ROLE_KIND) if cur is not None else None
        if kind == "patch":
            self.canvas.show_patch(cur.data(0, ROLE_ID))
        else:
            self.canvas.clear_marker()
            if kind == "group" and self.canvas.tool != "crop":   # клик по разделу выделяет его картинку
                img = self.canvas.find_item(cur.data(0, ROLE_ID))
                if img is not None:
                    self.canvas.scene().clearSelection()
                    img.setSelected(True)
        self._refresh_patch_panel()

    def _on_patch_double(self, item, col=0):
        if item.data(0, ROLE_KIND) == "patch":
            self.show_patch()

    def _on_group_toggle(self, item, expanded):
        if item.data(0, ROLE_KIND) == "group":
            self._group_open[item.data(0, ROLE_ID)] = expanded

    def _clear_patch_selection(self):
        """Клик в пустое место холста или списка снимает выделение участка."""
        self.patch_tree.clearSelection()
        self.patch_tree.setCurrentItem(None)

    def _focus_patch(self, pid):
        tree = self.patch_tree
        for i in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(i)
            for j in range(g.childCount()):
                if g.child(j).data(0, ROLE_ID) == pid:
                    g.setExpanded(True)
                    tree.setCurrentItem(g.child(j))
                    return

    def copy_patch(self):
        """Копирует в буфер то, что сейчас видно: участок или всю картинку (с учётом замен)."""
        kind, ident = self._current_target()
        if kind is None:
            self.statusBar().showMessage("Сначала выберите участок или картинку в списке слева", 4000)
        elif kind == "patch":
            self.canvas.copy_patch(ident)
        else:
            self.canvas.copy_image(ident)

    def copy_action(self):
        """Ctrl+C: если фокус в списке участков — копируем выбранное, иначе стандартное копирование холста."""
        if self.patch_tree.hasFocus() and self._current_target()[0] is not None:
            self.copy_patch()
        else:
            self.canvas.copy_current()

    def _patch_menu(self, pos):
        item = self.patch_tree.itemAt(pos)
        kind = item.data(0, ROLE_KIND) if item is not None else None
        if kind not in ("patch", "group"):
            return
        self.patch_tree.setCurrentItem(item)
        target = "участок" if kind == "patch" else "картинку"
        m = QMenu(self)
        m.addAction(f"Копировать {target}", self.copy_patch).setEnabled(self.btn_p_copy.isEnabled())
        m.addAction("Показать на холсте", self.show_patch).setEnabled(self.btn_p_show.isEnabled())
        m.addSeparator()
        what = "вставку" if kind == "patch" else "замену"
        m.addAction("Заменить куском из буфера" if kind == "patch" else "Заменить картинку из буфера",
                    self.replace_patch_from_clipboard).setEnabled(self.btn_p_clip.isEnabled())
        m.addAction("Заменить куском из файла…" if kind == "patch" else "Заменить картинку из файла…",
                    self.replace_patch_from_file).setEnabled(self.btn_p_open.isEnabled())
        m.addAction(f"Скрыть {what}" if self.btn_p_eye.isChecked() else f"Показать {what}",
                    self.toggle_patch_eye).setEnabled(self.btn_p_eye.isEnabled())
        m.addAction("Выбрать итерацию…", lambda: self.show_iteration_menu(global_pos=QCursor.pos())) \
            .setEnabled(self.btn_p_hist.isEnabled())
        m.addSeparator()
        m.addAction("Экспорт в Photoshop (PSD)…", self.export_psd)
        if kind == "patch":
            m.addAction("Удалить запись", self.delete_patch)
        m.exec(self.patch_tree.mapToGlobal(pos))

    def _export_target_uid(self):
        """Какая картинка экспортируется: выбранный в панели раздел-картинка или участок; иначе — выделенная на холсте."""
        it = self.patch_tree.currentItem()
        if it is not None:
            if it.data(0, ROLE_KIND) == "group":
                return it.data(0, ROLE_ID)
            p = self.canvas.get_patch(it.data(0, ROLE_ID))
            if p is not None:
                return p["uid"]
        sel = self.canvas.selected_images()
        return sel[0].uid if len(sel) == 1 else None

    def export_psd(self, _checked=False, path=None):
        """Экспорт выбранной картинки в слоёный PSD: картинка и её актуальные вставки (без истории и без штрихов)."""
        uid = self._export_target_uid()
        item = self.canvas.find_item(uid) if uid else None
        if item is None:
            self.statusBar().showMessage("Выберите картинку (раздел) в списке «Участки» слева", 5000)
            return
        if path is None:
            folder = self.settings.get("export_dir") or QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
            path, _ = QFileDialog.getSaveFileName(self, "Экспорт в Photoshop",
                                                  str(Path(folder) / f"{Path(item.name).stem}.psd"),
                                                  "Photoshop (*.psd)")
            if not path:
                return
        if not path.lower().endswith(".psd"):
            path += ".psd"
        dlg = QProgressDialog("Экспорт в PSD…", None, 0, 100, self)
        dlg.setWindowTitle("Экспорт в Photoshop")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        def tick(done, total):
            dlg.setMaximum(total)
            dlg.setValue(done)
            QApplication.processEvents()

        try:
            n = self.canvas.export_psd(uid, path, tick)
        except (ValueError, OSError) as ex:
            dlg.close()
            QMessageBox.warning(self, "Экспорт в Photoshop", f"Не удалось сохранить PSD: {ex}")
            return
        dlg.close()
        self.settings.set("export_dir", str(Path(path).parent))
        self.statusBar().showMessage(f"PSD сохранён: {path} · слоёв: {n + 1} (картинка + {n} вставок)", 8000)

    def show_patch(self):
        kind, ident = self._current_target()
        if kind == "patch":
            if not self.canvas.show_patch(ident, center=True):
                self.statusBar().showMessage("Картинки этого участка нет на холсте", 4000)
        elif kind == "image":
            self.canvas.show_image(ident)

    def delete_patch(self):
        pid = self._current_pid()
        if pid is None:
            return
        r = QMessageBox.question(self, "Удалить запись об участке",
                                 f"Удалить запись об участке #{pid}?\n"
                                 "Картинка не изменится, но сохранённые вставки и возможность вернуть исходный "
                                 "участок для него пропадут.")
        if r == QMessageBox.Yes:
            self.canvas.delete_patch(pid)

    def toggle_patch_eye(self, _checked=False):
        """Глазик: скрыть вставку/замену (виден оригинал) или показать её."""
        kind, ident = self._current_target()
        if kind == "patch":
            self.canvas.toggle_patch_visible(ident)
        elif kind == "image":
            self.canvas.toggle_base_visible(ident)
        self._refresh_patch_panel()   # вернёт кнопку в верное состояние, если действие не удалось

    def _iter_icon(self, it):
        key = (self.project.id if self.project else "", it.get("file"))
        if key not in self._patch_icons:
            img = load_image(self.project.asset_path(it["file"])) if self.project and it.get("file") else None
            self._patch_icons[key] = QIcon(QPixmap.fromImage(
                img.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))) if img is not None else QIcon()
        return self._patch_icons[key]

    def show_iteration_menu(self, _checked=False, global_pos=None):
        """Выбор из последних вставок/замен (до трёх): выбранная становится активной и показывается."""
        kind, ident = self._current_target()
        c = self.canvas
        if kind == "patch":
            p = c.get_patch(ident)
            meta = p if p is not None else None
            orig_ok = bool(p.get("orig")) if p else False
            pick = lambda i: c.set_patch_iteration(ident, i)
            hide = lambda: c.toggle_patch_visible(ident)
            icons = True
            hide_text = "Исходный участок (скрыть вставку)"
        elif kind == "image":
            item = c.find_item(ident)
            meta = item.base if item is not None else None
            orig_ok = item is not None
            pick = lambda i: c.set_base_iteration(ident, i)
            hide = lambda: c.toggle_base_visible(ident)
            icons = False   # полные картинки не декодируем ради значков меню
            hide_text = "Оригинальная картинка (скрыть замену)"
        else:
            return
        if meta is None or not meta.get("iters"):
            return
        m = QMenu(self)
        m.addAction("Последние вставки" if kind == "patch" else "Последние замены").setEnabled(False)
        shown = meta.get("visible", True)
        for idx in reversed(range(len(meta["iters"]))):
            it = meta["iters"][idx]
            when = time.strftime("%H:%M", time.localtime(it["ts"]))
            label = f"Итерация {it['n']}  ·  {it['src_w']}×{it['src_h']}  ·  {when}"
            act = m.addAction(self._iter_icon(it), label) if icons else m.addAction(label)
            act.setCheckable(True)
            act.setChecked(idx == meta["active"] and shown)
            act.triggered.connect(lambda _=False, i=idx: pick(i))
        m.addSeparator()
        orig = m.addAction(hide_text)
        orig.setCheckable(True)
        orig.setChecked(not shown)
        orig.setEnabled(shown and orig_ok)
        orig.triggered.connect(lambda: hide())
        pos = global_pos or self.btn_p_hist.mapToGlobal(QPoint(0, self.btn_p_hist.height()))
        m.exec(pos)

    def replace_patch_from_clipboard(self):
        kind, ident = self._current_target(use_canvas=True)
        if kind is None:
            self.statusBar().showMessage("Сначала выберите участок или картинку в списке слева", 4000)
            return
        img = self.canvas.clipboard_image()
        if img is None or img.isNull():
            self.statusBar().showMessage("В буфере обмена нет картинки", 4000)
            return
        self._replace_with(kind, ident, img)

    def replace_patch_from_file(self):
        kind, ident = self._current_target(use_canvas=True)
        if kind is None:
            return
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXT))
        path, _ = QFileDialog.getOpenFileName(self, "Картинка для замены", "", f"Изображения ({exts})")
        if not path:
            return
        img = load_image(path)
        self.canvas.source_note = "из файла"
        if img is None:
            self.statusBar().showMessage("Не удалось открыть изображение", 4000)
            return
        self._replace_with(kind, ident, img)

    def _replace_with(self, kind, ident, img):
        """Замена участка или основной картинки: если соотношение сторон не совпадает — спрашиваем, как подогнать."""
        c = self.canvas
        if kind == "patch":
            p = c.get_patch(ident)
            if p is None:
                return
            tw, th, diff, what = p["w"], p["h"], c.patch_ratio_diff(ident, img), "Участок"
        else:
            item = c.find_item(ident)
            if item is None:
                return
            tw, th, diff, what = item.w, item.h, c.base_ratio_diff(ident, img), "Картинка"
        fit = "crop"
        if diff > 0.02:
            box = QMessageBox(self)
            box.setWindowTitle("Другое соотношение сторон")
            box.setText(f"{what} {tw}×{th} ({tw / th:.3f}), "
                        f"вставляемый кусок {img.width()}×{img.height()} ({img.width() / img.height():.3f}).\n\n"
                        "Соотношения не совпадают. Как подогнать кусок по пикселям?")
            b_crop = box.addButton("Обрезать по центру", QMessageBox.AcceptRole)
            b_str = box.addButton("Растянуть", QMessageBox.ActionRole)
            box.addButton("Отмена", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is b_crop:
                fit = "crop"
            elif box.clickedButton() is b_str:
                fit = "stretch"
            else:
                return
        if kind == "patch":
            c.replace_patch(ident, img, fit)
        else:
            c.replace_base(ident, img, fit)

    def on_escape(self):
        if self.canvas.crop:
            self.canvas.set_tool("select")
        else:
            self.canvas.scene().clearSelection()

    # ================================================================= папки → проекты → рабочие окна
    def _info(self, item):
        if item is None:
            return None, None, None
        return item.data(0, ROLE_KIND), item.data(0, ROLE_ID), item.data(0, ROLE_PARENT)

    def _get_project(self, pid):
        if self.project is not None and self.project.id == pid:
            return self.project   # один и тот же объект, чтобы метаданные не расходились
        return self.store.get(pid)

    def _selected_project(self):
        kind, id_, parent = self._info(self.proj_tree.currentItem())
        pid = id_ if kind == "project" else parent if kind == "ws" else None
        return self._get_project(pid) if pid else self.project

    def _selected_folder_id(self):
        kind, id_, _ = self._info(self.proj_tree.currentItem())
        if kind == "folder":
            return id_
        if kind in ("project", "ws"):
            p = self._selected_project()
            return p.folder if p else None
        return None

    def _fill_projects(self):
        tree = self.proj_tree
        opened = self.settings.d.setdefault("tree_open", {})
        self._filling = True
        tree.blockSignals(True)
        tree.clear()
        folders = self.folders.folders
        folder_ids = {f["id"] for f in folders}
        projects = self.store.all()

        def add_project(parent, p):
            it = QTreeWidgetItem([p.name])
            it.setIcon(0, theme.icon("grid", 18))
            it.setData(0, ROLE_KIND, "project")
            it.setData(0, ROLE_ID, p.id)
            if parent is None:
                tree.addTopLevelItem(it)
            else:
                parent.addChild(it)
            for w in p.workspaces:
                c = QTreeWidgetItem([w["name"]])
                c.setIcon(0, theme.icon("window", 18))
                c.setData(0, ROLE_KIND, "ws")
                c.setData(0, ROLE_ID, w["id"])
                c.setData(0, ROLE_PARENT, p.id)
                it.addChild(c)
            it.setExpanded(bool(opened.get("p:" + p.id, True)))

        for f in folders:
            fit = QTreeWidgetItem([f["name"]])
            fit.setIcon(0, theme.icon("folder", 18))
            fit.setData(0, ROLE_KIND, "folder")
            fit.setData(0, ROLE_ID, f["id"])
            tree.addTopLevelItem(fit)
            for p in projects:
                if p.folder == f["id"]:
                    add_project(fit, p)
            fit.setExpanded(bool(opened.get("f:" + f["id"], True)))
        for p in projects:
            if p.folder not in folder_ids:
                add_project(None, p)
        tree.blockSignals(False)
        self._filling = False
        self._mark_current()

    def _iter_items(self):
        stack = [self.proj_tree.topLevelItem(i) for i in range(self.proj_tree.topLevelItemCount())]
        while stack:
            it = stack.pop(0)
            yield it
            stack = [it.child(i) for i in range(it.childCount())] + stack

    def _mark_current(self):
        """Выделяет жирным открытый проект и рабочее окно и ставит на окно указатель дерева."""
        normal, bold = QFont(), QFont()
        bold.setBold(True)
        self.proj_tree.blockSignals(True)
        for it in self._iter_items():
            kind, id_, parent = self._info(it)
            cur = False
            if self.project is not None:
                cur = (kind == "project" and id_ == self.project.id) or \
                      (kind == "ws" and parent == self.project.id and id_ == self.ws_id)
            it.setFont(0, bold if cur else normal)
            if kind == "ws" and cur:
                # setCurrentItem раскрывает всех «родителей»: для свёрнутых веток указатель не ставим
                p, hidden = it.parent(), False
                while p is not None:
                    hidden = hidden or not p.isExpanded()
                    p = p.parent()
                if not hidden:
                    self.proj_tree.setCurrentItem(it)
        self.proj_tree.blockSignals(False)

    def _on_expand(self, item, expanded):
        if self._filling:
            return
        kind, id_, _ = self._info(item)
        if kind in ("folder", "project"):
            self.settings.d.setdefault("tree_open", {})[("f:" if kind == "folder" else "p:") + id_] = expanded

    def _on_tree_click(self, item, col=0):
        kind, id_, parent = self._info(item)
        if kind == "ws":
            proj = self._get_project(parent)
            if proj is not None:
                self.open_workspace(proj, id_)
        elif kind == "project":
            proj = self._get_project(id_)
            if proj is not None:
                self.open_workspace(proj, proj.active_ws)

    def open_workspace(self, proj, ws_id):
        if proj is None or ws_id not in {w["id"] for w in proj.workspaces}:
            return
        if self.project is not None and self.project.id == proj.id and self.ws_id == ws_id:
            return
        self.leave_workspace()
        self.project = proj
        self.ws_id = ws_id
        proj.set_active_ws(ws_id)
        self.canvas.project = proj
        self.undo.clear()
        self._patch_icons.clear()
        self.canvas.load_dict(proj.load_ws(ws_id))
        self._sync_grid_ui()
        self._set_titles()
        self.settings.set("last_project", proj.id)
        self.settings.set("last_ws", ws_id)
        self._mark_current()
        self.btn_undo.setEnabled(False)
        self.btn_redo.setEnabled(False)
        self.canvas.setFocus()

    def _set_titles(self):
        if self.project is None or self.ws_id is None:
            self.lbl_project.setText("")
            self.setWindowTitle("Previewer")
            return
        text = f"{self.project.name}  ›  {self.project.ws_name(self.ws_id)}"
        self.lbl_project.setText(text)
        self.lbl_project.setToolTip(text)
        self.setWindowTitle(f"{text} — Previewer")

    def leave_workspace(self):
        """Сохраняет открытое окно и чистит неиспользуемые файлы проекта (история отмены ещё не сброшена).
        Файлы других рабочих окон проекта и удалённых окон чистка не трогает."""
        if self.project is None or self.ws_id is None:
            return
        self._save_timer.stop()
        data = self.canvas.to_dict()
        self.project.save_ws(self.ws_id, data)
        self.undo.clear()
        used = self.canvas.referenced_files() | self.project.referenced_elsewhere(exclude_ws=self.ws_id)
        self.project.prune_assets(used)

    def _schedule_save(self):
        if self.project is not None and self.ws_id is not None:
            self._save_timer.start()

    def save_now(self):
        if self.project is None or self.ws_id is None:
            return
        self.project.save_ws(self.ws_id, self.canvas.to_dict())

    # ---- создание
    def new_folder(self):
        name, ok = QInputDialog.getText(self, "Новая папка проектов", "Название папки:",
                                        text=f"Папка {len(self.folders.folders) + 1}")
        name = name.strip()
        if ok and name:
            self.folders.create(name)
            self._fill_projects()

    def new_project(self):
        name, ok = QInputDialog.getText(self, "Новый проект", "Название проекта:")
        name = name.strip()
        if ok and name:
            proj = self.store.create(name, self._selected_folder_id())
            self._fill_projects()
            self.open_workspace(proj, proj.active_ws)

    def new_workspace(self):
        proj = self._selected_project()
        if proj is None:
            return
        name, ok = QInputDialog.getText(self, "Новое рабочее окно", f"Название окна в проекте «{proj.name}»:",
                                        text=f"Рабочее окно {len(proj.workspaces) + 1}")
        name = name.strip()
        if ok and name:
            self.settings.d.setdefault("tree_open", {})["p:" + proj.id] = True
            ws_id = proj.add_ws(name)
            self._fill_projects()
            self.open_workspace(proj, ws_id)

    # ---- переименование и удаление
    def rename_current(self):
        kind, id_, parent = self._info(self.proj_tree.currentItem())
        if kind is None:
            return
        if kind == "folder":
            f = self.folders.get(id_)
            title, old = "Переименовать папку", f["name"]
        elif kind == "project":
            old = self._get_project(id_).name
            title = "Переименовать проект"
        else:
            old = self._get_project(parent).ws_name(id_)
            title = "Переименовать рабочее окно"
        name, ok = QInputDialog.getText(self, title, "Новое название:", text=old)
        name = name.strip()
        if not (ok and name) or name == old:
            return
        if kind == "folder":
            self.folders.rename(id_, name)
        elif kind == "project":
            self._get_project(id_).rename(name)
        else:
            self._get_project(parent).rename_ws(id_, name)
        self._fill_projects()
        self._set_titles()

    def delete_current(self):
        kind, id_, parent = self._info(self.proj_tree.currentItem())
        if kind == "folder":
            self._delete_folder(id_)
        elif kind == "project":
            self._delete_project(id_)
        elif kind == "ws":
            self._delete_workspace(parent, id_)

    def _delete_folder(self, fid):
        f = self.folders.get(fid)
        if f is None:
            return
        r = QMessageBox.question(self, "Удалить папку",
                                 f"Удалить папку «{f['name']}»?\nПроекты из неё не удаляются: "
                                 "они переедут в корень списка.")
        if r != QMessageBox.Yes:
            return
        for p in self.store.all():
            if p.folder == fid:
                self._get_project(p.id).set_folder(None)
        self.folders.delete(fid)
        self._fill_projects()

    def _delete_project(self, pid):
        proj = self._get_project(pid)
        if proj is None:
            return
        r = QMessageBox.question(self, "Удалить проект",
                                 f"Удалить проект «{proj.name}» со всеми рабочими окнами?\n"
                                 "Папка будет перенесена в data/trash — её можно вернуть вручную.")
        if r != QMessageBox.Yes:
            return
        if self.project is not None and self.project.id == pid:
            self._save_timer.stop()
            self.undo.clear()
            self.project = None
            self.ws_id = None
            self.canvas.project = None
            self.canvas.clear_all()
        self.store.delete(proj)
        rest = self.store.all()
        nxt = rest[0] if rest else self.store.create("Проект 1")
        self._fill_projects()
        if self.project is None:
            self.open_workspace(nxt, nxt.active_ws)

    def _delete_workspace(self, pid, ws_id):
        proj = self._get_project(pid)
        if proj is None:
            return
        if len(proj.workspaces) <= 1:
            QMessageBox.information(self, "Удалить рабочее окно", "В проекте должно остаться хотя бы одно рабочее окно.")
            return
        r = QMessageBox.question(self, "Удалить рабочее окно",
                                 f"Удалить рабочее окно «{proj.ws_name(ws_id)}»?\n"
                                 "Его файл будет перенесён в папку trash проекта, картинки не стираются.")
        if r != QMessageBox.Yes:
            return
        was_open = self.project is not None and self.project.id == pid and self.ws_id == ws_id
        if was_open:
            self._save_timer.stop()
            self.undo.clear()
            self.ws_id = None
            self.canvas.clear_all()
        proj.delete_ws(ws_id)
        self._fill_projects()
        if was_open:
            self.open_workspace(proj, proj.active_ws)

    def move_project(self, pid, folder_id):
        proj = self._get_project(pid)
        if proj is not None:
            proj.set_folder(folder_id)
            self._fill_projects()

    def _tree_menu(self, pos):
        item = self.proj_tree.itemAt(pos)
        if item is not None:
            self.proj_tree.setCurrentItem(item)
        kind, id_, parent = self._info(item)
        m = QMenu(self)
        if kind == "folder":
            m.addAction("Новый проект в папке", self.new_project)
            m.addAction("Переименовать папку", self.rename_current)
            m.addSeparator()
            m.addAction("Удалить папку", self.delete_current)
        elif kind == "project":
            m.addAction("Новое рабочее окно", self.new_workspace)
            m.addAction("Переименовать проект", self.rename_current)
            sub = m.addMenu("Переместить в папку")
            sub.addAction("Без папки", lambda: self.move_project(id_, None))
            for f in self.folders.folders:
                sub.addAction(f["name"], lambda fid=f["id"]: self.move_project(id_, fid))
            m.addSeparator()
            m.addAction("Удалить проект", self.delete_current)
        elif kind == "ws":
            m.addAction("Переименовать окно", self.rename_current)
            m.addSeparator()
            m.addAction("Удалить окно", self.delete_current)
        else:
            m.addAction("Новая папка проектов", self.new_folder)
            m.addAction("Новый проект", self.new_project)
        m.exec(self.proj_tree.mapToGlobal(pos))

    # ================================================================= файлы
    def add_images_dialog(self):
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Добавить картинки", "", f"Изображения ({exts});;Все файлы (*)")
        if files:
            self.canvas.add_image_files(files)

    def closeEvent(self, e):
        self.leave_workspace()
        self.settings.set("geometry", bytes(self.saveGeometry().toHex()).decode())
        self.settings.save()
        super().closeEvent(e)

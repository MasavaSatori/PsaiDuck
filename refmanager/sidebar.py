"""Элементы боковой панели: сворачиваемый/перемещаемый раздел и дерево, сбрасывающее выделение по клику в пустоту."""
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QTreeWidget, QVBoxLayout, QWidget

from . import theme

ROLE_KIND = Qt.UserRole        # "folder" | "project" | "ws" | "group" | "patch"
ROLE_ID = Qt.UserRole + 1      # id папки / проекта / окна / uid картинки / id участка
ROLE_PARENT = Qt.UserRole + 2  # для окна — id проекта


def small_button(icon_name, tip, size=16, box=26):
    b = QToolButton()
    b.setObjectName("Chev")
    b.setIcon(theme.icon(icon_name, size))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.setFixedSize(box, box)
    b.setFocusPolicy(Qt.NoFocus)
    return b


class SectionBox(QWidget):
    """Раздел панели: заголовок со стрелкой сворачивания, кнопками ▲▼ (переместить раздел) и телом."""
    move_requested = Signal(str)   # "up" | "down"
    toggled = Signal(bool)

    def __init__(self, title, body, extra_widgets=()):
        super().__init__()
        self.body = body
        self._open = True
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        head = QHBoxLayout()
        head.setContentsMargins(8, 0, 8, 0)
        head.setSpacing(0)
        self.btn_title = QToolButton()
        self.btn_title.setObjectName("SecTitle")
        self.btn_title.setText(title.upper())
        self.btn_title.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_title.setIconSize(QSize(16, 16))
        self.btn_title.setAutoRaise(True)
        self.btn_title.setFocusPolicy(Qt.NoFocus)
        self.btn_title.setToolTip("Свернуть / развернуть раздел")
        self.btn_title.clicked.connect(lambda: self.set_open(not self._open, emit=True))
        head.addWidget(self.btn_title)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("Muted")
        head.addWidget(self.lbl_count)
        head.addStretch(1)
        for w in extra_widgets:
            head.addWidget(w)
        self.btn_up = small_button("chev_up", "Переместить раздел выше")
        self.btn_down = small_button("chev_down", "Переместить раздел ниже")
        self.btn_up.clicked.connect(lambda: self.move_requested.emit("up"))
        self.btn_down.clicked.connect(lambda: self.move_requested.emit("down"))
        head.addWidget(self.btn_up)
        head.addWidget(self.btn_down)
        v.addLayout(head)
        v.addWidget(body, 1)
        self.set_open(True)

    def is_open(self):
        return self._open

    def set_open(self, on, emit=False):
        self._open = bool(on)
        self.body.setVisible(self._open)
        self.btn_title.setIcon(theme.icon("chev_down" if self._open else "chev_right", 16))
        if emit:
            self.toggled.emit(self._open)

    def set_count(self, text):
        self.lbl_count.setText(f"  {text}" if text else "")

    def set_move_enabled(self, up, down):
        self.btn_up.setEnabled(up)
        self.btn_down.setEnabled(down)


class ClearableTree(QTreeWidget):
    """Дерево, у которого клик в пустое место снимает выделение."""

    def mousePressEvent(self, e):
        if self.itemAt(e.position().toPoint()) is None:
            self.clearSelection()
            self.setCurrentItem(None)
        super().mousePressEvent(e)

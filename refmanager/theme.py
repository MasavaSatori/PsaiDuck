"""Тёмная современная тема: QSS, палитра и векторные иконки, нарисованные в коде."""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap

BG = "#15171D"
PANEL = "#1C1F27"
INPUT = "#12141A"
BORDER = "#2C303B"
TEXT = "#E6E8EE"
MUTED = "#8B92A5"
DISABLED = "#4A5062"
HOVER = "#272B36"
ACCENT = "#6C7CFF"
ACCENT_HOVER = "#8391FF"
ACCENT_TEXT = "#B4BEFF"
ACCENT_SOFT = "#2B3158"
CANVAS_BG = (18, 20, 26)  # rgb поля с сеткой (альфа задаётся отдельно)
GRID_MINOR = (170, 180, 215)
GRID_MAJOR = (170, 180, 215)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# --------------------------------------------------------------------------- icons
def _poly(p, pts, close=False):
    path = QPainterPath(QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(x, y)
    if close:
        path.closeSubpath()
    p.drawPath(path)


def _line(p, x1, y1, x2, y2):
    p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _undo(p):
    path = QPainterPath(QPointF(4, 11))
    path.lineTo(14, 11)
    path.cubicTo(19.5, 11, 19.5, 19, 14, 19)
    path.lineTo(9, 19)
    p.drawPath(path)
    _poly(p, [(8, 7), (4, 11), (8, 15)])


def _redo(p):
    p.translate(24, 0)
    p.scale(-1, 1)
    _undo(p)


def _almond(p):
    path = QPainterPath(QPointF(2.5, 12))
    path.cubicTo(6, 6.5, 18, 6.5, 21.5, 12)
    path.cubicTo(18, 17.5, 6, 17.5, 2.5, 12)
    path.closeSubpath()
    p.drawPath(path)


def _grid(p):
    p.drawRoundedRect(QRectF(4, 4, 16, 16), 2, 2)
    for v in (9.33, 14.66):
        _line(p, v, 4, v, 20)
        _line(p, 4, v, 20, v)


_DRAW = {
    "select": lambda p: _poly(p, [(6, 3.5), (6, 19), (10, 15.2), (13, 20.5), (15.6, 19.2), (12.8, 14), (18, 13.6)], True),
    "brush": lambda p: (_poly(p, [(4, 20), (4.8, 15.4), (16.2, 4), (20, 7.8), (8.6, 19.2)], True),
                        _line(p, 13.6, 6.6, 17.4, 10.4)),
    "eraser": lambda p: (_poly(p, [(3.5, 15), (12, 6.5), (19, 13.5), (13, 19.5), (8, 19.5)], True),
                         _line(p, 8.5, 10, 15.5, 17), _line(p, 13, 19.5, 20.5, 19.5)),
    "crop": lambda p: (_poly(p, [(7, 3), (7, 17), (21, 17)]), _poly(p, [(3, 7), (17, 7), (17, 21)])),
    "image": lambda p: (p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 2.5, 2.5),
                        p.drawEllipse(QPointF(9, 10), 1.6, 1.6),
                        _poly(p, [(4, 18), (9.5, 13.5), (13.5, 17), (16.5, 14.2), (20, 17.5)])),
    "undo": _undo,
    "redo": _redo,
    "pin": lambda p: (_poly(p, [(8, 4), (16, 4), (15, 10), (18, 14), (6, 14), (9, 10)], True),
                      _line(p, 12, 14, 12, 21)),
    "grid": _grid,
    "copy": lambda p: (p.drawRoundedRect(QRectF(9, 9, 11, 11), 2, 2),
                       _poly(p, [(15, 9), (15, 6.5), (13.5, 5), (6.5, 5), (5, 6.5), (5, 13.5), (6.5, 15), (9, 15)])),
    "trash": lambda p: (_line(p, 4, 7, 20, 7), _poly(p, [(6, 7), (7, 21), (17, 21), (18, 7)]),
                        _poly(p, [(9, 7), (9, 4), (15, 4), (15, 7)]),
                        _line(p, 10, 11, 10, 17), _line(p, 14, 11, 14, 17)),
    "plus": lambda p: (_line(p, 12, 5, 12, 19), _line(p, 5, 12, 19, 12)),
    "fit": lambda p: (_poly(p, [(4, 9), (4, 4), (9, 4)]), _poly(p, [(15, 4), (20, 4), (20, 9)]),
                      _poly(p, [(20, 15), (20, 20), (15, 20)]), _poly(p, [(9, 20), (4, 20), (4, 15)])),
    "sidebar": lambda p: (p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 2.5, 2.5), _line(p, 9.5, 4.5, 9.5, 19.5)),
    "panel": lambda p: (p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 2.5, 2.5), _line(p, 14.5, 4.5, 14.5, 19.5)),
    "folder": lambda p: _poly(p, [(3.5, 7), (3.5, 18.5), (20.5, 18.5), (20.5, 9), (11.5, 9), (9.5, 6), (3.5, 6)], True),
    "export": lambda p: (_poly(p, [(5, 14), (5, 19), (19, 19), (19, 14)]), _line(p, 12, 15, 12, 4),
                         _poly(p, [(8, 8), (12, 4), (16, 8)])),
    "eye": lambda p: (_almond(p), p.drawEllipse(QPointF(12, 12), 3, 3)),
    "eye_off": lambda p: (_almond(p), _line(p, 4.5, 20, 19.5, 4)),
    "history": lambda p: (p.drawEllipse(QPointF(12, 12), 8, 8), _poly(p, [(12, 7.5), (12, 12), (15.5, 14)])),
    "window": lambda p: (p.drawRoundedRect(QRectF(3.5, 5, 17, 14), 2.5, 2.5), _line(p, 3.5, 9.5, 20.5, 9.5)),
    "chev_down": lambda p: _poly(p, [(7, 9.5), (12, 14.5), (17, 9.5)]),
    "chev_right": lambda p: _poly(p, [(9.5, 7), (14.5, 12), (9.5, 17)]),
    "chev_up": lambda p: _poly(p, [(7, 14.5), (12, 9.5), (17, 14.5)]),
    "win_min": lambda p: _line(p, 6, 12, 18, 12),
    "win_max": lambda p: p.drawRoundedRect(QRectF(6, 6, 12, 12), 1.5, 1.5),
    "win_restore": lambda p: (p.drawRoundedRect(QRectF(5.5, 8.5, 10, 10), 1.5, 1.5),
                              _poly(p, [(8.5, 8.5), (8.5, 5.5), (18.5, 5.5), (18.5, 15.5), (15.5, 15.5)])),
    "win_close": lambda p: (_line(p, 6, 6, 18, 18), _line(p, 18, 6, 6, 18)),
}

_icon_cache = {}


def _pix(name, color, size):
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24.0, size / 24.0)
    p.setPen(QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    _DRAW[name](p)
    p.end()
    return pm


def icon(name, size=22):
    key = (name, size)
    if key not in _icon_cache:
        ic = QIcon()
        for state, col in ((QIcon.Off, TEXT), (QIcon.On, ACCENT_TEXT)):
            ic.addPixmap(_pix(name, col, size), QIcon.Normal, state)
            ic.addPixmap(_pix(name, col, size), QIcon.Active, state)
            ic.addPixmap(_pix(name, DISABLED, size), QIcon.Disabled, state)
        _icon_cache[key] = ic
    return _icon_cache[key]


def _ensure_chevron_png(name: str) -> str:
    """Стрелки сворачивания для веток дерева (QSS умеет только url картинок)."""
    path = DATA_DIR / "cache" / f"{name}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pm = _pix(name, MUTED, 16)
        pm.save(str(path))
    return path.as_posix()


def _ensure_check_png() -> str:
    path = DATA_DIR / "cache" / "check.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("white"), 3.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        _poly(p, [(8, 17), (14, 23), (24, 10)])
        p.end()
        pm.save(str(path))
    return path.as_posix()


# --------------------------------------------------------------------------- QSS / палитра
def stylesheet() -> str:
    check = _ensure_check_png()
    chev_r = _ensure_chevron_png("chev_right")
    chev_d = _ensure_chevron_png("chev_down")
    return f"""
* {{ font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 13px; color: {TEXT}; }}
QMainWindow {{ background: {BG}; }}
#Root {{ background: {BG}; }}
#TopBar, #SidePanel, #Inspector {{ background: {PANEL}; }}
#TopBar {{ border-bottom: 1px solid {BORDER}; }}
#SidePanel {{ border-right: 1px solid {BORDER}; }}
#Inspector {{ border-left: 1px solid {BORDER}; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar QLabel {{ color: {MUTED}; }}
QLabel {{ background: transparent; }}
QLabel#Section {{ color: {MUTED}; font-size: 11px; font-weight: 600; }}
QLabel#Title {{ font-size: 14px; font-weight: 600; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Info {{ background: {INPUT}; border-radius: 8px; padding: 8px 10px; color: {TEXT}; }}
QToolButton {{ border: none; border-radius: 8px; padding: 6px; background: transparent; }}
QToolButton:hover {{ background: {HOVER}; }}
QToolButton:checked {{ background: {ACCENT_SOFT}; }}
QToolButton:pressed {{ background: {ACCENT_SOFT}; }}
QToolButton#WinBtn {{ border-radius: 6px; padding: 4px; }}
QToolButton#WinClose {{ border-radius: 6px; padding: 4px; }}
QToolButton#WinClose:hover {{ background: #E5484D; }}
QPushButton {{ border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 12px; background: {PANEL}; }}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT_TEXT}; }}
QPushButton:disabled {{ color: {DISABLED}; }}
QPushButton#Primary {{ background: {ACCENT}; color: white; border: none; font-weight: 600; padding: 8px 12px; }}
QPushButton#Primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#Ratio {{ padding: 5px 0px; min-width: 42px; font-size: 12px; }}
QPushButton#Seg {{ border-radius: 0px; padding: 5px 10px; }}
QPushButton#Seg[pos="first"] {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
QPushButton#Seg[pos="last"] {{ border-top-right-radius: 8px; border-bottom-right-radius: 8px; }}
QPushButton#Seg[pos="middle"], QPushButton#Seg[pos="last"] {{ border-left: none; }}
QPushButton#Seg:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
    background: {TEXT}; border: 2px solid {ACCENT}; }}
QSlider::handle:horizontal:disabled {{ border-color: {DISABLED}; background: {MUTED}; }}
QSlider::sub-page:horizontal:disabled {{ background: {DISABLED}; }}
QListWidget {{ border: none; background: transparent; outline: 0; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 8px; margin: 1px 6px; }}
QListWidget::item:hover {{ background: {HOVER}; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; }}
QTreeWidget {{ border: none; background: transparent; outline: 0; show-decoration-selected: 0; }}
QTreeWidget::item {{ padding: 6px 4px; border-radius: 8px; margin: 1px 6px 1px 0px; }}
QTreeWidget::item:hover {{ background: {HOVER}; }}
QTreeWidget::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; }}
QTreeView::branch, QTreeView::branch:selected, QTreeView::branch:hover {{ background: {PANEL}; }}
QTreeView::branch:closed:has-children {{ image: url({chev_r}); }}
QTreeView::branch:open:has-children {{ image: url({chev_d}); }}
QToolButton#Chev {{ padding: 2px; border-radius: 6px; }}
QToolButton#SecTitle {{ color: {MUTED}; font-size: 11px; font-weight: 600; padding: 4px 6px 4px 2px; }}
QToolButton::menu-indicator {{ image: none; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSpinBox, QLineEdit {{ border: 1px solid {BORDER}; border-radius: 8px; padding: 5px 8px; background: {INPUT};
    selection-background-color: {ACCENT}; }}
QSpinBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0px; border: none; }}
QSpinBox QLineEdit {{ border: none; padding: 0px; background: transparent; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1.5px solid {DISABLED}; background: {INPUT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({check}); }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; padding: 4px; }}
QMenu::item {{ padding: 7px 20px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QToolTip {{ background: {HOVER}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 8px; }}
QScrollBar:vertical {{ width: 10px; background: transparent; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QDialog, QMessageBox, QInputDialog, QColorDialog {{ background: {PANEL}; }}
"""


def palette() -> QPalette:
    p = QPalette()
    c = QColor
    p.setColor(QPalette.Window, c(BG))
    p.setColor(QPalette.WindowText, c(TEXT))
    p.setColor(QPalette.Base, c(INPUT))
    p.setColor(QPalette.AlternateBase, c(PANEL))
    p.setColor(QPalette.Text, c(TEXT))
    p.setColor(QPalette.Button, c(PANEL))
    p.setColor(QPalette.ButtonText, c(TEXT))
    p.setColor(QPalette.ToolTipBase, c(HOVER))
    p.setColor(QPalette.ToolTipText, c(TEXT))
    p.setColor(QPalette.Highlight, c(ACCENT))
    p.setColor(QPalette.HighlightedText, c("white"))
    p.setColor(QPalette.PlaceholderText, c(MUTED))
    p.setColor(QPalette.Link, c(ACCENT_TEXT))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c(DISABLED))
    return p


def apply(app):
    app.setPalette(palette())
    app.setStyleSheet(stylesheet())

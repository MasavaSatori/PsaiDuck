import os
import sys

# Полное разрешение: снимаем лимит Qt на размер декодируемой картинки (по умолчанию 256 МБ).
os.environ.setdefault("QT_IMAGEIO_MAXALLOC", "0")

from PySide6.QtWidgets import QApplication  # noqa: E402

from refmanager import theme  # noqa: E402
from refmanager.window import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Previewer")
    app.setStyle("Fusion")
    theme.apply(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

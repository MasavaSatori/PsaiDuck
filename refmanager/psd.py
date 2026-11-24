"""Запись слоёного PSD для Photoshop: RGB, 8 бит на канал, слои без сжатия (максимальная совместимость).

Что пишется:
  * шапка и встроенный ICC-профиль sRGB — чтобы Photoshop не спрашивал про профиль и показывал цвета как у нас;
  * слои снизу вверх: у каждого прямоугольник, имя (в том числе русское: блок «luni»), видимость и канал прозрачности
    (без него Photoshop растягивает слой на весь холст и заливает белым);
  * склеенное изображение (то, что видно сейчас) — для превью и программ, не читающих слои.
Каналы пишутся потоком, по одному, поэтому для картинки 8K не нужно держать в памяти несколько копий.
"""
import struct

from PySide6.QtGui import QColorSpace, QImage

MAX_SIDE = 30000   # ограничение формата PSD (больше нужен PSB)


def srgb_icc() -> bytes:
    try:
        return bytes(QColorSpace(QColorSpace.NamedColorSpace.SRgb).iccProfile())
    except Exception:  # noqa: BLE001 — профиль необязателен, без него файл тоже валиден
        return b""


def _pascal(name: str) -> bytes:
    raw = name.encode("ascii", "replace")[:255]
    b = bytes([len(raw)]) + raw
    return b + b"\0" * (-len(b) % 4)


def _luni(name: str) -> bytes:
    u = name.encode("utf-16-be")
    data = struct.pack(">I", len(u) // 2) + u
    data += b"\0" * (-len(data) % 4)
    return b"8BIM" + b"luni" + struct.pack(">I", len(data)) + data


def _prepare(img: QImage):
    """Возвращает (QImage в формате с порядком байт B,G,R[,A] без премультипликации, есть ли альфа)."""
    if img.hasAlphaChannel():
        return (img if img.format() == QImage.Format_ARGB32 else img.convertToFormat(QImage.Format_ARGB32)), True
    return (img if img.format() == QImage.Format_RGB32 else img.convertToFormat(QImage.Format_RGB32)), False


def _plane(img: QImage, byte_index: int) -> bytes:
    """Одна цветовая плоскость из пикселей BGRA (little-endian), без построчного цикла на Python."""
    assert img.bytesPerLine() == img.width() * 4
    return memoryview(img.constBits())[byte_index::4].tobytes()


def write_psd(path, width, height, composite: QImage, layers, progress=None):
    """layers — список словарей снизу вверх: name, x, y, image (QImage), visible (bool).
    composite — склеенное изображение размером width×height. progress(done, total) вызывается по ходу записи."""
    if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE):
        raise ValueError(f"Размер {width}×{height} не поддерживается форматом PSD (максимум {MAX_SIDE} px)")

    prepared = []
    for l in layers:
        img, alpha = _prepare(l["image"])
        # (id канала PSD, индекс байта в BGRA; None — сплошная непрозрачность). Канал прозрачности есть у КАЖДОГО слоя:
        # без него Photoshop считает слой непрозрачным и дополняет его белым до размера всего холста.
        chans = [(0, 2), (1, 1), (2, 0), (-1, 3 if alpha else None)]   # цвета, затем прозрачность (как у ImageMagick)
        prepared.append((l, img, chans))

    # ---- записи слоёв
    records = []
    for l, img, chans in prepared:
        w, h = img.width(), img.height()
        rec = struct.pack(">iiii", l["y"], l["x"], l["y"] + h, l["x"] + w) + struct.pack(">H", len(chans))
        for cid, _ in chans:
            rec += struct.pack(">hI", cid, 2 + w * h)
        flags = 0 if l.get("visible", True) else 2   # бит 1 = слой скрыт
        rec += b"8BIM" + b"norm" + bytes([255, 0, flags, 0])
        extra = struct.pack(">I", 0) + struct.pack(">I", 0) + _pascal(l["name"]) + _luni(l["name"])
        rec += struct.pack(">I", len(extra)) + extra
        records.append(rec)

    chan_bytes = sum(2 + img.width() * img.height() for _, img, chans in prepared for _ in chans)
    layer_info_len = 2 + sum(len(r) for r in records) + chan_bytes
    layer_info_pad = layer_info_len % 2
    layer_info_len += layer_info_pad
    layer_section_len = 4 + layer_info_len + 4   # длина информации о слоях + она сама + глобальная маска (пустая)

    comp, comp_alpha = _prepare(composite)
    if comp_alpha:   # склеенное изображение пишем как RGB: прозрачность кладём на белый фон
        flat = QImage(comp.size(), QImage.Format_RGB32)
        flat.fill(0xFFFFFFFF)
        from PySide6.QtGui import QPainter
        p = QPainter(flat)
        p.drawImage(0, 0, comp)
        p.end()
        comp = flat

    icc = srgb_icc()
    resources = b""
    if icc:
        data = icc + (b"\0" if len(icc) % 2 else b"")
        resources = b"8BIM" + struct.pack(">H", 1039) + b"\0\0" + struct.pack(">I", len(icc)) + data

    total_steps = sum(len(c) for _, _, c in prepared) + 3
    done = [0]

    def tick():
        done[0] += 1
        if progress:
            progress(done[0], total_steps)

    with open(path, "wb") as f:
        f.write(b"8BPS" + struct.pack(">H", 1) + b"\0" * 6)
        f.write(struct.pack(">HIIHH", 3, height, width, 8, 3))       # каналов, высота, ширина, глубина, RGB
        f.write(struct.pack(">I", 0))                                 # данные цветового режима
        f.write(struct.pack(">I", len(resources)) + resources)        # ресурсы изображения (ICC)
        f.write(struct.pack(">I", layer_section_len))
        f.write(struct.pack(">I", layer_info_len))
        f.write(struct.pack(">h", len(prepared)))
        for rec in records:
            f.write(rec)
        for _, img, chans in prepared:
            for _, byte_index in chans:
                f.write(b"\0\0")                                       # сжатие: без сжатия
                f.write(_plane(img, byte_index) if byte_index is not None
                        else bytes([255]) * (img.width() * img.height()))
                tick()
        if layer_info_pad:
            f.write(b"\0")
        f.write(struct.pack(">I", 0))                                  # глобальная маска слоёв
        f.write(b"\0\0")                                               # склеенное изображение: без сжатия
        for byte_index in (2, 1, 0):                                   # R, G, B плоскостями
            f.write(_plane(comp, byte_index))
            tick()

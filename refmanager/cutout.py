"""Белый фон вставки -> прозрачный.

Прозрачным становится только фон, СВЯЗАННЫЙ С КРАЯМИ картинки: белое внутри объекта (глаза, одежда,
блики) остаётся. На границе делается мягкий переход, а цвет краевых пикселей очищается от белой подмешки,
чтобы вокруг объекта не было светлой каймы. Если края вставки не белые (например, обычная фотография),
ничего не меняется.
"""
try:
    import numpy as np
except ImportError:   # без numpy функция просто выключена
    np = None

from PySide6.QtGui import QImage

WHITE_MIN = 235          # минимальная яркость самого тёмного канала, чтобы считать пиксель белым
BAND = 3                 # ширина полосы вокруг фона, где край делается мягким (px)
BORDER_SHARE = 0.5       # какая доля краевых пикселей должна быть белой, чтобы считать фон белым
MIN_REMOVED = 0.002      # меньше такой доли убранного фона — вставку не трогаем


def available() -> bool:
    return np is not None


def _propagate_rows(reached, white):
    """Белая «заливка» вдоль строк: всё, что лежит в одной непрерывной белой полосе с достигнутым пикселем."""
    h, w = white.shape
    flat = white.ravel()
    starts = flat.copy()
    starts[1:] &= ~flat[:-1]
    starts.reshape(h, w)[:, 0] = white[:, 0]            # каждая строка начинает новую полосу
    run_id = np.cumsum(starts, dtype=np.int32)
    hit = np.zeros(int(run_id[-1]) + 1, dtype=bool)
    sel = reached.ravel() & flat
    hit[run_id[sel]] = True
    return (flat & hit[run_id]).reshape(h, w)


def _flood_from_border(white):
    """Область белого, связанная с краями картинки (4-связность). Без циклов по пикселям."""
    h, w = white.shape
    seed = np.zeros_like(white)
    seed[0, :], seed[-1, :], seed[:, 0], seed[:, -1] = white[0, :], white[-1, :], white[:, 0], white[:, -1]
    reached = seed
    count = int(reached.sum())
    for _ in range(80):
        r = _propagate_rows(reached, white)
        r = _propagate_rows(np.ascontiguousarray(r.T), np.ascontiguousarray(white.T)).T
        n = int(r.sum())
        reached = r
        if n == count:
            break
        count = n
    return reached


def _dilate(mask, steps):
    m = mask.copy()
    for _ in range(steps):
        d = m.copy()
        d[1:, :] |= m[:-1, :]
        d[:-1, :] |= m[1:, :]
        d[:, 1:] |= m[:, :-1]
        d[:, :-1] |= m[:, 1:]
        m = d
    return m


def white_to_alpha(img: QImage):
    """Возвращает (QImage ARGB32_Premultiplied с прозрачным фоном, доля убранного фона) или None,
    если у вставки нет белого фона по краям."""
    if np is None or img.isNull():
        return None
    src = img.convertToFormat(QImage.Format_ARGB32)       # в памяти байты B, G, R, A
    h, w = src.height(), src.width()
    arr = np.frombuffer(memoryview(src.constBits()), dtype=np.uint8).reshape(h, w, 4).copy()
    minc = arr[..., :3].min(axis=2)
    white = minc >= WHITE_MIN
    border = np.concatenate([white[0, :], white[-1, :], white[:, 0], white[:, -1]])
    if border.mean() < BORDER_SHARE:
        return None
    bg = _flood_from_border(white)
    removed = float(bg.mean())
    if removed < MIN_REMOVED:
        return None
    # граница: полоса BAND px вокруг фона. Пиксель в ней — смесь «цвет объекта» и белого:
    # P = a*C + (1-a)*255, где C берём у ближайших внутренних пикселей, а прозрачность a находим проекцией.
    # Считаем только по пикселям полосы (их немного), поэтому большие вставки обрабатываются быстро.
    near = _dilate(bg, BAND)
    band = near & ~bg
    by, bx = np.nonzero(band)
    n = by.size
    rgb = arr[..., :3]                                  # вид на массив: B, G, R
    P = rgb[by, bx].astype(np.float32)
    est = P.copy()
    if n:
        have = ~near                                    # внутренние пиксели с известным цветом объекта
        uy, ux, pos = by, bx, np.arange(n)
        for _ in range(BAND + 3):
            if uy.size == 0:
                break
            acc = np.zeros((uy.size, 3), dtype=np.float32)
            cnt = np.zeros(uy.size, dtype=np.float32)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny = np.clip(uy + dy, 0, h - 1)
                nx = np.clip(ux + dx, 0, w - 1)
                hv = have[ny, nx]
                acc += rgb[ny, nx].astype(np.float32) * hv[:, None]
                cnt += hv
            fill = cnt > 0
            if fill.any():
                vals = acc[fill] / cnt[fill][:, None]
                rgb[uy[fill], ux[fill]] = vals.astype(np.uint8)   # оценка цвета объекта кладём прямо в массив
                est[pos[fill]] = vals
                have[uy[fill], ux[fill]] = True
            uy, ux, pos = uy[~fill], ux[~fill], pos[~fill]
        white_v = np.float32(255.0)
        d = white_v - est                               # вектор от цвета объекта к белому
        dd = (d * d).sum(axis=1)
        proj = ((white_v - P) * d).sum(axis=1) / np.maximum(dd, 1.0)
        a_b = np.clip(proj, 0.0, 1.0)
        a_b[dd < 400.0] = 1.0                           # объект почти белый: смесь не различить, оставляем как есть
        clean = np.clip((P - white_v * (1.0 - a_b[:, None])) / np.maximum(a_b[:, None], 0.02), 0, 255)
        clean[a_b <= 0.02] = est[a_b <= 0.02]
        rgb[by, bx] = clean.astype(np.uint8)
    alpha = np.full((h, w), 255, dtype=np.uint8)
    alpha[bg] = 0
    if n:
        alpha[by, bx] = (a_b * 255.0).astype(np.uint8)
    arr[..., 3] = alpha
    out = QImage(arr.data, w, h, w * 4, QImage.Format_ARGB32).copy()
    return out.convertToFormat(QImage.Format_ARGB32_Premultiplied), removed

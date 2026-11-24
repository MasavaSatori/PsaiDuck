"""Хранилище: папки проектов, проекты, рабочие окна проекта, ассеты (оригиналы картинок), настройки.

Структура на диске (data/):
  folders.json                  — папки проектов
  projects/<папка проекта>/
      project.json              — метаданные: имя, папка, список рабочих окон
      ws/<id>.json              — содержимое рабочего окна (картинки, штрихи, участки, сетка, вид)
      assets/                   — оригиналы картинок и исходные пиксели участков (общие для всех окон проекта)
      trash/                    — удалённые рабочие окна (их ассеты не чистятся)
"""
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROJECTS = DATA / "projects"
TRASH = DATA / "trash"

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
META_KEYS = {"version", "name", "created", "folder", "workspaces", "active_ws"}


def _safe_dirname(name: str) -> str:
    s = _BAD.sub("_", name).strip().rstrip(".")
    return s or "project"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    for f in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


class Project:
    def __init__(self, path: Path):
        self.path = path
        self.id = path.name
        self.file = path / "project.json"
        self.assets_dir = path / "assets"
        self.ws_dir = path / "ws"
        self.trash_dir = path / "trash"
        meta = _read_json(self.file)
        self.name = meta.get("name") or path.name
        self.created = meta.get("created") or path.stat().st_mtime
        self.folder = meta.get("folder")
        self.workspaces = [w for w in meta.get("workspaces", []) if isinstance(w, dict) and "id" in w]
        self.active_ws = meta.get("active_ws")
        if not self.workspaces:
            self._migrate(meta)
        if self.active_ws not in {w["id"] for w in self.workspaces}:
            self.active_ws = self.workspaces[0]["id"]

    # ---- миграция старого формата (весь холст лежал прямо в project.json)
    def _migrate(self, legacy: dict):
        canvas = {k: v for k, v in legacy.items() if k not in META_KEYS}
        ws_id = _new_id()
        _atomic_write(self.ws_dir / f"{ws_id}.json", json.dumps(canvas or {"version": 1, "items": []},
                                                                   ensure_ascii=False))
        backup = self.path / "project.json.legacy"
        if self.file.exists() and not backup.exists():
            try:
                shutil.copy2(self.file, backup)   # исходный файл остаётся нетронутой копией
            except OSError:
                pass
        self.workspaces = [{"id": ws_id, "name": "Рабочее окно 1"}]
        self.active_ws = ws_id
        self.save_meta()

    # ---- метаданные
    def save_meta(self):
        _atomic_write(self.file, json.dumps({
            "version": 2, "name": self.name, "created": self.created, "folder": self.folder,
            "workspaces": self.workspaces, "active_ws": self.active_ws}, ensure_ascii=False))

    def rename(self, new_name: str):
        self.name = new_name
        self.save_meta()

    def set_folder(self, folder_id):
        self.folder = folder_id
        self.save_meta()

    # ---- рабочие окна
    def ws_name(self, ws_id):
        return next((w["name"] for w in self.workspaces if w["id"] == ws_id), "")

    def load_ws(self, ws_id) -> dict:
        return _read_json(self.ws_dir / f"{ws_id}.json")

    def save_ws(self, ws_id, payload: dict):
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.ws_dir / f"{ws_id}.json", json.dumps(payload, ensure_ascii=False))

    def add_ws(self, name: str) -> str:
        ws_id = _new_id()
        self.workspaces.append({"id": ws_id, "name": name})
        self.save_ws(ws_id, {"version": 1, "items": []})
        self.save_meta()
        return ws_id

    def rename_ws(self, ws_id, name: str):
        for w in self.workspaces:
            if w["id"] == ws_id:
                w["name"] = name
        self.save_meta()

    def delete_ws(self, ws_id):
        """Не стирает: убирает окно из списка, а его файл переносит в trash/ проекта."""
        if len(self.workspaces) <= 1:
            return False
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        src = self.ws_dir / f"{ws_id}.json"
        if src.exists():
            shutil.move(str(src), str(self.trash_dir / f"{ws_id}_{int(time.time())}.json"))
        self.workspaces = [w for w in self.workspaces if w["id"] != ws_id]
        if self.active_ws == ws_id:
            self.active_ws = self.workspaces[0]["id"]
        self.save_meta()
        return True

    def set_active_ws(self, ws_id):
        if self.active_ws != ws_id:
            self.active_ws = ws_id
            self.save_meta()

    # ---- ассеты (оригиналы без перекодирования)
    def asset_path(self, fname: str) -> Path:
        return self.assets_dir / fname

    def import_file(self, src) -> str:
        src = Path(src)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{uuid.uuid4().hex[:10]}{src.suffix.lower()}"
        shutil.copyfile(src, self.assets_dir / fname)
        return fname

    def import_qimage(self, image) -> str:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{uuid.uuid4().hex[:10]}.png"
        # quality=100 — минимальное сжатие: файл больше, но пишется в 2-3 раза быстрее (без потерь)
        if not image.save(str(self.assets_dir / fname), "PNG", 100):
            raise OSError("не удалось сохранить изображение")
        return fname

    def remove_asset(self, fname: str):
        try:
            (self.assets_dir / fname).unlink()
        except OSError:
            pass

    @staticmethod
    def _files_in(data: dict) -> set:
        used = set()
        for d in data.get("items", []):
            if isinstance(d, dict) and d.get("file"):
                used.add(d["file"])
                used.update(it["file"] for it in (d.get("base") or {}).get("iters", [])
                            if isinstance(it, dict) and it.get("file"))
        for p in data.get("patches", []):
            if isinstance(p, dict):
                if p.get("orig"):
                    used.add(p["orig"])
                used.update(it["file"] for it in p.get("iters", []) if isinstance(it, dict) and it.get("file"))
        return used

    def referenced_elsewhere(self, exclude_ws=None) -> set:
        """Файлы, нужные другим рабочим окнам проекта и удалённым окнам из trash/ (их чистить нельзя)."""
        used = set()
        sources = [self.ws_dir / f"{w['id']}.json" for w in self.workspaces if w["id"] != exclude_ws]
        if self.trash_dir.exists():
            sources += list(self.trash_dir.glob("*.json"))
        for f in sources:
            used |= self._files_in(_read_json(f))
        return used

    def prune_assets(self, used: set):
        """Удаляет файлы, на которые больше нет ссылок. Вызывать, только когда история отмены очищена
        и в used учтены все рабочие окна проекта."""
        if not self.assets_dir.exists():
            return
        for f in self.assets_dir.iterdir():
            if f.is_file() and f.name not in used:
                try:
                    f.unlink()
                except OSError:
                    pass


class ProjectStore:
    def __init__(self):
        PROJECTS.mkdir(parents=True, exist_ok=True)

    def all(self) -> list:
        res = []
        for d in PROJECTS.iterdir():
            if d.is_dir() and (d / "project.json").exists():
                try:
                    res.append(Project(d))
                except OSError:
                    continue
        res.sort(key=lambda p: p.created)
        return res

    def get(self, pid: str):
        d = PROJECTS / pid
        if d.is_dir() and (d / "project.json").exists():
            return Project(d)
        return None

    def create(self, name: str, folder=None) -> Project:
        base = _safe_dirname(name)
        path = PROJECTS / base
        n = 2
        while path.exists():
            path = PROJECTS / f"{base} ({n})"
            n += 1
        path.mkdir(parents=True)
        (path / "assets").mkdir()
        ws_id = _new_id()
        meta = {"version": 2, "name": name, "created": time.time(), "folder": folder,
                "workspaces": [{"id": ws_id, "name": "Рабочее окно 1"}], "active_ws": ws_id}
        _atomic_write(path / "project.json", json.dumps(meta, ensure_ascii=False))
        _atomic_write(path / "ws" / f"{ws_id}.json", json.dumps({"version": 1, "items": []}))
        return Project(path)

    def delete(self, proj: Project):
        """Не удаляет насовсем: переносит папку в data/trash."""
        TRASH.mkdir(parents=True, exist_ok=True)
        dest = TRASH / f"{proj.id}_{int(time.time())}"
        shutil.move(str(proj.path), str(dest))


class FolderStore:
    """Папки проектов. Принадлежность проекта папке хранится в самом проекте (project.json: folder)."""

    def __init__(self):
        self.path = DATA / "folders.json"
        self.folders = []
        data = _read_json(self.path)
        self.folders = [f for f in data.get("folders", []) if isinstance(f, dict) and "id" in f]

    def _save(self):
        _atomic_write(self.path, json.dumps({"folders": self.folders}, ensure_ascii=False))

    def get(self, fid):
        return next((f for f in self.folders if f["id"] == fid), None)

    def create(self, name: str) -> dict:
        f = {"id": _new_id(), "name": name}
        self.folders.append(f)
        self._save()
        return f

    def rename(self, fid, name: str):
        f = self.get(fid)
        if f:
            f["name"] = name
            self._save()

    def delete(self, fid):
        """Удаляет только папку; проекты из неё остаются (переносятся в корень вызывающим кодом)."""
        self.folders = [f for f in self.folders if f["id"] != fid]
        self._save()


class Settings:
    DEFAULTS = {
        "pinned": True,
        "keep_ratio": False,
        "last_project": "",
        "last_ws": "",
        "geometry": "",
        "brush_color": "#E5484D",
        "brush_size": 4,
        "sidebar": True,
        "inspector": True,
        "crop_mode": "free",
        "crop_ratio": "1:1",
        "crop_res": "1K",
        "export_dir": "",
        "remove_white": True,
        "projects_open": True,
        "patches_open": True,
        "sections_order": ["projects", "patches"],
        "tree_open": {},
    }

    def __init__(self):
        self.path = DATA / "settings.json"
        self.d = dict(self.DEFAULTS)
        try:
            self.d.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.d.get(key, self.DEFAULTS.get(key))

    def set(self, key, value):
        self.d[key] = value

    def save(self):
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

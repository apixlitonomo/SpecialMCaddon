"""Thumbnails for the mod import panel, rendered off the UI thread.

Cards ask for their thumbnail while drawing (``thumbnail``) and the panel
gives the order it wants them in (``request``): the cards on screen, then the
rest of the category outward from them. Missing ones render in a pool of
headless Blenders (``importers/mod_thumbnail_worker.py``) that read the order
again between models, so the cards on screen always come next and scrolling
finds the next rows ready. Up to one worker per four CPU threads (four at
most) render at once while many are missing.

The first worker starts when a JAR is dropped (``prepare``), while the
panel is still reading the mod, and gets ready for it: it compiles EEVEE's
shaders while it opens the vanilla client JAR. When the panel closes one worker
stays warm for two minutes, so the next mod's thumbnails start at once. PNGs
are cached per JAR (path, size and modification time) and per version of the
code that reads mods, so a mod renders once.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import asdict

import bpy

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Twice the 136 x 112 card tile, so HiDPI displays stay sharp.
SIZE = (272, 224)
WORKERS = max(1, min(4, (os.cpu_count() or 4) // 4))
PER_WORKER = 6  # missing thumbnails that make another worker worth starting
HEAD = 24  # missing thumbnails checked per tick, from the front of the order
_VERSION = 1

# The workers, the files they read and what they are asked for.
_pool = {"workers": [], "files": {}, "jar": "", "order": (), "written": None, "starts": {}, "crashes": 0}
_done = {}          # (jar, key) -> PNG
_failed = set()     # (jar, key)
_gave_up = False    # the workers could not start: placeholders only
_jar_dirs = {}


_jar_stats = {}     # jar -> (when looked, build key): cards ask every frame


def cache_dir(jar):
    """The thumbnail folder of one JAR build."""
    now = time.monotonic()
    seen = _jar_stats.get(jar)
    if seen is None or now - seen[0] > 2.0:
        try:
            stat = os.stat(jar)
        except OSError:
            return ""
        seen = _jar_stats[jar] = (now, (os.path.normcase(os.path.abspath(jar)), stat.st_size, stat.st_mtime_ns))
    key = seen[1]
    if key not in _jar_dirs:
        from ..importers.archive_extractor import user_cache_dir
        from ..importers.mod_jar import _scanner_stamp

        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(jar))[0])[:48]
        # A changed reader (a new add-on version) renders the mod's models again.
        digest = hashlib.sha1(repr((key, _scanner_stamp())).encode("utf-8")).hexdigest()[:10]
        _jar_dirs[key] = os.path.join(user_cache_dir("mod_thumbnails"), f"{stem}_{digest}_v{_VERSION}")
    return _jar_dirs[key]


def _path(jar, key):
    folder = cache_dir(jar)
    return os.path.join(folder, hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".png") if folder else ""


_looked = {}        # (jar, key) -> when its files were last looked for


def _due(jar, key, fresh):
    """Cards draw every frame; the disk is asked about one at most twice a second."""
    now = time.monotonic()
    if not fresh and now - _looked.get((jar, key), -1.0) < 0.5:
        return False
    _looked[(jar, key)] = now
    return True


def thumbnail(jar, key, fresh=False):
    """The rendered PNG, or ``""`` while it is pending (or failed)."""
    found = _done.get((jar, key))
    if found:
        return found
    if not _due(jar, key, fresh):
        return ""
    path = _path(jar, key)
    if path and os.path.isfile(path):
        _done[(jar, key)] = path
        return path
    return ""


def failed(jar, key, fresh=False):
    if (jar, key) in _failed:
        return True
    path = _path(jar, key) if fresh or (jar, key) not in _looked else ""
    if path and os.path.isfile(os.path.splitext(path)[0] + ".failed"):
        _failed.add((jar, key))
        return True
    return False


def gave_up():
    """The workers could not start: cards keep their placeholder."""
    return _gave_up


def try_again():
    """Another mod is shown: workers that failed before get another chance."""
    global _gave_up
    _gave_up = False
    _pool["crashes"] = 0


def prepare(jar):
    """A JAR was dropped: start a worker now, to be ready when the panel asks.

    A new drop also gives workers that failed before another chance."""
    try_again()
    if cache_dir(jar) and not _live_workers():
        try:
            _spawn()
        except OSError as exc:
            print(f"[Sparkle] Mod thumbnails could not start: {exc}")


def request(jar, keys):
    """Render the missing thumbnails of ``keys``, in that order (the cards on screen first).

    Cheap enough to call on every redraw: the timer does the file checks.
    """
    if _gave_up:
        return
    keys = tuple(keys)
    if jar != _pool["jar"] or keys != _pool["order"]:
        _pool["jar"], _pool["order"] = jar, keys
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=0.01)


def _missing(jar, order):
    """The first missing thumbnails of ``order``; ``True`` when one arrived since the last look."""
    head, arrived = [], False
    for key in order:
        if (jar, key) in _done or (jar, key) in _failed:
            continue
        if thumbnail(jar, key, fresh=True) or failed(jar, key, fresh=True):
            arrived = True
            continue
        head.append(key)
        if len(head) >= HEAD:
            break
    return head, arrived


def _queue_dir():
    """This Blender's files for its workers; ones left by Blenders that quit go."""
    from ..importers.archive_extractor import user_cache_dir

    root = os.path.join(user_cache_dir("mod_thumbnails"), "queue")
    folder = os.path.join(root, str(os.getpid()))
    if not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
        for entry in os.scandir(root):
            if entry.path != folder and time.time() - entry.stat().st_mtime > 6 * 3600:
                shutil.rmtree(entry.path, ignore_errors=True)
    return folder


def _start_file(jar):
    """The JAR's start file for the workers: every model's record, so they never scan its classes."""
    from ..importers.mod_jar import mod_catalog

    # Keyed by the build's folder: a changed JAR at the same path renders anew.
    folder = cache_dir(jar)
    path = _pool["starts"].get(folder, "")
    if path and os.path.isfile(path):
        return path
    os.makedirs(folder, exist_ok=True)
    _forget_missing_file_failures(folder)
    path = os.path.join(_queue_dir(), f"start_{hashlib.sha1(jar.encode('utf-8')).hexdigest()[:12]}.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"jar": jar, "folder": folder,
                   "assets": [asdict(asset) for asset in mod_catalog(jar).assets]}, stream)
    _pool["starts"][folder] = path
    return path


def _forget_missing_file_failures(folder):
    """Failures from file problems (the JAR missing, files in use) say nothing about the models: they go."""
    for entry in os.scandir(folder):
        if not entry.name.endswith(".failed"):
            continue
        try:
            with open(entry.path, encoding="utf-8") as stream:
                reason = stream.readline()
            if (reason.startswith(("FileNotFoundError", "OSError", "PermissionError"))
                    or "No such file" in reason or "Archive file was not found" in reason):
                os.remove(entry.path)
                _failed.difference_update({pair for pair in _failed if _path(*pair) == entry.path[:-7] + ".png"})
        except OSError:
            pass


def _write_order(start, order):
    if _pool["written"] == (start, order):
        return
    path = _pool["files"]["order"]
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as stream:
            json.dump({"start": start, "order": list(order)}, stream)
        os.replace(path + ".tmp", path)
        _pool["written"] = (start, order)
    except OSError:  # a worker is reading it: next tick
        pass


def _spawn():
    from ..importers.vanilla_assets import known_vanilla_jars

    if not os.path.isfile(_pool["files"].get("control", "")):
        folder = _queue_dir()
        _pool["files"] = {name: os.path.join(folder, name + ".json") for name in ("control", "order", "error")}
        _pool["written"] = None
        _write_order("", ())
    files = _pool["files"]
    # The installed client JARs as Blender's UI last found them: the worker skips the folder walk.
    with open(files["control"], "w", encoding="utf-8") as stream:
        json.dump({"addon_dir": _ADDON_DIR, "size": SIZE, "order": files["order"],
                   "error": files["error"], "vanilla_jars": known_vanilla_jars()}, stream)
    script = os.path.join(_ADDON_DIR, "importers", "mod_thumbnail_worker.py")
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)) if os.name == "nt" else 0
    process = subprocess.Popen(
        [bpy.app.binary_path, "--background", "--factory-startup", "-noaudio", "--threads", "2",
         "--python", script, "--", _pool["files"]["control"]],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    _pool["workers"].append({"process": process, "rendered": False})


def _live_workers():
    return [worker for worker in _pool["workers"] if worker["process"].poll() is None]


def _reap():
    """Workers that quit: fine when idle; twice failing in a row, give up until the next drop."""
    global _gave_up
    for worker in [worker for worker in _pool["workers"] if worker["process"].poll() is not None]:
        _pool["workers"].remove(worker)
        error = _pool["files"].get("error", "")
        if error and os.path.isfile(error):
            with open(error, encoding="utf-8") as stream:
                print(f"[Sparkle] Mod thumbnails: {stream.read().strip()}")
            os.remove(error)
            _pool["crashes"] += 1
        elif worker["process"].returncode != 0 and not worker["rendered"]:
            _pool["crashes"] += 1
    if _pool["crashes"] >= 2 and not _gave_up:
        print("[Sparkle] Mod thumbnails could not render; showing placeholders")
        _gave_up = True
        _redraw()


def _tick():
    jar, order = _pool["jar"], _pool["order"]
    head, arrived = _missing(jar, order) if jar else ([], False)
    for worker in _pool["workers"]:
        worker["rendered"] = worker["rendered"] or arrived
    _reap()
    if arrived:
        _pool["crashes"] = 0
        _redraw()
    if not head or _gave_up:
        return None
    try:
        start = _start_file(jar)
        live = _live_workers()
        # One more each tick, so the first starts rendering without the others competing.
        # A mod's first thumbnail unpacks its textures: one worker until then, so they never race.
        ready = any(worker["rendered"] for worker in _pool["workers"]) or any(
            (jar, key) in _done for key in order[:64])
        if len(live) < (max(1, min(WORKERS, len(head) // PER_WORKER)) if ready else 1):
            _spawn()
        _write_order(start, order)
    except OSError as exc:  # the JAR or the cache folder went away: try again in a second
        print(f"[Sparkle] Mod thumbnails could not start: {exc}")
        return 1.0
    return 0.1


def stop():
    """The panel closed: the queue goes; one worker stays warm (two minutes) for the next mod."""
    if _pool["files"]:
        _pool["jar"], _pool["order"] = "", ()
        _write_order("", ())
    for worker in _live_workers()[1:]:
        try:
            worker["process"].stdin.close()
        except OSError:
            pass
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)


def cancel():
    """Stop every worker now (the add-on is turning off)."""
    for worker in _pool["workers"]:
        try:
            worker["process"].kill()
        except OSError:
            pass
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _pool["files"]:
        shutil.rmtree(os.path.dirname(_pool["files"]["control"]), ignore_errors=True)
    _pool.update(workers=[], files={}, jar="", order=(), written=None, starts={}, crashes=0)


def _redraw():
    """A thumbnail arrived: redraw what shows it, not every 3D view's regions."""
    from .mod_import_panel import _PANEL

    session = _PANEL.get("session")
    if session is not None:
        session.tag_redraw()
    for listener in tuple(_LISTENERS):
        listener()


_LISTENERS = []  # other views of mod thumbnails (the asset browser) redraw too


_icons = {}  # cache folder -> the logo path or "": the header asks every frame


def mod_icon(jar):
    """The mod's own logo from the JAR (``fabric.mod.json`` icon, ``logoFile``), cached, or ``""``."""
    folder = cache_dir(jar)
    if not folder:
        return ""
    if folder not in _icons:
        _icons[folder] = _read_mod_icon(jar, folder)
    return _icons[folder]


def _read_mod_icon(jar, folder):
    target = os.path.join(folder, "mod_icon.png")
    if os.path.isfile(target):
        return target
    if os.path.isfile(target + ".none"):
        return ""
    from ..importers.mod_jar import mod_logo

    found = ""
    try:
        with zipfile.ZipFile(jar) as archive:
            found = mod_logo(archive)
            os.makedirs(folder, exist_ok=True)
            if found:
                with open(target, "wb") as stream:
                    stream.write(archive.read(found))
            else:
                open(target + ".none", "w").close()
    except (OSError, zipfile.BadZipFile):
        return ""
    return target if found else ""

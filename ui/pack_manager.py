"""Resource Pack Manager panel for discovering and importing packs."""

import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from functools import lru_cache

try:
    from ..importers import pack_features
except (ImportError, ValueError):
    from importers import pack_features

_feature_index = None
# Park content inspection/filtering for a future release. Name search remains live.
PACK_CONTENT_FILTERS_ENABLED = False


def pack_feature_snapshot(scene):
    global _feature_index
    if not PACK_CONTENT_FILTERS_ENABLED:
        return {}, 0
    if _feature_index is None:
        _feature_index = pack_features.FeatureIndex()
    generation = (scene.as_pointer(), scene.get('_mcbedrock_pack_scan_revision', 0))
    return _feature_index.snapshot([p.path for p in scene.mcbedrock_discovered_packs], generation)

try:
    from ..importers.utils import sanitize_pack_name, strip_formatting_codes
except ImportError:  # pragma: no cover - plain Python test path
    import importlib.util

    _utils_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "importers", "utils.py")
    _utils_spec = importlib.util.spec_from_file_location("mcbedrock_importer_utils", _utils_path)
    _utils_module = importlib.util.module_from_spec(_utils_spec)
    _utils_spec.loader.exec_module(_utils_module)
    sanitize_pack_name = _utils_module.sanitize_pack_name
    strip_formatting_codes = _utils_module.strip_formatting_codes

try:
    from ..importers.preview_fallback import (
        png_has_visible_pixels,
        write_deterministic_fallback,
    )
except (ImportError, ValueError):  # pragma: no cover - plain Python test path
    import importlib.util as _importlib_util
    _preview_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "importers", "preview_fallback.py")
    _preview_spec = _importlib_util.spec_from_file_location("mcbedrock_preview_fallback", _preview_path)
    _preview_module_helpers = _importlib_util.module_from_spec(_preview_spec)
    _preview_spec.loader.exec_module(_preview_module_helpers)
    png_has_visible_pixels = _preview_module_helpers.png_has_visible_pixels
    write_deterministic_fallback = _preview_module_helpers.write_deterministic_fallback

try:
    import bpy
    from bpy.app.handlers import persistent
    from bpy.types import Operator, Panel, PropertyGroup, UIList
    from bpy_extras.io_utils import ImportHelper
except ImportError:
    def persistent(function):
        return function

    class _DummyProps:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class _DummyBpy:
        props = _DummyProps()

        def __bool__(self):
            return False

    class Operator:
        pass

    class Panel:
        pass

    class PropertyGroup:
        pass

    class UIList:
        pass

    class ImportHelper:
        pass

    bpy = _DummyBpy()


# Official Minecraft pack icons only — avoids branding folders / loose copies
# that ship a random icon.png without being a real resource pack.
PACK_ICON_NAMES = ("pack.png", "pack_icon.png")
PACK_SCAN_CACHE_VERSION = 9
MAX_ARCHIVE_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_ICON_BYTES = 8 * 1024 * 1024
_SUBTITLE_MAX_LEN = 64
_DOUBLE_CLICK_SEC = 0.35

_pack_previews = None
_preview_module = None
_list_click = {"index": -1, "time": 0.0}


def _addon_dir():
    """Return the package directory for this add-on."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cache_dir():
    return os.path.join(_addon_dir(), "mc_asset_cache")


_icon_dir = None


def _pack_icon_cache_dir():
    # Resolved once on Blender's thread (at register): pack folders are read
    # on helper threads, which must not call into bpy.
    global _icon_dir
    if not bpy:
        return os.path.join(_cache_dir(), "pack_icons")
    if _icon_dir is None:
        _icon_dir = bpy.utils.user_resource('DATAFILES', path='mcbedrock_particles/pack_icons', create=True)
    return _icon_dir


_MANUAL_FIELDS = ('name', 'path', 'icon_path', 'uuid', 'kind', 'launcher',
                  'is_archive', 'description', 'author')


def _manual_catalog_path():
    return os.path.join(os.path.dirname(_pack_icon_cache_dir()), 'manual_packs.json')


def _load_manual_catalog(path=None):
    try:
        with open(path or _manual_catalog_path(), encoding='utf-8') as stream:
            entries = json.load(stream).get('packs', [])
        return [entry for entry in entries if isinstance(entry, dict)
                and isinstance(entry.get('path'), str) and entry['path']]
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def _save_manual_catalog(entries, path=None):
    """Atomically save small metadata records, independently of .blend files."""
    import tempfile
    path = path or _manual_catalog_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.manual_packs_', suffix='.json', dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'packs': entries}, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _library_catalog_path():
    # Retire the whole-PC catalog; the startup worker rebuilds this scoped list.
    return os.path.join(os.path.dirname(_manual_catalog_path()), 'installed_packs_scoped.json')


def _excluded_catalog_path():
    return os.path.join(os.path.dirname(_manual_catalog_path()), 'excluded_packs.json')


def _excluded_pack_paths():
    return {_normalized_path(entry['path']) for entry in _load_manual_catalog(_excluded_catalog_path())}


def _repair_java_reset_exclusions(installed_java):
    """Recover installed game assets hidden by the old, overly broad reset."""
    entries = _load_manual_catalog(_excluded_catalog_path())
    remaining = [entry for entry in entries
                 if _normalized_path(entry['path']) not in installed_java
                 or entry.get('reason') == 'manual']
    if len(remaining) != len(entries):
        _save_manual_catalog(remaining, _excluded_catalog_path())
    return {_normalized_path(entry['path']): entry for entry in remaining}


def _repair_installed_pack_exclusions(roots):
    """Recover installed packs hidden by legacy/reset records, not explicit removals."""
    roots = [_normalized_path(item['root']) for item in roots if item.get('root')]
    entries = _load_manual_catalog(_excluded_catalog_path())
    remaining = []
    for entry in entries:
        path = _normalized_path(entry['path'])
        installed = any(path == root or path.startswith(root + os.sep) for root in roots)
        if (entry.get('reason') not in (None, 'reset') or not installed
                or not os.path.exists(entry['path'])):
            remaining.append(entry)
    if len(remaining) != len(entries):
        _save_manual_catalog(remaining, _excluded_catalog_path())


def _prune_excluded_packs(scene, excluded):
    changed = False
    for collection in (scene.mcbedrock_discovered_packs, scene.mcbedrock_manual_packs):
        for index in range(len(collection) - 1, -1, -1):
            if _normalized_path(collection[index].path) in excluded:
                collection.remove(index)
                changed = True
    if changed:
        scene.mcbedrock_discovered_pack_index = -1


def exclude_pack_from_library(path):
    """Persist a library exclusion without touching source files or active packs."""
    key = _normalized_path(path)
    entries = _load_manual_catalog(_excluded_catalog_path())
    entries = [item for item in entries if _normalized_path(item['path']) != key]
    _save_manual_catalog(entries + [{'path': os.path.abspath(path), 'reason': 'manual'}], _excluded_catalog_path())
    for catalog in (_manual_catalog_path(), _library_catalog_path()):
        entries = _load_manual_catalog(catalog)
        filtered = [item for item in entries if _normalized_path(item['path']) != key]
        if len(entries) != len(filtered):
            _save_manual_catalog(filtered, catalog)
    for scene in bpy.data.scenes:
        _prune_excluded_packs(scene, {key})
    _list_click.update(index=-1, time=0.0)


def restore_pack_library(scene):
    collection = scene.mcbedrock_discovered_packs
    excluded = _excluded_pack_paths()
    _prune_excluded_packs(scene, excluded)
    known = {_normalized_path(item.path) for item in collection}
    for meta in _load_manual_catalog(_library_catalog_path()):
        key = _normalized_path(meta['path'])
        if key not in known and key not in excluded:
            _add_meta_to_collection(collection, meta)
            known.add(key)
    restore_manual_packs(scene)


_startup_scan = None
_startup_scan_log = None
# The installed-pack scan runs once per session, when the library is first
# shown (not at every Blender start), and again on Refresh.
_scan_requested = False


def scan_in_progress():
    """True while the installed-pack scan is queued or running."""
    return _startup_scan is not None or bool(bpy and bpy.app.timers.is_registered(_poll_startup_scan))


def ensure_startup_scan():
    """Refresh the installed-pack list once per session; safe to call while drawing."""
    global _scan_requested
    if _scan_requested or not bpy or bpy.app.background:
        return
    _scan_requested = True
    if _startup_scan is None and not bpy.app.timers.is_registered(_poll_startup_scan):
        bpy.app.timers.register(_poll_startup_scan, first_interval=.1, persistent=True)


def _notify(message):
    from .asset_browser import notify_sidebar
    notify_sidebar(message)


def _drop_scan_log():
    global _startup_scan_log
    if _startup_scan_log:
        try:
            os.remove(_startup_scan_log)
        except OSError:
            pass
    _startup_scan_log = None


def _scan_failure_reason():
    """The last error line the scan's background Blender printed, if any."""
    try:
        with open(_startup_scan_log, encoding='utf-8', errors='replace') as stream:
            lines = [line.strip() for line in stream if line.strip()]
    except (OSError, TypeError):
        return ''
    errors = [line for line in lines if re.match(r'^[\w.]*(Error|Exception)\b', line)]
    return (errors or [''])[-1][:200]


def _stop_startup_scan():
    global _startup_scan
    if bpy and bpy.app.timers.is_registered(_poll_startup_scan):
        bpy.app.timers.unregister(_poll_startup_scan)
    if _startup_scan is not None:
        if _startup_scan.poll() is None:
            _startup_scan.kill()
        _startup_scan.wait()
        _startup_scan = None
    _drop_scan_log()


def _poll_startup_scan():
    global _startup_scan, _startup_scan_log
    if _startup_scan is None:
        import subprocess
        import tempfile
        worker = os.path.join(_addon_dir(), 'importers', 'pack_scan_worker.py')
        scene = bpy.context.scene
        scene.mcbedrock_pack_manager_status = 'Finding resource packs…'
        _drop_scan_log()
        try:
            handle, _startup_scan_log = tempfile.mkstemp(prefix='mc_pack_scan_', suffix='.log')
            # The error output is kept so a failed scan can say why.
            with os.fdopen(handle, 'wb') as log:
                _startup_scan = subprocess.Popen([
                    bpy.app.binary_path, '--background', '--factory-startup', '--threads', '1',
                    '--python-exit-code', '1', '--python', worker, '--',
                    str(scene.mcbedrock_scan_depth), str(int(scene.mcbedrock_include_archives)),
                ], stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                                   | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)))
        except OSError as exc:
            _drop_scan_log()
            scene.mcbedrock_pack_manager_status = 'Could not find packs: ' + str(exc)
            _notify('Error: Could not start the resource pack scan: ' + str(exc))
            return None
        return .5
    result = _startup_scan.poll()
    if result is None:
        return .5
    _startup_scan = None
    if result == 0:
        _drop_scan_log()
        for scene in bpy.data.scenes:
            scene.mcbedrock_discovered_packs.clear()
            restore_pack_library(scene)
            scene['_mcbedrock_pack_scan_revision'] = scene.get('_mcbedrock_pack_scan_revision', 0) + 1
            scene.mcbedrock_pack_manager_status = f'{len(scene.mcbedrock_discovered_packs)} packs found'
        # The cleared list freed the rows a drawn sidebar still holds: rebuild first.
        from .asset_browser import refresh_browser_pages
        refresh_browser_pages()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    else:
        reason = _scan_failure_reason()
        _drop_scan_log()
        for scene in bpy.data.scenes:
            scene.mcbedrock_pack_manager_status = 'Pack scan failed; try refreshing again'
        # The list keeps its last packs, so without this nothing would say it failed.
        _notify('Error: The resource pack scan failed' + (': ' + reason if reason else '')
                + '. The list shows the packs found before; try Refresh again.')
        from .asset_browser import refresh_browser_pages
        refresh_browser_pages()
    return None


def _remember_manual_pack(meta):
    _remember_manual_packs([meta])


def _remember_manual_packs(metas):
    """Save manual library entries (and lift their exclusions) in one catalog write."""
    entries = []
    for meta in metas:
        entry = {field: meta.get(field, False if field=='is_archive' else '') for field in _MANUAL_FIELDS}
        entry['launcher'] = 'Manual'
        entry['path'] = os.path.abspath(entry['path'])
        entries.append(entry)
    keys = {_normalized_path(entry['path']) for entry in entries}
    excluded = _load_manual_catalog(_excluded_catalog_path())
    remaining = [old for old in excluded if _normalized_path(old['path']) not in keys]
    if len(remaining) != len(excluded):
        _save_manual_catalog(remaining, _excluded_catalog_path())
    # Newest first, as one entry at a time used to be put in front.
    newest, seen = [], set()
    for entry in reversed(entries):
        key = _normalized_path(entry['path'])
        if key not in seen:
            seen.add(key)
            newest.append(entry)
    older = [old for old in _load_manual_catalog() if _normalized_path(old['path']) not in keys]
    _save_manual_catalog(newest + older)


def restore_manual_packs(scene):
    """Restore the user's manual library without activating or scanning packs."""
    manual = getattr(scene, 'mcbedrock_manual_packs', None)
    discovered = getattr(scene, 'mcbedrock_discovered_packs', None)
    if manual is None or discovered is None:
        return
    excluded = _excluded_pack_paths()
    _prune_excluded_packs(scene, excluded)
    entries = [entry for entry in _load_manual_catalog() if _normalized_path(entry['path']) not in excluded]
    known = {_normalized_path(meta['path']) for meta in entries}
    migrated = False
    for item in list(manual) + list(discovered):
        if item.launcher=='Manual' and _normalized_path(item.path) not in known:
            entries.append({field: getattr(item, field) for field in _MANUAL_FIELDS})
            known.add(_normalized_path(item.path))
            migrated = True
    if migrated:
        _save_manual_catalog(entries)
    for collection in (manual, discovered):
        existing = {_normalized_path(item.path) for item in collection}
        for meta in entries:
            if _normalized_path(meta['path']) not in existing:
                _add_meta_to_collection(collection, dict(meta, launcher='Manual'))
                existing.add(_normalized_path(meta['path']))


def _persist_pack_icon(path):
    """Keep a content-addressed copy outside the replaceable add-on install."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        folder = _pack_icon_cache_dir()
        if _normalized_path(os.path.dirname(path)) == _normalized_path(folder):
            return path
        with open(path, 'rb') as handle:
            payload = handle.read(MAX_ARCHIVE_ICON_BYTES + 1)
        if len(payload) > MAX_ARCHIVE_ICON_BYTES:
            return path
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, hashlib.sha256(payload).hexdigest() + (os.path.splitext(path)[1] or '.png'))
        if not os.path.isfile(target):
            with open(target, 'wb') as handle:
                handle.write(payload)
        return target
    except OSError:
        return path


def restore_pack_icons(scene):
    """Repair saved icon paths, sharing recovery between list and active stack."""
    recovered = {}
    for collection_name in ('mcbedrock_discovered_packs', 'mcbedrock_active_packs'):
        for item in getattr(scene, collection_name, ()):
            source = getattr(item, 'source_path', '') or item.path
            source = bpy.path.abspath(source) if bpy else source
            key = _normalized_path(source)
            if key in recovered:
                item.icon_path = recovered[key]
                continue
            icon = bpy.path.abspath(item.icon_path) if bpy and item.icon_path else item.icon_path
            if not icon or not os.path.isfile(icon):
                meta = (_read_archive_metadata(source) if os.path.isfile(source)
                        else _read_pack_metadata(source) if os.path.isdir(source) else None)
                icon = (meta or {}).get('icon_path', '')
            icon = _persist_pack_icon(icon)
            if icon:
                item.icon_path = icon
                recovered[key] = icon


def _fallback_pack_icon(key):
    """Return a deterministic bitmap for a pack whose official icon is unusable."""
    digest = hashlib.sha1(str(key or "pack").encode("utf-8", "ignore")).hexdigest()
    path = os.path.join(_pack_icon_cache_dir(), "fallback_" + digest + ".png")
    if not png_has_visible_pixels(path):
        write_deterministic_fallback(path, key=key, kind="pack", size=64)
    return path if png_has_visible_pixels(path) else ""


def _ensure_pack_preview(icon_path, key):
    """Keep a usable official icon or replace it with a stable generated one."""
    if icon_path and png_has_visible_pixels(icon_path):
        return _persist_pack_icon(icon_path)
    return _fallback_pack_icon(key)


def _finalize_pack_meta(meta):
    if meta:
        meta['name'] = _pack_display_name(meta.get('name',''), os.path.basename(meta.get('path','').rstrip('\\/')))
        if _logoless_mod(meta):
            # Shown as one of its own models once rendered (mod_pack_icon),
            # never as the generated bitmap.
            meta["icon_path"] = ""
        else:
            meta["icon_path"] = _ensure_pack_preview(meta.get("icon_path", ""), meta.get("path", ""))
    return meta


def is_mod_kind(kind):
    """A mod .jar's archive kind ("java mod resource archive")."""
    return "java mod" in (kind or "").lower()


def is_generated_icon(path):
    """The generated bitmap (``_fallback_pack_icon``) of a pack without a usable icon."""
    return os.path.basename(path or "").startswith("fallback_")


def _is_loader_mod(path):
    try:
        from ..importers.archive_inputs import is_mod_jar
    except ImportError:
        from importers.archive_inputs import is_mod_jar
    return is_mod_jar(path)


def _logoless_mod(meta):
    icon = meta.get("icon_path", "")
    if icon and png_has_visible_pixels(icon):
        return False
    return is_mod_kind(meta.get("kind")) and _is_loader_mod(meta.get("path", ""))


# Mods without a logo show one of their own models, mobs first, as the import
# panel renders it: Alex's Mobs' own creative-tab icon is a mob render too.
# Normalized JAR path -> icon ('' when none could be made); None while making it.
_mod_icons = {}
_mod_icon_jobs = []
MOD_ICON_SIZE = 96  # the saved icon's side, in pixels
MOD_ICON_CANDIDATES = 4  # first mobs rendered: the one that fills a square best is the icon


def mod_pack_icon(item):
    """The icon of a mod without a logo, or '' until it is rendered. Asking
    starts it once: the catalog is read on a helper thread, the render comes
    from the import panel's thumbnail workers, and the result is saved with
    the pack so it shows at once next time."""
    jar = getattr(item, 'source_path', '') or item.path
    jar = bpy.path.abspath(jar) if bpy and jar else jar
    key = _normalized_path(jar)
    if key not in _mod_icons:
        _mod_icons[key] = None
        _start_mod_icon(jar)
    return _mod_icons[key] or ''


def _start_mod_icon(jar):
    import threading
    job = {'jar': jar, 'assets': None, 'asked': 0.0}

    def run():
        keys = []
        try:
            if _is_loader_mod(jar):  # a client JAR is no mod: it keeps the plain tile
                try:
                    from ..importers.mod_jar import mod_catalog
                except ImportError:
                    from importers.mod_jar import mod_catalog
                catalog = mod_catalog(jar)
                assets = catalog.assets if catalog else ()
                # Its first mobs, else its first model, block or item.
                keys = [a.key for a in assets if a.kind == 'entity'][:MOD_ICON_CANDIDATES]
                rank = {'model': 0, 'block': 1, 'item': 2}
                keys = keys or [a.key for a in sorted(assets, key=lambda a: rank.get(a.kind, 9))][:1]
        except Exception as exc:
            print(f"[Sparkle] Could not read {jar} for its icon: {exc}")
        job['assets'] = keys

    job['thread'] = threading.Thread(target=run, daemon=True)
    job['thread'].start()
    _mod_icon_jobs.append(job)
    if not bpy.app.timers.is_registered(_poll_mod_icons):
        bpy.app.timers.register(_poll_mod_icons, first_interval=.5)


def _poll_mod_icons():
    from . import mod_thumbnails
    now = time.monotonic()
    for job in list(_mod_icon_jobs):
        if job['thread'].is_alive():
            continue
        jar = job['jar']
        renders = {key: mod_thumbnails.thumbnail(jar, key, fresh=True) for key in job['assets']}
        waiting = [key for key, path in renders.items()
                   if not path and not mod_thumbnails.failed(jar, key, fresh=True)]
        if waiting and not mod_thumbnails.gave_up():
            if now - job['asked'] > 2.0:
                # The Assets grid may be asking the same workers for its cards:
                # ask again now and then rather than on every tick.
                job['asked'] = now
                mod_thumbnails.request(jar, waiting)
            continue
        icon = _save_mod_icon([path for path in renders.values() if path])
        _mod_icons[_normalized_path(jar)] = icon  # '' keeps the plain tile
        if icon:
            _apply_mod_icon(jar, icon)
        _mod_icon_jobs.remove(job)
    return .5 if _mod_icon_jobs else None


def _save_mod_icon(renders):
    """The render whose model fills a square best, cropped to the model and
    saved small and square in the pack icon folder; '' when there is none."""
    import numpy as np
    best = None
    for path in renders:
        try:
            image = bpy.data.images.load(path, check_existing=False)
        except RuntimeError:
            continue
        try:
            width, height = image.size
            pixels = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels)
        finally:
            bpy.data.images.remove(image)
        pixels = pixels.reshape(height, width, 4)
        alpha = pixels[:, :, 3] > .05
        rows, cols = np.nonzero(alpha.any(axis=1))[0], np.nonzero(alpha.any(axis=0))[0]
        if not len(rows):
            continue
        crop = pixels[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        squareness = min(crop.shape[:2]) / max(crop.shape[:2])  # a flat turtle reads poorly as an icon
        if best is None or squareness > best[0]:
            best = (squareness, crop, path)
    if best is None:
        return ''
    _, crop, path = best
    height, width = crop.shape[:2]
    side = int(max(width, height) * 1.08) + 2  # a little room around the model
    square = np.zeros((side, side, 4), dtype=np.float32)
    top, left = (side - height) // 2, (side - width) // 2
    square[top:top + height, left:left + width] = crop
    try:
        with open(path, 'rb') as stream:
            digest = hashlib.sha1(stream.read()).hexdigest()
        folder = _pack_icon_cache_dir()
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, f"modrender_{digest}.png")
        if os.path.isfile(target):
            return target
        image = bpy.data.images.new('Sparkle mod icon', side, side, alpha=True)
        try:
            image.pixels.foreach_set(square.ravel())
            if side > MOD_ICON_SIZE:
                image.scale(MOD_ICON_SIZE, MOD_ICON_SIZE)
            image.filepath_raw = target
            image.file_format = 'PNG'
            image.save()
        finally:
            bpy.data.images.remove(image)
        return target
    except (OSError, RuntimeError) as exc:
        print(f"[Sparkle] Could not save a mod icon: {exc}")
        return ''


def _apply_mod_icon(jar, icon):
    """Give every row of this mod (library, manual list, enabled stack) its icon, and save it."""
    key = _normalized_path(jar)
    for scene in bpy.data.scenes:
        for name in ('mcbedrock_discovered_packs', 'mcbedrock_manual_packs', 'mcbedrock_active_packs'):
            for item in getattr(scene, name, ()):
                source = getattr(item, 'source_path', '') or item.path
                if source and _normalized_path(bpy.path.abspath(source)) == key:
                    item.icon_path = icon
    for catalog in (_manual_catalog_path(), _library_catalog_path()):
        entries = _load_manual_catalog(catalog)
        changed = False
        for entry in entries:
            if _normalized_path(entry.get('path', '')) == key and entry.get('icon_path') != icon:
                entry['icon_path'] = icon
                changed = True
        if changed:
            _save_manual_catalog(entries, catalog)
    from .asset_browser import refresh_browser_pages
    refresh_browser_pages()


def _stop_mod_icons():
    _mod_icon_jobs.clear()  # helper threads finish on their own; nothing adopts their results
    _mod_icons.clear()
    if bpy and bpy.app.timers.is_registered(_poll_mod_icons):
        bpy.app.timers.unregister(_poll_mod_icons)

def _metadata_cache_path():
    return os.path.join(_cache_dir(), "pack_scan_cache.json")


def _safe_folder_name(value):
    """Make a readable folder name safe for Windows paths."""
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value or "Resource Pack")
    return value.strip(" ._") or "Resource Pack"


def _decode_text(value):
    """Convert Minecraft JSON text fields to a simple display string."""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("translate") or "") + ''.join(
            _decode_text(part) for part in value.get('extra', []) if isinstance(part, (str, dict)))
    if isinstance(value, list):
        return " ".join(_decode_text(part) for part in value).strip()
    
    val = strip_formatting_codes(str(value or ""))
    val = val.replace("\n", " ")
    # Strip non-printable ASCII and control characters
    val = re.sub(r"[\x00-\x1F\x7F]", "", val).strip()
    return val


def _clean_pack_name(name):
    """Strip Minecraft formatting codes, trailing versions, and stray glyphs."""
    if not name:
        return ""
    # Remove Minecraft formatting codes: § followed by one code character.
    name = re.sub(r"§[0-9A-Za-z]", "", name)
    # Remove a trailing version number, e.g. " 1.21.11" or " v1.2.3".
    name = re.sub(r"\s+v?\d+(?:\.\d+)+\s*$", "", name, flags=re.IGNORECASE)
    # "Name - v1.8.1" / "Name - 1.8.1" left after "+" glyphs are stripped.
    name = re.sub(r"\s*[-_]\s*v?\d+(?:\.\d+)+\s*$", "", name, flags=re.IGNORECASE)
    # Keep the plus sign because it is part of real pack titles such as VFX+.
    name = re.sub(r"[\x00-\x1f\x7f]", " ", name)
    # Collapse whitespace, then strip dangling separators (e.g. "FAAll Extensions -").
    name = re.sub(r"\s+", " ", name).strip(" -_")
    return name

_TITLE_AUTHOR_SUFFIX = re.compile(
    r"^(?P<title>.*?)\s+(?:[|\-–—•]\s*)?by\s+"
    r"(?P<author>@?[A-Za-z0-9][A-Za-z0-9_ .'-]{0,47})\s*$",
    re.IGNORECASE,
)


def _pack_display_name(candidate, fallback=""):
    """Return a pack title, never a creator credit or generated cache prefix."""
    raw = _decode_text(candidate or fallback).replace("_", " ")
    # Extracted archives use ``000_title`` directories internally. That index
    # is storage metadata and must never leak into the Resource Pack Manager.
    # Windows/browser duplicate suffixes are not part of the pack title.
    raw = re.sub(r"\s+\(\d+\)\s*$", "", raw)
    raw = re.sub(r"^\d{3}[ _-]+(?=\S)", "", raw)
    clean = _clean_pack_name(raw)
    match = _TITLE_AUTHOR_SUFFIX.match(clean)
    if match and match.group("title").strip(" _-"):
        clean = match.group("title").strip(" _-")
    # A bare "By Creator" is a credit, not a usable title. Prefer the source
    # folder/archive name in that case.
    if re.match(r"^\s*by\s+\S", clean, re.IGNORECASE):
        fallback_clean = _clean_pack_name(_decode_text(fallback).replace("_", " "))
        if fallback_clean and not re.match(r"^\s*by\s+\S", fallback_clean, re.IGNORECASE):
            clean = fallback_clean
    return clean or _clean_pack_name(_decode_text(fallback).replace("_", " ")) or raw



def _unsupported_archive_reason(filepath):
    ext = os.path.splitext(str(filepath or "").lower())[1]
    if ext == ".mcproject":
        return (
            ".mcproject is a project container, not a resource pack. "
            "Export its resource pack as .mcpack first."
        )
    if ext == ".mcstructure":
        return (
            ".mcstructure contains world structure data, not a resource pack, "
            "and cannot be activated here."
        )
    return ""


def _archive_kind_label(filepath, resource_kind=""):
    ext = os.path.splitext(str(filepath or "").lower())[1]
    labels = {
        ".mcpack": "bedrock resource pack archive",
        ".mcaddon": "bedrock add-on archive",
        ".mcworld": "bedrock world archive",
        ".mctemplate": "bedrock world template archive",
        ".jar": "java mod resource archive",
        ".mcproject": "unsupported minecraft project",
        ".mcstructure": "unsupported minecraft structure",
    }
    if ext == ".zip" and resource_kind:
        return f"{resource_kind} archive"
    return labels.get(ext, "archive")


def _edition_label(kind):
    """Short archive/edition badge for the selected-pack card."""
    value = (kind or "").strip().lower()
    if "unsupported" in value:
        return "Unsupported"
    if "add-on" in value:
        return "Bedrock Add-On"
    if "world template" in value:
        return "Bedrock Template"
    if "bedrock world" in value:
        return "Bedrock World"
    if "bedrock" in value:
        return "Bedrock"
    if "java mod" in value:
        return "Mod"
    if "java" in value:
        return "Java"
    if "archive" in value:
        return "Archive"
    return ""


def _pack_matches_search(item, query):
    """True when item matches a case-insensitive pack list search query."""
    query = (query or "").strip().lower()
    if not query:
        return True
    haystack = " ".join(
        (
            getattr(item, "name", "") or "",
            getattr(item, "author", "") or "",
            getattr(item, "kind", "") or "",
            os.path.basename(getattr(item, "path", "") or ""),
        )
    ).lower()
    return query in haystack


def _is_active_pack(context, item):
    """True when this discovered pack is anywhere in the active stack."""
    path = getattr(item, "path", "") or ""
    if not path:
        return False
    try:
        wanted = os.path.normcase(os.path.abspath(path))
    except OSError:
        return False
    active_packs = getattr(context.scene, "mcbedrock_active_packs", ())
    for active in active_packs:
        source = getattr(active, "source_path", "") or getattr(active, "path", "")
        if not source:
            continue
        try:
            if os.path.normcase(os.path.abspath(source)) == wanted:
                return True
        except OSError:
            continue
    # Old scenes and partial registrations may only have the legacy property.
    active = getattr(context.scene, "mcbedrock_active_pack_path", "") or ""
    if not active:
        return False
    try:
        return os.path.normcase(os.path.abspath(active)) == wanted
    except OSError:
        return False


def _normalized_path(path):
    """Stable absolute-path key used to deduplicate stack entries."""
    if not path:
        return ""
    # The sidebar asks for every listed pack on each rebuild. An absolute path's
    # key never changes; a relative one depends on the working folder.
    if isinstance(path, str) and os.path.isabs(path):
        return _normalized_absolute(path)
    try:
        return os.path.normcase(os.path.abspath(path))
    except OSError:
        return os.path.normcase(str(path))


@lru_cache(maxsize=8192)
def _normalized_absolute(path):
    try:
        return os.path.normcase(os.path.abspath(path))
    except OSError:
        return os.path.normcase(path)


def active_pack_roots(scene, include_legacy=True):
    """Return enabled pack roots in priority order (index zero wins).

    An archive bundle can extract to a container holding several resource-pack
    roots. Those roots remain adjacent at that bundle's priority position.
    ``include_legacy`` keeps older files and callers useful until a stack entry
    has been created.
    """
    roots = []
    seen = set()

    def _append(root):
        if not root:
            return
        try:
            root = os.path.abspath(root)
        except OSError:
            return
        if not os.path.isdir(root):
            return
        key = _normalized_path(root)
        if key and key not in seen:
            seen.add(key)
            roots.append(root)

    for item in getattr(scene, "mcbedrock_active_packs", ()):
        root = getattr(item, "path", "") or ""
        if not root or not os.path.isdir(root):
            continue
        nested = list(_iter_pack_roots(root, max_depth=4))
        if nested and not _has_pack_metadata(root):
            for pack_root in nested:
                _append(pack_root)
        else:
            _append(root)

    if not roots and include_legacy:
        _append(getattr(scene, "mcbedrock_resource_pack_path", "") or "")
    return roots


def _refresh_asset_browser(context):
    """Refresh the legacy browser after the highest-priority pack changes."""
    try:
        bpy.ops.mcbedrock.asset_browser_refresh("EXEC_DEFAULT")
        return len(getattr(context.scene, "mcbedrock_browser_assets", ()))
    except Exception:
        return 0


def sync_legacy_pack_paths(context, refresh_browser=True, defer_browser=False):
    """Mirror active index zero into the single-pack compatibility properties."""
    scene = context.scene
    active_packs = getattr(scene, "mcbedrock_active_packs", ())
    top = active_packs[0] if active_packs else None
    root = getattr(top, "path", "") if top is not None else ""
    source = getattr(top, "source_path", "") if top is not None else ""
    root = os.path.abspath(root) if root else ""
    source = source or root

    if hasattr(scene, "mcbedrock_resource_pack_path"):
        scene.mcbedrock_resource_pack_path = root
    if hasattr(scene, "mcbedrock_browser_root"):
        browser_root = scene.mcbedrock_browser_root
        if not any(_normalized_path(p.path) == _normalized_path(browser_root) for p in active_packs):
            scene.mcbedrock_browser_root = root
            refresh_browser = True
    if hasattr(scene, "mcbedrock_active_pack_path"):
        scene.mcbedrock_active_pack_path = source

    count = _refresh_asset_browser(context) if refresh_browser and root and not defer_browser else 0
    label = getattr(top, "name", "") if top is not None else ""
    if hasattr(scene, "mcbedrock_pack_manager_status"):
        if top is None:
            scene.mcbedrock_pack_manager_status = "No active resource packs"
        else:
            scene.mcbedrock_pack_manager_status = (
                "Active: %s" % (label or os.path.basename(root.rstrip("\\/")))
                + ((" (%d assets)" % count) if count else "")
            )
    return count


def resolve_active_browser_root(item):
    """Recover a saved active pack from its source when its cache is missing."""
    from types import SimpleNamespace
    root = bpy.path.abspath(item.path) if item.path else ""
    if root and os.path.isdir(root):
        return root
    source = bpy.path.abspath(item.source_path) if item.source_path else root
    if source and os.path.exists(source):
        root = _resolve_pack_root_for_item(SimpleNamespace(
            path=source, is_archive=item.is_archive or os.path.isfile(source)))
        if root and os.path.isdir(root):
            item.path = root
            return root
    raise RuntimeError("Resource pack source is unavailable: %s" % (source or item.name))


def _extract_author(data):
    """Best-effort author string from a Bedrock/Java pack metadata dict."""
    if not isinstance(data, dict):
        return ""
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    pack = data.get("pack") if isinstance(data.get("pack"), dict) else {}
    candidates = (
        meta.get("authors"),
        meta.get("author"),
        meta.get("contributors"),
        data.get("authors"),
        data.get("author"),
        data.get("credits"),
        header.get("author"),
        header.get("authors"),
        pack.get("author"),
        pack.get("authors"),
    )
    for authors in candidates:
        if isinstance(authors, list):
            names = [_decode_text(part) for part in authors if part]
            joined = ", ".join(name for name in names if name)
            if joined:
                return joined
        if isinstance(authors, str) and authors.strip():
            return _decode_text(authors)
    return ""


_AUTHOR_ONLY_DESC = re.compile(r"^\s*by\s+([^|\n]{1,48})\s*$", re.IGNORECASE)
_AUTHOR_TRAILING = re.compile(
    r"(?:^|[|\-–—•]\s*)by\s+([A-Za-z0-9][A-Za-z0-9_ .'-]{0,39})\s*$",
    re.IGNORECASE,
)


def _author_from_description(description):
    """Pull a short author credit out of a description when that is all it is."""
    if not description:
        return ""
    lines = [ln.strip() for ln in str(description).replace("\r", "\n").split("\n") if ln.strip()]
    for line in lines:
        # Strip Minecraft formatting before matching.
        clean = re.sub(r"§.", "", line).strip()
        clean = clean.replace("\u25a0", "|")
        match = _AUTHOR_ONLY_DESC.match(clean)
        if match:
            name = match.group(1).strip()
            if name and len(name.split()) <= 5:
                return name
    # Last line / trailing "| by Name" credit on an otherwise short blurb.
    if lines:
        clean = re.sub(r"§.", "", lines[-1]).strip()
        clean = clean.replace("\u25a0", "|")
        match = _AUTHOR_TRAILING.search(clean)
        if match:
            name = match.group(1).strip(" .-_|")
            if name and len(name.split()) <= 5 and len(clean) <= 56:
                return name
    return ""


def _normalize_author(author):
    author = (author or "").strip()
    if author.lower().startswith("by "):
        author = author[3:].strip()
    return author


def _pack_subtitle(item):
    """Selected-pack secondary line: author only ('By Name'). Never show description."""
    author = _normalize_author(getattr(item, "author", "") or "")
    if not author:
        author = _normalize_author(
            _author_from_description(getattr(item, "description", "") or "")
        )
    if not author:
        return ""
    text = f"By {author}"
    if len(text) > _SUBTITLE_MAX_LEN:
        text = text[: _SUBTITLE_MAX_LEN - 1].rstrip() + "…"
    return text


def _pack_name_key(item):
    return re.sub(r'[^a-z0-9]+', '', (item.name or os.path.basename(item.path)).casefold())


def _pack_identity_hint(item, peers):
    """Show source details only when visible pack names can be confused."""
    key=_pack_name_key(item)
    return _identity_hint_among(item,[other for other in peers if _pack_name_key(other)==key])


# The last answer of _pack_identity_hints and the names and paths it came from.
_identity_hints_cache = [None, None]


def _pack_identity_hints(items, peers):
    """Hints for a whole list. The sidebar asks on every rebuild, so the last
    answer is kept until a listed pack's name or path changes."""
    key=(tuple((item.name,item.path) for item in items),tuple((other.name,other.path) for other in peers))
    if _identity_hints_cache[0]!=key:
        _identity_hints_cache[:]=[key,_compute_identity_hints(items,peers)]
    return dict(_identity_hints_cache[1])


def _compute_identity_hints(items, peers):
    """One name-key pass, not one per pair of packs."""
    groups={}
    for other in peers:
        groups.setdefault(_pack_name_key(other),[]).append(other)
    return {item.path:_identity_hint_among(item,groups.get(_pack_name_key(item),())) for item in items}


def _identity_hint_among(item, same):
    if len(same)<2:return ''
    details=list(_pack_identity_hint_base(item))
    if not details or all(_pack_identity_hint_base(other)==tuple(details) for other in same):
        details.append(os.path.splitext(os.path.basename(item.path))[0])
    return ' · '.join(dict.fromkeys(details))


def _pack_identity_hint_base(item):
    path=os.path.normpath(item.path)
    stem=os.path.splitext(os.path.basename(path))[0]
    version=re.search(r'(?<!\d)(?:1\.\d{1,2}|2\d\.\d{1,2})(?:\.\d{1,2})?(?!\d)',stem)
    parts=path.replace('\\','/').split('/')
    folders=[part.casefold() for part in parts]
    index=next((i for i in range(len(parts)-2,-1,-1) if folders[i]=='resourcepacks'),-1)
    source=parts[index-1] if index>0 else parts[-2] if len(parts)>1 else ''
    if source.casefold() in {'.minecraft','minecraft','game','packs'} and index>1:
        source=parts[index-2]
    if source.casefold() in {'resourcepacks','.minecraft','minecraft'}:source=''
    return tuple(detail for detail in (version.group() if version else '',source) if detail)


def _has_pack_metadata(folder):
    """Return True when a folder looks like a Java or Bedrock pack root."""
    return (
        os.path.isfile(os.path.join(folder, "manifest.json"))
        or os.path.isfile(os.path.join(folder, "pack.mcmeta"))
    )


def _has_pack_icon(folder):
    """True when the folder has a Minecraft pack icon (filters branding/datapacks)."""
    for icon_name in PACK_ICON_NAMES:
        if os.path.isfile(os.path.join(folder, icon_name)):
            return True
    return False


def _is_listable_pack_folder(folder):
    """Pack roots are defined by Minecraft metadata; artwork is optional."""
    return _has_pack_metadata(folder)


def _archive_has_pack_icon(names):
    return any(os.path.basename(name).lower() in PACK_ICON_NAMES for name in names)


def _iter_pack_roots(root, max_depth=5):
    """Yield pack folders under a root without walking too deep."""
    if not root or not os.path.isdir(root):
        return
    root = os.path.abspath(root)
    if _has_pack_metadata(root):
        yield root
        return
    for current, dirs, files in os.walk(root):
        rel = os.path.relpath(current, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [
            d for d in dirs
            if d.lower() not in {
                "__macosx", ".git", "node_modules", "__pycache__",
                "server-resource-packs", "server_resource_packs",
                "behavior_packs", "development_behavior_packs",
                "world_behavior_packs", "world_resource_packs"
            }
        ]
        if max_depth is not None and depth >= max_depth:
            dirs[:] = []
        if "manifest.json" in files or "pack.mcmeta" in files:
            yield current
            dirs[:] = []


def _read_pack_metadata(folder):
    """Read display metadata from a Java or Bedrock resource-pack folder."""
    folder = os.path.abspath(folder)
    name = os.path.basename(folder.rstrip("\\/"))
    pack_uuid = ""
    icon_path = ""
    kind = "folder"
    pack_desc = ""
    pack_author = ""

    manifest_path = os.path.join(folder, "manifest.json")
    if os.path.isfile(manifest_path):
        kind = "bedrock"
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                
            modules = data.get("modules", [])
            module_types = {
                str(module.get("type", "")).casefold()
                for module in modules
                if isinstance(module, dict)
            }
            if not module_types.intersection({"resources", "client_data"}):
                return {}
                
            header = data.get("header", {}) if isinstance(data, dict) else {}

            pack_name = _decode_text(header.get("name"))
            pack_desc = _decode_text(header.get("description"))
            # Bedrock manifests commonly reference keys in texts/en_US.lang.
            try:
                with open(os.path.join(folder, 'texts', 'en_US.lang'), encoding='utf-8-sig') as stream:
                    translations = dict(line.split('#', 1)[0].strip().split('=', 1) for line in stream
                                        if '=' in line and not line.lstrip().startswith('#'))
                pack_name = translations.get(pack_name, pack_name)
                pack_desc = translations.get(pack_desc, pack_desc)
            except (OSError, ValueError):
                pass
            pack_author = _extract_author(data)

            pack_name = sanitize_pack_name(pack_name)
            name = pack_name or name

            pack_uuid = str(header.get("uuid") or "")
        except Exception:
            return {}

    pack_meta = os.path.join(folder, "pack.mcmeta")
    if os.path.isfile(pack_meta):
        kind = "java"
        try:
            with open(pack_meta, "r", encoding="utf-8-sig", errors="ignore") as handle:
                data = json.load(handle)
            pack = data.get("pack", {}) if isinstance(data, dict) else {}

            pack_desc = _decode_text(pack.get("description"))
            if not pack_author:
                pack_author = _extract_author(data) or _extract_author(pack)

            name = sanitize_pack_name(name) or name
        except Exception:
            pass

    for icon_name in PACK_ICON_NAMES:
        candidate = os.path.join(folder, icon_name)
        if os.path.isfile(candidate):
            icon_path = candidate
            break

    if not pack_author:
        pack_author = _author_from_description(pack_desc)
    pack_author = _normalize_author(pack_author)

    return _finalize_pack_meta({
        "name": _pack_display_name(name, os.path.basename(folder.rstrip("\\/"))),
        "uuid": pack_uuid,
        "icon_path": icon_path,
        "kind": kind,
        "path": folder,
        "is_archive": False,
        "description": pack_desc,
        "author": pack_author,
    })


def _read_zip_json(zip_file, name):
    try:
        info = zip_file.getinfo(name)
        if info.file_size > MAX_ARCHIVE_METADATA_BYTES:
            return {}
        with zip_file.open(info, "r") as handle:
            raw = handle.read(MAX_ARCHIVE_METADATA_BYTES + 1)
        if len(raw) > MAX_ARCHIVE_METADATA_BYTES:
            return {}
        data = json.loads(raw.decode("utf-8-sig", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cache_archive_icon(filepath, zip_file, names):
    icon_name = next(
        (name for name in names if os.path.basename(name).lower() in PACK_ICON_NAMES),
        "",
    )
    if not icon_name:
        return ""
    try:
        info = zip_file.getinfo(icon_name)
        if info.file_size <= 0 or info.file_size > MAX_ARCHIVE_ICON_BYTES:
            return ""
        stat = os.stat(filepath)
        key = f"{os.path.abspath(filepath)}|{stat.st_mtime_ns}|{stat.st_size}|{icon_name}"
        digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()
        ext = os.path.splitext(icon_name)[1] or ".png"
        os.makedirs(_pack_icon_cache_dir(), exist_ok=True)
        cached = os.path.join(_pack_icon_cache_dir(), f"{digest}{ext}")
        if not os.path.isfile(cached):
            with zip_file.open(info, "r") as source, open(cached, "wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 256)
        return cached
    except Exception:
        return ""


def _inspect_resource_archive(filepath):
    """Use the shared recursive inspector when loaded as an add-on or in tests."""
    try:
        from ..importers.archive_extractor import inspect_archive
    except (ImportError, ValueError):
        try:
            from importers.archive_extractor import inspect_archive
        except ImportError:
            return None
    return inspect_archive(filepath, include_icons=True)


def _cache_archive_icon_bytes(filepath, icon_name, payload):
    if not payload or len(payload) > MAX_ARCHIVE_ICON_BYTES:
        return ""
    try:
        info = os.stat(filepath)
        key = (
            f"{os.path.abspath(filepath)}|{info.st_mtime_ns}|{info.st_size}|"
            f"{icon_name}"
        )
        digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()
        ext = os.path.splitext(icon_name)[1].lower()
        if ext not in {".png", ".jpg", ".jpeg"}:
            ext = ".png"
        os.makedirs(_pack_icon_cache_dir(), exist_ok=True)
        cached = os.path.join(_pack_icon_cache_dir(), f"{digest}{ext}")
        if not os.path.isfile(cached):
            with open(cached, "wb") as handle:
                handle.write(payload)
        return cached
    except Exception:
        return ""


def _read_archive_metadata(filepath):
    """Read display metadata from an archive without extracting it."""
    name = os.path.splitext(os.path.basename(filepath))[0]
    pack_uuid = ""
    kind = "archive"
    icon_path = ""
    pack_desc = ""
    pack_author = ""
    rejection = _unsupported_archive_reason(filepath)
    if rejection:
        return _finalize_pack_meta({
            "name": _clean_pack_name(name.replace("_", " ")) or name,
            "uuid": "",
            "icon_path": "",
            "kind": _archive_kind_label(filepath),
            "path": os.path.abspath(filepath),
            "is_archive": True,
            "description": rejection,
            "author": "",
            "unsupported": True,
        })

    inspection = _inspect_resource_archive(filepath)
    if inspection is not None:
        packs = inspection.get("resource_packs", [])
        if not packs:
            return {}
        first = packs[0]
        manifest = first.get("manifest", {})
        pack_name = sanitize_pack_name(_decode_text(first.get("name", "")))
        pack_desc = _decode_text(first.get("description", ""))
        pack_author = _extract_author(manifest)
        if not pack_author:
            pack_author = _author_from_description(pack_desc)
        pack_uuid = str(first.get("uuid") or "")
        kind = _archive_kind_label(filepath, first.get("kind", ""))
        icon_path = _cache_archive_icon_bytes(
            filepath,
            first.get("icon_name", ""),
            first.get("icon_bytes", b""),
        )
        # Java's pack.mcmeta has a description but no title field. Use the
        # archive filename as its actual pack name; the description/credit is
        # secondary metadata only.
        name = pack_name or name
        pack_author = _normalize_author(pack_author)
        return _finalize_pack_meta({
            "name": _pack_display_name(name, os.path.splitext(os.path.basename(filepath))[0]),
            "uuid": pack_uuid,
            "icon_path": icon_path,
            "kind": kind,
            "path": os.path.abspath(filepath),
            "is_archive": True,
            "description": pack_desc,
            "author": pack_author,
            "resource_pack_count": len(packs),
        })

    try:
        with zipfile.ZipFile(filepath, "r") as zip_file:
            names = zip_file.namelist()
            icon_path = _cache_archive_icon(filepath, zip_file, names)
            manifest_name = next((n for n in names if n.lower().endswith("manifest.json")), "")
            pack_meta_name = next((n for n in names if n.lower().endswith("pack.mcmeta")), "")
            # Name comes from the archive filename only; still read the manifest
            # UUID for duplicate detection and set the kind label.
            if manifest_name:
                try:
                    data = _read_zip_json(zip_file, manifest_name)

                    modules = data.get("modules", []) if isinstance(data, dict) else []
                    if any(isinstance(m, dict) and m.get("type") in ("data", "world_template", "skin_pack") for m in modules):
                        return {}

                    header = data.get("header", {}) if isinstance(data, dict) else {}

                    pack_name = _decode_text(header.get("name"))
                    pack_desc = _decode_text(header.get("description"))
                    pack_author = _extract_author(data)

                    pack_name = sanitize_pack_name(pack_name)
                    name = pack_name or name

                    pack_uuid = str(header.get("uuid") or "")
                    kind = "bedrock archive"
                except Exception:
                    pass
            elif pack_meta_name:
                try:
                    data = _read_zip_json(zip_file, pack_meta_name)
                    pack = data.get("pack", {}) if isinstance(data, dict) else {}

                    pack_desc = _decode_text(pack.get("description"))
                    pack_author = _extract_author(data) or _extract_author(pack)

                    name = sanitize_pack_name(name) or name
                    kind = "java archive"
                except Exception:
                    pass
    except Exception:
        pass
    if not pack_author:
        pack_author = _author_from_description(pack_desc)
    pack_author = _normalize_author(pack_author)
    return _finalize_pack_meta({
        "name": _pack_display_name(name, os.path.splitext(os.path.basename(filepath))[0]),
        "uuid": pack_uuid,
        "icon_path": icon_path,
        "kind": kind,
        "path": os.path.abspath(filepath),
        "is_archive": True,
        "description": pack_desc,
        "author": pack_author,
    })


def _load_scan_cache():
    try:
        with open(_metadata_cache_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("version") == PACK_SCAN_CACHE_VERSION:
            return data.get("entries", {}) if isinstance(data.get("entries"), dict) else {}
    except Exception:
        pass
    return {}


def _save_scan_cache(cache):
    try:
        os.makedirs(os.path.dirname(_metadata_cache_path()), exist_ok=True)
        with open(_metadata_cache_path(), "w", encoding="utf-8") as handle:
            json.dump({"version": PACK_SCAN_CACHE_VERSION, "entries": cache}, handle, indent=2)
    except Exception:
        pass


def _path_signature(path):
    try:
        stat = os.stat(path)
        return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    except OSError:
        return {"mtime_ns": 0, "size": 0}


def _cached_metadata(path, is_archive, cache):
    key = os.path.normcase(os.path.abspath(path))
    signature = _path_signature(path)
    cached = cache.get(key)
    if (cached and cached.get("signature") == signature and isinstance(cached.get("meta"), dict)
            and os.path.isfile(cached["meta"].get("icon_path", ""))):
        return _finalize_pack_meta(dict(cached["meta"]))
    meta = _read_archive_metadata(path) if is_archive else _read_pack_metadata(path)
    cache[key] = {"signature": signature, "meta": dict(meta)}
    return meta


def _first_pack_root(folder):
    """Return the first pack root inside an extracted archive."""
    if _has_pack_metadata(folder):
        return folder
    for pack_root in _iter_pack_roots(folder, max_depth=4):
        return pack_root
    return folder


def _preview_icon_id(path):
    """Load a pack.png into Blender's preview collection; return icon_id or 0."""
    global _pack_previews, _preview_module
    if not bpy or not path:
        return 0
    # FILE_PATH properties / blend-relative strings need resolving first.
    try:
        path = bpy.path.abspath(path)
    except Exception:
        path = os.path.expanduser(path)
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        return 0
    try:
        if _preview_module is None:
            import bpy.utils.previews as previews
            _preview_module = previews
        if _pack_previews is None:
            _pack_previews = _preview_module.new()
        key = os.path.normcase(os.path.abspath(path))
        if key not in _pack_previews:
            _pack_previews.load(key, path, "IMAGE")
        icon_id = _pack_previews[key].icon_id
        # A 0 id usually means a prior failed load — force one reload.
        if not icon_id:
            try:
                del _pack_previews[key]
            except Exception:
                pass
            _pack_previews.load(key, path, "IMAGE")
            icon_id = _pack_previews[key].icon_id
        return icon_id or 0
    except Exception:
        return 0


def _activate_pack(context, pack_root, name="", source_path=""):
    """Compatibility entry point: replace the stack with one active pack."""
    pack_root = os.path.abspath(pack_root)
    meta = _read_pack_metadata(pack_root) or {'name': name, 'path': pack_root}
    meta['path'] = source_path or pack_root
    meta['is_archive'] = os.path.isfile(meta['path'])
    _remember_manual_pack(meta)
    restore_manual_packs(context.scene)
    active_packs = getattr(context.scene, "mcbedrock_active_packs", None)
    if active_packs is not None:
        active_packs.clear()
        active = active_packs.add()
        active.name = name or os.path.basename(pack_root.rstrip("\\/"))
        active.path = pack_root
        active.source_path = source_path or pack_root
        context.scene.mcbedrock_active_pack_index = 0
        count = sync_legacy_pack_paths(context, refresh_browser=True)
    else:
        context.scene.mcbedrock_resource_pack_path = pack_root
        context.scene.mcbedrock_browser_root = pack_root
        if source_path:
            try:
                context.scene.mcbedrock_active_pack_path = os.path.abspath(source_path)
            except OSError:
                context.scene.mcbedrock_active_pack_path = source_path
        count = _refresh_asset_browser(context)
    label = name or os.path.basename(pack_root.rstrip("\\/"))
    context.scene.mcbedrock_pack_manager_status = (
        f"Active: {label}" + (f" ({count} assets)" if count else "")
    )
    return count


def set_single_active_pack(context, pack_root, name="", source_path=""):
    """Public compatibility helper for legacy folder-picker operators."""
    return _activate_pack(context, pack_root, name=name, source_path=source_path)


def initialize_active_pack_state(context=None):
    """Restore legacy path mirrors after registration or loading a saved file."""
    if not bpy:
        return 0
    context = context or getattr(bpy, "context", None)
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        return 0
    # Older blend files can retain filters removed from the browser menu.
    if scene.get('mcbedrock_browser_filter') in {3, 6, 7, 8, 9}:
        scene.mcbedrock_browser_filter = 'IMPORTABLE'
    restore_pack_library(scene)
    restore_pack_icons(scene)
    if not getattr(scene, "mcbedrock_active_packs", None):
        return 0
    return sync_legacy_pack_paths(context, refresh_browser=False, defer_browser=True)


@persistent
def _sync_active_pack_on_load(_unused):
    """Keep saved stacks mirrored after opening another blend file."""
    try:
        initialize_active_pack_state()
    except Exception:
        pass


def _remove_active_pack_load_handlers():
    """Remove current or stale reload copies of our load-post handler."""
    if not bpy:
        return
    for handler in list(bpy.app.handlers.load_post):
        if handler is _sync_active_pack_on_load or (
            getattr(handler, "__name__", "") == "_sync_active_pack_on_load"
            and getattr(handler, "__module__", "") == __name__
        ):
            try:
                bpy.app.handlers.load_post.remove(handler)
            except ValueError:
                pass


def _extracted_pack_root(folder):
    """Return one pack root, or the shared container when it holds several."""
    if not folder or not os.path.isdir(folder):
        return ""
    # JAR extraction intentionally keeps assets only, without pack.mcmeta.
    container = os.path.join(folder, 'resource_packs')
    if os.path.isdir(container):
        java_roots = [os.path.join(container, name) for name in os.listdir(container)
                      if os.path.isfile(os.path.join(container, name, '.java_mod_resource.json'))
                      and os.path.isdir(os.path.join(container, name, 'assets'))]
        if len(java_roots) == 1:
            return java_roots[0]
    roots = list(_iter_pack_roots(folder, max_depth=4))
    if len(roots) == 1:
        return roots[0]
    return folder if roots else ""


def _resolve_pack_root_for_item(item):
    """Resolve a discovered pack to an on-disk root, without copying it.

    Archives are unpacked into the read cache first (a zip cannot be read in
    place); folder packs are used exactly where they already live.
    """
    from ..importers.archive_extractor import get_cache_dir

    if item.is_archive:
        job = _preparations.get(_normalized_path(item.path))
        if job is not None:
            # A helper thread is extracting this archive into the same folder:
            # wait for it rather than extracting over it.
            job['thread'].join()
        return _extract_pack_root(item.path, get_cache_dir(_addon_dir()))
    return _first_pack_root(item.path)


def _extract_pack_root(path, cache_dir):
    """Unpack an archive pack into the read cache; safe on a helper thread."""
    from ..importers.archive_inputs import extract_archive_result
    result = extract_archive_result(path, cache_dir)
    if not result.get("output_path"):
        raise OSError(result.get("error") or "Archive extraction failed")
    return _extracted_pack_root(result["output_path"])


def _archive_needs_extraction(path):
    """True for an archive whose extracted copy is missing: opening it takes a while."""
    if not path or not os.path.isfile(path):
        return False  # gone: the caller reports it straight away
    from ..importers.archive_extractor import get_cache_dir
    from ..importers.archive_inputs import cached_archive_output
    try:
        return cached_archive_output(path, get_cache_dir(_addon_dir())) is None
    except OSError:
        return False


# Archive packs being extracted on helper threads, by normalized archive path.
# A 30 MB game JAR is ~11k files: seconds that used to freeze Blender.
_preparations = {}


def archive_preparing(path):
    """True while ``path`` (an archive pack) is being extracted."""
    return bool(path) and _normalized_path(path) in _preparations


def prepare_archive(path, callback):
    """Extract an archive pack on a helper thread.

    ``callback(root, error)`` runs later on Blender's thread. Requests for an
    archive that is already being extracted share that extraction.
    """
    import threading
    from ..importers.archive_extractor import get_cache_dir
    key = _normalized_path(path)
    job = _preparations.get(key)
    if job is None:
        cache_dir = get_cache_dir(_addon_dir())
        job = {'callbacks': [], 'result': None}

        def run():
            try:
                job['result'] = (_extract_pack_root(path, cache_dir), None)
            except Exception as exc:
                job['result'] = (None, exc)

        job['thread'] = threading.Thread(target=run, daemon=True)
        _preparations[key] = job
        job['thread'].start()
        if not bpy.app.timers.is_registered(_poll_preparations):
            bpy.app.timers.register(_poll_preparations, first_interval=.1, persistent=True)
    job['callbacks'].append(callback)


def _poll_preparations():
    finished = [key for key, job in _preparations.items() if not job['thread'].is_alive()]
    for key in finished:
        job = _preparations.pop(key)
        root, error = job['result'] or (None, RuntimeError('the extraction stopped'))
        for callback in job['callbacks']:
            try:
                callback(root, error)
            except Exception:
                import traceback
                traceback.print_exc()
    if finished:
        from .asset_browser import refresh_browser_pages
        refresh_browser_pages()
    return .1 if _preparations else None


def _stop_preparations():
    _preparations.clear()  # threads finish on their own; nobody adopts their results
    if bpy and bpy.app.timers.is_registered(_poll_preparations):
        bpy.app.timers.unregister(_poll_preparations)


def pack_identity(item):
    """Stable key for an enabled pack (its source), for page state."""
    return _normalized_path(getattr(item, 'source_path', '') or getattr(item, 'path', ''))


def needs_preparation(item):
    """True when an enabled archive pack's extracted copy is gone and must be rebuilt."""
    root = bpy.path.abspath(item.path) if item.path else ''
    if root and os.path.isdir(root):
        return False
    source = bpy.path.abspath(item.source_path) if item.source_path else ''
    return _archive_needs_extraction(source)


def prepare_active_pack(item, on_done=None):
    """Re-extract an enabled archive pack in the background, then point it at the result."""
    source = bpy.path.abspath(item.source_path)
    key = _normalized_path(source)

    def done(root, error):
        if error is None and root:
            for scene in bpy.data.scenes:
                for active in getattr(scene, 'mcbedrock_active_packs', ()):
                    if _normalized_path(bpy.path.abspath(active.source_path or active.path)) == key:
                        active.path = root
        if on_done is not None:
            on_done(root, error)

    prepare_archive(source, done)


# Whether listed packs still exist, re-checked at most every two seconds: rows
# and carousels ask on every draw.
_EXISTS = {}
_EXISTS_SECONDS = 2.0


def _exists_cached(path):
    now = time.monotonic()
    entry = _EXISTS.get(path)
    if entry is not None and now - entry[0] < _EXISTS_SECONDS:
        return entry[1]
    exists = os.path.exists(path)
    if len(_EXISTS) > 4096:
        _EXISTS.clear()
    _EXISTS[path] = (now, exists)
    return exists


def path_missing(path, fresh=False):
    """True when a listed pack's folder or file is gone (``fresh`` skips the 2 s memory)."""
    if not path:
        return False
    path = bpy.path.abspath(path) if bpy else path
    if fresh:
        _EXISTS.pop(path, None)
    return not _exists_cached(path)


def active_pack_missing(item, fresh=False):
    """True when neither an enabled pack's folder nor its source file exists any more."""
    path = getattr(item, 'path', '') or ''
    return path_missing(path, fresh) and path_missing(getattr(item, 'source_path', '') or path, fresh)


def _add_meta_to_collection(collection, meta):
    """Append one discovered pack metadata record to the Blender collection."""
    item = collection.add()
    item.selected = False  # Nothing checked by default; user opts in per pack.
    item.name = meta.get("name") or ""
    item.description = meta.get("description") or ""
    item.author = meta.get("author") or ""
    item.path = meta.get("path") or ""
    item.icon_path = meta.get("icon_path", "")
    item.uuid = meta.get("uuid", "")
    item.kind = meta.get("kind", "") or ""
    item.launcher = meta.get("launcher", meta.get("kind", ""))
    item.is_archive = bool(meta.get("is_archive", False))
    item.status = "New"


class MCBEDROCK_PG_discovered_pack(PropertyGroup):
    selected: bpy.props.BoolProperty(default=False)
    name: bpy.props.StringProperty(default="")
    description: bpy.props.StringProperty(default="")
    author: bpy.props.StringProperty(default="")
    path: bpy.props.StringProperty(default="", subtype="FILE_PATH")
    icon_path: bpy.props.StringProperty(default="")
    status: bpy.props.StringProperty(default="New")
    uuid: bpy.props.StringProperty(default="")
    kind: bpy.props.StringProperty(default="")
    launcher: bpy.props.StringProperty(default="")
    is_archive: bpy.props.BoolProperty(default=False)


class MCBEDROCK_PG_active_pack(PropertyGroup):
    """One enabled resource pack; collection order is resolver priority."""

    name: bpy.props.StringProperty(default="")
    path: bpy.props.StringProperty(default="", subtype="DIR_PATH")
    source_path: bpy.props.StringProperty(default="", subtype="FILE_PATH")
    icon_path: bpy.props.StringProperty(default="")
    uuid: bpy.props.StringProperty(default="")
    kind: bpy.props.StringProperty(default="")
    is_archive: bpy.props.BoolProperty(default=False)


class MCBEDROCK_UL_active_pack_list(UIList):
    """Enabled packs in descending priority order."""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        icon_id = _preview_icon_id(item.icon_path)
        icon_name = "FILE_ARCHIVE" if item.is_archive else "FILE_FOLDER"
        label = item.name or os.path.basename(item.path.rstrip("\\/"))
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            if icon_id:
                row.label(text="", icon_value=icon_id)
            else:
                row.label(text="", icon=icon_name)
            row.label(text=label)
            if index == 0:
                row.label(text="Highest", icon="SOLO_ON")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            if icon_id:
                layout.label(text="", icon_value=icon_id)
            else:
                layout.label(text="", icon=icon_name)


class MCBEDROCK_UL_pack_list(UIList):
    """Discovered packs: thumbnail + name; double-click adds to active."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        icon_id = _preview_icon_id(item.icon_path)
        icon_name = "FILE_ARCHIVE" if item.is_archive else "FILE_FOLDER"
        label = item.name or os.path.basename(item.path)
        is_active = _is_active_pack(context, item)

        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            if icon_id:
                row.label(text="", icon_value=icon_id)
            else:
                row.label(text="", icon=icon_name)
            # Emboss-free name click: select on single click, activate on double.
            op = row.operator("mcbedrock.pack_list_click", text=label, emboss=False)
            op.index = index
            if is_active:
                row.label(text="", icon="CHECKMARK")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            if icon_id:
                layout.label(text="", icon_value=icon_id)
            else:
                layout.label(text="", icon=icon_name)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        query = getattr(context.scene, "mcbedrock_pack_search", "") or ""
        if not PACK_CONTENT_FILTERS_ENABLED:
            return [self.bitflag_filter_item if _pack_matches_search(item, query) else 0
                    for item in items], []
        from .pack_filter_ui import query_for
        features, _ = pack_feature_snapshot(context.scene)
        ordered = pack_features.order_entries(list(enumerate(items)), features, query_for(context.scene))
        visible_indices = {i for i, _ in ordered}
        flags = []
        for index, item in enumerate(items):
            visible = index in visible_indices and _pack_matches_search(item, query)
            flags.append(self.bitflag_filter_item if visible else 0)
        indices = [i for i, _ in ordered] + [i for i in range(len(items)) if i not in visible_indices]
        new_order = [0] * len(items)
        for rank, index in enumerate(indices):
            new_order[index] = rank
        return flags, new_order


class MCBEDROCK_OT_pack_version_range(Operator):
    bl_idname = 'mcbedrock.pack_version_range'
    bl_label = 'Game Version Range'
    minimum: bpy.props.StringProperty(name='From', description='Game version, for example 1.20. Leave blank for any')
    maximum: bpy.props.StringProperty(name='Through', description='Game version, for example 1.21.11. Leave blank for any')

    def invoke(self, context, event):
        self.minimum = context.scene.mcbedrock_pack_version_min
        self.maximum = context.scene.mcbedrock_pack_version_max
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.label(text='Show packs whose supported versions overlap:')
        self.layout.prop(self, 'minimum')
        self.layout.prop(self, 'maximum')
        self.layout.label(text='Unknown versions are hidden when a limit is set.')

    def execute(self, context):
        lo, hi = pack_features.version(self.minimum), pack_features.version(self.maximum)
        if (self.minimum.strip() and lo is None) or (self.maximum.strip() and hi is None) or (lo and hi and lo > hi):
            self.report({'ERROR'}, 'Enter valid game versions in ascending order, such as 1.20 and 1.21.11')
            return {'CANCELLED'}
        context.scene.mcbedrock_pack_version_min = self.minimum.strip()
        context.scene.mcbedrock_pack_version_max = self.maximum.strip()
        return {'FINISHED'}


def reset_pack_library_state(state):
    for key in list(state):
        if key.startswith('packs.') or key == 'app.scroll.packs':
            state.pop(key, None)
    for key in ('_menu', '_runtime_focus', '_runtime_pressed', '_runtime_hover'):
        state.pop(key, None)
    state['_tree_dirty'] = True


class MCBEDROCK_OT_reset_pack_library(Operator):
    """Clear the scanned pack library without deleting files or disabling active packs"""
    bl_idname = 'mcbedrock.reset_pack_library'
    bl_label = 'Reset Resource Pack Library'

    def execute(self, context):
        # The worker writes the installed catalog itself. Stop it before replacing
        # catalogs so a finishing scan cannot resurrect the cleared entries.
        _stop_startup_scan()
        from ..importers.vanilla_assets import discover_vanilla_jars
        installed_java = {_normalized_path(path) for _, path in discover_vanilla_jars()}
        try:
            excluded = _repair_java_reset_exclusions(installed_java)
        except OSError as exc:
            self.report({'ERROR'}, 'Could not restore Java assets: %s' % exc)
            return {'CANCELLED'}
        entries = _load_manual_catalog() + _load_manual_catalog(_library_catalog_path())
        for scene in bpy.data.scenes:
            for collection in (scene.mcbedrock_discovered_packs, scene.mcbedrock_manual_packs):
                entries.extend({field: getattr(item, field) for field in _MANUAL_FIELDS}
                               for item in collection if item.path)
        kept = {}
        for item in entries:
            key = _normalized_path(item['path'])
            if key in installed_java:
                if key not in excluded:
                    kept[key] = dict(item, launcher='Minecraft Java')
                continue
            excluded.setdefault(key, {'path': os.path.abspath(item['path']), 'reason': 'reset'})
        try:
            # Reuse the same exclusions as Remove from list. Adding a folder
            # deliberately again removes its exclusions through _remember_manual_pack.
            _save_manual_catalog(list(excluded.values()), _excluded_catalog_path())
            _save_manual_catalog([])
            _save_manual_catalog(list(kept.values()), _library_catalog_path())
        except OSError as exc:
            self.report({'ERROR'}, 'Could not reset the resource pack library: %s' % exc)
            return {'CANCELLED'}
        for scene in bpy.data.scenes:
            scene.mcbedrock_discovered_packs.clear()
            scene.mcbedrock_manual_packs.clear()
            for meta in kept.values():
                _add_meta_to_collection(scene.mcbedrock_discovered_packs, meta)
            scene.mcbedrock_discovered_pack_index = -1
            scene.mcbedrock_pack_search = ''
            scene.mcbedrock_list_scroll = 0
            scene['_mcbedrock_pack_scan_revision'] = scene.get('_mcbedrock_pack_scan_revision', 0) + 1
            scene.mcbedrock_pack_manager_status = 'Resource pack library reset'
        _list_click.update(index=-1, time=0.0)
        bpy.ops.mcbedrock.asset_browser_reset('EXEC_DEFAULT')
        from .gpu_framework import runtime
        for session in runtime._ACTIVE.values():
            reset_pack_library_state(session.state)
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        self.report({'INFO'}, 'Resource pack library reset; files and enabled packs kept')
        return {'FINISHED'}


class MCBEDROCK_OT_scan_packs(Operator):
    """Scan common Minecraft folders for resource packs."""
    bl_idname = "mcbedrock.scan_resource_packs"
    bl_label = "Scan for Packs"
    bl_options = {"REGISTER"}
    background: bpy.props.BoolProperty(default=False, options={'HIDDEN','SKIP_SAVE'})

    def execute(self, context):
        from ..importers.path_discovery import discover_all_roots_and_archives

        if self.background:
            # An explicit refresh replaces an in-flight scan as well.
            _stop_startup_scan()
            if not bpy.app.timers.is_registered(_poll_startup_scan):
                bpy.app.timers.register(_poll_startup_scan, first_interval=.01)
            context.scene.mcbedrock_pack_manager_status = 'Finding resource packs…'
            return {'FINISHED'}

        _stop_startup_scan()
        restore_manual_packs(context.scene)
        discovered = discover_all_roots_and_archives()
        _repair_installed_pack_exclusions(discovered.get('roots', []))
        # Discovery only admits installed vanilla JARs, not arbitrary mod JARs.
        # Repair old reset exclusions here, away from panel drawing and before
        # accepting metadata, so an update/Refresh restores missing versions.
        installed_java = {_normalized_path(path) for path in discovered.get('archives', [])
                          if path.lower().endswith('.jar')}
        _repair_java_reset_exclusions(installed_java)
        cache = _load_scan_cache()
        metas = []
        seen = set()

        max_depth = context.scene.mcbedrock_scan_depth
        include_archives = context.scene.mcbedrock_include_archives
        seen_names = set()
        excluded = _excluded_pack_paths()

        manual = {}
        for collection in (getattr(context.scene, 'mcbedrock_manual_packs', ()),
                           context.scene.mcbedrock_discovered_packs):
            for item in collection:
                if item.launcher == 'Manual':
                    manual[_normalized_path(item.path)] = {field: getattr(item,field) for field in
                        ('name','path','icon_path','uuid','kind','launcher','is_archive','description','author')}

        def _accept(meta):
            """Dedupe by uuid, normalized path, and cleaned name; preload icon."""
            if _normalized_path(meta['path']) in excluded:
                return False
            key = meta.get("uuid") or os.path.normcase(os.path.abspath(meta["path"]))
            name_key = (meta.get("name") or "").strip().lower()
            if key in seen or (not meta.get('uuid') and name_key and name_key in seen_names):
                return False
            seen.add(key)
            if name_key:
                seen_names.add(name_key)
            # Load the preview now so icons appear on the first draw.
            _preview_icon_id(meta.get("icon_path", ""))
            metas.append(meta)
            return True

        for meta in manual.values():
            _accept(meta)

        # Folders first so a folder wins over an identically-named archive.
        for root_info in discovered.get("roots", []):
            root = root_info.get("root", "")
            for pack_root in _iter_pack_roots(root, max_depth=max_depth):
                # Require pack metadata + a pack icon so datapacks and branding
                # folders with a stray manifest do not clutter the list.
                if not _is_listable_pack_folder(pack_root):
                    continue
                meta = _cached_metadata(pack_root, False, cache)
                if not meta or not meta.get("icon_path"):
                    continue  # Skip filtered (data/world/skin) or icon-less packs.
                meta["launcher"] = root_info.get("launcher", root_info.get("kind", ""))
                _accept(meta)

        if include_archives:
            for archive_path in discovered.get("archives", []):
                meta = _cached_metadata(archive_path, True, cache)
                if not meta or meta.get('unsupported'):
                    continue
                if not meta.get("icon_path") and not meta.get("unsupported"):
                    continue  # Valid packs still need their official pack icon.
                archive_key = _normalized_path(archive_path)
                for root_info in discovered.get('roots', []):
                    if archive_key.startswith(_normalized_path(root_info['root']) + os.sep):
                        meta['launcher'] = root_info.get('launcher', meta.get('launcher', ''))
                        break
                _accept(meta)

        _save_scan_cache(cache)

        collection = context.scene.mcbedrock_discovered_packs
        collection.clear()
        for meta in sorted(metas, key=lambda item: (item.get('launcher')!='Manual',item.get("name", "").lower())):
            _add_meta_to_collection(collection, meta)

        _save_manual_catalog([{field: getattr(item, field) for field in _MANUAL_FIELDS}
                              for item in collection], _library_catalog_path())

        context.scene.mcbedrock_pack_manager_status = (
            f"{len(collection)} packs found" if collection else "No packs found"
        )
        # List-first: no auto-selected preview until the user clicks a row.
        context.scene.mcbedrock_discovered_pack_index = -1
        context.scene['_mcbedrock_pack_scan_revision'] = context.scene.get('_mcbedrock_pack_scan_revision', 0) + 1
        self.report({"INFO"}, context.scene.mcbedrock_pack_manager_status)
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_OT_pack_list_click(Operator):
    """Select a pack on click; add it to active on a quick second click."""
    bl_idname = "mcbedrock.pack_list_click"
    bl_label = "Select Pack"
    bl_options = {"REGISTER", "INTERNAL"}

    index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        packs = context.scene.mcbedrock_discovered_packs
        if not (0 <= self.index < len(packs)):
            return {"CANCELLED"}

        now = time.time()
        is_double = (
            _list_click["index"] == self.index
            and (now - _list_click["time"]) <= _DOUBLE_CLICK_SEC
        )
        _list_click["index"] = self.index
        _list_click["time"] = now

        context.scene.mcbedrock_discovered_pack_index = self.index
        if is_double:
            result = bpy.ops.mcbedrock.add_resource_pack_to_active(
                "EXEC_DEFAULT", pack_index=self.index
            )
            # Reset so a third click does not immediately re-trigger.
            _list_click["time"] = 0.0
            if "CANCELLED" in result:
                return {"CANCELLED"}

        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_OT_clear_pack_search(Operator):
    """Clear the pack list search field."""
    bl_idname = "mcbedrock.clear_pack_search"
    bl_label = "Clear Search"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        context.scene.mcbedrock_pack_search = ""
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_OT_toggle_pack_selection(Operator):
    """Toggle whether this pack is checked for import."""
    bl_idname = "mcbedrock.toggle_pack_selection"
    bl_label = "Toggle Pack Selection"
    bl_options = {"REGISTER", "INTERNAL"}

    # SKIP_SAVE is essential: without it Blender reuses the last operator's
    # property value, so clicking one row toggles the wrong (or several) items.
    index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        packs = context.scene.mcbedrock_discovered_packs
        if 0 <= self.index < len(packs):
            packs[self.index].selected = not packs[self.index].selected
            context.scene.mcbedrock_discovered_pack_index = self.index
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_OT_exclude_pack(Operator):
    """Remove a pack from the library listing; keep its source files intact."""
    bl_idname = "mcbedrock.exclude_pack"
    bl_label = "Remove Pack from List"
    bl_options = {"REGISTER", "INTERNAL"}

    filepath: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        key = _normalized_path(self.filepath) if self.filepath else ''
        item = next((item for item in context.scene.mcbedrock_discovered_packs
                     if _normalized_path(item.path) == key), None)
        if item is None:
            self.report({'ERROR'}, 'Pack is no longer in the list')
            return {'CANCELLED'}
        name = item.name
        try:
            exclude_pack_from_library(item.path)
        except OSError as exc:
            self.report({'ERROR'}, 'Could not save the pack exclusion: %s' % exc)
            return {'CANCELLED'}
        context.scene.mcbedrock_pack_manager_status = 'Removed %s from the list' % name
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return {'FINISHED'}


class MCBEDROCK_OT_open_pack_location(Operator):
    """Open this pack's location in the system file explorer."""
    bl_idname = "mcbedrock.open_pack_location"
    bl_label = "Open Pack Location"
    bl_options = {"REGISTER", "INTERNAL"}

    index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        packs = context.scene.mcbedrock_discovered_packs
        if not (0 <= self.index < len(packs)):
            self.report({"ERROR"}, "Invalid pack")
            return {"CANCELLED"}
        path = packs[self.index].path
        # For archive files, reveal the containing folder rather than launching.
        target = path if os.path.isdir(path) else os.path.dirname(path)
        if not target or not os.path.exists(target):
            self.report({"ERROR"}, "Location not found")
            return {"CANCELLED"}
        try:
            bpy.ops.wm.path_open(filepath=target)
        except Exception:
            try:
                os.startfile(target)  # Windows fallback.
            except Exception as exc:
                self.report({"ERROR"}, f"Could not open location: {exc}")
                return {"CANCELLED"}
        return {"FINISHED"}


class MCBEDROCK_OT_clear_cache(Operator):
    """Delete the entire pack metadata/icon cache folder."""
    bl_idname = "mcbedrock.clear_cache"
    bl_label = "Clear Cache"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        global _pack_previews
        path = _cache_dir()
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            os.makedirs(_pack_icon_cache_dir(), exist_ok=True)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not clear cache: {exc}")
            return {"CANCELLED"}
        # Drop loaded previews so freed icon files are not referenced.
        if _pack_previews is not None and _preview_module is not None:
            try:
                _preview_module.remove(_pack_previews)
            except Exception:
                pass
            _pack_previews = None
        self.report({"INFO"}, "Cache cleared")
        return {"FINISHED"}


def _active_index_for_source(scene, source_path):
    wanted = _normalized_path(source_path)
    if not wanted:
        return -1
    for index, item in enumerate(getattr(scene, "mcbedrock_active_packs", ())):
        source = getattr(item, "source_path", "") or getattr(item, "path", "")
        if _normalized_path(source) == wanted:
            return index
    return -1


def _append_active_pack(scene, discovered, pack_root):
    active = scene.mcbedrock_active_packs.add()
    active.name = discovered.name or os.path.basename(pack_root.rstrip("\\/"))
    active.path = os.path.abspath(pack_root)
    active.source_path = discovered.path or pack_root
    active.icon_path = discovered.icon_path
    active.uuid = discovered.uuid
    active.kind = discovered.kind
    active.is_archive = discovered.is_archive
    return len(scene.mcbedrock_active_packs) - 1


def _finish_background_enable(scene, path, name, root, error):
    """An archive switched on in the sidebar is extracted: add it now (one undo step)."""
    if error is not None or not root:
        message = "Could not open %s: %s" % (name, error or "nothing readable in it")
        try:
            scene.mcbedrock_pack_manager_status = message
        except ReferenceError:
            pass
        _notify("Error: " + message)
        return
    try:
        if bpy.context.scene != scene:
            return  # another scene is shown now; switching the pack on again is instant
        key = _normalized_path(path)
        index = next((i for i, item in enumerate(scene.mcbedrock_discovered_packs)
                      if _normalized_path(item.path) == key), -1)
    except ReferenceError:
        return  # another file was opened meanwhile
    if index < 0:
        return
    try:
        bpy.ops.mcbedrock.add_resource_pack_to_active('EXEC_DEFAULT', True, pack_index=index)
    except RuntimeError as exc:
        _notify("Error: " + str(exc))


class MCBEDROCK_OT_add_pack_to_active(Operator):
    """Add the highlighted discovered pack at the bottom of the priority stack."""
    bl_idname = "mcbedrock.add_resource_pack_to_active"
    bl_label = "Add to Active"
    bl_options = {"REGISTER", "UNDO"}

    pack_index: bpy.props.IntProperty(default=-1)
    # The sidebar's switch: an archive that still has to be extracted is
    # extracted on a helper thread, and the pack is added when it is ready.
    background: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        packs = scene.mcbedrock_discovered_packs
        index = self.pack_index if self.pack_index >= 0 else scene.mcbedrock_discovered_pack_index
        if index < 0 or index >= len(packs):
            self.report({"ERROR"}, "No discovered pack selected")
            return {"CANCELLED"}
        item = packs[index]
        rejection = _unsupported_archive_reason(item.path)
        if rejection:
            self.report({"ERROR"}, rejection)
            return {"CANCELLED"}
        duplicate = _active_index_for_source(scene, item.path)
        if duplicate >= 0:
            scene.mcbedrock_active_pack_index = duplicate
            self.report({"INFO"}, "%s is already active" % item.name)
            return {"FINISHED"}
        if getattr(self, "background", False) and item.is_archive and _archive_needs_extraction(item.path):
            path, name = item.path, item.name or os.path.basename(item.path)
            prepare_archive(path, lambda root, error: _finish_background_enable(scene, path, name, root, error))
            self.report({"INFO"}, "Preparing %s…" % name)
            return {"FINISHED"}

        try:
            pack_root = _resolve_pack_root_for_item(item)
        except OSError as exc:
            self.report({"ERROR"}, "Could not open %s: %s" % (item.name, exc))
            return {"CANCELLED"}
        if not pack_root or not os.path.isdir(pack_root):
            self.report({"ERROR"}, "Selected pack could not be opened")
            return {"CANCELLED"}
        scene.mcbedrock_active_pack_index = _append_active_pack(scene, item, pack_root)
        sync_legacy_pack_paths(context, refresh_browser=False, defer_browser=True)
        self.report({"INFO"}, "Added to active stack: %s" % item.name)
        return {"FINISHED"}


class MCBEDROCK_OT_reorder_schematic_pack(Operator):
    """Commit a drag in one move without reindexing the asset browser."""
    bl_idname = 'mcbedrock.reorder_schematic_pack'
    bl_label = 'Reorder Resource Pack'
    bl_options = {'REGISTER', 'UNDO'}

    pack_path: bpy.props.StringProperty(options={'HIDDEN'})
    target_index: bpy.props.IntProperty(options={'HIDDEN'})

    def execute(self, context):
        packs = context.scene.mcbedrock_active_packs
        source = next((i for i, p in enumerate(packs) if p.path == self.pack_path), -1)
        target = max(0, min(len(packs)-1, self.target_index))
        if source < 0:
            return {'CANCELLED'}
        if source == target:
            return {'FINISHED'}
        packs.move(source, target)
        context.scene.mcbedrock_active_pack_index = target
        sync_legacy_pack_paths(context, refresh_browser=False, defer_browser=True)
        # Rebuilt once the edits pause (a failure pops up then), not on every click.
        from .schematic_importer import refresh_schematic_soon
        try:
            refresh_schematic_soon(context)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
        return {'FINISHED'}


class MCBEDROCK_OT_move_active_pack_up(Operator):
    """Raise the selected pack's priority."""
    bl_idname = "mcbedrock.move_active_pack_up"
    bl_label = "Move Up"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        index = scene.mcbedrock_active_pack_index
        packs = scene.mcbedrock_active_packs
        if index <= 0 or index >= len(packs):
            return {"CANCELLED"}
        packs.move(index, index - 1)
        scene.mcbedrock_active_pack_index = index - 1
        sync_legacy_pack_paths(context, refresh_browser=False, defer_browser=True)
        # Rebuilt once the edits pause (a failure pops up then), not on every click.
        from .schematic_importer import refresh_schematic_soon
        try:
            refresh_schematic_soon(context)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
        return {"FINISHED"}


class MCBEDROCK_OT_move_active_pack_down(Operator):
    """Lower the selected pack's priority."""
    bl_idname = "mcbedrock.move_active_pack_down"
    bl_label = "Move Down"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        index = scene.mcbedrock_active_pack_index
        packs = scene.mcbedrock_active_packs
        if index < 0 or index >= len(packs) - 1:
            return {"CANCELLED"}
        packs.move(index, index + 1)
        scene.mcbedrock_active_pack_index = index + 1
        sync_legacy_pack_paths(context, refresh_browser=False, defer_browser=True)
        # Rebuilt once the edits pause (a failure pops up then), not on every click.
        from .schematic_importer import refresh_schematic_soon
        try:
            refresh_schematic_soon(context)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
        return {"FINISHED"}


class MCBEDROCK_OT_remove_active_pack(Operator):
    """Remove the selected pack from the enabled stack."""
    bl_idname = "mcbedrock.remove_active_pack"
    bl_label = "Remove"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        index = scene.mcbedrock_active_pack_index
        packs = scene.mcbedrock_active_packs
        if index < 0 or index >= len(packs):
            return {"CANCELLED"}
        removed_top = index == 0
        name = packs[index].name
        packs.remove(index)
        scene.mcbedrock_active_pack_index = min(index, len(packs) - 1)
        sync_legacy_pack_paths(context, refresh_browser=False, defer_browser=True)
        if not packs:
            scene.mcbedrock_browser_assets.clear()
            scene.mcbedrock_browser_index = -1
            scene.mcbedrock_gallery_rows.clear()
        self.report({"INFO"}, "Removed from active stack: %s" % name)
        return {"FINISHED"}


def _after_relocate(identity, preparing='', error=None):
    from .make_panels import pack_relocated
    pack_relocated(identity, preparing, error)


class MCBEDROCK_OT_relocate_active_pack(Operator, ImportHelper):
    """Choose where an enabled resource pack's folder or file is now"""
    bl_idname = "mcbedrock.relocate_active_pack"
    bl_label = "Locate Pack"
    bl_options = {"REGISTER", "UNDO"}

    pack_index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})
    filter_glob: bpy.props.StringProperty(
        default="*.zip;*.mcpack;*.mcaddon;*.mctemplate;*.mcworld;*.jar;pack.mcmeta;manifest.json",
        options={"HIDDEN"}, maxlen=255)
    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        from types import SimpleNamespace
        scene = context.scene
        packs = scene.mcbedrock_active_packs
        if not 0 <= self.pack_index < len(packs):
            self.report({"ERROR"}, "That pack is no longer enabled")
            return {"CANCELLED"}
        chosen = bpy.path.abspath(getattr(self, "filepath", "") or "")
        marker = os.path.basename(chosen).lower() in {"pack.mcmeta", "manifest.json"}
        if os.path.isfile(chosen) and not marker:
            # An archive pack (.zip, .mcpack, .jar...).
            reason = _unsupported_archive_reason(chosen)
            meta = {} if reason else (_read_archive_metadata(chosen) or {})
            if reason or not meta or meta.get("unsupported"):
                self.report({"ERROR"}, reason or "That file is not a resource pack")
                return {"CANCELLED"}
            archive = True
        else:
            # A folder: the one chosen, or the one holding the chosen pack.mcmeta/manifest.json.
            folder = os.path.dirname(chosen) if marker else (self.directory or chosen)
            folder = bpy.path.abspath(folder) if folder else ""
            root = _first_pack_root(folder) if folder and os.path.isdir(folder) else ""
            if not root or not _has_pack_metadata(root):
                self.report({"ERROR"}, "That folder does not contain a resource pack")
                return {"CANCELLED"}
            meta = _read_pack_metadata(root) or {"name": os.path.basename(root), "path": root}
            chosen, archive = root, False
        chosen = os.path.abspath(chosen)
        item = packs[self.pack_index]
        old_identity = pack_identity(item)
        item.source_path = chosen
        item.is_archive = archive
        if meta.get("icon_path"):
            item.icon_path = meta["icon_path"]
        preparing = archive and _archive_needs_extraction(chosen)
        if not archive:
            item.path = chosen
        elif not preparing:
            item.path = _resolve_pack_root_for_item(SimpleNamespace(path=chosen, is_archive=True))
        else:
            item.path = ""
        # The library lists the new place as well; the old row keeps its "Missing" mark.
        _remember_manual_pack(dict(meta, path=chosen, is_archive=archive, launcher="Manual"))
        restore_manual_packs(scene)
        _EXISTS.clear()
        sync_legacy_pack_paths(context, refresh_browser=False, defer_browser=True)
        new_identity = pack_identity(item)
        if preparing:
            prepare_active_pack(item, lambda root, error: _after_relocate(new_identity, '', error))
        _after_relocate(old_identity, new_identity if preparing else '')
        self.report({"INFO"}, "%s found at %s" % (item.name or "Pack", chosen))
        return {"FINISHED"}


class MCBEDROCK_OT_use_selected_pack(Operator):
    """Legacy single-pack action retained for scripts and old keymaps."""
    bl_idname = "mcbedrock.use_selected_resource_pack"
    bl_label = "Use Selected Pack"
    bl_options = {"REGISTER", "UNDO"}
    
    pack_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        packs = context.scene.mcbedrock_discovered_packs
        index = self.pack_index if self.pack_index >= 0 else context.scene.mcbedrock_discovered_pack_index
        if index < 0 or index >= len(packs):
            self.report({"ERROR"}, "No pack selected")
            return {"CANCELLED"}
        item = packs[index]
        rejection = _unsupported_archive_reason(item.path)
        if rejection:
            self.report({"ERROR"}, rejection)
            return {"CANCELLED"}

        try:
            pack_root = _resolve_pack_root_for_item(item)
        except OSError as exc:
            self.report({"ERROR"}, "Could not open %s: %s" % (item.name, exc))
            return {"CANCELLED"}
        if not pack_root or not os.path.isdir(pack_root):
            self.report({"ERROR"}, "Selected pack could not be opened")
            return {"CANCELLED"}
        count = _activate_pack(context, pack_root, item.name, source_path=item.path)
        self.report({"INFO"}, f"Active: {item.name}" + (f" ({count} assets)" if count else ""))
        return {"FINISHED"}


class MCBEDROCK_OT_select_all_packs(Operator):
    """Select all discovered packs."""
    bl_idname = "mcbedrock.select_all_resource_packs"
    bl_label = "Select All"

    def execute(self, context):
        for item in context.scene.mcbedrock_discovered_packs:
            item.selected = True
        return {"FINISHED"}


class MCBEDROCK_OT_deselect_all_packs(Operator):
    """Deselect all discovered packs."""
    bl_idname = "mcbedrock.deselect_all_resource_packs"
    bl_label = "Deselect All"

    def execute(self, context):
        for item in context.scene.mcbedrock_discovered_packs:
            item.selected = False
        return {"FINISHED"}


def _folder_pack_metadata(folder):
    """Read every folder/archive pack, without treating a collection as one pack."""
    try:
        from ..importers.path_discovery import ARCHIVE_EXTENSIONS
    except ImportError:
        from importers.path_discovery import ARCHIVE_EXTENSIONS
    for root in _iter_pack_roots(folder, max_depth=None):
        meta = _read_pack_metadata(root)
        if meta:
            yield meta
    for current, dirs, files in os.walk(folder, followlinks=False):
        if _has_pack_metadata(current):
            dirs[:] = []
            continue
        # A launcher's mods folders hold a whole modpack: adding an instance
        # adds its resource packs, never its mods. A mod is added on its own,
        # or by choosing its mods folder itself.
        dirs[:] = [name for name in dirs if name.lower() not in
                   {'.git', '__macosx', 'node_modules', '__pycache__', 'mods'}]
        for name in files:
            if os.path.splitext(name.lower())[1] in ARCHIVE_EXTENSIONS:
                meta = _read_archive_metadata(os.path.join(current, name))
                if meta and not meta.get('unsupported'):
                    yield meta


def _add_folder_metas(scene, metas):
    """Put folder-scan results in the library as manual packs, new rows on top."""
    for meta in metas:
        meta["launcher"] = "Manual"
    _remember_manual_packs(metas)
    packs = scene.mcbedrock_discovered_packs
    added = 0
    for collection in (packs, scene.mcbedrock_manual_packs):
        known = {}
        for item in collection:
            known.setdefault(_normalized_path(item.path), item)
        fresh, seen = [], set()
        for meta in metas:
            key = _normalized_path(meta['path'])
            if key in known:
                known[key].launcher = "Manual"
            elif key not in seen:
                seen.add(key)
                fresh.append(meta)
        # Every launcher write happens before any add: adding can move the rows.
        known.clear()
        for meta in fresh:
            _add_meta_to_collection(collection, meta)
            if collection is packs:
                packs.move(len(packs) - 1, 0)
                added += 1
    scene.mcbedrock_discovered_pack_index = 0
    scene.mcbedrock_pack_search = ""
    scene.mcbedrock_list_scroll = 0
    _list_click.update(index=-1, time=0.0)
    scene.mcbedrock_pack_manager_status = f"Added {added} resource packs ({len(metas)} found)"
    return added


# One "Add Folder" read at a time: walking a large folder and opening every
# archive in it runs on a helper thread (it froze Blender for minutes).
_folder_job = None


def _folder_label(folder):
    return os.path.basename(os.path.normpath(folder)) or folder


def _pack_metadata_at(path):
    """The packs a folder holds (folders and archives in it), or the one pack
    archive or mod .jar chosen."""
    if os.path.isfile(path):
        meta = _read_archive_metadata(path)
        if meta and not meta.get('unsupported'):
            yield meta
        return
    yield from _folder_pack_metadata(path)


def _nothing_found(path):
    """Why adding ``path`` added nothing."""
    if not os.path.isfile(path):
        return 'No resource packs found in %s' % _folder_label(path)
    reason = _unsupported_archive_reason(path)
    if reason:
        return reason
    if path.lower().endswith('.jar'):
        return '%s has no textures or models (assets folder) to browse' % _folder_label(path)
    return '%s is not a resource pack' % _folder_label(path)


def _reading_text(path):
    return ('Reading %s…' if os.path.isfile(path) else 'Looking for packs in %s…') % _folder_label(path)


def start_folder_job(scene, folder):
    """Read a folder of packs, or one pack archive or mod .jar, on a helper thread."""
    global _folder_job
    import threading
    _pack_icon_cache_dir()  # resolved here: the helper thread must not call into bpy
    try:
        from ..importers import path_discovery  # noqa: F401  (imported on this thread)
    except ImportError:
        pass
    # The text is kept: the sidebar shows it on every draw while the job runs.
    job = {'scene': scene, 'folder': folder, 'metas': None, 'error': None, 'text': _reading_text(folder)}

    def run():
        try:
            job['metas'] = list(_pack_metadata_at(folder))
        except Exception as exc:
            job['error'] = exc

    job['thread'] = threading.Thread(target=run, daemon=True)
    _folder_job = job
    job['thread'].start()
    scene.mcbedrock_pack_manager_status = job['text']
    if not bpy.app.timers.is_registered(_poll_folder_job):
        bpy.app.timers.register(_poll_folder_job, first_interval=.2)


def _poll_folder_job():
    global _folder_job
    job = _folder_job
    if job is None:
        return None
    if job['thread'].is_alive():
        return .2
    _folder_job = None
    scene, label = job['scene'], _folder_label(job['folder'])
    try:
        if job['error'] is not None:
            raise job['error']
        if not job['metas']:
            message = _nothing_found(job['folder'])
            scene.mcbedrock_pack_manager_status = message
            _notify('Error: ' + message)
        else:
            _add_folder_metas(scene, job['metas'])
    except ReferenceError:
        pass  # the scene is gone
    except Exception as exc:
        message = 'Could not add packs from %s: %s' % (label, exc)
        try:
            scene.mcbedrock_pack_manager_status = message
        except ReferenceError:
            pass
        _notify('Error: ' + message)
    from .asset_browser import refresh_browser_pages
    refresh_browser_pages()
    return None


def _stop_folder_job():
    global _folder_job
    _folder_job = None
    if bpy and bpy.app.timers.is_registered(_poll_folder_job):
        bpy.app.timers.unregister(_poll_folder_job)


def library_activity():
    """What the pack library is busy with, for the sidebar's status line ('' when idle)."""
    if _folder_job is not None:
        return _folder_job['text']
    if scan_in_progress():
        return 'Finding resource packs…'
    return ''


class MCBEDROCK_OT_add_pack_folder(Operator, ImportHelper):
    """Add a resource pack or mod .jar, or every pack in a folder, to the pack library"""
    bl_idname = "mcbedrock.add_resource_pack_folder"
    bl_label = "Add Packs"
    bl_options = {"REGISTER"}

    filter_glob: bpy.props.StringProperty(
        default="*.zip;*.mcpack;*.mcaddon;*.mctemplate;*.mcworld;*.jar;pack.mcmeta;manifest.json",
        options={"HIDDEN"}, maxlen=255)
    directory: bpy.props.StringProperty(subtype="DIR_PATH")
    # The sidebar's folder button: read the folder on a helper thread.
    background: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def _chosen(self):
        """A chosen pack archive or mod .jar on its own, else the folder."""
        try:
            from ..importers.path_discovery import ARCHIVE_EXTENSIONS
        except ImportError:
            from importers.path_discovery import ARCHIVE_EXTENSIONS
        filepath = getattr(self, "filepath", "") or ""
        if os.path.isfile(filepath) and os.path.splitext(filepath.lower())[1] in ARCHIVE_EXTENSIONS:
            return filepath
        folder = getattr(self, "directory", "")
        if not folder:
            folder = filepath if os.path.isdir(filepath) else os.path.dirname(filepath)
        return folder if folder and os.path.isdir(folder) else ""

    def execute(self, context):
        chosen = self._chosen()
        if not chosen:
            self.report({"ERROR"}, "No folder or pack file selected")
            return {"CANCELLED"}
        if getattr(self, "background", False):
            start_folder_job(context.scene, chosen)
            self.report({"INFO"}, _reading_text(chosen))
            return {"FINISHED"}

        metas = list(_pack_metadata_at(chosen))
        if not metas:
            self.report({"ERROR"}, _nothing_found(chosen))
            return {"CANCELLED"}
        _add_folder_metas(context.scene, metas)
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_PT_pack_manager(Panel):
    bl_label = "Resource Pack Manager"
    bl_idname = "MCBEDROCK_PT_pack_manager"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Sparkle"
    bl_order = 0

    @classmethod
    def poll(cls, context):
        from .custom_ui import legacy_ui_enabled
        return legacy_ui_enabled(context)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        ensure_startup_scan()

        layout.label(text="Active Resource Packs", icon="ASSET_MANAGER")
        active_row = layout.row()
        active_row.template_list(
            "MCBEDROCK_UL_active_pack_list",
            "",
            scene,
            "mcbedrock_active_packs",
            scene,
            "mcbedrock_active_pack_index",
            rows=4,
        )
        controls = active_row.column(align=True)
        controls.operator("mcbedrock.move_active_pack_up", text="", icon="TRIA_UP")
        controls.operator("mcbedrock.move_active_pack_down", text="", icon="TRIA_DOWN")
        controls.separator()
        controls.operator("mcbedrock.remove_active_pack", text="", icon="X")

        if not scene.mcbedrock_active_packs:
            empty = layout.row()
            empty.active = False
            empty.label(text="Add packs below; index 0 has highest priority", icon="INFO")

        layout.separator()
        layout.label(text="Discovered Packs", icon="VIEWZOOM")

        # Full-width scan action (refresh left via operator icon / folder right).
        # Both read folders away from Blender's UI thread.
        row = layout.row(align=True)
        row.operator("mcbedrock.scan_resource_packs", icon="FILE_REFRESH").background = True
        row.operator("mcbedrock.add_resource_pack_folder", icon="FILEBROWSER", text="").background = True
        activity = library_activity()
        if activity:
            busy = layout.row()
            busy.active = False
            busy.label(text=activity, icon="TIME")

        search = layout.row(align=True)
        search.prop(scene, "mcbedrock_pack_search", text="", icon="VIEWZOOM")
        search.operator("mcbedrock.clear_pack_search", text="", icon="X")

        layout.template_list(
            "MCBEDROCK_UL_pack_list",
            "",
            scene,
            "mcbedrock_discovered_packs",
            scene,
            "mcbedrock_discovered_pack_index",
            rows=5,
        )

        packs = scene.mcbedrock_discovered_packs
        idx = scene.mcbedrock_discovered_pack_index
        query = (scene.mcbedrock_pack_search or "").strip()

        if not packs:
            status = layout.row()
            status.active = False
            status.label(text=scene.mcbedrock_pack_manager_status or "Scan for packs to begin")
        elif not (0 <= idx < len(packs)):
            status = layout.row()
            status.active = False
            if query:
                visible = sum(1 for item in packs if _pack_matches_search(item, query))
                status.label(text=f"{visible} of {len(packs)} packs · select one for details")
            else:
                status.label(text=f"{len(packs)} packs · select one for details")
        else:
            item = packs[idx]
            # Concept card: large icon | name + By author + edition badge.
            box = layout.box()
            row = box.row(align=True)

            icon_id = _preview_icon_id(item.icon_path)
            icon_name = "FILE_ARCHIVE" if item.is_archive else "FILE_FOLDER"
            if icon_id:
                row.template_icon(icon_value=icon_id, scale=3.6)
            else:
                row.label(text="", icon=icon_name)

            subtitle = _pack_subtitle(item)
            edition = _edition_label(item.kind)
            is_active = _is_active_pack(context, item)
            # Extra line (edition / Active) needs a bit less vertical pad.
            extra = bool(subtitle or edition or is_active)
            pad_y = 1.25 if not extra else 0.45

            text = row.column(align=True)

            top = text.column()
            top.scale_y = pad_y
            top.label(text="")

            body = text.column(align=True)
            title = body.row(align=True)
            title.scale_y = 1.1
            title.label(text=item.name or os.path.basename(item.path))
            if subtitle:
                sub = body.row(align=True)
                sub.active = False
                sub.scale_y = 0.85
                sub.label(text=subtitle)
            meta_bits = []
            if edition:
                meta_bits.append(edition)
            if is_active:
                meta_bits.append("Active")
            if meta_bits:
                badge = body.row(align=True)
                badge.active = False
                badge.scale_y = 0.8
                badge.label(text=" · ".join(meta_bits))

            bot = text.column()
            bot.scale_y = pad_y
            bot.label(text="")

            action = layout.row()
            action.enabled = not is_active
            action.operator("mcbedrock.add_resource_pack_to_active", icon="ADD")


classes = (
    MCBEDROCK_OT_pack_version_range,
    MCBEDROCK_PG_discovered_pack,
    MCBEDROCK_PG_active_pack,
    MCBEDROCK_UL_pack_list,
    MCBEDROCK_UL_active_pack_list,
    MCBEDROCK_OT_reset_pack_library,
    MCBEDROCK_OT_scan_packs,
    MCBEDROCK_OT_pack_list_click,
    MCBEDROCK_OT_clear_pack_search,
    MCBEDROCK_OT_toggle_pack_selection,
    MCBEDROCK_OT_open_pack_location,
    MCBEDROCK_OT_exclude_pack,
    MCBEDROCK_OT_clear_cache,
    MCBEDROCK_OT_add_pack_to_active,
    MCBEDROCK_OT_move_active_pack_up,
    MCBEDROCK_OT_reorder_schematic_pack,
    MCBEDROCK_OT_move_active_pack_down,
    MCBEDROCK_OT_remove_active_pack,
    MCBEDROCK_OT_relocate_active_pack,
    MCBEDROCK_OT_use_selected_pack,
    MCBEDROCK_OT_select_all_packs,
    MCBEDROCK_OT_deselect_all_packs,
    MCBEDROCK_OT_add_pack_folder,
    MCBEDROCK_PT_pack_manager,
)


def _register_impl():
    """Register Resource Pack Manager classes and scene properties."""
    if not bpy:
        return
    os.makedirs(_pack_icon_cache_dir(), exist_ok=True)
    for cls in classes:
        bpy.utils.register_class(cls)
    for prop in ('filters', 'version_min', 'version_max'):
        setattr(bpy.types.Scene, 'mcbedrock_pack_' + prop, bpy.props.StringProperty(default='', options={'SKIP_SAVE'}))
    for prop in ('filter_exclude', 'match_all', 'sort_desc'):
        setattr(bpy.types.Scene, 'mcbedrock_pack_' + prop, bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'}))
    bpy.types.Scene.mcbedrock_pack_sort = bpy.props.EnumProperty(
        items=[(key, label, '') for key, label in pack_features.SORTS], default='library', options={'SKIP_SAVE'})
    bpy.types.Scene.mcbedrock_discovered_packs = bpy.props.CollectionProperty(
        type=MCBEDROCK_PG_discovered_pack, options={"SKIP_SAVE"}
    )
    bpy.types.Scene.mcbedrock_manual_packs = bpy.props.CollectionProperty(type=MCBEDROCK_PG_discovered_pack)
    bpy.types.Scene.mcbedrock_discovered_pack_index = bpy.props.IntProperty(
        default=-1, options={"SKIP_SAVE"}
    )
    bpy.types.Scene.mcbedrock_active_packs = bpy.props.CollectionProperty(
        name="Active Resource Packs",
        description="Enabled resource packs in descending priority order",
        type=MCBEDROCK_PG_active_pack,
    )
    bpy.types.Scene.mcbedrock_active_pack_index = bpy.props.IntProperty(
        name="Active Pack Index",
        description="Selected row in the active resource-pack priority stack",
        default=-1,
    )
    bpy.types.Scene.mcbedrock_pack_manager_status = bpy.props.StringProperty(
        default="", options={"SKIP_SAVE"}
    )
    bpy.types.Scene.mcbedrock_pack_search = bpy.props.StringProperty(
        name="Search Packs",
        description="Filter the discovered pack list by name, author, or edition",
        default="",
        options={"SKIP_SAVE", "TEXTEDIT_UPDATE"},
    )
    bpy.types.Scene.mcbedrock_active_pack_path = bpy.props.StringProperty(
        name="Active Pack Path",
        description="Discovered pack path currently linked for imports",
        default="",
        options={"SKIP_SAVE"},
    )
    bpy.types.Scene.mcbedrock_list_scroll = bpy.props.IntProperty(
        default=0, min=0, options={"SKIP_SAVE"}
    )
    bpy.types.Scene.mcbedrock_scan_depth = bpy.props.IntProperty(
        name="Scan Depth",
        description="How deep to walk folders when scanning for resource packs",
        default=5,
        min=1,
        max=10,
    )
    bpy.types.Scene.mcbedrock_include_archives = bpy.props.BoolProperty(
        name="Include Archives",
        description=(
            "Scan .mcpack/.mcaddon/.mcworld/.mctemplate/ZIP/JAR inputs; "
            ".mcproject and .mcstructure files are shown as unsupported"
        ),
        default=True,
    )
    _remove_active_pack_load_handlers()
    bpy.app.handlers.load_post.append(_sync_active_pack_on_load)


def register():
    """Register transactionally, removing residue from a failed enable."""
    if not bpy:
        return
    unregister()
    try:
        # The installed-pack scan starts when the library is first shown
        # (ensure_startup_scan), not with every Blender start.
        _register_impl()
    except Exception:
        unregister()
        raise


def unregister():
    """Unregister Resource Pack Manager classes and scene properties."""
    global _pack_previews, _preview_module, _feature_index, _scan_requested
    if _feature_index is not None:
        _feature_index.close()
        _feature_index = None
    _stop_startup_scan()
    _scan_requested = False
    _stop_folder_job()
    _stop_preparations()
    _stop_mod_icons()
    _EXISTS.clear()
    if _pack_previews is not None and _preview_module is not None:
        try:
            _preview_module.remove(_pack_previews)
        except Exception:
            pass
        _pack_previews = None
    if not bpy:
        return
    _remove_active_pack_load_handlers()
    for prop in (
        'mcbedrock_pack_filters', 'mcbedrock_pack_version_min', 'mcbedrock_pack_version_max',
        'mcbedrock_pack_filter_exclude', 'mcbedrock_pack_match_all', 'mcbedrock_pack_sort_desc', 'mcbedrock_pack_sort',
        "mcbedrock_include_archives",
        "mcbedrock_scan_depth",
        "mcbedrock_list_scroll",
        "mcbedrock_active_pack_path",
        "mcbedrock_active_pack_index",
        "mcbedrock_active_packs",
        "mcbedrock_pack_search",
        "mcbedrock_pack_manager_status",
        "mcbedrock_discovered_pack_index",
        "mcbedrock_discovered_packs",
        "mcbedrock_manual_packs",
    ):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            registered_cls = getattr(bpy.types, cls.__name__, None)
            if registered_cls is not None and registered_cls is not cls:
                try:
                    bpy.utils.unregister_class(registered_cls)
                except Exception:
                    pass

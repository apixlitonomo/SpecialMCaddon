try:
    import bpy
    from bpy.types import Operator, Panel, PropertyGroup, UIList
    from bpy_extras.io_utils import ImportHelper
except ImportError:
    bpy = None
    Operator = Panel = PropertyGroup = UIList = object
    ImportHelper = object


ASSET_FILTER_ITEMS = [
    # Preserve saved enum numbers when removing choices from the menu.
    ("ALL", "All", "Show all indexed assets", 0),
    ("IMPORTABLE", "Importable", "Models, entities, particles, textures, and animations", 1),
    ("MODEL", "Models", "Bedrock and Java model JSON", 2),
    ("ENTITY", "Entities", "Client entities and entity model JSON", 4),
    ("PARTICLE", "Particles", "Particles and particle model JSON", 5),
    ("TEXTURE", "Textures", "Textures and texture sets", 6),
]

VIEW_MODE_ITEMS = [
    ("LIST", "List", "Show assets as a compact list", 'PRESET', 0),
    ("GRID", "Grid", "Show assets as a grid of tiles", 'IMGDISPLAY', 1),
]

INTERPOLATION_ITEMS = [
    ('Closest', "Closest (Pixel Art)", "Minecraft default, sharp pixelated edges"),
    ('Linear', "Linear (Soft/Foggy)", "Soft edges, good for glows, smoke, foggy effects"),
]


import json
import os
import subprocess
import sys
import threading
import zipfile
import time
from collections import OrderedDict, deque, namedtuple

try:
    from bpy.app.handlers import persistent
except ImportError:  # plain Python test path
    def persistent(function):
        return function

# Pack switches reuse bounded in-session snapshots; Refresh always rescans.
_browser_snapshots = OrderedDict()
_browser_prepared = {}
_index_job = None
_index_error = ''
_apply_job = None
# (scene pointer, root, rows) the gallery draws while the scene list fills.
_loaded_rows = None
# Scene-list fill work per timer call. The gallery already shows the rows, so
# short slices keep a pack switch's frames smooth without delaying anything.
_FILL_SLICE_SECONDS = .008

# One prepared browser row; fields mirror MCBEDROCK_PG_browser_asset, so the
# gallery reads rows and scene items alike.
BrowserRow = namedtuple('BrowserRow', ('name', 'asset_type', 'identifier', 'source_path', 'asset_key',
                                       'pack_name', 'status', 'detail', 'importable', 'geometry_index',
                                       'metadata_json'))


def _fill_browser_assets(collection, rows, start=0, deadline=None):
    """Append ``rows[start:]`` until the deadline; return the next row index.

    Writes ID properties directly: RNA setters tag the scene, and the next
    depsgraph update then copies every row (20-50 ms for a large pack).
    """
    add = collection.add
    index, stop = start, len(rows)
    while index < stop:
        item = add()
        (item['name'], item['asset_type'], item['identifier'], item['source_path'], item['asset_key'],
         item['pack_name'], item['status'], item['detail'], item['importable'], item['geometry_index'],
         item['metadata_json']) = rows[index]
        index += 1
        if deadline is not None and not index % 64 and time.monotonic() >= deadline:
            break
    return index


def _finish_browser_fill(scene):
    from .custom_ui import legacy_ui_enabled
    if legacy_ui_enabled(bpy.context):
        scene.mcbedrock_browser_index = 0 if scene.mcbedrock_browser_assets else -1
        rebuild_gallery_rows(scene)
    else:
        # The custom gallery keeps its own selection. Any RNA write to the
        # scene here would make the next depsgraph update copy every row.
        _filtered_views.clear()
    _redraw_ui()


def _apply_browser_batch():
    global _apply_job, _index_error
    job = _apply_job
    if job is None:
        return None
    scene = job['scene']
    try:
        if bpy.context.scene != scene or scene.mcbedrock_browser_root != job['root']:
            _apply_job = None
            return None
        job['next'] = _fill_browser_assets(scene.mcbedrock_browser_assets, job['rows'], job['next'],
                                           deadline=time.monotonic() + _FILL_SLICE_SECONDS)
        if job['next'] < len(job['rows']):
            return 0.0
        _apply_job = None
        _finish_browser_fill(scene)
        return None
    except Exception as exc:
        _apply_job = None
        _index_error = 'Could not load pack: ' + str(exc)
        refresh_browser_pages(scene)
        return None


def finish_browser_fill(scene):
    """Complete a pending scene-list fill before code reads it by index."""
    global _apply_job
    job = _apply_job
    if job is None or job['scene'] != scene or scene.mcbedrock_browser_root != job['root']:
        return
    _apply_job = None
    if bpy.app.timers.is_registered(_apply_browser_batch):
        bpy.app.timers.unregister(_apply_browser_batch)
    _fill_browser_assets(scene.mcbedrock_browser_assets, job['rows'], job['next'])
    _finish_browser_fill(scene)


def _browser_rows_for(scene):
    """Prepared rows that the scene list holds, or is still filling from."""
    loaded = _loaded_rows
    if loaded is None or loaded[0] != scene.as_pointer():
        return None
    _pointer, root, rows = loaded
    if scene.mcbedrock_browser_root != root or scene.get('_mcbedrock_loaded_browser_root') != root:
        return None
    job = _apply_job
    if job is not None and job['scene'] == scene and job['rows'] is rows:
        return rows
    return rows if len(scene.mcbedrock_browser_assets) == len(rows) else None


def browser_index_pending(scene):
    return any(job is not None and job['scene'] == scene for job in (_index_job, _apply_job))


def browser_loading(scene):
    """True while the pack's index is still being read or built (not the list fill)."""
    job = _index_job
    return job is not None and job['scene'] == scene


def notify_sidebar(message):
    """Show ``message`` as Blender's popup from the Sparkle sidebar.

    Timers have no window to open a popup in, so the next sidebar event shows
    it. One session (the visible one) shows it, never every open sidebar.
    """
    from .gpu_framework import runtime
    sessions = [session for session in tuple(runtime._ACTIVE.values())
                if not getattr(session, '_finished', False)]
    target = next((s for s in sessions if getattr(s, '_was_visible', False)),
                  sessions[0] if sessions else None)
    if target is not None:
        target.state['app.status'] = message
        try:
            target._tag_redraw()
        except (AttributeError, ReferenceError):
            pass
    print('[Sparkle] ' + message)


def browser_gallery_pending(scene):
    """True until the gallery has rows to draw: a worker is still indexing."""
    return browser_index_pending(scene) and _browser_rows_for(scene) is None


def refresh_browser_pages(scene=None):
    """Rebuild custom sidebar pages for new browser data (timer-safe)."""
    from .gpu_framework import runtime
    for session in tuple(runtime._ACTIVE.values()):
        try:
            if scene is None or session.window.scene == scene:
                session.state['_tree_dirty'] = True
        except (AttributeError, ReferenceError):
            pass
    _redraw_ui()


def _stop_browser_index():
    global _index_job, _apply_job
    _apply_job = None
    if bpy.app.timers.is_registered(_apply_browser_batch):
        bpy.app.timers.unregister(_apply_browser_batch)
    job, _index_job = _index_job, None
    if job:
        process = job['process']
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for path in (job['request'], job['output']):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    if bpy.app.timers.is_registered(_poll_browser_index):
        bpy.app.timers.unregister(_poll_browser_index)


def _accept_index_result(result, cache_key, assets=None):
    """Adopt an index result (fresh from the worker or saved on disk)."""
    from ..importers.asset_indexer import BrowserAsset
    from ..importers.thumbnail_cache import _DEPENDENCY_FINGERPRINT_CACHE
    from ..importers.brarchive_pack import _VIEWS
    if assets is None:
        assets = [BrowserAsset(**row) for row in result['assets']]
    _DEPENDENCY_FINGERPRINT_CACHE.update(result.get('fingerprints', {}))
    _DEPENDENCY_FINGERPRINT_CACHE.update(
        {('stack', path): fingerprint for path, fingerprint in result.get('stack_fingerprints', [])})
    _VIEWS.update({tuple(key): tuple(value) for key, value in result.get('prepared_views', [])})
    _browser_snapshots[cache_key] = assets
    return assets


def _prepare_rows(assets):
    """The gallery's rows for an index: hidden baby models and repeated records dropped."""
    from ..importers.asset_indexer import browser_asset_key, browser_refresh_identity
    rows = []
    seen = set()
    for asset in assets:
        if asset.asset_type == "Model" and "baby" in f"{asset.name} {asset.identifier}".lower():
            continue
        identity = browser_refresh_identity(asset)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(BrowserRow(asset.name, asset.asset_type, asset.identifier, asset.source_path,
            browser_asset_key(asset), asset.pack_name, asset.status, asset.detail,
            bool(asset.importable), int((asset.metadata or {}).get("geometry_index", -1)),
            json.dumps(asset.metadata or {}, ensure_ascii=True, sort_keys=True)))
    return rows


class _IndexTask:
    """Load one pack's index away from Blender's UI thread.

    An unchanged pack loads from its saved index; otherwise the index worker
    process scans it. Either way the gallery's rows are prepared here too, so
    the UI thread only adopts them (a 20k-asset pack took ~0.4 s there).
    Answers ``poll``/``wait``/``kill`` like the worker process it wraps.
    """

    def __init__(self, root, cache_key, command, flags, output, use_saved, save):
        self.root, self.cache_key, self.command, self.flags = root, cache_key, command, flags
        self.output, self.use_saved, self.save = output, use_saved, save
        self.process = None
        self.result = self.assets = self.rows = self.error = None
        self.fingerprint = ''
        self._killed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def poll(self):
        return None if self._thread.is_alive() else 0

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return self.poll()

    def kill(self):
        with self._lock:
            self._killed = True
            process = self.process
        if process is not None and process.poll() is None:
            process.kill()

    def _prepare(self):
        from ..importers.asset_indexer import BrowserAsset
        if 'error' in self.result:
            return
        self.assets = [BrowserAsset(**row) for row in self.result['assets']]
        self.rows = _prepare_rows(self.assets)

    def _run(self):
        from ..importers import browser_index
        try:
            if self.use_saved or self.save:
                self.fingerprint = browser_index.pack_fingerprint(self.root)
            if self.use_saved:
                self.result = browser_index.load_cached_index(self.cache_key, self.fingerprint)
                if self.result is not None:
                    try:
                        self._prepare()
                    except (TypeError, ValueError, KeyError):
                        # Saved by an older build with different asset fields: rescan.
                        self.result = None
            if self.result is not None:
                return
            with self._lock:
                if self._killed:
                    return
                self.process = subprocess.Popen(self.command, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, creationflags=self.flags)
            self.process.wait()
            if self._killed:
                return
            with open(self.output, encoding='utf-8') as stream:
                self.result = json.load(stream)
            self._prepare()
            if self.fingerprint and 'error' not in self.result:
                # Unchanged packs load straight from this in later sessions.
                threading.Thread(target=browser_index.store_cached_index,
                                 args=(self.cache_key, self.fingerprint, self.result), daemon=True).start()
        except Exception as exc:
            self.error = exc


def start_browser_index(context, root, cache_key, use_saved=False, save=False):
    """Load ``root``'s index in the background; ``use_saved`` tries the saved index first."""
    global _index_job, _index_error
    import tempfile
    if _index_job and _index_job['scene']==context.scene and _index_job['key']==cache_key:
        return
    _stop_browser_index()
    _cancel_thumbnail_preparation()
    _index_error = ''
    handle, request = tempfile.mkstemp(prefix='mc_index_', suffix='.json')
    os.close(handle)
    output = request + '.result'
    with open(request, 'w', encoding='utf-8') as stream:
        from ..importers.brarchive_pack import selections
        json.dump({'root': root, 'vanilla': cache_key[1], 'output': output,
                   'resource_roots': [root],
                   'choices': selections(context.scene)}, stream)
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'importers', 'browser_index_worker.py')
    flags = (getattr(subprocess,'CREATE_NO_WINDOW',0) | getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0)) if os.name=='nt' else 0
    # The indexer never imports bpy, so Blender's bundled Python runs it
    # without the half second a background Blender takes to start.
    python = sys.executable or ''
    if os.path.basename(python).lower().startswith('python') and os.path.isfile(python):
        command = [python, '-I', script, '--', request]
    else:
        command = [bpy.app.binary_path, '--background', '--factory-startup',
            '--threads', '2', '--python', script, '--', request]
    task = _IndexTask(root, cache_key, command, flags, output, use_saved, save)
    _index_job = {'scene':context.scene, 'root':root, 'key':cache_key,
                  'process':task, 'request':request, 'output':output}
    context.scene['_mcbedrock_loaded_browser_root'] = ''
    context.scene.mcbedrock_browser_assets.clear()
    context.scene.mcbedrock_gallery_rows.clear()
    context.scene.mcbedrock_browser_index = -1
    if not bpy.app.timers.is_registered(_poll_browser_index):
        bpy.app.timers.register(_poll_browser_index, first_interval=.05)


def _poll_browser_index():
    global _index_job, _index_error
    job = _index_job
    if not job:
        return None
    task = job['process']
    if task.poll() is None:
        return .05
    _index_job = None
    try:
        if bpy.context.scene != job['scene'] or job['scene'].mcbedrock_browser_root != job['root']:
            return None
        if isinstance(task, _IndexTask):
            if task.error is not None:
                raise task.error
            result = task.result
        else:
            with open(job['output'], encoding='utf-8') as stream:
                result = json.load(stream)
        if result is None:
            raise RuntimeError('the pack index stopped without a result')
        if 'error' in result:
            raise RuntimeError(result['error'])
        assets = _accept_index_result(result, job['key'], getattr(task, 'assets', None))
        rows = getattr(task, 'rows', None)
        if rows is not None:
            # Prepared off the UI thread; the refresh below only adopts them.
            _browser_prepared[job['key']] = (assets, rows)
        bpy.ops.mcbedrock.asset_browser_refresh('EXEC_DEFAULT', use_cached=True, background=True)
    except Exception as exc:
        _index_error = 'Could not load pack: '+str(exc)
        print('[Sparkle] '+_index_error)
    finally:
        for path in (job['request'], job['output']):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        # Show the grid (or the error) now, not at the next periodic rebuild.
        refresh_browser_pages(job['scene'])
    return None


# Thumbnail quality -> pixel size. The size is part of the cache hash, so the
# operator and the icon lookup must agree on it or every lookup misses.
QUALITY_RES = {"LOW": 128, "MEDIUM": 256, "HIGH": 512}


def _thumb_size(scene):
    return QUALITY_RES.get(getattr(scene, "mcbedrock_thumb_quality", "LOW"), 128)


_scene_thumbnail_caches = OrderedDict()


def _cache_for(scene):
    """ThumbnailCache bound to the scene's current thumbnail quality."""
    from ..importers.browser_thumbnail_cache import BrowserThumbnailCache
    from ..importers.brarchive_pack import selections
    from ..importers.bedrock_assets import layered_browser_roots
    # A browsed pack that is switched on shows the packs stacked with it; a
    # change elsewhere in the stack that resolves nothing keeps every thumbnail.
    root = bpy.path.abspath(scene.mcbedrock_browser_root) if scene.mcbedrock_browser_root else ''
    roots = layered_browser_roots(scene, root, bpy.path.abspath)
    key = (scene.as_pointer(), _thumb_size(scene),
           getattr(scene, 'mcbedrock_browser_root', ''),
           getattr(scene, 'mcbedrock_vanilla_assets_path', ''),
           scene.get('mcbedrock_pack_experiences', ''),
           tuple(roots))
    cache = _scene_thumbnail_caches.get(key)
    if cache is None:
        cache = BrowserThumbnailCache(size=_thumb_size(scene),
            resource_roots=roots, choices=selections(scene),
            vanilla_path=bpy.path.abspath(scene.mcbedrock_vanilla_assets_path or ''))
        _scene_thumbnail_caches[key] = cache
    _scene_thumbnail_caches.move_to_end(key)
    while len(_scene_thumbnail_caches) > 8:
        _scene_thumbnail_caches.popitem(last=False)
    return cache


def _selected_browser_item(context):
    scene = context.scene
    finish_browser_fill(scene)
    assets = scene.mcbedrock_browser_assets
    index = scene.mcbedrock_browser_index
    if 0 <= index < len(assets):
        return assets[index]
    return None


def _browser_item_key(item):
    """Return an item's persisted exact browser-record identity."""
    key = getattr(item, "asset_key", "") or ""
    if key:
        return key
    from ..importers.asset_indexer import browser_asset_key
    return browser_asset_key(item)


def _resolve_browser_item_index(
    assets, *, preferred_index=-1, asset_key="", source_path="",
    asset_type="", identifier="", geometry_index=-1,
):
    """Resolve a card without ever degrading to path-only selection."""
    from ..importers.asset_indexer import resolve_browser_asset_index

    if asset_key:
        return resolve_browser_asset_index(assets, preferred_index, asset_key)

    # Legacy saved calls may carry the full tuple. Rebuild its canonical key;
    # a path by itself is deliberately insufficient for multi-record files.
    if source_path and asset_type:
        from ..importers.asset_indexer import BrowserAsset, browser_asset_key
        legacy = BrowserAsset(
            name="", asset_type=asset_type, source_path=source_path,
            identifier=identifier or "", metadata={"geometry_index": int(geometry_index)},
        )
        return resolve_browser_asset_index(
            assets, preferred_index, browser_asset_key(legacy),
        )

    if not source_path:
        return resolve_browser_asset_index(assets, preferred_index, "")
    return -1


_lang_cache = {}
_entity_setup_meta_cache = {}
_entity_capability_cache = {}

def _get_pack_lang(source_path):
    import os
    folder = os.path.dirname(source_path)
    for _ in range(5):
        if os.path.isfile(os.path.join(folder, 'manifest.json')):
            break
        parent = os.path.dirname(folder)
        if parent == folder: break
        folder = parent

    if folder in _lang_cache:
        return _lang_cache[folder]

    lang = {}
    lang_file = os.path.join(folder, 'texts', 'en_US.lang')
    if os.path.isfile(lang_file):
        try:
            with open(lang_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if '=' in line:
                        k, v = line.strip().split('=', 1)
                        lang[k.strip()] = v.strip()
        except: pass
    _lang_cache[folder] = lang
    return lang

def _prettify_animation_name_with_lang(identifier, lang):
    name = lang.get('animation.' + identifier, lang.get(identifier))
    if name:
        return name
    from ..importers.vfs import strip_obfuscation
    raw = strip_obfuscation(identifier or '')
    raw = str(raw).split('.')[-1]
    raw = raw.replace('_', ' ').replace('-', ' ').strip()
    return raw.title() if raw else 'Animation'

def _prettify_animation_name(identifier):
    from ..importers.vfs import strip_obfuscation
    raw = strip_obfuscation(identifier or "")
    raw = str(raw).split(".")[-1]
    raw = raw.replace("_", " ").replace("-", " ").strip()
    return raw.title() if raw else "Animation"


# Blender reads an enum callback's strings after the callback returns, so the
# lists must stay referenced or menu labels turn into garbage. They are also
# reused per file revision instead of parsing the entity JSON on every redraw.
_ANIMATION_NONE = [("__NONE__", "No action", "Use the selected neutral/raw pose without an animation clip")]
_animation_items = OrderedDict()


def _browser_animation_items(self, context):
    if not context:
        return _ANIMATION_NONE
    item = _selected_browser_item(context)
    if not item or item.asset_type != "Entity":
        return _ANIMATION_NONE
    try:
        key = (os.path.normcase(os.path.abspath(item.source_path)), os.path.getmtime(item.source_path))
    except (OSError, ValueError):
        return _ANIMATION_NONE
    items = _animation_items.get(key)
    if items is not None:
        _animation_items.move_to_end(key)
        return items
    items = list(_ANIMATION_NONE)
    try:
        from ..importers.bedrock_assets import BedrockClientEntityParser

        parser = BedrockClientEntityParser(item.source_path)
        seen = set()
        for ref in parser.get_animation_references():
            if not isinstance(ref, str) or not ref.startswith("animation.") or ref in seen:
                continue
            seen.add(ref)
            lang = _get_pack_lang(item.source_path)
            items.append((ref, _prettify_animation_name_with_lang(ref, lang), ref))
        if seen:
            items.append((
                "__ALL__", "All animations / cutscenes",
                "Import all resolved clips as a sequential playable NLA preview",
            ))
    except Exception:
        pass
    _animation_items[key] = items
    while len(_animation_items) > 32:
        # The list returned last is the newest entry, so it is never the one dropped.
        _animation_items.popitem(last=False)
    return items


def _entity_geometry_summary(item):
    """Return a concise geometry/variant summary for the selected entity."""
    if (item.asset_type or "").lower() == "model":
        return item.identifier or ""
    if (item.asset_type or "").lower() != "entity" or not os.path.isfile(item.source_path):
        return ""

    try:
        modified = os.path.getmtime(item.source_path)
    except OSError:
        modified = 0.0
    key = (os.path.normcase(os.path.abspath(item.source_path)), modified)
    cached = _entity_setup_meta_cache.get(key)
    if cached is not None:
        return cached

    summary = ""
    try:
        from ..importers.bedrock_assets import BedrockClientEntityParser

        parser = BedrockClientEntityParser(item.source_path)
        references = []
        for _alias, reference in parser.get_geometry_reference_items():
            if isinstance(reference, str) and reference and reference not in references:
                references.append(reference)
        if len(references) == 1:
            summary = references[0]
        elif references:
            summary = f"{references[0]} (+{len(references) - 1} variants)"
    except Exception:
        summary = ""

    _entity_setup_meta_cache.clear()
    _entity_setup_meta_cache[key] = summary
    return summary


def _entity_setup_capabilities(item):
    """Detect only the entity-state controls referenced by this asset."""
    defaults = {
        "baby": False,
        "variant": False,
        "marking": False,
        "armor": False,
        "saddled": False,
        "rider": False,
        "storage": False,
        "all_variants": False,
    }
    if (item.asset_type or "").lower() != "entity" or not os.path.isfile(item.source_path):
        return defaults

    try:
        modified = os.path.getmtime(item.source_path)
    except OSError:
        modified = 0.0
    key = (os.path.normcase(os.path.abspath(item.source_path)), modified)
    cached = _entity_capability_cache.get(key)
    if cached is not None:
        return cached

    capabilities = dict(defaults)
    try:
        from ..importers.bedrock_assets import BedrockClientEntityParser

        parser = BedrockClientEntityParser(item.source_path)
        geometry_items = [
            (str(alias).lower(), str(reference).lower())
            for alias, reference in parser.get_geometry_reference_items()
            if isinstance(reference, str) and reference
        ]
        text = json.dumps(parser.data, sort_keys=True).lower()
        geometry_text = " ".join(
            token for pair in geometry_items for token in pair
        )
        combined = f"{text} {geometry_text}"

        capabilities["baby"] = any(
            token in combined
            for token in ("query.is_baby", "variable.is_baby", "geometry.baby", "_baby", ".baby")
        )
        non_baby_geometry = {
            reference for alias, reference in geometry_items
            if "baby" not in alias and "baby" not in reference
        }
        capabilities["variant"] = (
            len(non_baby_geometry) > 1
            or "query.variant" in combined
            or "variable.variant" in combined
        )
        capabilities["marking"] = "mark_variant" in combined
        capabilities["armor"] = any(
            token in combined for token in ("armor_slot", "armor_texture", "query.armor")
        )
        capabilities["saddled"] = "is_saddled" in combined
        capabilities["rider"] = any(
            token in combined for token in ("has_rider", "is_riding", "query.rider")
        )
        capabilities["storage"] = any(
            token in combined for token in ("is_chested", "has_chest", "query.storage")
        )
        capabilities["all_variants"] = (
            capabilities["variant"] and len({reference for _alias, reference in geometry_items}) > 1
        )
    except Exception:
        capabilities = dict(defaults)

    _entity_capability_cache[key] = capabilities
    return capabilities


def _icon_for(item):
    asset_type = item.asset_type.lower()
    if item.status in {"Protected", "Unreadable", "Encrypted"}:
        return 'LOCKED'
    if "entity" in asset_type:
        return 'OUTLINER_OB_ARMATURE'
    if "particle" in asset_type:
        return 'PARTICLES'
    if "block" in asset_type:
        return 'MESH_CUBE'
    if "model" in asset_type:
        return 'MESH_CUBE'
    if "animation" in asset_type:
        return 'ACTION'
    if "texture" in asset_type:
        return 'IMAGE_DATA'
    if "pack" in asset_type:
        return 'FILE_FOLDER'
    return 'FILE_BLANK'


# Memoized metadata for the active-pack banner (avoids disk reads on every redraw).
_active_meta_cache = {}


def _read_pack_author(path):
    """Best-effort read of the pack author from a Bedrock manifest."""
    try:
        import json
        manifest = os.path.join(path, "manifest.json")
        if os.path.isfile(manifest):
            with open(manifest, "r", encoding="utf-8-sig", errors="ignore") as handle:
                data = json.load(handle)
            meta = data.get("metadata", {}) if isinstance(data, dict) else {}
            authors = meta.get("authors") or meta.get("author")
            if isinstance(authors, list) and authors:
                return str(authors[0])
            if isinstance(authors, str):
                return authors
    except Exception:
        pass
    return ""


def _active_pack_meta(path):
    """Return cached display metadata (name/icon/author) for the active pack folder."""
    if not path:
        return {}
    key = os.path.normcase(os.path.abspath(path))
    if key in _active_meta_cache:
        return _active_meta_cache[key]
    meta = {"name": os.path.basename(path.rstrip("\\/")), "icon_path": "", "author": ""}
    try:
        from . import pack_manager
        meta = dict(pack_manager._read_pack_metadata(path))
    except Exception:
        pass
    meta.setdefault("author", "")
    if not meta["author"]:
        meta["author"] = _read_pack_author(path)
    _active_meta_cache[key] = meta
    return meta


# Preview collection holding loaded thumbnail PNGs (icon_id lookups for the grid).
_thumb_previews = None


def _clear_thumb_previews():
    """Free the loaded thumbnail previews so fresh renders reload from disk."""
    global _thumb_previews
    if _thumb_previews is not None:
        try:
            import bpy.utils.previews as previews
            previews.remove(_thumb_previews)
        except Exception:
            pass
        _thumb_previews = None


def _asset_thumbnail_id(item):
    """Preview icon id for a browser asset's cached thumbnail, or 0 if none.

    Resolves the cache at the scene's current thumbnail quality: the pixel size
    is baked into the cache hash, so looking up at a different size than the
    worker rendered would miss forever.
    """
    global _thumb_previews
    if not _br_metadata(item).get('br_available', True):
        return 0
    try:
        path = asset_thumbnail_path(bpy.context.scene, item)
        if not path and item.asset_type == 'Texture' and os.path.isfile(item.source_path):
            # Blender's preview icons shrink the texture themselves; no need to
            # wait for the sidebar's downscaled copy.
            path = item.source_path
    except Exception:
        path = ""
    if not path:
        return 0
    try:
        import bpy.utils.previews as previews
        if _thumb_previews is None:
            _thumb_previews = previews.new()
        if path not in _thumb_previews:
            _thumb_previews.load(path, path, 'IMAGE')
        icon_id = _thumb_previews[path].icon_id
        if not icon_id:
            try:
                del _thumb_previews[path]
            except Exception:
                pass
            _thumb_previews.load(path, path, 'IMAGE')
            icon_id = _thumb_previews[path].icon_id
        return icon_id or 0
    except Exception:
        return 0


def is_mod_asset(item):
    """A mod's mob, model, block or item: listed, drawn and imported through its
    JAR (``mod_thumbnails``, ``mcbedrock.import_mod_jar``). Its identifier is the
    mod catalog's key."""
    return (getattr(item, 'asset_type', '') or '').startswith('Mod ')


def asset_thumbnail_path(scene, item):
    """The card's thumbnail PNG, or '' while it is pending (or failed)."""
    if is_mod_asset(item):
        from . import mod_thumbnails
        return mod_thumbnails.thumbnail(item.source_path, item.identifier)
    return _cache_for(scene).get_thumbnail_path(
        item.source_path, item.asset_type, item.identifier, item.geometry_index)


def thumbnails_gave_up(item):
    """The workers that render this card could not start: it keeps its placeholder."""
    if is_mod_asset(item):
        from . import mod_thumbnails
        return mod_thumbnails.gave_up()
    return _pool["gave_up"]


def _asset_thumb_failed(item):
    """True when the worker tried this asset and could not render it.

    Distinguishes a genuine render failure (error icon) from an asset that is
    merely still queued (neutral type icon).
    """
    try:
        if is_mod_asset(item):
            from . import mod_thumbnails
            return mod_thumbnails.failed(item.source_path, item.identifier)
        cache = _cache_for(bpy.context.scene)
        return cache.has_failed(item.source_path, item.asset_type, item.identifier, item.geometry_index)
    except Exception:
        return False


def _thumb_fallback_icon(item):
    """Icon when no rendered thumbnail exists.

    Failed renders used to show the ERROR warning triangle, which made whole
    packs (e.g. Origin Realms custom models) look broken. Use the normal type
    icon instead; locked/unreadable assets still get LOCKED via ``_icon_for``.
    """
    return _icon_for(item)


GALLERY_COLUMNS = 2
GALLERY_ROWS = 2
GALLERY_PAGE_SIZE = GALLERY_COLUMNS * GALLERY_ROWS
# Card fill requested for gallery tiles (#272a31).
GALLERY_CARD_RGBA = (0x27 / 255.0, 0x2A / 255.0, 0x31 / 255.0, 1.0)


def _push_gallery_box_color():
    """Tint theme widget fills used by gallery cards to ``#272a31``.

    Blender layouts cannot set a per-box hex color, so we briefly override the
    theme fills that ``layout.box()`` / list cells read, then restore them.
    Returns a list of ``(wcol, old_inner)`` pairs for :func:`_pop_gallery_box_color`.
    """
    saved = []
    from .custom_ui import _addon_preferences
    if getattr(_addon_preferences(bpy.context), 'ui_theme', 'BLENDER') == 'BLENDER':
        return saved
    try:
        ui = bpy.context.preferences.themes[0].user_interface
    except Exception:
        return saved
    for name in ("wcol_box", "wcol_list_item", "wcol_tool", "wcol_regular"):
        wcol = getattr(ui, name, None)
        if wcol is None:
            continue
        try:
            old_inner = tuple(wcol.inner)
            # Prefer RGBA; some Blender builds only keep RGB.
            try:
                wcol.inner = GALLERY_CARD_RGBA
            except Exception:
                wcol.inner = GALLERY_CARD_RGBA[:3]
            saved.append((wcol, old_inner))
        except Exception:
            continue
    return saved


def _pop_gallery_box_color(saved):
    for wcol, old_inner in saved or ():
        try:
            wcol.inner = old_inner
        except Exception:
            pass


def _gallery_card_label(item):
    label = item.name or os.path.basename(item.source_path or "")
    if len(label) > 22:
        label = label[:21] + "…"
    return label


def _item_is_configurable(item):
    """Entities expose render variants and animation/cutscene choices."""
    return (item.asset_type or "").lower() == "entity"


def _br_metadata(item):
    try:
        return json.loads(getattr(item, 'metadata_json', '{}') or '{}')
    except (ValueError, TypeError):
        return {}


_br_enum_items = []


def _br_dialog_item(properties, context):
    # RNA callbacks receive OperatorProperties, not the Python operator instance.
    if context is None:
        return None
    finish_browser_fill(context.scene)
    assets = context.scene.mcbedrock_browser_assets
    index = getattr(properties, 'index', -1)
    return assets[index] if 0 <= index < len(assets) else None


def _br_experience_items(self, context):
    global _br_enum_items
    item = _br_dialog_item(self, context)
    _br_enum_items = [(key, name, '') for key, name in _br_metadata(item).get('br_options', [])]
    return _br_enum_items or [('BASE', 'Default', '')]


def _br_experience_changed(self, context):
    from ..importers.brarchive_pack import selections, selected
    item = _br_dialog_item(self, context)
    root = _br_metadata(item).get('br_root')
    if not root:
        return
    scene = context.scene
    choices = selections(scene)
    if selected(root, choices) == self.pack_experience:
        return
    choices[os.path.normcase(os.path.abspath(root))] = self.pack_experience
    scene['mcbedrock_pack_experiences'] = json.dumps(choices)
    # The scene list diverges from the prepared rows here; the gallery reads it.
    global _loaded_rows
    _loaded_rows = None
    for row in scene.mcbedrock_browser_assets:
        meta = _br_metadata(row)
        if meta.get('br_root') != root:
            continue
        variant = meta['br_variants'].get(self.pack_experience)
        meta['br_available'] = variant is not None
        if variant:
            for field in ('name', 'source_path', 'identifier', 'status', 'detail', 'importable'):
                setattr(row, field, variant[field])
            row.geometry_index = int(variant['metadata'].get('geometry_index', -1))
            updated = dict(variant['metadata'])
            updated.update({k: v for k, v in meta.items() if k.startswith('br_')})
            meta = updated
        else:
            row.status = 'Choose Experience'
            row.detail = 'Unavailable in this Pack Experience. Choose another experience in Import.'
        row.metadata_json = json.dumps(meta)
    _entity_setup_meta_cache.clear()
    _entity_capability_cache.clear()
    scene.mcbedrock_browser_animation = '__NONE__'
    _clear_thumb_previews()
    rebuild_gallery_rows(scene)
    global _visible_thumbnail_signature
    _visible_thumbnail_signature = None
    _redraw_ui()  # The gallery schedules only visible cards on the next layout.


def _item_has_setup(item):
    """Only client entities need the variant/animation setup dialog."""
    return (item.asset_type or "").lower() == "entity" or bool(_br_metadata(item).get('br_options'))


def _draw_thumbnail_preview(col, item):
    """Draw a fixed-height rendered preview or an actionable neutral fallback."""
    preview = col.column(align=True)
    preview.alignment = "CENTER"
    icon_id = _asset_thumbnail_id(item)
    if icon_id:
        row = preview.row(align=True)
        row.alignment = "CENTER"
        row.template_icon(icon_value=icon_id, scale=3.6)
        return

    scene = bpy.context.scene
    generating = bool(getattr(scene, "mcbedrock_thumb_generating", False))
    failed = _asset_thumb_failed(item)

    placeholder = preview.row(align=True)
    placeholder.alignment = "CENTER"
    placeholder.scale_y = 1.65
    placeholder.label(text="", icon=_thumb_fallback_icon(item))

    status = preview.row(align=True)
    status.alignment = "CENTER"
    status.scale_y = 0.85
    if generating and not failed:
        status.label(text="Generating preview...", icon="TIME")
    elif failed:
        status.label(text="Preview unavailable")
    else:
        status.label(text="Preview not generated")


def _draw_gallery_card_contents(col, item, index, scene):
    """Draw one independent card with its established import action."""
    header = col.row(align=True)
    header.alignment = "LEFT"
    header.label(text=_gallery_card_label(item))

    _draw_thumbnail_preview(col, item)

    btn = col.row(align=True)
    btn.scale_y = 1.15
    btn.enabled = bool(item.importable) and item.status not in {"Unreadable", "Protected"}
    operator_id = (
        "mcbedrock.asset_browser_import_dialog"
        if _item_has_setup(item)
        else "mcbedrock.asset_browser_import_selected"
    )
    import_op = btn.operator(
        operator_id,
        text="Import",
        icon="IMPORT",
    )
    import_op.index = index
    import_op.source_path = item.source_path
    import_op.asset_key = _browser_item_key(item)
    import_op.asset_type = item.asset_type
    import_op.asset_identifier = item.identifier
    import_op.geometry_index = item.geometry_index


def _draw_one_gallery_tile(layout, item, index, scene=None):
    """Draw a single gallery tile into ``layout``."""
    scene = scene or bpy.context.scene
    saved = _push_gallery_box_color()
    try:
        card = layout.box()
        col = card.column(align=True)
        _draw_gallery_card_contents(col, item, index, scene)
    finally:
        _pop_gallery_box_color(saved)


def _gallery_max_scroll(scene):
    # The filtered view is kept per filter and search: no pass over every row.
    visible_count = len(filtered_browser_view(scene))
    total_rows = max(1, (visible_count + GALLERY_COLUMNS - 1) // GALLERY_COLUMNS)
    return max(0, total_rows - GALLERY_ROWS)


def _draw_gallery_grid(layout, scene):
    """Draw a native two-column thumbnail grid with Blender scrolling."""
    # Blender 5.1 removed ``GRID`` from UILayout.template_list (only
    # DEFAULT/COMPACT are accepted). Each entry is already a two-card row,
    # preserving the 2x2 gallery while using Blender's real scrollbar.
    layout.template_list(
        "MCBEDROCK_UL_asset_browser_gallery",
        "mcbedrock_asset_browser_grid",
        scene,
        "mcbedrock_gallery_rows",
        scene,
        "mcbedrock_gallery_row_index",
        rows=2,
        maxrows=2,
    )
    footer = layout.row()
    footer.active = False
    # Drawn on every redraw (hover included): count the kept view, not every row.
    footer.label(text=f"{len(filtered_browser_view(scene))} assets")


def _browser_item_visible(item, asset_filter, query):
    """Same visibility rules as the UIList filter."""
    asset_type = (item.asset_type or "").lower()
    if asset_filter == "ALL":
        visible = True
    elif asset_filter == "IMPORTABLE":
        visible = bool(item.importable)
    elif asset_filter == "MODEL":
        visible = (
            ("model" in asset_type or asset_type in {"mod block", "mod item"})  # as Java block/item models
            and "entity" not in asset_type
            and "particle" not in asset_type
        )
    elif asset_filter == "BLOCK":
        visible = "block" in asset_type
    elif asset_filter == "ENTITY":
        visible = "entity" in asset_type
    elif asset_filter == "PARTICLE":
        visible = "particle" in asset_type
    elif asset_filter == "ANIMATION":
        visible = "animation" in asset_type
    elif asset_filter == "TEXTURE":
        visible = "texture" in asset_type
    elif asset_filter == "PACK":
        visible = "pack" in asset_type
    elif asset_filter == "LOCKED":
        visible = item.status in {"Unreadable", "Protected"} or "locked" in asset_type
    else:
        visible = True
    if visible and query:
        haystack = " ".join(
            (item.name, item.asset_type, item.identifier, item.pack_name, item.detail)
        ).lower()
        visible = query in haystack
    return visible


_filtered_views = {}
# Views over prepared rows outlive pack switches: returning to a pack reuses one.
_row_views = OrderedDict()
# The native list's filter flags, kept with the view they were made from.
_list_flags = {}


class _BrowserAssetView:
    """A virtual grid index: fetch only the cards actually being displayed.

    Cards come from the prepared rows when given, otherwise from scene RNA.
    """
    def __init__(self, scene, indices, identities, rows=None):
        self.scene = scene
        self.indices = indices
        self.identities = identities
        self.rows = rows

    def __len__(self):
        return len(self.indices)

    def _item(self, index):
        return self.rows[index] if self.rows is not None else self.scene.mcbedrock_browser_assets[index]

    def __getitem__(self, position):
        index = self.indices[position]
        return index, self._item(index)

    def selected(self, identity):
        index = self.identities.get(identity)
        return (index, self._item(index)) if index is not None else None


def filtered_browser_view(scene):
    rows = _browser_rows_for(scene)
    if rows is not None:
        # Filtering Python rows is several times cheaper than walking every RNA
        # item (13k-20k in a large pack), and the view is kept for revisits.
        key = (id(rows), scene.mcbedrock_browser_filter, scene.mcbedrock_browser_search)
        view = _row_views.get(key)
        if view is None or view.rows is not rows:
            query = (scene.mcbedrock_browser_search or "").lower().strip()
            asset_filter = scene.mcbedrock_browser_filter
            indices, identities = [], {}
            for index, row in enumerate(rows):
                if _browser_item_visible(row, asset_filter, query):
                    indices.append(index)
                    identities.setdefault(_browser_item_key(row), index)
            view = _row_views[key] = _BrowserAssetView(scene, indices, identities, rows)
            while len(_row_views) > 6:
                _row_views.popitem(last=False)
        _row_views.move_to_end(key)
        return view
    key = (scene.mcbedrock_browser_root, len(scene.mcbedrock_browser_assets),
           scene.mcbedrock_browser_filter, scene.mcbedrock_browser_search,
           scene.get('_mcbedrock_gallery_revision', 0))
    pointer = (scene.as_pointer(), key)
    cached = _filtered_views.get(pointer)
    if cached is None:
        indices, identities = [], {}
        for index, item in _iter_filtered_browser_assets(scene):
            indices.append(index)
            identities.setdefault(_browser_item_key(item), index)
        cached = key, _BrowserAssetView(scene, indices, identities)
        # Bounded filter views contain indices, not retained asset RNA.
        while len(_filtered_views) >= 8:
            _filtered_views.pop(next(iter(_filtered_views)))
        _filtered_views[pointer] = cached
    return cached[1]


def _iter_filtered_browser_assets(scene):
    """Yield ``(collection_index, item)`` for assets matching filter + search."""
    query = (scene.mcbedrock_browser_search or "").lower().strip()
    asset_filter = scene.mcbedrock_browser_filter
    for index, item in enumerate(scene.mcbedrock_browser_assets):
        if _browser_item_visible(item, asset_filter, query):
            yield index, item


def rebuild_gallery_rows(scene, native=True):
    """Rebuild 2-up gallery rows from the current filter/search.

    Each row is one UIList entry containing up to two asset indices, so a normal
    scrollable list with ``rows=2`` shows a true 2x2 gallery.
    """
    _filtered_views.clear()
    scene['_mcbedrock_gallery_revision'] = scene.get('_mcbedrock_gallery_revision', 0) + 1
    if not native:
        return
    if not hasattr(scene, "mcbedrock_gallery_rows"):
        return
    rows = scene.mcbedrock_gallery_rows
    rows.clear()
    indices = filtered_browser_view(scene).indices
    add = rows.add
    # ID property writes: RNA setters run update code per write, and this runs
    # per keystroke in the native search (thousands of rows in a large pack).
    for i in range(0, len(indices), GALLERY_COLUMNS):
        row = add()
        row['left_index'] = indices[i]
        row['right_index'] = indices[i + 1] if i + 1 < len(indices) else -1
    # The UIList active row is deliberately not a model selection. Import
    # buttons carry their exact collection index, preventing left/right cards
    # from fighting over the selected entity.
    scene.mcbedrock_gallery_row_index = -1


def _on_gallery_filter_changed(self, context):
    """Search / filter / view-mode update: restart at the first gallery page."""
    scene = context.scene
    from .custom_ui import legacy_ui_enabled
    if legacy_ui_enabled(context):
        scene.mcbedrock_gallery_scroll = 0
        rebuild_gallery_rows(scene)
    else:
        # The native gallery is hidden: skip its full pass per keystroke, and
        # start the sidebar grid's new results at the top.
        from .gpu_framework import runtime
        for session in tuple(runtime._ACTIVE.values()):
            for key in ('assets.library.scroll', 'app.scroll.assets'):
                session.state.pop(key, None)
    if context.area:
        context.area.tag_redraw()


def _on_gallery_row_index_changed(self, context):
    """Selecting a gallery row focuses the left-hand asset (for animation UI)."""
    scene = context.scene
    idx = scene.mcbedrock_gallery_row_index
    rows = scene.mcbedrock_gallery_rows
    if 0 <= idx < len(rows) and rows[idx].left_index >= 0:
        scene.mcbedrock_browser_index = rows[idx].left_index


def _on_browser_index_changed(self, context):
    """Reset per-entity clip state whenever the selected browser item changes."""
    scene = context.scene
    try:
        scene.mcbedrock_browser_animation = "__NONE__"
    except Exception:
        pass
    if context.area:
        context.area.tag_redraw()


def _on_gallery_scroll_changed(self, context):
    """Clamp the gallery page slider to the current filter window."""
    scene = context.scene
    max_scroll = _gallery_max_scroll(scene)
    if scene.mcbedrock_gallery_scroll > max_scroll:
        scene.mcbedrock_gallery_scroll = max_scroll


def _active_pack_subtitle(meta):
    """Banner secondary line: 'By {author}' when available, else edition kind."""
    author = (meta.get("author") or "").strip()
    if author.lower().startswith("by "):
        author = author[3:].strip()
    if author:
        return f"By {author}"
    kind = (meta.get("kind") or "").strip()
    if "bedrock" in kind.lower():
        return "Bedrock"
    if "java" in kind.lower():
        return "Java"
    return ""


class MCBEDROCK_PG_browser_asset(PropertyGroup):
    name: bpy.props.StringProperty(default="")
    asset_type: bpy.props.StringProperty(default="")
    identifier: bpy.props.StringProperty(default="")
    source_path: bpy.props.StringProperty(default="", subtype='FILE_PATH')
    asset_key: bpy.props.StringProperty(default="")
    pack_name: bpy.props.StringProperty(default="")
    status: bpy.props.StringProperty(default="")
    detail: bpy.props.StringProperty(default="")
    importable: bpy.props.BoolProperty(default=False)
    geometry_index: bpy.props.IntProperty(default=-1)
    metadata_json: bpy.props.StringProperty(default="")


class MCBEDROCK_PG_gallery_row(PropertyGroup):
    """One scrollable gallery row holding up to two asset collection indices."""
    left_index: bpy.props.IntProperty(default=-1)
    right_index: bpy.props.IntProperty(default=-1)


class _MCBEDROCK_UL_asset_browser_base(UIList):
    """Shared draw/filter logic for the compact (list) asset browser."""

    def _draw_row(self, layout, item):
        row = layout.row(align=True)
        row.enabled = item.status not in {"Unreadable", "Protected"}
        row.label(text=item.name, icon=_thumb_fallback_icon(item))

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        self._draw_row(layout, item)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        if propname == 'mcbedrock_browser_assets' and data == context.scene:
            # Blender filters on every redraw (hover included). Reuse the kept
            # filtered view instead of reading every row of a large pack again.
            view = filtered_browser_view(context.scene)
            key = (type(self).__name__, len(items), self.bitflag_filter_item)
            cached = _list_flags.get(key)
            if cached is not None and cached[0] is view:
                return cached[1], []
            flags = [0] * len(items)
            for index in view.indices:
                if index < len(flags):
                    flags[index] = self.bitflag_filter_item
            _list_flags.clear()
            _list_flags[key] = (view, flags)
            return flags, []
        flags = []
        asset_filter = context.scene.mcbedrock_browser_filter
        query = context.scene.mcbedrock_browser_search.lower().strip()
        for item in items:
            visible = self._matches_filter(item, asset_filter)
            if query:
                haystack = " ".join(
                    (item.name, item.asset_type, item.identifier, item.pack_name, item.detail)
                ).lower()
                visible = visible and query in haystack
            flags.append(self.bitflag_filter_item if visible else 0)
        return flags, []

    def _matches_filter(self, item, asset_filter):
        return _browser_item_visible(item, asset_filter, "")


class MCBEDROCK_UL_asset_browser_default(_MCBEDROCK_UL_asset_browser_base):
    pass


class MCBEDROCK_UL_asset_browser_gallery(UIList):
    """Scrollable gallery: each list entry is a row of up to two tiles."""

    def draw_filter(self, context, layout):
        # Search and filtering already live in the toolbar above the gallery.
        # Keep Blender's native scrollbar without duplicating those controls.
        pass

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        assets = context.scene.mcbedrock_browser_assets
        # Non-aligned columns keep Blender from visually merging two adjacent
        # card boxes into one control.
        split = layout.split(factor=0.5, align=False)
        left = split.column(align=False)
        right = split.column(align=False)

        if 0 <= item.left_index < len(assets):
            _draw_one_gallery_tile(left, assets[item.left_index], item.left_index)
        else:
            left.label(text="")

        if 0 <= item.right_index < len(assets):
            _draw_one_gallery_tile(right, assets[item.right_index], item.right_index)
        else:
            right.label(text="")


class MCBEDROCK_UL_asset_browser_grid(_MCBEDROCK_UL_asset_browser_base):
    """Native thumbnail grid with Blender-owned selection and scrolling."""

    def draw_filter(self, context, layout):
        pass

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        col = layout.column(align=True)
        col.alignment = "CENTER"
        col.label(text=_gallery_card_label(item))
        icon_id = _asset_thumbnail_id(item)
        if icon_id:
            col.template_icon(icon_value=icon_id, scale=3.2)
        else:
            col.label(text="", icon=_thumb_fallback_icon(item))


class MCBEDROCK_OT_asset_browser_select(Operator):
    """Select an asset browser item (gallery card header)."""
    bl_idname = "mcbedrock.asset_browser_select"
    bl_label = "Select Asset"
    bl_options = {"REGISTER", "INTERNAL"}

    index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        assets = getattr(context.scene, "mcbedrock_browser_assets", ())
        index = getattr(properties, "index", -1)
        if 0 <= index < len(assets):
            item = assets[index]
            identifier = f"\n{item.identifier}" if item.identifier else ""
            return f"Select {item.name} to review model, variant, and animation options{identifier}"
        return "Select this asset and review its import options"


    def execute(self, context):
        finish_browser_fill(context.scene)
        assets = context.scene.mcbedrock_browser_assets
        if 0 <= self.index < len(assets):
            context.scene.mcbedrock_browser_index = self.index
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_OT_asset_browser_import_dialog(Operator):
    """Choose model variants and animation before importing this asset"""
    bl_idname = "mcbedrock.asset_browser_import_dialog"
    bl_label = "Import Minecraft Asset"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})
    source_path: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    asset_key: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    asset_type: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    asset_identifier: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    geometry_index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})
    pack_experience: bpy.props.EnumProperty(name='Pack Experience', items=_br_experience_items,
                                            update=_br_experience_changed)

    def _item(self, context):
        return _br_dialog_item(self, context)

    def invoke(self, context, event):
        finish_browser_fill(context.scene)
        assets = context.scene.mcbedrock_browser_assets
        self.index = _resolve_browser_item_index(
            assets,
            preferred_index=self.index,
            asset_key=self.asset_key,
            source_path=self.source_path,
            asset_type=self.asset_type,
            identifier=self.asset_identifier,
            geometry_index=self.geometry_index,
        )
        if self.index < 0 or self.index >= len(assets):
            self.report({"ERROR"}, "No browser asset selected")
            return {"CANCELLED"}
        scene = context.scene
        if self.index != scene.mcbedrock_browser_index:
            scene.mcbedrock_browser_index = self.index

        # Start every entity from a predictable state so choices from another
        # model never leak into this dialog.
        scene.mcbedrock_preview_preset = "ADULT"
        scene.mcbedrock_preview_variant = 0
        scene.mcbedrock_preview_mark_variant = 0
        scene.mcbedrock_preview_armor_slot = 0
        scene.mcbedrock_preview_is_baby = False
        scene.mcbedrock_preview_is_saddled = False
        scene.mcbedrock_preview_has_rider = False
        scene.mcbedrock_preview_is_chested = False
        scene.mcbedrock_import_all_variants = False
        from ..importers.brarchive_pack import selected, selections
        root = _br_metadata(self._item(context)).get('br_root')
        if root and _br_metadata(self._item(context)).get('br_options'):
            self.pack_experience = selected(root, selections(scene))
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        item = self._item(context)
        if not item:
            layout.label(text="Asset is no longer available", icon="ERROR")
            return

        layout.label(text=item.name, icon=_icon_for(item))
        if item.asset_type == "Texture":
            layout.label(text="Imports a flat textured plane, not a 3D model", icon="IMAGE_DATA")
        metadata = _br_metadata(item)
        if metadata.get('br_options'):
            layout.prop(self, 'pack_experience', text='Pack Experience')
            if not metadata.get('br_available', True):
                layout.label(text='Unavailable in this experience. Choose another.', icon='ERROR')
                return
        if item.identifier:
            layout.label(text=item.identifier)
        geometry = _entity_geometry_summary(item)
        if geometry and geometry != item.identifier:
            layout.label(text=f"Geometry: {geometry}")

        capabilities = _entity_setup_capabilities(item)
        if capabilities["baby"]:
            layout.prop(scene, "mcbedrock_preview_preset", text="Appearance")

        show_state_options = (
            not capabilities["baby"]
            or scene.mcbedrock_preview_preset == "CUSTOM"
        )
        optional_fields = [
            ("variant", "mcbedrock_preview_variant", "Variant"),
            ("marking", "mcbedrock_preview_mark_variant", "Marking"),
            ("armor", "mcbedrock_preview_armor_slot", "Armor"),
            ("saddled", "mcbedrock_preview_is_saddled", "Saddled"),
            ("rider", "mcbedrock_preview_has_rider", "Rider"),
            ("storage", "mcbedrock_preview_is_chested", "Storage"),
        ]
        visible_fields = [
            (property_name, label)
            for capability, property_name, label in optional_fields
            if capabilities[capability]
        ]
        if show_state_options and visible_fields:
            options = layout.box()
            for offset in range(0, len(visible_fields), 2):
                row = options.row(align=True)
                for property_name, label in visible_fields[offset:offset + 2]:
                    row.prop(scene, property_name, text=label)

        animation_items = _browser_animation_items(None, context)
        if len(animation_items) > 1:
            layout.prop(
                scene,
                "mcbedrock_browser_animation",
                text="Animation / Cutscene",
            )

        if capabilities["all_variants"]:
            layout.prop(
                scene,
                "mcbedrock_import_all_variants",
                text="Import All Variants",
            )
    def execute(self, context):
        item = self._item(context)
        if not item:
            self.report({"ERROR"}, "Asset is no longer available")
            return {"CANCELLED"}
        result = bpy.ops.mcbedrock.asset_browser_import_selected(
            "EXEC_DEFAULT",
            index=self.index,
            source_path=item.source_path,
            asset_key=_browser_item_key(item),
            asset_type=item.asset_type,
            asset_identifier=item.identifier,
            geometry_index=item.geometry_index,
        )
        if "FINISHED" in result:
            active = context.view_layer.objects.active
            preview_frame = (
                int(active.get("mcbedrock_preview_frame", 0))
                if active is not None
                and bool(active.get("mcbedrock_is_emitter", False))
                else 0
            )
            context.scene.frame_set(preview_frame or 1)
        return result



class MCBEDROCK_OT_asset_browser_gallery_scroll(Operator):
    """Scroll the gallery grid by one row (two assets)."""
    bl_idname = "mcbedrock.asset_browser_gallery_scroll"
    bl_label = "Scroll Gallery"
    bl_options = {"REGISTER", "INTERNAL"}

    delta: bpy.props.IntProperty(default=1, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        scene = context.scene
        max_scroll = _gallery_max_scroll(scene)
        scene.mcbedrock_gallery_scroll = min(
            max(0, int(scene.mcbedrock_gallery_scroll) + int(self.delta)),
            max_scroll,
        )
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_OT_asset_browser_set_root(Operator, ImportHelper):
    """Choose the folder scanned by the asset browser"""
    bl_idname = "mcbedrock.asset_browser_set_root"
    bl_label = "Set Browser Folder"
    bl_options = {'REGISTER'}

    filter_glob: bpy.props.StringProperty(default="*.*", options={'HIDDEN'}, maxlen=255)
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        import os
        folder = os.path.dirname(self.filepath) if getattr(self, "filepath", "") else self.directory
        if not folder:
            self.report({'ERROR'}, "No folder selected")
            return {'CANCELLED'}

        context.scene.mcbedrock_browser_root = folder
        result = bpy.ops.mcbedrock.asset_browser_refresh("EXEC_DEFAULT")
        if "FINISHED" in result:
            self.report({'INFO'}, f"Asset browser refreshed: {folder}")
        return result


class MCBEDROCK_OT_asset_browser_set_pack(Operator, ImportHelper):
    """Choose a custom resource pack and refresh the asset gallery"""
    bl_idname = "mcbedrock.asset_browser_set_pack"
    bl_label = "Set Resource Pack"
    bl_description = "Choose a custom Bedrock or Java resource-pack folder"
    bl_options = {"REGISTER"}

    filter_glob: bpy.props.StringProperty(
        default="*.*",
        options={"HIDDEN"},
        maxlen=255,
    )
    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def execute(self, context):
        folder = (
            os.path.dirname(self.filepath)
            if getattr(self, "filepath", "")
            else self.directory
        )
        if not folder:
            self.report({"ERROR"}, "No resource-pack folder selected")
            return {"CANCELLED"}

        from ..importers.bedrock_assets import find_resource_pack_root

        pack_root = find_resource_pack_root("", folder)
        if not pack_root:
            self.report({"ERROR"}, "That folder does not contain a Minecraft resource pack")
            return {"CANCELLED"}

        scene = context.scene
        scene.mcbedrock_resource_pack_path = pack_root
        scene.mcbedrock_browser_root = pack_root
        result = bpy.ops.mcbedrock.asset_browser_refresh("EXEC_DEFAULT")
        if "FINISHED" in result:
            self.report({"INFO"}, f"Resource pack active: {pack_root}")
        return result



def reset_browser_state(state):
    """Forget browser selection and scrolling without changing the panel layout."""
    for key in list(state):
        if key.startswith('assets.') or key == 'app.scroll.assets':
            state.pop(key, None)
    for key in ('_menu', '_runtime_focus', '_runtime_pressed', '_runtime_hover'):
        state.pop(key, None)
    state['_tree_dirty'] = True


class MCBEDROCK_OT_asset_browser_reset(Operator):
    """Stop scanning and clear the asset browser; keep files, scene objects and resource packs"""
    bl_idname = "mcbedrock.asset_browser_reset"
    bl_label = "Reset Asset Browser"

    def execute(self, context):
        global _index_error, _loaded_rows
        scene = context.scene
        # Cancel workers and pending batches before changing their destination.
        _stop_worker()
        _browser_snapshots.clear()
        _browser_prepared.clear()
        _row_views.clear()
        _loaded_rows = None
        _entity_setup_meta_cache.clear()
        _entity_capability_cache.clear()
        _active_meta_cache.clear()
        _clear_thumb_previews()
        _index_error = ''
        scene.mcbedrock_browser_assets.clear()
        scene.mcbedrock_gallery_rows.clear()
        scene.mcbedrock_browser_root = ''
        if '_mcbedrock_loaded_browser_root' in scene:
            del scene['_mcbedrock_loaded_browser_root']
        scene.mcbedrock_browser_search = ''
        scene.mcbedrock_browser_filter = 'IMPORTABLE'
        scene.mcbedrock_gallery_scroll = 0
        scene.mcbedrock_browser_index = -1
        scene.mcbedrock_gallery_row_index = -1
        from .gpu_framework import runtime
        for session in runtime._ACTIVE.values():
            if session.window.scene == scene:
                reset_browser_state(session.state)
        _redraw_ui()
        self.report({'INFO'}, "Asset browser reset; files and resource packs kept")
        return {'FINISHED'}


class MCBEDROCK_OT_asset_browser_refresh(Operator):
    """Scan the selected folder for readable Minecraft assets"""
    bl_idname = "mcbedrock.asset_browser_refresh"
    bl_label = "Refresh Asset Browser"
    bl_options = {'REGISTER'}

    use_cached: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    background: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        from ..importers.thumbnail_cache import invalidate_dependency_fingerprints
        global _apply_job
        _apply_job = None
        if bpy.app.timers.is_registered(_apply_browser_batch):
            bpy.app.timers.unregister(_apply_browser_batch)

        root = context.scene.mcbedrock_browser_root
        if not root:
            self.report({'ERROR'}, "Set an asset browser folder first")
            return {'CANCELLED'}

        root = bpy.path.abspath(root)
        if not os.path.isdir(root):
            self.report({'ERROR'}, "Resource pack folder is unavailable: " + root)
            return {'CANCELLED'}
        if not self.use_cached:
            _scene_thumbnail_caches.clear()
        cache_key = (os.path.normcase(os.path.abspath(root)),
                     getattr(context.scene, "mcbedrock_vanilla_assets_path", ""))
        from ..importers.brarchive_pack import selections, clear_views, has_archives
        if has_archives(root):
            cache_key += (json.dumps(selections(context.scene), sort_keys=True),)
        if _index_job and (_index_job['key'] != cache_key or not self.background):
            _stop_browser_index()
        assets = _browser_snapshots.get(cache_key) if self.use_cached else None
        if assets is None and self.background:
            # Fingerprinting the pack, reading its saved index and preparing
            # rows all happen on a helper thread; "Loading assets…" shows meanwhile.
            archives = has_archives(root)
            start_browser_index(context, root, cache_key,
                                use_saved=self.use_cached and not archives, save=not archives)
            return {'FINISHED'}
        if assets is None:
            # Explicit Refresh rereads dependencies; cached switches reuse them.
            _scene_thumbnail_caches.clear()
            invalidate_dependency_fingerprints()
            clear_views()
            from ..importers.browser_index import scan_browser_assets
            assets = scan_browser_assets(root, cache_key[1], selections(context.scene))
            _browser_snapshots[cache_key] = assets
        _browser_snapshots.move_to_end(cache_key)
        while len(_browser_snapshots) > 8:
            _browser_snapshots.popitem(last=False)
        collection = context.scene.mcbedrock_browser_assets
        global _index_error
        archive_issues = [a.detail for a in assets if a.asset_type == 'Archive Issue']
        _index_error = (f'{len(archive_issues)} archive issue(s): ' + archive_issues[0]) if archive_issues else ''
        collection.clear()
        context.scene['_mcbedrock_loaded_browser_root'] = root
        _entity_setup_meta_cache.clear()
        _entity_capability_cache.clear()

        prepared = _browser_prepared.get(cache_key)
        if prepared is None or prepared[0] is not assets:
            rows = _prepare_rows(assets)
            _browser_prepared[cache_key] = (assets, rows)
        else:
            rows = prepared[1]
        for key in list(_browser_prepared):
            if key not in _browser_snapshots:
                del _browser_prepared[key]
        global _loaded_rows
        _loaded_rows = (context.scene.as_pointer(), root, rows)
        from .custom_ui import legacy_ui_enabled
        if self.background and len(rows) > 128 and not legacy_ui_enabled(context):
            # The gallery draws the prepared rows. The scene list fills only
            # when an operator reads it by index (finish_browser_fill), so
            # browsing a big pack keeps thousands of rows out of every undo step.
            context.scene.mcbedrock_browser_root = root
            context.scene.mcbedrock_browser_index = -1
            context.scene.mcbedrock_gallery_scroll = 0
            rebuild_gallery_rows(context.scene, native=False)
            _apply_job = {'scene': context.scene, 'root': root, 'rows': rows, 'next': 0}
            return {'FINISHED'}
        _fill_browser_assets(collection, rows)

        context.scene.mcbedrock_browser_root = root
        context.scene.mcbedrock_browser_index = 0 if collection else -1
        context.scene.mcbedrock_gallery_scroll = 0
        from .custom_ui import legacy_ui_enabled
        rebuild_gallery_rows(context.scene, native=legacy_ui_enabled(context))
        self.report({'INFO'}, f"Indexed {len(collection)} assets")

        global _visible_thumbnail_signature
        _visible_thumbnail_signature = None
        from .custom_ui import legacy_ui_enabled
        if legacy_ui_enabled(context):
            schedule_thumbnail_preparation(context, assets)
        return {'FINISHED'}


class MCBEDROCK_OT_asset_browser_import_selected(Operator):
    """Import the selected browser asset with the existing Minecraft importers"""
    bl_idname = "mcbedrock.asset_browser_import_selected"
    bl_label = "Import Selected Asset"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})
    source_path: bpy.props.StringProperty(default="", options={'HIDDEN'})
    asset_key: bpy.props.StringProperty(default="", options={'HIDDEN'})
    asset_type: bpy.props.StringProperty(default="", options={'HIDDEN'})
    asset_identifier: bpy.props.StringProperty(default="", options={'HIDDEN'})
    geometry_index: bpy.props.IntProperty(default=-1, options={'HIDDEN'})

    def execute(self, context):
        from ..importers.bedrock_assets import browser_pack_scope
        with browser_pack_scope(context):
            return self._execute(context)

    def _execute(self, context):
        import os
        from .. import builder

        scene = context.scene
        finish_browser_fill(scene)
        assets = scene.mcbedrock_browser_assets
        preferred_index = self.index if self.index >= 0 else scene.mcbedrock_browser_index
        index = _resolve_browser_item_index(
            assets,
            preferred_index=preferred_index,
            asset_key=self.asset_key,
            source_path=self.source_path,
            asset_type=self.asset_type,
            identifier=self.asset_identifier,
            geometry_index=self.geometry_index,
        )
        if index != scene.mcbedrock_browser_index:
            scene.mcbedrock_browser_index = index
        if index < 0 or index >= len(assets):
            self.report({'ERROR'}, "No browser asset selected")
            return {'CANCELLED'}

        item = assets[index]
        if not _br_metadata(item).get('br_available', True):
            self.report({'ERROR'}, 'Asset unavailable in this Pack Experience; choose another in Import')
            return {'CANCELLED'}
        if not item.importable:
            self.report({'ERROR'}, f"{item.asset_type} is not directly importable")
            return {'CANCELLED'}
        if item.status in {"Unreadable", "Protected"}:
            self.report({'ERROR'}, "This asset only exposes metadata and cannot be imported directly")
            return {'CANCELLED'}
        if not os.path.isfile(item.source_path):
            self.report({'ERROR'}, f"File not found: {item.source_path}")
            return {'CANCELLED'}

        if item.asset_type == "Texture":
            try:
                plane = _import_texture(context, item.source_path)
                self.report({'INFO'}, f"Imported textured plane: {plane.name}")
                return {'FINISHED'}
            except Exception as exc:
                self.report({'ERROR'}, f"Texture import failed: {exc}")
                return {'CANCELLED'}

        if is_mod_asset(item):
            # The mod importer, as the import panel uses it: at the 3D cursor,
            # in-game size, the mob's default texture.
            try:
                result = bpy.ops.mcbedrock.import_mod_jar(
                    'EXEC_DEFAULT', filepath=item.source_path, asset_key=item.identifier)
            except RuntimeError as exc:
                self.report({'ERROR'}, str(exc).removeprefix("Error: ").strip())
                return {'CANCELLED'}
            return {'FINISHED'} if 'FINISHED' in result else {'CANCELLED'}

        if item.asset_type == "Java Block":
            try:
                metadata = json.loads(item.metadata_json or "{}")
                from ..builders.schematic_builder import build_single_block
                pack_root = str(
                    metadata.get("pack_root")
                    or scene.mcbedrock_resource_pack_path
                    or scene.mcbedrock_browser_root
                )
                if not pack_root or not os.path.isdir(pack_root):
                    raise RuntimeError("The source resource pack is no longer available")
                from ..importers.blockstate_resolver import block_asset_context
                from ..importers.bedrock_assets import asset_resource_roots
                roots, vanilla_source = block_asset_context(
                    metadata, asset_resource_roots(context, item.source_path),
                    bpy.path.abspath(getattr(scene, "mcbedrock_vanilla_assets_path", "") or ""),
                )
                stats = build_single_block(
                    context,
                    str(metadata.get("block_state") or item.identifier),
                    roots,
                    vanilla_path=vanilla_source,
                    interpolation=scene.mcbedrock_particle_interpolation,
                    vanilla_geometry=(
                        metadata.get("geometry_policy") == "VANILLA"
                    ),
                )
                stats['root'].location=context.scene.cursor.location.copy()
                self.report(
                    {'INFO'},
                    "Imported block: %s (%d faces)" % (
                        item.name, int(stats.get("faces", 0))
                    ),
                )
                return {'FINISHED'}
            except Exception as exc:
                self.report({'ERROR'}, f"Block import failed: {exc}")
                return {'CANCELLED'}

        from ..importers.bedrock_assets import detect_json_asset

        # Advanced import settings shared across asset types.
        tex_override = ""
        if scene.mcbedrock_texture_override_path:
            tex_override = bpy.path.abspath(scene.mcbedrock_texture_override_path)
        interpolation = scene.mcbedrock_particle_interpolation

        detected_type = detect_json_asset(item.source_path)
        indexed_particle = item.asset_type in {"Particle", "Java Particle"}
        is_particle = detected_type in {"particle", "java_particle"} or (detected_type == "unknown" and indexed_particle)
        try:
            if is_particle:
                result = builder.build_particle_system(
                    context,
                    item.source_path,
                    texture_override=tex_override,
                    spawn_offset=tuple(context.scene.cursor.location),
                    force_loop=scene.mcbedrock_particle_force_loop,
                    interpolation=interpolation,
                )
                builder.apply_particle_preview_frame(context, result)
                if result and result.get("warning"):
                    self.report({'WARNING'}, result["warning"])
                else:
                    self.report({'INFO'}, f"Imported particle: {item.name}")
                return {'FINISHED'}

            animation_filter = "__NONE__"
            if detected_type == "client_entity" or (detected_type == "unknown" and item.asset_type == "Entity"):
                valid_animation_ids = {choice[0] for choice in _browser_animation_items(self, context)}
                selected_animation = scene.mcbedrock_browser_animation
                if selected_animation in valid_animation_ids:
                    animation_filter = selected_animation

            # Geometry index: explicit advanced override wins, else the value indexed at refresh.
            if item.asset_type == "Model" and scene.mcbedrock_geometry_index > 0:
                geometry_index = scene.mcbedrock_geometry_index
            elif item.asset_type == "Model" and item.geometry_index >= 0:
                geometry_index = item.geometry_index
            else:
                geometry_index = 0

            capabilities = _entity_setup_capabilities(item)
            is_entity = item.asset_type == "Entity"
            use_custom_state = (
                is_entity
                and (
                    not capabilities["baby"]
                    or scene.mcbedrock_preview_preset == "CUSTOM"
                )
            )

            bpy.ops.mcbedrock.import_asset_json(
                'EXEC_DEFAULT',
                filepath=item.source_path,
                directory=os.path.dirname(item.source_path),
                geometry_identifier=item.identifier if detected_type == "model" else "",
                geometry_index=geometry_index,
                display_name=item.name,
                animation_filter=animation_filter,
                pose_preset=scene.mcbedrock_pose_preset,
                use_entity_preview_state=is_entity,
                preview_variant=(
                    scene.mcbedrock_preview_variant
                    if use_custom_state and capabilities["variant"] else 0
                ),
                preview_mark_variant=(
                    scene.mcbedrock_preview_mark_variant
                    if use_custom_state and capabilities["marking"] else 0
                ),
                preview_armor_slot=(
                    scene.mcbedrock_preview_armor_slot
                    if use_custom_state and capabilities["armor"] else 0
                ),
                preview_is_baby=(
                    capabilities["baby"]
                    and (
                        scene.mcbedrock_preview_preset == 'BABY'
                        or (
                            scene.mcbedrock_preview_preset == 'CUSTOM'
                            and scene.mcbedrock_preview_is_baby
                        )
                    )
                ),
                preview_is_saddled=(
                    scene.mcbedrock_preview_is_saddled
                    if use_custom_state and capabilities["saddled"] else False
                ),
                preview_has_rider=(
                    scene.mcbedrock_preview_has_rider
                    if use_custom_state and capabilities["rider"] else False
                ),
                preview_is_chested=(
                    scene.mcbedrock_preview_is_chested
                    if use_custom_state and capabilities["storage"] else False
                ),
                entity_effects='PREVIEW',
                import_all_variants=(
                    capabilities["all_variants"]
                    and scene.mcbedrock_import_all_variants
                ),
                manual_texture=tex_override,
                interpolation=interpolation,
            )
            self.report({'INFO'}, f"Imported: {item.name}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}


def _import_texture(context, filepath, offset=0.0):
    """Create a UV-mapped texture plane at the cursor, one unit high."""
    image = bpy.data.images.load(bpy.path.abspath(filepath), check_existing=True)
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("Texture has no readable pixels")
    image.use_fake_user = True
    name = os.path.splitext(os.path.basename(filepath))[0]
    aspect = width / height
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-aspect/2, -.5, 0), (aspect/2, -.5, 0),
                      (aspect/2, .5, 0), (-aspect/2, .5, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    uv = mesh.uv_layers.new(name='UVMap')
    for loop, coordinate in zip(uv.data, [(0, 0), (1, 0), (1, 1), (0, 1)]):
        loop.uv = coordinate
    material = bpy.data.materials.new(name + ' Texture')
    material.use_nodes = True
    material.use_backface_culling = False
    if hasattr(material, 'surface_render_method'):
        material.surface_render_method = 'DITHERED'
    elif hasattr(material, 'blend_method'):
        material.blend_method = 'HASHED'
    nodes = material.node_tree.nodes
    texture = nodes.new('ShaderNodeTexImage')
    texture.image = image
    texture.interpolation = getattr(context.scene, 'mcbedrock_particle_interpolation', 'Closest')
    texture.location = (-300, 100)
    nodes.active = texture
    shader = nodes.get('Principled BSDF')
    material.node_tree.links.new(texture.outputs['Color'], shader.inputs['Base Color'])
    material.node_tree.links.new(texture.outputs['Alpha'], shader.inputs['Alpha'])
    shader.inputs['Roughness'].default_value = 1.0
    mesh.materials.append(material)
    plane = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(plane)
    plane.location = context.scene.cursor.location
    plane.location.x += offset
    for selected in context.selected_objects:
        selected.select_set(False)
    plane.select_set(True)
    context.view_layer.objects.active = plane
    for area in getattr(context.screen, "areas", ()):
        if area.type == 'IMAGE_EDITOR':
            area.spaces.active.image = image
    return plane


class MCBEDROCK_OT_import_texture(Operator, ImportHelper):
    bl_idname = "mcbedrock.import_texture"
    bl_label = "Import Textures"
    bl_description = "Import texture images as UV-mapped planes at the 3D cursor"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: bpy.props.StringProperty(default="*.png;*.tga;*.jpg;*.jpeg", options={'HIDDEN'})
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        paths = [os.path.join(self.directory, file.name) for file in self.files] or [self.filepath]
        imported = 0
        right_edge = None
        for path in paths:
            try:
                plane = _import_texture(context, path)
                width = max(v.co.x for v in plane.data.vertices) * 2
                if right_edge is not None:
                    plane.location.x = right_edge + .1 + width / 2
                right_edge = plane.location.x + width / 2
                imported += 1
            except Exception as exc:
                self.report({'WARNING'}, f"Could not import {os.path.basename(path)}: {exc}")
        if not imported:
            return {'CANCELLED'}
        self.report({'INFO'}, f"Imported {imported} textured plane(s)")
        return {'FINISHED'}


class MCBEDROCK_OT_asset_browser_use_as_pack(Operator):
    """Use the browser folder as the active resource pack search path"""
    bl_idname = "mcbedrock.asset_browser_use_as_pack"
    bl_label = "Use Browser Folder For Resources"
    bl_options = {'REGISTER'}

    def execute(self, context):
        from ..importers.bedrock_assets import find_resource_pack_root

        if not context.scene.mcbedrock_browser_root:
            self.report({'ERROR'}, "Set an asset browser folder first")
            return {'CANCELLED'}
        rp_root = find_resource_pack_root("", context.scene.mcbedrock_browser_root)
        if not rp_root:
            self.report({'ERROR'}, "No resource pack root found in browser folder")
            return {'CANCELLED'}
        context.scene.mcbedrock_resource_pack_path = rp_root
        context.scene.mcbedrock_browser_root = rp_root
        count = 0
        try:
            bpy.ops.mcbedrock.asset_browser_refresh()
            count = len(context.scene.mcbedrock_browser_assets)
        except Exception:
            pass
        self.report({'INFO'}, f"Resource pack active: {rp_root}" + (f" ({count} assets)" if count else ""))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Headless thumbnail workers: subprocesses + app timer, never the main thread.
# ---------------------------------------------------------------------------

# A pool of render workers that stay up (``thumbnail_worker.pool_main``): they
# read one queue file, the visible cards first, and the browser rewrites it as
# cards come into view instead of starting a new Blender for every request.
_WORKERS = max(1, min(4, (os.cpu_count() or 4) // 4))
_PER_WORKER = 6  # waiting thumbnails that make another worker worth starting
_pool = {"workers": [], "files": {}, "jobs": [], "pending": deque(), "queued": None, "waiting": 0,
         "crashes": 0, "gave_up": False, "told": False, "script": ""}
# Finished-file checks per poll: a whole-pack queue (native "Generate
# Thumbnails") is checked a slice at a time instead of all at once.
_POLL_BATCH = 64
_lookup = {"failures": 0, "blender": False}  # path-lookup helper: failures in a row; run it in Blender
_worker_total = 0
_thumbnail_preparation = None
_visible_thumbnail_signature = None
_preview_job = None
_handoff = None  # a large render queue being checked on a helper thread


def _prepare_preview_lookup(request):
    """Poll a filesystem-only helper; never scan resource trees during drawing."""
    global _preview_job
    import tempfile
    cache = request['cache']
    jobs = cache.ready_jobs(request['records'])
    if jobs is not None:
        return jobs
    if _preview_job is not None:
        job = _preview_job
        if job['process'].poll() is None:
            return None
        _preview_job = None
        try:
            try:
                with open(job['output'], encoding='utf-8') as stream:
                    result = json.load(stream)
            except (OSError, ValueError):
                result = {'error': 'the path lookup stopped without a result'}
            if 'error' in result:
                _lookup['failures'] += 1
                if job['command'][0] != bpy.app.binary_path:
                    _lookup['blender'] = True  # Blender's Python could not do it: use Blender
                raise RuntimeError(result['error'])
            _lookup['failures'] = 0
            job['cache'].accept(result)
        finally:
            for path in (job['request'], job['output']):
                if os.path.isfile(path):
                    os.remove(path)
        _redraw_ui()
        jobs = cache.ready_jobs(request['records'])
        if jobs is not None:
            return jobs
    _prewarm_worker(cache)
    handle, path = tempfile.mkstemp(prefix='mc_preview_', suffix='.json')
    os.close(handle)
    output = path + '.result'
    fields = ('source_path', 'asset_type', 'identifier', 'geometry_index', 'metadata_json')
    payload = {'assets': [{field: getattr(a, field, '') for field in fields}
                          for a in request['records']],
               'resource_roots': cache.resource_roots, 'choices': cache.choices,
               'root': request['root'], 'vanilla': cache.vanilla_path,
               'helper_state': cache.helper_state,
               'size': cache.size, 'cache_dir': str(cache.cache_dir), 'output': output}
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream)
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'importers', 'browser_thumbnail_worker.py')
    flags = (getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)) if os.name == 'nt' else 0
    command = _helper_command(script, path)
    try:
        process = subprocess.Popen(command,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception:
        os.remove(path)
        raise
    _preview_job = {'process': process, 'request': path, 'output': output, 'cache': cache, 'command': command}
    return None


def _prewarm_worker(cache):
    """Boot a render worker (shaders compile for a second or two) while paths are looked up.

    Only when this pack's last lookup still found cards to render: texture-only
    views and fully cached packs never start a background Blender, and nothing
    is started from inside drawing.
    """
    if (not getattr(cache, 'needs_renders', False) or _pool["gave_up"] or _live_workers()
            or not hasattr(cache, 'worker_script')):
        return
    _pool["script"] = cache.worker_script()
    try:
        _spawn_render_worker()
    except OSError as exc:
        print(f"[MCBedrock] could not launch thumbnail worker: {exc}")


def _helper_command(script, path):
    """The path lookup needs no Blender: Blender's own Python starts it in a tenth of the time."""
    python = sys.executable
    if (not _lookup['blender'] and python and os.path.basename(python).lower().startswith('python')
            and os.path.isfile(python)):
        return [python, '-I', script, '--', path]
    return [bpy.app.binary_path, '--background', '--factory-startup', '--threads', '2',
            '--python', script, '--', path]


def request_visible_thumbnails(context, items):
    """Queue only the viewport and nearby cards, never retain Blender RNA."""
    global _visible_thumbnail_signature
    from types import SimpleNamespace
    mods = [item for item in items if is_mod_asset(item)]
    if mods:
        _request_mod_thumbnails(mods)
        items = [item for item in items if not is_mod_asset(item)]
        if not items:
            return
    fields = ('source_path', 'asset_type', 'identifier', 'geometry_index',
              'metadata_json', 'importable', 'status')
    records = [tuple(getattr(item, field) for field in fields) for item in items]
    signature = (context.scene.as_pointer(), context.scene.mcbedrock_browser_root,
                 _thumb_size(context.scene), tuple(records),
                 context.scene.mcbedrock_vanilla_assets_path,
                 context.scene.get('mcbedrock_pack_experiences', ''),
                 context.scene.get('_mcbedrock_gallery_revision', 0))
    if signature == _visible_thumbnail_signature:
        return
    _visible_thumbnail_signature = signature
    if _pool["gave_up"]:
        # New cards to show: give the workers another chance.
        _pool["gave_up"], _pool["crashes"] = False, 0
    schedule_thumbnail_preparation(context, [SimpleNamespace(**dict(zip(fields, row))) for row in records])


_mod_jar_shown = None  # the JAR whose mod cards the gallery last asked for


def _request_mod_thumbnails(items):
    """Mod cards render in the mod import panel's own pool, in the order shown.

    Cheap to repeat: the pool's timer does the file checks and starts workers.
    """
    global _mod_jar_shown
    from . import mod_thumbnails
    jar = items[0].source_path
    if jar != _mod_jar_shown:
        _mod_jar_shown = jar
        mod_thumbnails.try_again()  # another mod: workers that failed get another chance
    mod_thumbnails.request(jar, [item.identifier for item in items if item.source_path == jar])


def schedule_thumbnail_preparation(context, assets):
    """Debounce pack clicks and check thumbnail files in small timer batches."""
    global _thumbnail_preparation
    assets = [a for a in assets if a.importable and a.status not in {'Unreadable', 'Protected'}]
    mods = [a for a in assets if is_mod_asset(a)]
    if mods:
        _request_mod_thumbnails(mods)
        assets = [a for a in assets if not is_mod_asset(a)]
        if not assets:
            _cancel_thumbnail_preparation()
            return
    _thumbnail_preparation = {
        'scene': context.scene, 'root': context.scene.mcbedrock_browser_root,
        'assets': iter(assets), 'records': assets, 'jobs': [], 'seen': set(),
        'cache': _cache_for(context.scene), 'after': time.monotonic() + .15,
    }
    # This runs from the gallery's layout: no worker starts here. The timer
    # starts one once a lookup shows cards that need rendering.
    if not bpy.app.timers.is_registered(_prepare_thumbnail_batch):
        bpy.app.timers.register(_prepare_thumbnail_batch, first_interval=.15)


def _cancel_thumbnail_preparation():
    global _thumbnail_preparation
    _thumbnail_preparation = None
    if bpy.app.timers.is_registered(_prepare_thumbnail_batch):
        bpy.app.timers.unregister(_prepare_thumbnail_batch)


def _prepare_thumbnail_batch():
    global _thumbnail_preparation
    request = _thumbnail_preparation
    if request is None:
        return None
    try:
        if (bpy.context.scene != request['scene']
                or request['scene'].mcbedrock_browser_root != request['root']):
            _thumbnail_preparation = None
            return None
        remaining = request['after'] - time.monotonic()
        if remaining > 0:
            return remaining
        from ..importers.browser_thumbnail_cache import BrowserThumbnailCache
        if isinstance(request['cache'], BrowserThumbnailCache):
            jobs = _prepare_preview_lookup(request)
            if jobs is None:
                return .05
            _thumbnail_preparation = None
            if jobs:
                spawn_thumbnail_worker(bpy.context, (), prepared_jobs=jobs, prepared_cache=request['cache'])
            return None
        deadline = time.monotonic() + .008
        for _ in range(32):
            asset = next(request['assets'], None)
            if asset is None:
                _thumbnail_preparation = None
                if request['jobs']:
                    spawn_thumbnail_worker(bpy.context, (), prepared_jobs=request['jobs'])
                return None
            if asset.importable and asset.status not in {'Unreadable', 'Protected'}:
                for job in request['cache'].build_queue([asset]):
                    if job['output'] not in request['seen']:
                        request['seen'].add(job['output'])
                        request['jobs'].append(job)
            if time.monotonic() >= deadline:
                break
        return .01
    except Exception as exc:
        _thumbnail_preparation = None
        print('[Sparkle] Thumbnail preparation failed: %s' % exc)
        _ask_again(min(10.0, 0.5 * 2 ** _lookup['failures']))
        return None


def _ask_again(delay):
    """Let the gallery's next redraw ask for its cards again (after ``delay`` seconds).

    The gallery skips a request it already made, so a request that failed or
    was cut short would otherwise leave its cards waiting until a scroll.
    """
    def forget():
        global _visible_thumbnail_signature
        _visible_thumbnail_signature = None
        _redraw_ui()
        return None
    bpy.app.timers.register(forget, first_interval=delay)


def _redraw_ui():
    """Tag every 3D viewport sidebar for redraw (safe from an app timer)."""
    try:
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _set_status(text):
    try:
        scene = bpy.context.scene
        if hasattr(scene, "mcbedrock_pack_manager_status"):
            scene.mcbedrock_pack_manager_status = text
    except Exception:
        pass


def _pool_files():
    """The control, queue and error files of this Blender's workers; old ones of closed Blenders go."""
    files = _pool["files"]
    if files and os.path.isfile(files["control"]):
        return files
    import shutil
    from ..importers.archive_extractor import user_cache_dir
    root = os.path.join(user_cache_dir("thumbs"), "queue")
    folder = os.path.join(root, str(os.getpid()))
    os.makedirs(folder, exist_ok=True)
    for entry in os.scandir(root):
        if entry.path != folder and time.time() - entry.stat().st_mtime > 6 * 3600:
            shutil.rmtree(entry.path, ignore_errors=True)
    files = {name: os.path.join(folder, name + ".json") for name in ("control", "order", "error")}
    with open(files["control"], "w", encoding="utf-8") as stream:
        json.dump({"order": files["order"], "error": files["error"]}, stream)
    _pool["files"] = files
    return files


def _live_workers():
    return [worker for worker in _pool["workers"] if worker["process"].poll() is None]


def _write_pool_queue():
    """Hand the waiting jobs to the workers (in one step); False while a worker is reading it."""
    queued = _pool["queued"]
    if queued is None:
        return True
    jobs, cache, resource_pack = queued
    order = _pool_files()["order"]
    try:
        cache.write_queue(jobs, resource_pack=resource_pack, queue_path=order + ".tmp")
        os.replace(order + ".tmp", order)
    except OSError:
        return False
    _pool["queued"] = None
    return True


def _spawn_render_worker():
    files = _pool_files()
    creationflags = 0
    if os.name == "nt":
        # Keep the background Blender from flashing a console window.
        creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    process = subprocess.Popen(
        [bpy.app.binary_path, "-b", "--factory-startup", "--threads", "2", "-noaudio",
         "-P", _pool["script"], "--", files["control"], "--pool"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags)
    _pool["workers"].append({"process": process, "rendered": False})


def _reap_workers(arrived):
    """Workers that quit: fine when idle. One that crashed on an asset marks that asset
    failed (so the next worker moves on); three failing before any thumbnail, stop."""
    from ..importers.thumbnail_cache import FAILED_SUFFIX
    if arrived:
        _pool["crashes"] = 0
    for worker in _pool["workers"]:
        worker["rendered"] = worker["rendered"] or arrived
    for worker in [worker for worker in _pool["workers"] if worker["process"].poll() is not None]:
        _pool["workers"].remove(worker)
        error = _pool["files"].get("error", "")
        if error and os.path.isfile(error):
            with open(error, encoding="utf-8") as stream:
                print(f"[MCBedrock] thumbnail worker: {stream.read().strip()}")
            os.remove(error)
        if worker["process"].returncode == 0:
            continue
        pid = str(worker["process"].pid)
        for job in _pool["jobs"]:
            claim = os.path.splitext(job["output"])[0] + ".claim"
            try:
                with open(claim, encoding="utf-8") as stream:
                    mine = stream.read().strip() == pid
            except OSError:
                continue
            if mine:
                print(f"[MCBedrock] thumbnail worker crashed on {job.get('file_path', '')}")
                with open(os.path.splitext(job["output"])[0] + FAILED_SUFFIX, "w", encoding="utf-8") as stream:
                    stream.write("the thumbnail worker crashed on this asset")
                os.remove(claim)
        if not worker["rendered"]:
            _pool["crashes"] += 1
    if arrived:
        _pool["told"] = False
    if _pool["crashes"] >= 3 and not _pool["gave_up"]:
        _pool["gave_up"] = True
        _set_status("Thumbnail worker failed - see console")
        if not _pool["told"]:
            # Cards would otherwise just keep saying "Preview not generated".
            _pool["told"] = True
            notify_sidebar("Error: Thumbnails stopped: the background Blender that renders them "
                           "failed three times. See the system console for details.")


def poll_worker():
    """Every tenth of a second while thumbnails are on their way.

    Redraws as PNGs land (the cards pick them up on that redraw), starts
    another worker while many are waiting (one per tick), and finishes with
    the status line once every job is done. Returns None to stop.
    """
    if not _write_pool_queue():
        return 0.05
    from ..importers.thumbnail_cache import FAILED_SUFFIX
    from ..importers.browser_thumbnail_cache import forget_lookups
    from ..importers.preview_fallback import remember_visible_png
    pending = _pool["pending"]
    if not _pool["jobs"]:
        pending.clear()
    # A slice per tick: a whole-pack queue must not stat every file ten times a second.
    for _ in range(min(len(pending), _POLL_BATCH)):
        job = pending.popleft()
        output = job["output"]
        if os.path.isfile(output):
            # The worker checked it before renaming it into place: drawing need not decode it.
            remember_visible_png(output)
        elif not os.path.isfile(os.path.splitext(output)[0] + FAILED_SUFFIX):
            pending.append(job)
    waiting = len(pending)
    arrived = waiting < _pool["waiting"]
    _pool["waiting"] = waiting
    _reap_workers(arrived)
    if arrived:
        forget_lookups()
        _redraw_ui()
    if waiting and not _pool["gave_up"]:
        if len(_live_workers()) < max(1, min(_WORKERS, waiting // _PER_WORKER)):
            try:
                _spawn_render_worker()
            except OSError as exc:
                print(f"[MCBedrock] could not launch thumbnail worker: {exc}")
                _pool["gave_up"] = True
        return 0.1
    try:
        bpy.context.scene.mcbedrock_thumb_generating = False
    except Exception:
        pass
    if not _pool["gave_up"]:
        _set_status(f"Thumbnails ready ({_worker_total})")
    _clear_thumb_previews()  # Reload the freshly rendered PNGs from disk.
    _ask_again(0.2)  # cards this queue left out (it was replaced, a worker stopped) are asked for
    return None


def _hand_to_pool(jobs, cache, resource_pack):
    """Make ``jobs`` (the visible cards first) the workers' queue; start one if none is up."""
    _pool["jobs"] = list(jobs)
    _pool["pending"] = deque(jobs)
    _pool["waiting"] = len(jobs)
    _pool["queued"] = (list(jobs), cache, resource_pack)
    _pool["script"] = cache.worker_script()
    _write_pool_queue()
    if not _live_workers() and not _pool["gave_up"]:
        _spawn_render_worker()
    if not bpy.app.timers.is_registered(poll_worker):
        # Persistent: a .blend load mid-render must not strand the workers.
        bpy.app.timers.register(poll_worker, first_interval=0.1, persistent=True)


def _stop_worker(kill=False):
    """Stop thumbnail work: the queue goes and the workers wait (warm) for the next one.

    ``kill`` ends them too (the add-on is turning off).
    """
    global _visible_thumbnail_signature, _preview_job, _handoff
    _stop_browser_index()
    # Also called before load/undo/redo, when saved RNA indices can change.
    _filtered_views.clear()
    _scene_thumbnail_caches.clear()
    _visible_thumbnail_signature = None
    _cancel_thumbnail_preparation()
    _handoff = None
    if bpy.app.timers.is_registered(_finish_handoff):
        bpy.app.timers.unregister(_finish_handoff)
    if _preview_job:
        job, _preview_job = _preview_job, None
        if job['process'].poll() is None:
            job['process'].kill()
            job['process'].wait(timeout=5)
        for path in (job['request'], job['output']):
            if os.path.isfile(path):
                os.remove(path)
    _pool["jobs"], _pool["queued"], _pool["waiting"] = [], None, 0
    _pool["pending"] = deque()
    files = _pool["files"]
    if files:
        try:
            with open(files["order"] + ".tmp", "w", encoding="utf-8") as stream:
                json.dump({"items": []}, stream)
            os.replace(files["order"] + ".tmp", files["order"])
        except OSError:
            pass
    if kill:
        for worker in _pool["workers"]:
            try:
                worker["process"].kill()
            except OSError:
                pass
        if files:
            import shutil
            shutil.rmtree(os.path.dirname(files["control"]), ignore_errors=True)
        _pool.update(workers=[], files={}, crashes=0, gave_up=False)
    try:
        if bpy.app.timers.is_registered(poll_worker):
            bpy.app.timers.unregister(poll_worker)
    except Exception:
        pass


def spawn_thumbnail_worker(context, assets, retry_failed=False, prepared_jobs=None, prepared_cache=None):
    """Build the render queue and launch the background worker for ``assets``.

    Shared by the manual "Generate Thumbnails" button and the automatic
    trigger on browser refresh. If a worker is already in flight it is killed
    first, so rapidly clicking through resource packs cannot pile up dozens
    of background Blenders.

    ``retry_failed`` drops the failure markers first, so assets that previously
    failed to render are attempted again. The automatic refresh trigger leaves
    them alone -- otherwise every refresh would re-render the same broken
    assets forever.

    Returns one of ``'STARTED'``, ``'CACHED'``, ``'NO_ASSETS'``, ``'ERROR'``.
    """
    global _worker_total

    scene = context.scene
    renderable = [
        a for a in assets
        if a.importable
        and a.status not in {"Unreadable", "Protected"}
        and os.path.isfile(a.source_path)
    ]
    if not renderable and prepared_jobs is None:
        return 'NO_ASSETS'

    cache = prepared_cache or _cache_for(scene)
    if retry_failed:
        cache.clear_failures()

    # build_queue drops anything already cached, and anything that previously
    # failed to render (its marker survives until the source changes).
    if prepared_jobs is None:
        from ..importers.bedrock_assets import browser_pack_scope, asset_resource_roots
        from ..importers.thumbnail_cache import ThumbnailCache
        with browser_pack_scope(context):
            roots = asset_resource_roots(context)
        direct = ThumbnailCache(cache.cache_dir, cache.size, roots, cache.vanilla_path)
        jobs = direct.build_queue(renderable)
        cache.accept({'resource_roots': roots, 'jobs': jobs, 'paths': [
            ([a.source_path, a.asset_type, a.identifier, a.geometry_index],
             direct.get_cache_path(a.source_path, a.asset_type, a.identifier, a.geometry_index))
            for a in renderable]})
    else:
        jobs = prepared_jobs
    resource_pack = ""
    if scene.mcbedrock_browser_root or scene.mcbedrock_resource_pack_path:
        resource_pack = bpy.path.abspath(scene.mcbedrock_browser_root or scene.mcbedrock_resource_pack_path)
    if prepared_jobs is not None:
        from ..importers.preview_fallback import png_has_visible_pixels
        from ..importers.thumbnail_cache import FAILED_SUFFIX
        from ..importers.browser_thumbnail_cache import LARGE_REQUEST
        if len(jobs) > LARGE_REQUEST:
            # A whole pack (native "Generate Thumbnails"): check which are done on
            # a helper thread, then hand the rest to the workers.
            _queue_large_render(jobs, cache, resource_pack)
            return 'STARTED'
        jobs = [job for job in jobs if not png_has_visible_pixels(job['output'])
                and not os.path.isfile(os.path.splitext(job['output'])[0] + FAILED_SUFFIX)]
    if not jobs:
        _clear_thumb_previews()
        _redraw_ui()
        return 'CACHED'

    try:
        _hand_to_pool(jobs, cache, resource_pack)
    except OSError as exc:
        print(f"[MCBedrock] could not launch thumbnail worker: {exc}")
        return 'ERROR'

    _started_rendering(len(jobs))
    return 'STARTED'


def _started_rendering(count):
    global _worker_total
    _worker_total = count
    try:
        bpy.context.scene.mcbedrock_thumb_generating = True
    except Exception:
        pass
    _clear_thumb_previews()  # Drop stale icons so new renders show up.
    _set_status(f"Rendering {count} thumbnail(s)...")


def _queue_large_render(jobs, cache, resource_pack):
    """Drop finished jobs on a helper thread (stats only), then hand the rest to the pool."""
    global _handoff
    from ..importers.thumbnail_cache import FAILED_SUFFIX
    jobs = list(jobs)
    handoff = {'cache': cache, 'resource_pack': resource_pack, 'jobs': None}

    def check():
        handoff['jobs'] = [job for job in jobs if not os.path.isfile(job['output'])
                           and not os.path.isfile(os.path.splitext(job['output'])[0] + FAILED_SUFFIX)]

    handoff['thread'] = threading.Thread(target=check, daemon=True)
    handoff['thread'].start()
    _handoff = handoff
    _set_status(f"Checking {len(jobs)} thumbnails...")
    if not bpy.app.timers.is_registered(_finish_handoff):
        bpy.app.timers.register(_finish_handoff, first_interval=.1)


def _finish_handoff():
    global _handoff
    handoff = _handoff
    if handoff is None:
        return None
    if handoff['thread'].is_alive():
        return .1
    _handoff = None
    jobs = handoff['jobs'] or []
    if not jobs:
        _set_status("All thumbnails already cached")
        _clear_thumb_previews()
        _redraw_ui()
        return None
    try:
        _hand_to_pool(jobs, handoff['cache'], handoff['resource_pack'])
    except OSError as exc:
        print(f"[MCBedrock] could not launch thumbnail worker: {exc}")
        notify_sidebar(f"Error: Could not start the thumbnail worker: {exc}")
        return None
    _started_rendering(len(jobs))
    return None


class MCBEDROCK_OT_generate_thumbnails(Operator):
    """Render preview thumbnails for the browsed assets in a background Blender process"""
    bl_idname = "mcbedrock.generate_thumbnails"
    bl_label = "Generate Thumbnails"
    bl_options = {'REGISTER'}

    def execute(self, context):
        from types import SimpleNamespace
        _cancel_thumbnail_preparation()
        finish_browser_fill(context.scene)
        fields = ('source_path', 'asset_type', 'identifier', 'geometry_index',
                  'metadata_json', 'importable', 'status')
        # Plain records: the lookup outlives this call, the scene list may not.
        records = [SimpleNamespace(**{field: getattr(item, field) for field in fields})
                   for item in context.scene.mcbedrock_browser_assets
                   if item.importable and item.status not in {"Unreadable", "Protected"}]
        if not records:
            self.report({'WARNING'}, "No renderable assets in the list")
            return {'CANCELLED'}
        # Explicit user action: retry assets whose last render failed.
        _cache_for(context.scene).clear_failures()
        if _pool["gave_up"]:
            _pool["gave_up"], _pool["crashes"] = False, 0
        # Paths and the render queue are worked out by the helper process and
        # a helper thread, not on this thread (seconds for a 20k-asset pack).
        schedule_thumbnail_preparation(context, records)
        if _thumbnail_preparation is not None:  # None when only mod cards were asked for
            _thumbnail_preparation['after'] = 0
        self.report({'INFO'}, f"Preparing thumbnails for {len(records)} assets in the background")
        return {'FINISHED'}


class MCBEDROCK_PT_asset_browser(Panel):
    bl_label = "Asset Browser"
    bl_idname = "MCBEDROCK_PT_asset_browser"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sparkle'
    bl_order = 1

    @classmethod
    def poll(cls, context):
        from .custom_ui import legacy_ui_enabled
        return legacy_ui_enabled(context)

    # ------------------------------------------------------------------ #
    #  Asset Browser section                                             #
    # ------------------------------------------------------------------ #
    def _draw_active_banner(self, layout, scene):
        box = layout.box()
        path = scene.mcbedrock_resource_pack_path
        if not path:
            box.label(text="No active resource pack", icon="ERROR")
            return

        meta = _active_pack_meta(path)
        name = meta.get("name") or os.path.basename(path.rstrip("\\/"))
        subtitle = _active_pack_subtitle(meta)

        icon_id = 0
        try:
            from . import pack_manager
            icon_id = pack_manager._preview_icon_id(meta.get("icon_path", ""))
        except Exception:
            icon_id = 0

        row = box.row(align=True)
        if icon_id:
            row.template_icon(icon_value=icon_id, scale=3.2)
        else:
            row.label(text="", icon="FILE_FOLDER")

        # Left-aligned name/author, vertically centered beside the icon.
        mid = row.column()
        mid.alignment = "CENTER"
        text = mid.column(align=True)
        text.alignment = "LEFT"
        title = text.row(align=True)
        title.scale_y = 1.1
        title.label(text=name)
        if subtitle:
            sub = text.row(align=True)
            sub.active = False
            sub.scale_y = 0.85
            sub.label(text=subtitle)

        check = row.row(align=True)
        check.alignment = "RIGHT"
        check.label(text="", icon="CHECKMARK")

    def _draw_advanced(self, layout, scene):
        header = layout.row(align=True)
        header.prop(
            scene,
            "mcbedrock_asset_advanced_show",
            text="Advanced",
            icon="TRIA_DOWN" if scene.mcbedrock_asset_advanced_show else "TRIA_RIGHT",
            emboss=False,
        )
        if not scene.mcbedrock_asset_advanced_show:
            return

        adv = layout.box()
        # Folder / refresh / pack-link live under Advanced so gallery stays clean.
        root = adv.row(align=True)
        root.prop(scene, "mcbedrock_browser_root", text="")
        root.operator("mcbedrock.asset_browser_set_root", text="", icon="FILEBROWSER")
        root.operator("mcbedrock.asset_browser_refresh", text="", icon="FILE_REFRESH")
        root.operator("mcbedrock.asset_browser_use_as_pack", text="", icon="LINKED")

        gen = adv.row(align=True)
        gen.enabled = not scene.mcbedrock_thumb_generating
        gen.prop(scene, "mcbedrock_thumb_quality", text="")
        gen.operator(
            "mcbedrock.generate_thumbnails",
            icon="RENDER_STILL",
            text="Generate Thumbnails",
        )

        adv.prop(scene, "mcbedrock_texture_override_path", text="Texture")

        col = adv.column(align=True)
        col.label(text="Particle", icon="PARTICLES")
        col.prop(scene, "mcbedrock_particle_force_loop", text="Force Loop")
        col.prop(scene, "mcbedrock_particle_interpolation", text="Interpolation")

        col = adv.column(align=True)
        col.label(text="Model / Entity", icon="OUTLINER_OB_ARMATURE")
        col.prop(scene, "mcbedrock_geometry_index", text="Geometry Index")
        col.prop(scene, "mcbedrock_import_all_variants", text="Import All Variants")
        col.prop(scene, "mcbedrock_pose_preset", text="Pose Mode")
        col.prop(scene, "mcbedrock_entity_effects", text="Entity Effects")

    def _draw_selected_asset_setup(self, layout, context, item):
        """Draw the model/entity choices that must be reviewed before import."""
        if not item or not _item_has_setup(item):
            return False

        scene = context.scene
        box = layout.box()

        title = box.row(align=True)
        title.label(text=f"Selected: {item.name}", icon=_icon_for(item))

        info = box.column(align=True)
        info.label(text=f"Model: {item.name}")
        if item.identifier:
            info.label(text=f"Identifier: {item.identifier}")
        geometry = _entity_geometry_summary(item)
        if geometry and geometry != item.identifier:
            info.label(text=f"Geometry: {geometry}")

        options = box.column(align=True)
        options.prop(scene, "mcbedrock_preview_preset", text="Appearance")

        if scene.mcbedrock_preview_preset == "CUSTOM":
            custom = options.column(align=True)
            row = custom.row(align=True)
            row.prop(scene, "mcbedrock_preview_variant", text="Variant")
            row.prop(scene, "mcbedrock_preview_mark_variant", text="Marking")
            custom.prop(scene, "mcbedrock_preview_armor_slot", text="Armor")
            row = custom.row(align=True)
            row.prop(scene, "mcbedrock_preview_is_baby", text="Baby")
            row.prop(scene, "mcbedrock_preview_is_saddled", text="Saddled")
            row = custom.row(align=True)
            row.prop(scene, "mcbedrock_preview_has_rider", text="Rider")
            row.prop(scene, "mcbedrock_preview_is_chested", text="Storage")

        if (item.asset_type or "").lower() == "entity":
            animation_items = _browser_animation_items(None, context)
            if len(animation_items) > 1:
                options.prop(
                    scene,
                    "mcbedrock_browser_animation",
                    text="Animation / Cutscene",
                )
            else:
                row = options.row(align=True)
                row.enabled = False
                row.label(text="Animation / Cutscene")
                row.label(text="Rest pose only", icon="ACTION")

        import_row = box.row(align=True)
        import_row.scale_y = 1.3
        import_row.enabled = bool(item.importable) and item.status not in {"Unreadable", "Protected"}
        import_op = import_row.operator(
            "mcbedrock.asset_browser_import_selected",
            text="Import Texture as Plane" if item.asset_type == "Texture" else "Import",
            icon="IMPORT",
        )
        import_op.index = scene.mcbedrock_browser_index
        import_op.source_path = item.source_path
        import_op.asset_key = _browser_item_key(item)
        import_op.asset_type = item.asset_type
        import_op.asset_identifier = item.identifier
        import_op.geometry_index = item.geometry_index
        return True


    def _draw_asset_browser(self, layout, context):
        scene = context.scene
        grid = scene.mcbedrock_browser_view_mode == "GRID"

        # 1. Active pack card (concept header).
        self._draw_active_banner(layout, scene)

        # 2. Advanced (folder/thumbs/import options).
        self._draw_advanced(layout, scene)

        # 3. Filter + search (+ compact view toggle).
        row = layout.row(align=True)
        row.prop(scene, "mcbedrock_browser_filter", text="")
        row.prop(scene, "mcbedrock_browser_search", text="", icon="VIEWZOOM")
        row.operator(
            "mcbedrock.asset_browser_set_pack",
            text="",
            icon="FILE_FOLDER",
        )
        row.operator(
            "mcbedrock.asset_browser_refresh",
            text="",
            icon="FILE_REFRESH",
        )
        row.prop(scene, "mcbedrock_browser_view_mode", text="", icon_only=True)

        # 4. Asset list / gallery.
        if grid:
            # The gallery UIList supplies Blender's native scrollbar.
            _draw_gallery_grid(layout, scene)
        else:
            layout.template_list(
                "MCBEDROCK_UL_asset_browser_default",
                "mcbedrock_asset_browser_list",
                scene,
                "mcbedrock_browser_assets",
                scene,
                "mcbedrock_browser_index",
                rows=8,
            )

        item = None
        if 0 <= scene.mcbedrock_browser_index < len(scene.mcbedrock_browser_assets):
            item = scene.mcbedrock_browser_assets[scene.mcbedrock_browser_index]

        # Gallery cards own their import actions; list-only controls stay hidden.
        if item and item.asset_type == 'Texture' and item.importable:
            posterize_row = layout.row()
            posterize_row.operator_context = 'EXEC_DEFAULT'
            posterize_row.operator('mcbedrock.posterize_open', text='Posterize').filepath = item.source_path
        if grid:
            return


        if item and item.asset_type == "Entity":
            layout.prop(scene, "mcbedrock_browser_animation", text="Animation")

        # Gallery cards already have per-item Import ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â keep the bottom bar +
        # details for list mode only so the gallery stays clean.

        label = "Import Texture as Plane" if item and item.asset_type == "Texture" else "Import"
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.enabled = bool(item and item.importable)
        import_op = row.operator(
            "mcbedrock.asset_browser_import_selected",
            text=label,
            icon="IMPORT",
        )
        if item is not None:
            import_op.index = scene.mcbedrock_browser_index
            import_op.source_path = item.source_path
            import_op.asset_key = _browser_item_key(item)
            import_op.asset_type = item.asset_type
            import_op.asset_identifier = item.identifier
            import_op.geometry_index = item.geometry_index

        if item:
            details = layout.box()
            details.label(text=item.name, icon=_icon_for(item))
            details.label(text=item.asset_type)
            if item.identifier:
                details.label(text=item.identifier)
            if item.pack_name:
                details.label(text=item.pack_name)
            if item.status:
                details.label(text=item.status)
            if item.detail:
                details.label(text=item.detail[:160])
            if item.source_path:
                details.label(text=item.source_path[:160])

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        self._draw_asset_browser(layout, context)


classes = (
    MCBEDROCK_PG_browser_asset,
    MCBEDROCK_PG_gallery_row,
    MCBEDROCK_UL_asset_browser_default,
    MCBEDROCK_UL_asset_browser_gallery,
    MCBEDROCK_UL_asset_browser_grid,
    MCBEDROCK_OT_asset_browser_select,
    MCBEDROCK_OT_asset_browser_import_dialog,
    MCBEDROCK_OT_asset_browser_gallery_scroll,
    MCBEDROCK_OT_asset_browser_set_root,
    MCBEDROCK_OT_asset_browser_set_pack,
    MCBEDROCK_OT_asset_browser_reset,
    MCBEDROCK_OT_asset_browser_refresh,
    MCBEDROCK_OT_asset_browser_import_selected,
    MCBEDROCK_OT_import_texture,
    MCBEDROCK_OT_asset_browser_use_as_pack,
    MCBEDROCK_OT_generate_thumbnails,
    MCBEDROCK_PT_asset_browser,
)


# Scene properties the browser and pack manager declare SKIP_SAVE: lists that
# are rebuilt from disk, indices, searches and status text. Blender writes them
# into .blend files anyway (a 20k-asset pack added megabytes, and reopening a
# file brought back stale rows and searches), so they are set aside while a
# file is written and put back straight after.
_TRANSIENT_SCENE_PROPS = (
    'mcbedrock_browser_assets', 'mcbedrock_gallery_rows', 'mcbedrock_discovered_packs',
    'mcbedrock_browser_root', 'mcbedrock_browser_index', 'mcbedrock_gallery_scroll',
    'mcbedrock_gallery_row_index', 'mcbedrock_pack_search', 'mcbedrock_pack_manager_status',
    'mcbedrock_discovered_pack_index', 'mcbedrock_active_pack_path', 'mcbedrock_list_scroll',
    'mcbedrock_pack_filters', 'mcbedrock_pack_version_min', 'mcbedrock_pack_version_max',
    'mcbedrock_pack_filter_exclude', 'mcbedrock_pack_match_all', 'mcbedrock_pack_sort_desc',
    'mcbedrock_pack_sort',
)
_TRANSIENT_CUSTOM_PROPS = ('_mcbedrock_loaded_browser_root',)
_set_aside = []


def _scene_system_properties(scene, create=False):
    getter = getattr(scene, 'bl_system_properties_get', None)
    if getter is None:
        return scene  # Before Blender 5.0 add-on properties lived with custom ones.
    return getter(do_create=create)


def _plain(value):
    if isinstance(value, list):
        return [_plain(item) for item in value]
    for name in ('to_dict', 'to_list'):
        convert = getattr(value, name, None)
        if convert is not None:
            return convert()
    return value


@persistent
def _set_transient_aside(*_args):
    _set_aside.clear()
    for scene in bpy.data.scenes:
        stored, custom = {}, {}
        try:
            props = _scene_system_properties(scene)
            if props is not None:
                for name in _TRANSIENT_SCENE_PROPS:
                    if name in props:
                        value = _plain(props[name])
                        del props[name]
                        if value != []:  # an absent collection reads as empty
                            stored[name] = value
            for name in _TRANSIENT_CUSTOM_PROPS:
                if name in scene:
                    custom[name] = scene[name]
                    del scene[name]
        except (AttributeError, TypeError, KeyError, ReferenceError) as exc:
            print('[Sparkle] could not leave session lists out of the file: %s' % exc)
        if stored or custom:
            _set_aside.append((scene, stored, custom))


@persistent
def _put_transient_back(*_args):
    entries = list(_set_aside)
    _set_aside.clear()
    for scene, stored, custom in entries:
        try:
            props = _scene_system_properties(scene, create=True)
            for name, value in stored.items():
                props[name] = value
            for name, value in custom.items():
                scene[name] = value
        except (AttributeError, TypeError, KeyError, ReferenceError) as exc:
            print('[Sparkle] could not restore session lists after saving: %s' % exc)
    if entries:
        # Sidebar widgets held rows that were set aside: rebuild before drawing.
        refresh_browser_pages()


_SAVE_HANDLERS = (('save_pre', _set_transient_aside), ('save_post', _put_transient_back),
                  ('save_post_fail', _put_transient_back))


def _remove_save_handlers():
    for name, callback in _SAVE_HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is None:
            continue
        for handler in list(handlers):
            if handler is callback or (getattr(handler, '__name__', '') == callback.__name__
                                       and getattr(handler, '__module__', '') == __name__):
                handlers.remove(handler)


def _listen_to_mod_thumbnails(listen):
    """Mod cards redraw when the mod pool finishes one of their thumbnails."""
    from . import mod_thumbnails
    mod_thumbnails._LISTENERS[:] = [listener for listener in mod_thumbnails._LISTENERS
                                    if getattr(listener, '__module__', '') != __name__]
    if listen:
        mod_thumbnails._LISTENERS.append(_redraw_ui)


def _register_impl():
    if not bpy:
        return
    from ..importers import thumbnail_cache
    thumbnail_cache.get_cache_dir()  # Ensure the thumbnail cache folder exists.
    _remove_save_handlers()
    for name, callback in _SAVE_HANDLERS:
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None:
            handlers.append(callback)
    _listen_to_mod_thumbnails(True)

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.mcbedrock_browser_root = bpy.props.StringProperty(
        name="Browser Folder",
        description="Folder to scan for Minecraft resource packs, world templates, models, particles, textures, and animations",
        default="",
        subtype='DIR_PATH',
        options={"SKIP_SAVE"},
    )
    bpy.types.Scene.mcbedrock_browser_search = bpy.props.StringProperty(
        name="Search",
        description="Filter assets by name, identifier, pack, type, or detail",
        default="",
        update=_on_gallery_filter_changed,
        options={"TEXTEDIT_UPDATE"},
    )
    bpy.types.Scene.mcbedrock_browser_filter = bpy.props.EnumProperty(
        name="Filter",
        description="Asset type filter",
        items=ASSET_FILTER_ITEMS,
        default="IMPORTABLE",
        update=_on_gallery_filter_changed,
    )
    bpy.types.Scene.mcbedrock_browser_view_mode = bpy.props.EnumProperty(
        name="View",
        description="Display assets as a compact list or a grid of tiles",
        items=VIEW_MODE_ITEMS,
        default="GRID",
        update=_on_gallery_filter_changed,
    )
    bpy.types.Scene.mcbedrock_gallery_scroll = bpy.props.IntProperty(
        name="Gallery Scroll",
        description="Scroll the 2x2 asset gallery by row",
        default=0,
        min=0,
        soft_max=500,
        options={"SKIP_SAVE"},
        update=_on_gallery_scroll_changed,
    )
    bpy.types.Scene.mcbedrock_gallery_rows = bpy.props.CollectionProperty(
        type=MCBEDROCK_PG_gallery_row,
        options={"SKIP_SAVE"},
    )
    bpy.types.Scene.mcbedrock_gallery_row_index = bpy.props.IntProperty(
        default=-1,
        options={"SKIP_SAVE"},
    )
    bpy.types.Scene.mcbedrock_browser_animation = bpy.props.EnumProperty(
        name="Animation / Cutscene",
        description="Animation or cutscene to apply when importing the selected Bedrock entity",
        items=_browser_animation_items,
    )
    bpy.types.Scene.mcbedrock_texture_override_path = bpy.props.StringProperty(
        name="Texture Override",
        description="Optional texture to use for the imported asset instead of the auto-resolved one",
        default="",
        subtype='FILE_PATH',
    )
    bpy.types.Scene.mcbedrock_particle_force_loop = bpy.props.BoolProperty(
        name="Force Loop",
        description="Force the imported particle emitter to loop",
        default=False,
    )
    bpy.types.Scene.mcbedrock_particle_interpolation = bpy.props.EnumProperty(
        name="Interpolation",
        description="Texture interpolation for imported particles and models",
        items=INTERPOLATION_ITEMS,
        default='Closest',
    )
    bpy.types.Scene.mcbedrock_geometry_index = bpy.props.IntProperty(
        name="Geometry Index",
        description="Geometry index to import from a multi-geometry model file (0 = use indexed default)",
        default=0,
        min=0,
    )
    bpy.types.Scene.mcbedrock_import_all_variants = bpy.props.BoolProperty(
        name="Import All Variants",
        description="Import every geometry variant referenced by render controllers (debug/cutscene use)",
        default=False,
    )
    bpy.types.Scene.mcbedrock_preview_preset = bpy.props.EnumProperty(
        name="Preview State",
        items=[
            ('AUTO', "Auto", "Canonical adult idle state"),
            ('ADULT', "Adult", "Adult idle geometry and textures"),
            ('BABY', "Baby", "Baby geometry and textures"),
            ('CUSTOM', "Custom", "Use the advanced query controls"),
        ],
        default='AUTO',
    )
    bpy.types.Scene.mcbedrock_pose_preset = bpy.props.EnumProperty(
        name="Pose Mode",
        items=[
            ('DEFAULT_IDLE', "In-game Neutral", "Deterministic renderer setup and safe idle preview"),
            ('REST_BIND', "Raw Authored", "Authored geometry and bind transforms without renderer assembly"),
        ],
        default='DEFAULT_IDLE',
    )
    bpy.types.Scene.mcbedrock_entity_effects = bpy.props.EnumProperty(
        name="Entity Effects",
        items=[
            ('PREVIEW', "Preview", "Import controller and animation particle previews"),
            ('OFF', "Off", "Skip entity-driven particles"),
        ],
        default='PREVIEW',
    )
    bpy.types.Scene.mcbedrock_preview_variant = bpy.props.IntProperty(
        name="Variant", description="Render-controller query.variant", default=0, min=0,
    )
    bpy.types.Scene.mcbedrock_preview_mark_variant = bpy.props.IntProperty(
        name="Marking", description="Render-controller query.mark_variant", default=0, min=0,
    )
    bpy.types.Scene.mcbedrock_preview_armor_slot = bpy.props.IntProperty(
        name="Armor", description="Preview armor texture slot", default=0, min=0,
    )
    bpy.types.Scene.mcbedrock_preview_is_baby = bpy.props.BoolProperty(
        name="Baby", description="Preview query.is_baby", default=False,
    )
    bpy.types.Scene.mcbedrock_preview_is_saddled = bpy.props.BoolProperty(
        name="Saddled", description="Preview query.is_saddled", default=False,
    )
    bpy.types.Scene.mcbedrock_preview_has_rider = bpy.props.BoolProperty(
        name="Rider", description="Preview query.has_rider", default=False,
    )
    bpy.types.Scene.mcbedrock_preview_is_chested = bpy.props.BoolProperty(
        name="Storage", description="Preview query.is_chested", default=False,
    )
    bpy.types.Scene.mcbedrock_browser_assets = bpy.props.CollectionProperty(
        type=MCBEDROCK_PG_browser_asset,
        options={'SKIP_SAVE'}
    )
    bpy.types.Scene.mcbedrock_browser_index = bpy.props.IntProperty(
        default=-1,
        options={'SKIP_SAVE'},
        update=_on_browser_index_changed,
    )


def register():
    """Register transactionally, removing residue from a failed enable."""
    if not bpy:
        return
    unregister()
    try:
        _register_impl()
    except Exception:
        unregister()
        raise


def unregister():
    global _mod_jar_shown
    if not bpy:
        return
    _remove_save_handlers()
    _listen_to_mod_thumbnails(False)
    _mod_jar_shown = None
    _browser_snapshots.clear()
    _browser_prepared.clear()
    _filtered_views.clear()
    _list_flags.clear()
    _scene_thumbnail_caches.clear()
    _stop_browser_index()
    _stop_worker(kill=True)  # Never orphan a background Blender or leave the timer live.
    _entity_setup_meta_cache.clear()
    _entity_capability_cache.clear()
    _active_meta_cache.clear()
    _clear_thumb_previews()
    for attr in (
        "mcbedrock_browser_root",
        "mcbedrock_browser_search",
        "mcbedrock_browser_filter",
        "mcbedrock_browser_view_mode",
        "mcbedrock_gallery_scroll",
        "mcbedrock_gallery_row_index",
        "mcbedrock_gallery_rows",
        "mcbedrock_browser_animation",
        "mcbedrock_texture_override_path",
        "mcbedrock_particle_force_loop",
        "mcbedrock_particle_interpolation",
        "mcbedrock_geometry_index",
        "mcbedrock_import_all_variants",
        "mcbedrock_preview_preset",
        "mcbedrock_pose_preset",
        "mcbedrock_entity_effects",
        "mcbedrock_preview_variant",
        "mcbedrock_preview_mark_variant",
        "mcbedrock_preview_armor_slot",
        "mcbedrock_preview_is_baby",
        "mcbedrock_preview_is_saddled",
        "mcbedrock_preview_has_rider",
        "mcbedrock_preview_is_chested",
        "mcbedrock_browser_assets",
        "mcbedrock_browser_index",
    ):
        if hasattr(bpy.types.Scene, attr):
            delattr(bpy.types.Scene, attr)

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

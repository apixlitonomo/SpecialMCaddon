"""On-disk cache for asset-browser thumbnails.

Thumbnails are 128px PNGs keyed by a hash of the asset's source path, type,
identifier and source modification time. The cache lives in the per-user
cache (``%LOCALAPPDATA%/MCAddon/thumbs`` on Windows), outside the installed
add-on, so installing a new build keeps every finished thumbnail.

Rendering never happens in the active Blender session. ``ThumbnailCache``
serialises a queue of pending assets to JSON and hands it to a headless Blender
subprocess running ``thumbnail_worker.py``; the UI only ever reads finished PNGs
back off disk.

The module-level functions at the bottom are the legacy API used by
``ui/asset_browser.py`` and are kept working on top of the class.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .archive_extractor import user_cache_dir
from .preview_fallback import png_has_visible_pixels


CACHE_DIR_NAME = "mc_thumb_cache"
WORKER_NAME = "thumbnail_worker.py"
THUMB_SIZE = 128

# Written by the worker beside the (absent) PNG when an asset cannot be
# rendered. Without it a failed asset is indistinguishable from a pending one,
# so the UI would show a neutral icon forever and every refresh would re-queue
# the same doomed asset. Change the suffix when the importer starts accepting
# assets it used to reject: old markers are then ignored, so only those
# assets render again while finished thumbnails stay cached.
FAILED_SUFFIX = ".failed-v2"

# Bump whenever import geometry, UVs or materials change so cached
# thumbnails re-render with the importer the user actually runs.
RENDER_SCHEMA_VERSION = "renderer-fidelity-v23-duplicate-cubes"
# Bump for Java-only changes: Java thumbnails re-render, Bedrock ones stay.
JAVA_RENDER_SCHEMA_VERSION = "java-v2-turned-not-mirrored"


def render_schema_version(asset_type):
    """The importer version a thumbnail of ``asset_type`` is rendered with."""
    if str(asset_type or "").lower().startswith("java"):
        return RENDER_SCHEMA_VERSION + "|" + JAVA_RENDER_SCHEMA_VERSION
    return RENDER_SCHEMA_VERSION


BEDROCK_DEPENDENCY_DIRS = {
    "animation_controllers", "animations", "attachables", "blocks",
    "entity", "models", "render_controllers", "textures",
}
BEDROCK_DEPENDENCY_EXTENSIONS = {".json", ".png", ".tga", ".jpg", ".jpeg"}
_DEPENDENCY_FINGERPRINT_CACHE = {}


def invalidate_dependency_fingerprints(root=""):
    """Forget cached Bedrock pack snapshots.

    Resource-pack refresh is the explicit invalidation boundary. Keeping the
    snapshot between UI redraws is essential: recursively walking a large pack
    from every thumbnail lookup blocks Blender's main thread.
    """
    if not root:
        _DEPENDENCY_FINGERPRINT_CACHE.clear()
        return
    target = os.path.normcase(os.path.abspath(str(root)))
    for cache_key in list(_DEPENDENCY_FINGERPRINT_CACHE):
        pack_root = os.path.normcase(cache_key[1] if isinstance(cache_key, tuple) else cache_key)
        try:
            inside = os.path.commonpath((pack_root, target)) == target
        except ValueError:
            inside = False
        if pack_root == target or inside:
            _DEPENDENCY_FINGERPRINT_CACHE.pop(cache_key, None)


def _bedrock_pack_root(file_path):
    current = Path(file_path).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def _is_bedrock_visual_asset(file_path, asset_type):
    kind = str(asset_type or "").lower()
    # Mod cards point at their JAR; the mod's own renderer draws them.
    if "java" in kind or "particle" in kind or kind.startswith("mod "):
        return False
    if any(token in kind for token in ("entity", "model", "attachable", "block")):
        return True
    parts = {part.lower() for part in Path(file_path).parts}
    return bool(parts & {"entity", "attachables", "models", "blocks"})


def _scan_files(directory, suffixes):
    """Yield ``(path, stat)`` for matching files below ``directory``.

    ``os.scandir`` returns size and mtime with the directory listing on
    Windows, so a 20k-file pack fingerprints in about a tenth of a second.
    """
    stack = [str(directory)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif (
                            entry.is_file()
                            and os.path.splitext(entry.name)[1].lower() in suffixes
                        ):
                            yield entry.path, entry.stat()
                    except OSError:
                        continue
        except OSError:
            continue


def _bedrock_dependency_records(file_path, asset_type):
    """``(path, mtime_ns, size)`` for pack files that can change a Bedrock render."""
    if not _is_bedrock_visual_asset(file_path, asset_type):
        return ()
    root = _bedrock_pack_root(file_path)
    if root is None:
        return ()
    found = []
    blocks_json = root / "blocks.json"
    if blocks_json.is_file():
        found.append((str(blocks_json), blocks_json.stat()))
    for directory_name in BEDROCK_DEPENDENCY_DIRS:
        directory = root / directory_name
        if directory.is_dir():
            found.extend(_scan_files(directory, BEDROCK_DEPENDENCY_EXTENSIONS))
    return tuple(sorted(
        (path.replace("\\", "/").lower(), int(stat.st_mtime_ns), int(stat.st_size))
        for path, stat in found
    ))


def _dependency_fingerprint(file_path, asset_type):
    if not _is_bedrock_visual_asset(file_path, asset_type):
        return ""
    root = _bedrock_pack_root(file_path)
    if root is None:
        return ""
    root_key = os.path.normcase(os.path.abspath(str(root)))
    if root_key in _DEPENDENCY_FINGERPRINT_CACHE:
        return _DEPENDENCY_FINGERPRINT_CACHE[root_key]

    records = list(_bedrock_dependency_records(file_path, asset_type))
    fingerprint = ""
    if records:
        payload = json.dumps(records, separators=(",", ":"), ensure_ascii=True)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    _DEPENDENCY_FINGERPRINT_CACHE[root_key] = fingerprint
    return fingerprint


def _stack_dependency_fingerprint(roots):
    """Snapshot all participating packs once per resource refresh, not redraw."""
    values = []
    for root in roots:
        if not root:
            continue
        path = Path(root).resolve()
        key = ('stack', str(path))
        fingerprint = _DEPENDENCY_FINGERPRINT_CACHE.get(key)
        if fingerprint is None:
            records = []
            if path.is_file():
                stat = path.stat()
                records.append((str(path), stat.st_mtime_ns, stat.st_size))
            else:
                suffixes = BEDROCK_DEPENDENCY_EXTENSIONS | {'.mcmeta'}
                for folder in ('assets', *sorted(BEDROCK_DEPENDENCY_DIRS)):
                    records.extend(
                        (candidate, stat.st_mtime_ns, stat.st_size)
                        for candidate, stat in _scan_files(path / folder, suffixes)
                    )
            for name in ('blocks.json', 'manifest.json', 'pack.mcmeta'):
                candidate = path / name
                if candidate.is_file():
                    stat = candidate.stat()
                    records.append((str(candidate), stat.st_mtime_ns, stat.st_size))
            fingerprint = hashlib.sha256(json.dumps(sorted(records)).encode()).hexdigest()
            _DEPENDENCY_FINGERPRINT_CACHE[key] = fingerprint
        values.append((str(path), fingerprint))
    return json.dumps(values)


def _normalise_geometry_index(value):
    """Return the stable record selector used by cache and worker identities."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _addon_dir():
    """Package root: ``importers/thumbnail_cache.py`` -> two levels up."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ThumbnailCache:
    """Hash-keyed PNG cache plus a launcher for the headless render worker."""

    def __init__(self, cache_dir=None, size=THUMB_SIZE, resource_roots=(), vanilla_path=""):
        self.size = int(size)
        self.resource_roots = tuple(resource_roots)
        self.vanilla_path = vanilla_path or ""
        self._cache_dir = Path(cache_dir) if cache_dir else Path(user_cache_dir("thumbs"))

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    @property
    def cache_dir(self):
        """Return the cache directory, creating it if missing."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return self._cache_dir

    def get_hash(self, file_path, asset_type, identifier, geometry_index=-1):
        """MD5 of the source path, type, identifier, record and source mtime.

        The mtime is part of the key, so editing a resource pack file yields a
        new hash and therefore a cache miss rather than a stale thumbnail.
        Missing/unreadable sources hash with an mtime of ``0`` so they stay
        stable instead of thrashing the cache.
        """
        try:
            mtime = os.path.getmtime(file_path) if file_path else 0.0
        except OSError:
            mtime = 0.0

        raw = "|".join((
            os.path.normcase(os.path.abspath(str(file_path))) if file_path else "",
            str(asset_type or ""),
            str(identifier or ""),
            str(_normalise_geometry_index(geometry_index)),
            "%.6f" % mtime,
            str(self.size),
            render_schema_version(asset_type),
            json.dumps((self.resource_roots, self.vanilla_path)),
            _dependency_fingerprint(file_path, asset_type),
            _stack_dependency_fingerprint((*self.resource_roots, self.vanilla_path)),
        ))
        return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()

    def get_cache_path(self, file_path, asset_type, identifier, geometry_index=-1):
        """Return the PNG path this asset maps to, whether or not it exists."""
        digest = self.get_hash(file_path, asset_type, identifier, geometry_index)
        return str(self.cache_dir / (digest + ".png"))

    def get_thumbnail_path(self, file_path, asset_type, identifier, geometry_index=-1):
        """Return a complete, non-transparent cached PNG or an empty string."""
        if asset_type == "Texture":
            return str(file_path) if os.path.isfile(file_path) else ""
        path = self.get_cache_path(file_path, asset_type, identifier, geometry_index)
        return path if png_has_visible_pixels(path) else ""

    def get_failed_path(self, file_path, asset_type, identifier, geometry_index=-1):
        """Return the failure-marker path for this asset (whether or not it exists)."""
        digest = self.get_hash(file_path, asset_type, identifier, geometry_index)
        return str(self.cache_dir / (digest + FAILED_SUFFIX))

    def has_failed(self, file_path, asset_type, identifier, geometry_index=-1):
        """True when the last render attempt for this asset failed.

        The source mtime is part of the hash, so editing the asset yields a new
        digest with no marker and the render is retried automatically.
        """
        if asset_type == "Texture":
            return False
        return os.path.isfile(
            self.get_failed_path(file_path, asset_type, identifier, geometry_index)
        )

    def clear_failures(self):
        """Drop every failure marker so the next queue retries those assets."""
        try:
            for name in os.listdir(str(self.cache_dir)):
                if name.endswith(FAILED_SUFFIX):
                    marker = self._cache_dir / name
                    fallback = marker.with_suffix(".png")
                    for path in (marker, fallback):
                        try:
                            os.remove(str(path))
                        except OSError:
                            pass
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Headless rendering
    # ------------------------------------------------------------------
    def build_queue(self, assets):
        """Turn asset records into worker job dicts, skipping already-cached ones.

        Assets whose previous render failed are skipped too: their marker stays
        until the source changes (new hash) or ``clear_failures`` is called, so a
        broken asset is not re-rendered on every single refresh.

        ``assets`` is an iterable of objects or dicts exposing ``source_path``,
        ``asset_type`` and ``identifier``.
        """
        jobs = []
        seen = set()
        for asset in assets:
            if isinstance(asset, dict):
                src = asset.get("source_path", "")
                atype = asset.get("asset_type", "")
                ident = asset.get("identifier", "")
                geometry_index = asset.get("geometry_index")
                if geometry_index is None:
                    geometry_index = (asset.get("metadata") or {}).get("geometry_index", -1)
                metadata_json = asset.get("metadata_json", "")
                if not metadata_json and asset.get("metadata"):
                    metadata_json = json.dumps(asset["metadata"], ensure_ascii=True)
            else:
                src = getattr(asset, "source_path", "")
                atype = getattr(asset, "asset_type", "")
                ident = getattr(asset, "identifier", "")
                geometry_index = getattr(asset, "geometry_index", None)
                if geometry_index is None:
                    geometry_index = (getattr(asset, "metadata", None) or {}).get(
                        "geometry_index", -1
                    )
                metadata_json = getattr(asset, "metadata_json", "")
                if not metadata_json and getattr(asset, "metadata", None):
                    metadata_json = json.dumps(
                        asset.metadata, ensure_ascii=True
                    )

            geometry_index = _normalise_geometry_index(geometry_index)
            if metadata_json:
                try:
                    if not json.loads(metadata_json).get('br_available', True):
                        continue
                except (ValueError, TypeError):
                    pass
            # Texture cards display the source image directly, without a 3D render.
            if atype == "Texture":
                continue
            if not src or not os.path.isfile(src):
                continue
            out_path = self.get_cache_path(src, atype, ident, geometry_index)
            if out_path in seen:
                continue
            if os.path.isfile(out_path):
                if png_has_visible_pixels(out_path):
                    continue
                # Old workers treated an empty/corrupt PNG as success. Remove
                # it and its marker so the corrected worker gets one retry.
                try:
                    os.remove(out_path)
                except OSError:
                    continue
                marker = self.get_failed_path(src, atype, ident, geometry_index)
                if os.path.isfile(marker):
                    try:
                        os.remove(marker)
                    except OSError:
                        pass
            if self.has_failed(src, atype, ident, geometry_index):
                continue
            seen.add(out_path)
            jobs.append({
                "file_path": os.path.abspath(src),
                "asset_type": str(atype or ""),
                "identifier": str(ident or ""),
                "geometry_index": geometry_index,
                "metadata_json": str(metadata_json or ""),
                "dependency_fingerprint": _dependency_fingerprint(src, atype),
                "output": out_path,
            })
        return jobs

    def write_queue(self, jobs, resource_pack="", queue_path=""):
        """Write the worker payload to JSON and return its path."""
        payload = {
            "size": self.size,
            "cache_dir": str(self.cache_dir),
            "resource_pack": resource_pack or "",
            "resource_roots": list(self.resource_roots),
            "vanilla_path": self.vanilla_path,
            "render_schema_version": RENDER_SCHEMA_VERSION,
            "items": list(jobs),
        }
        if not queue_path:
            handle, queue_path = tempfile.mkstemp(prefix="mc_thumb_queue_", suffix=".json")
            os.close(handle)
        with open(queue_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
        return queue_path

    def worker_script(self):
        """Absolute path to ``thumbnail_worker.py``."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), WORKER_NAME)

    def blender_executable(self):
        """Path to the running Blender binary (``sys.argv[0]`` inside Blender)."""
        try:
            import bpy
            return bpy.app.binary_path or sys.executable
        except ImportError:
            return sys.executable

    def render_queue(self, assets, resource_pack="", wait=False, timeout=600):
        """Render missing thumbnails in a background Blender process.

        Returns the ``subprocess.Popen`` handle, or ``None`` when nothing needs
        rendering. With ``wait=False`` (the default) the call returns
        immediately and the UI keeps polling :meth:`get_thumbnail_path`.
        """
        jobs = self.build_queue(assets)
        if not jobs:
            return None

        queue_path = self.write_queue(jobs, resource_pack=resource_pack)
        command = [
            self.blender_executable(),
            "-b",
            "--factory-startup",
            "-noaudio",
            "-P", self.worker_script(),
            "--", queue_path,
        ]

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        if wait:
            try:
                out, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                out, _ = process.communicate()
            if process.returncode != 0:
                print("[MCBedrock] thumbnail worker failed (%s):" % process.returncode)
                print((out or b"").decode("utf-8", "replace"))
            try:
                os.remove(queue_path)
            except OSError:
                pass
        return process

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def clear(self):
        """Delete every cached thumbnail."""
        try:
            if self._cache_dir.is_dir():
                shutil.rmtree(str(self._cache_dir), ignore_errors=True)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Legacy module-level API (used by ui/asset_browser.py)
# ----------------------------------------------------------------------
_default_cache = ThumbnailCache()


def get_cache_dir():
    """Return the per-user thumbnail folder (created if missing)."""
    return str(_default_cache.cache_dir)


def _asset_fields(asset):
    geometry_index = getattr(asset, "geometry_index", None)
    if geometry_index is None:
        geometry_index = (getattr(asset, "metadata", None) or {}).get(
            "geometry_index", -1
        )
    return (
        str(getattr(asset, "source_path", "")),
        str(getattr(asset, "asset_type", "")),
        str(getattr(asset, "identifier", "")),
        _normalise_geometry_index(geometry_index),
    )


def cache_path_for(asset):
    """Return the PNG path this asset would use (whether or not it exists)."""
    return _default_cache.get_cache_path(*_asset_fields(asset))


def get_thumbnail_path(asset):
    """Return the cached PNG path if present, else ``None`` (legacy contract)."""
    return _default_cache.get_thumbnail_path(*_asset_fields(asset)) or None


def generate_and_cache(asset, context=None, wait=False):
    """Queue a headless render for ``asset``; return its cached path if ready."""
    existing = get_thumbnail_path(asset)
    if existing:
        return existing

    resource_pack = ""
    if context is not None:
        resource_pack = getattr(context.scene, "mcbedrock_resource_pack_path", "") or ""

    try:
        _default_cache.render_queue([asset], resource_pack=resource_pack, wait=wait)
    except Exception as exc:  # pragma: no cover - defensive
        print("[MCBedrock] thumbnail worker launch failed: %s" % exc)
        return None
    return get_thumbnail_path(asset)


def render_assets(assets, context=None, wait=False):
    """Queue a headless render for many assets at once. Returns the Popen or None."""
    resource_pack = ""
    if context is not None:
        resource_pack = getattr(context.scene, "mcbedrock_resource_pack_path", "") or ""
    try:
        return _default_cache.render_queue(assets, resource_pack=resource_pack, wait=wait)
    except Exception as exc:  # pragma: no cover - defensive
        print("[MCBedrock] thumbnail worker launch failed: %s" % exc)
        return None


def clear_cache():
    """Delete every cached thumbnail."""
    _default_cache.clear()

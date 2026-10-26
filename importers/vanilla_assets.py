"""Locate vanilla Java block models so incomplete packs can still build.

Most texture packs - "Bare Bones" among them - override ``textures/`` only and
ship no ``blockstates/`` or ``models/``. On their own they can never describe a
block's geometry. This module finds a vanilla client jar, unpacks just its
``assets/`` tree into the add-on cache once, and hands back a root that can sit
*underneath* the active pack in a search chain: the pack supplies textures, and
vanilla supplies the blockstates and models it omits.

Pure Python - no ``bpy`` - so discovery and extraction are testable headless.
"""

import json
import os
import re
import time
import zipfile

try:
    from .archive_extractor import archive_hash, get_cache_dir, user_cache_dir
    from .path_discovery import discover_java_launcher_roots
except ImportError:  # pragma: no cover - plain Python test path
    from importers.archive_extractor import archive_hash, get_cache_dir, user_cache_dir
    from importers.path_discovery import discover_java_launcher_roots


# A jar is only useful to us if it carries block definitions.
BLOCKSTATE_MARKER = "assets/minecraft/blockstates/"
ASSET_PREFIX = "assets/"
EXTRACT_COMPLETE_MARKER = ".mcbedrock_assets_complete"

# Bounded search: <base>/versions/<ver>/<ver>.jar plus the per-instance layouts
# used by Prism, MultiMC, CurseForge and Modrinth.
_INSTANCE_SUBPATHS = ("", ".minecraft", "minecraft")
_MAX_INSTANCES = 200


def _version_sort_key(name):
    """Sort 1.21.4 above 1.9 above junk, without choking on modded names."""
    numbers = re.findall(r"\d+", name or "")
    if not numbers:
        return (0, ())
    return (1, tuple(int(part) for part in numbers[:4]))


def _default_versions_dirs():
    dirs = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        dirs.append(os.path.join(appdata, ".minecraft", "versions"))
        dirs.append(os.path.join(appdata, "ModrinthApp", "meta", "versions"))
    home = os.path.expanduser("~")
    # Linux / macOS layouts, for completeness.
    dirs.append(os.path.join(home, ".minecraft", "versions"))
    dirs.append(os.path.join(
        home, "Library", "Application Support", "minecraft", "versions"
    ))
    return dirs


def _instance_versions_dirs():
    """``versions`` folders beneath any launcher instance root we know about."""
    dirs = []
    try:
        roots = discover_java_launcher_roots()
    except Exception:
        return dirs
    for entry in roots:
        root = entry.get("root", "")
        if not root or not os.path.isdir(root):
            continue
        # The launcher root itself may be .../resourcepacks; its parent is the
        # instance, which is where versions/ lives.
        bases = [root, os.path.dirname(root)]
        for base in bases:
            for subpath in _INSTANCE_SUBPATHS:
                candidate = os.path.join(base, subpath, "versions") if subpath \
                    else os.path.join(base, "versions")
                if os.path.isdir(candidate):
                    dirs.append(candidate)
        try:
            entries = sorted(os.listdir(root))[:_MAX_INSTANCES]
        except OSError:
            continue
        for name in entries:
            instance = os.path.join(root, name)
            if not os.path.isdir(instance):
                continue
            for subpath in _INSTANCE_SUBPATHS:
                candidate = os.path.join(instance, subpath, "versions") if subpath \
                    else os.path.join(instance, "versions")
                if os.path.isdir(candidate):
                    dirs.append(candidate)
    return dirs


# Every Java import resolves the vanilla root. Reading each launcher jar's
# central directory and walking launcher folders cost about a second per
# import, so both are remembered: jar contents by path, size and mtime (also
# on disk, because pack indexing runs in a fresh process each time), and the
# discovery list for a few minutes (a newly installed version still appears
# without restarting Blender).
_JAR_CONTENT_CACHE = {}
_JAR_CONTENT_LOADED = False
_DISCOVERY_CACHE = {}
_DISCOVERY_TTL_SECONDS = 300.0


def _jar_content_cache_path():
    return os.path.join(user_cache_dir("index"), "vanilla_jars.json")


def _load_jar_content_cache():
    global _JAR_CONTENT_LOADED
    if _JAR_CONTENT_LOADED:
        return
    _JAR_CONTENT_LOADED = True
    try:
        with open(_jar_content_cache_path(), encoding="utf-8") as stream:
            saved = json.load(stream)
        for path, size, mtime_ns, result in saved:
            _JAR_CONTENT_CACHE.setdefault((path, size, mtime_ns), bool(result))
    except (OSError, ValueError, TypeError):
        pass


def _save_jar_content_cache():
    # Jars that are gone (old launcher versions) drop out of the saved list.
    entries = [
        [path, size, mtime_ns, result]
        for (path, size, mtime_ns), result in _JAR_CONTENT_CACHE.items()
        if os.path.isfile(path)
    ]
    path = _jar_content_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = "%s.%d.tmp" % (path, os.getpid())
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(entries, stream)
        os.replace(temporary, path)
    except OSError:
        pass


def jar_has_block_models(jar_path):
    """True when a jar actually contains vanilla blockstates."""
    try:
        stat = os.stat(jar_path)
    except OSError:
        return False
    _load_jar_content_cache()
    key = (os.path.normcase(os.path.abspath(jar_path)), stat.st_size, stat.st_mtime_ns)
    cached = _JAR_CONTENT_CACHE.get(key)
    if cached is not None:
        return cached
    result = False
    try:
        with zipfile.ZipFile(jar_path) as archive:
            result = any(
                name.startswith(BLOCKSTATE_MARKER) for name in archive.namelist()
            )
    except (OSError, zipfile.BadZipFile):
        result = False
    _JAR_CONTENT_CACHE[key] = result
    _save_jar_content_cache()
    return result


def known_vanilla_jars():
    """The last ``discover_vanilla_jars()`` result while it is fresh, else ``None``.

    Never walks the launcher folders, so Blender's UI can hand it to a helper
    process (the mod thumbnail workers) without waiting."""
    cached = _DISCOVERY_CACHE.get(True)
    if cached is not None and time.monotonic() - cached[0] < _DISCOVERY_TTL_SECONDS:
        return list(cached[1])
    return None


def remember_vanilla_jars(jars):
    """Use ``jars`` (from ``known_vanilla_jars`` in Blender's UI) as this process's discovery."""
    _DISCOVERY_CACHE[True] = (time.monotonic(), [tuple(item) for item in jars])


def discover_vanilla_jars(check_contents=True):
    """Return ``[(version, jar_path), ...]``, newest usable version first."""
    cached = _DISCOVERY_CACHE.get(bool(check_contents))
    if cached is not None and time.monotonic() - cached[0] < _DISCOVERY_TTL_SECONDS:
        return list(cached[1])
    seen = set()
    found = []
    search_dirs = _default_versions_dirs() + _instance_versions_dirs()
    for versions_dir in search_dirs:
        if not os.path.isdir(versions_dir):
            continue
        try:
            names = sorted(os.listdir(versions_dir))
        except OSError:
            continue
        for version in names:
            jar_path = os.path.join(versions_dir, version, version + ".jar")
            if not os.path.isfile(jar_path):
                continue
            key = os.path.normcase(os.path.abspath(jar_path))
            if key in seen:
                continue
            seen.add(key)
            if check_contents and not jar_has_block_models(jar_path):
                continue
            found.append((version, jar_path))
    found.sort(key=lambda item: _version_sort_key(item[0]), reverse=True)
    _DISCOVERY_CACHE[bool(check_contents)] = (time.monotonic(), list(found))
    return found


def _safe_member_target(output_dir, name):
    """Reject path-traversal members before writing anything."""
    target = os.path.abspath(os.path.join(output_dir, name))
    if os.path.commonpath([output_dir, target]) != output_dir:
        raise ValueError("Unsafe archive member: %s" % name)
    return target


def extract_vanilla_assets(jar_path, cache_dir, force=False):
    """Unpack only ``assets/`` from a client jar; returns the extracted root.

    Cached by path+size+mtime, so a second build reuses the folder. A jar is
    ~15 MB of class files that we have no use for, hence the prefix filter.
    """
    if not jar_path or not os.path.isfile(jar_path):
        return ""
    version = os.path.splitext(os.path.basename(jar_path))[0]
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", version) or "vanilla"
    output_dir = os.path.abspath(os.path.join(
        cache_dir, "vanilla_%s_%s" % (safe_version, archive_hash(jar_path)[:12])
    ))
    marker = os.path.join(output_dir, EXTRACT_COMPLETE_MARKER)
    if os.path.isfile(marker) and not force:
        return output_dir

    os.makedirs(output_dir, exist_ok=True)
    with zipfile.ZipFile(jar_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith(ASSET_PREFIX):
                continue
            target = _safe_member_target(output_dir, name)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as handle:
                handle.write(source.read())
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(version)
    return output_dir


def resolve_vanilla_root(explicit_path, cache_dir):
    """Return ``(root, note)`` for the vanilla asset source.

    ``explicit_path`` may be a folder containing ``assets/`` or a ``.jar``.
    With nothing given, the newest auto-detected client jar is used. ``root``
    is empty when no vanilla assets could be found at all.
    """
    explicit_path = (explicit_path or "").strip()
    if explicit_path:
        if os.path.isdir(explicit_path):
            if os.path.isdir(os.path.join(explicit_path, "assets")):
                return explicit_path, "vanilla folder: %s" % explicit_path
            return "", "folder has no assets/ subdirectory: %s" % explicit_path
        if os.path.isfile(explicit_path):
            if not jar_has_block_models(explicit_path):
                return "", "jar has no %s: %s" % (BLOCKSTATE_MARKER, explicit_path)
            root = extract_vanilla_assets(explicit_path, cache_dir)
            return root, "vanilla jar: %s" % explicit_path
        return "", "vanilla path does not exist: %s" % explicit_path

    jars = discover_vanilla_jars()
    if not jars:
        return "", "no vanilla client jar found automatically"
    version, jar_path = jars[0]
    root = extract_vanilla_assets(jar_path, cache_dir)
    return root, "auto-detected vanilla %s (%s)" % (version, jar_path)


def build_pack_roots(pack_root, vanilla_root):
    """Ordered search chain: every active pack, then Vanilla underneath.

    ``pack_root`` retains its historical name but accepts either one path or an
    ordered iterable, preserving compatibility with single-pack callers.
    """
    if isinstance(pack_root, (str, bytes, os.PathLike)):
        active_roots = [pack_root]
    else:
        active_roots = list(pack_root or ())
    roots = []
    seen = set()
    for root in active_roots + [vanilla_root]:
        root = (root or "").strip()
        if not root or not os.path.isdir(root):
            continue
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def addon_cache_dir():
    """The add-on's shared extraction cache."""
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return get_cache_dir(addon_dir)

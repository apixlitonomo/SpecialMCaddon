"""Resource-pack indexing shared by the UI and isolated index process."""
import hashlib
import json
import os
import threading
from .archive_extractor import user_cache_dir
from .asset_indexer import scan_asset_folder, synthesize_java_block_assets
from .utils import shared_walks

# Bump when the indexer's output changes so saved pack indexes are rebuilt.
BROWSER_INDEX_VERSION = 1


def pack_fingerprint(root):
    """Change signature of a pack folder: every file's path, size and mtime.

    ``os.scandir`` reports size and mtime with the directory listing on
    Windows, so even a 20k-file pack fingerprints in about 0.1 s. Any added,
    removed, renamed or edited file changes the result.
    """
    digest = hashlib.sha1()
    stack = [os.path.abspath(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            digest.update(
                f"{entry.path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode(
                    "utf-8", "surrogatepass"
                )
            )
    jar = mod_jar_of(root)
    if jar:
        # A mod pack also lists what the JAR's code builds: a new JAR build
        # or a changed mod reader lists it again.
        from .mod_jar import CATALOG_VERSION, _scanner_stamp

        try:
            stat = os.stat(jar)
        except OSError:
            return digest.hexdigest()
        digest.update(f"mod|{jar}|{stat.st_size}|{stat.st_mtime_ns}|{CATALOG_VERSION}|{_scanner_stamp()}\n"
                      .encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def _index_cache_path(cache_key):
    key = json.dumps([BROWSER_INDEX_VERSION, *cache_key], sort_keys=True)
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(user_cache_dir("index"), digest + ".json")


def load_cached_index(cache_key, fingerprint):
    """Saved index result for an unchanged pack, or None."""
    try:
        with open(_index_cache_path(cache_key), encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != BROWSER_INDEX_VERSION
        or payload.get("fingerprint") != fingerprint
        or not isinstance(payload.get("assets"), list)
    ):
        return None
    return payload


def store_cached_index(cache_key, fingerprint, result):
    """Save one index result so the next session loads the pack instantly."""
    path = _index_cache_path(cache_key)
    payload = dict(result, version=BROWSER_INDEX_VERSION, fingerprint=fingerprint)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(temporary, path)
    except OSError as exc:
        print("[Sparkle] Could not save pack index: %s" % exc)


MOD_ASSET_TYPES = {"entity": "Mod Entity", "model": "Mod Model", "block": "Mod Block", "item": "Mod Item"}
_MOD_KIND_NAMES = {"entity": "Mob", "model": "Model", "block": "Block", "item": "Item"}
# A mod's own JSON models: its catalog lists them with the rules that leave
# out templates and code-drawn items, and imports them through the mod.
_MOD_REPLACED_TYPES = {"Java Model", "Java Entity Model"}


def mod_jar_of(pack_root):
    """The mod JAR an extracted mod pack came from, named by its marker, or ''."""
    from .archive_inputs import is_mod_jar

    try:
        with open(os.path.join(pack_root, ".java_mod_resource.json"), encoding="utf-8") as stream:
            jar = json.load(stream).get("jar", "")
    except (OSError, ValueError, AttributeError):
        return ""
    # A client JAR or other archive with assets lists its files like any pack.
    return jar if isinstance(jar, str) and is_mod_jar(jar) else ""


def mod_browser_assets(jar):
    """Cards for everything a mod offers: mobs, code-built models, blocks and
    items, as the import panel lists them, imported by the mod importer."""
    from .asset_indexer import BrowserAsset
    from .mod_jar import mod_catalog

    catalog = mod_catalog(jar)
    if catalog is None:
        return []
    # A name two kinds share gets its kind, as in the import panel: "Critter Nest (Block)".
    counts = {}
    for asset in catalog.assets:
        counts[asset.name] = counts.get(asset.name, 0) + 1
    return [BrowserAsset(name=(f"{asset.name} ({_MOD_KIND_NAMES.get(asset.kind, asset.kind)})"
                               if counts[asset.name] > 1 else asset.name),
                         asset_type=MOD_ASSET_TYPES.get(asset.kind, "Mod Model"),
                         source_path=jar, pack_name=catalog.name, identifier=asset.key,
                         detail=f"{catalog.name}: {asset.identifier}",
                         metadata={"mod_key": asset.key, "mod_kind": asset.kind})
            for asset in catalog.assets]


def scan_browser_assets(root, vanilla_path="", choices=None):
    from .brarchive_pack import has_archives, scan_pack
    if has_archives(root):
        return scan_pack(root, choices)

    # Texture-only Java packs inherit blockstates/models from vanilla. Turn
    # their actual texture/model intersections into importable block cards.
    from .bedrock_assets import find_resource_pack_root
    from .vanilla_assets import addon_cache_dir, resolve_vanilla_root
    pack_root = find_resource_pack_root("", root)
    mod_jar = mod_jar_of(pack_root) if pack_root else ""
    if mod_jar:
        with shared_walks():
            assets = [asset for asset in scan_asset_folder(root) if asset.asset_type not in _MOD_REPLACED_TYPES]
        try:
            return mod_browser_assets(mod_jar) + assets
        except Exception as exc:  # an unreadable mod still lists its textures
            print("[Sparkle][Mods] Could not list %s: %s" % (mod_jar, exc))
            return assets
    java_pack = bool(pack_root) and os.path.isdir(os.path.join(pack_root, "assets"))
    # Finding the vanilla jar is mostly waiting on launcher folders, so it
    # runs alongside the pack scan instead of after it.
    vanilla = {}

    def find_vanilla():
        try:
            vanilla["root"] = resolve_vanilla_root(vanilla_path, addon_cache_dir())
        except Exception as exc:
            vanilla["error"] = exc

    finder = threading.Thread(target=find_vanilla, daemon=True) if java_pack else None
    if finder is not None:
        finder.start()
    with shared_walks():
        assets = scan_asset_folder(root)
        if finder is not None:
            finder.join()
            try:
                if "error" in vanilla:
                    raise vanilla["error"]
                vanilla_root, _vanilla_note = vanilla["root"]
                pack_name = next(
                    (
                        asset.pack_name for asset in assets
                        if asset.pack_name
                    ),
                    os.path.basename(pack_root.rstrip("\\/")),
                )
                assets.extend(synthesize_java_block_assets(
                    pack_root,
                    vanilla_root=vanilla_root,
                    pack_name=pack_name,
                    max_assets=max(0, 25000 - len(assets)),
                    save_model_graph=True,
                ))
            except Exception as exc:
                print("[Sparkle][Blocks] Card synthesis skipped: %s" % exc)
    return assets


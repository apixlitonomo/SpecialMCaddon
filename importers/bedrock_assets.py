import json
import os
import re
from contextlib import contextmanager
from .bedrock_materials import loads_material_jsonc

from .pack_stack import (
    PackLayer,
    ResolutionResult,
    ResolvedResource,
    ResourcePackStack,
    canonical_logical_id,
)


def texture_dedupe_key(texture_path):
    """Return a stable key for texture-scoped geometry deduplication."""
    if not texture_path:
        return ""
    return os.path.normcase(os.path.abspath(texture_path))


def load_json_file(filepath):
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()
    return loads_material_jsonc(content)


# Parsed files shared by read-only callers while a scope is open: path ->
# ((mtime, size), data). Packs that merge every animation into one file (7.7 MB
# in the Superman marketplace pack) were parsed once per animation reference,
# about 80 times per entity import. None outside a scope.
_SHARED_JSON = None
_SHARED_JSON_DEPTH = 0


class shared_json_reads:
    """Within this scope, ``load_json_shared`` parses each file once.

    The parsed data is dropped when the outermost scope closes: that 7.7 MB
    file holds about 50 MB once parsed.
    """

    def __enter__(self):
        global _SHARED_JSON, _SHARED_JSON_DEPTH
        if _SHARED_JSON is None:
            _SHARED_JSON = {}
        _SHARED_JSON_DEPTH += 1
        return self

    def __exit__(self, *_exc):
        global _SHARED_JSON, _SHARED_JSON_DEPTH
        _SHARED_JSON_DEPTH -= 1
        if _SHARED_JSON_DEPTH <= 0:
            _SHARED_JSON_DEPTH = 0
            _SHARED_JSON = None
        return False


def load_json_shared(filepath):
    """``load_json_file`` for callers that only read the result.

    Inside a ``shared_json_reads`` scope every caller gets the same object for
    a file until it changes on disk, so it must not be modified; outside one
    this is ``load_json_file``.
    """
    if _SHARED_JSON is None:
        return load_json_file(filepath)
    stat = os.stat(filepath)
    key = os.path.normcase(os.path.abspath(filepath))
    stamp = (stat.st_mtime_ns, stat.st_size)
    entry = _SHARED_JSON.get(key)
    if entry is not None and entry[0] == stamp:
        return entry[1]
    data = load_json_file(filepath)
    _SHARED_JSON[key] = (stamp, data)
    return data


_UNLOADED_JSON = object()


def detect_json_asset(filepath, data=_UNLOADED_JSON):
    try:
        if data is _UNLOADED_JSON:
            data = load_json_file(filepath)
    except Exception:
        return "unknown"

    if not isinstance(data, dict):
        return "unknown"

    if "particle_effect" in data or "minecraft:particle_effect" in data:
        return "particle"
    if "description" in data and "components" in data:
        return "particle"
    if "minecraft:client_entity" in data or "minecraft:attachable" in data:
        return "client_entity"
    if "minecraft:geometry" in data or any(k.startswith("geometry.") for k in data):
        return "model"
    if "animations" in data:
        return "animation"
    if "animation_controllers" in data:
        return "animation_controller"
    if "minecraft:block" in data:
        return "block"
    if "minecraft:item" in data:
        return "item"
    if "minecraft:entity" in data:
        return "unknown"
    normalized_path = filepath.replace("\\", "/").lower()
    if (
        "/assets/" in normalized_path
        and (
            "/polytone/custom_particles/" in normalized_path
            or "/polytone/particle_modifiers/" in normalized_path
        )
    ):
        return "java_particle"
    if "/assets/" in normalized_path and "/models/" in normalized_path:
        if any(key in data for key in ("parent", "textures", "elements", "overrides", "display")):
            return "java_model"
    if "/assets/" in normalized_path and "/particles/" in normalized_path:
        return "java_particle"

    for value in data.values():
        if isinstance(value, dict) and "description" in value and "components" in value:
            return "particle"

    return "unknown"


def geometry_bone_names(filepath, geometry_identifier="", geometry_index=0):
    """Return authored bone names for one Bedrock geometry entry."""
    try:
        from .parse_bedrock_geometry import BedrockModelParser

        geometry = BedrockModelParser(filepath=filepath).get_geometry(
            identifier=geometry_identifier or None,
            index=int(geometry_index or 0),
        )
    except Exception:
        return set()
    return {
        str(bone.get("name", "")).casefold()
        for bone in geometry.get("bones", [])
        if isinstance(bone, dict) and bone.get("name")
    }


def _loose_resource_root(filepath, preferred_root=""):
    """Find a useful loose-pack root even when no manifest is present."""
    root = find_resource_pack_root(filepath, preferred_root)
    if root:
        return root
    normalized = os.path.abspath(filepath).replace("\\", "/")
    lowered = normalized.casefold()
    for marker in ("/models/", "/entity/", "/animations/"):
        if marker in lowered:
            return normalized[:lowered.index(marker)]
    folder = os.path.dirname(os.path.abspath(filepath))
    while folder:
        if os.path.isdir(os.path.join(folder, "animations")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return os.path.dirname(os.path.abspath(filepath))


_GEOMETRY_ANIMATION_CLIP_CACHE = {}


def geometry_animation_clips(
    filepath,
    preferred_root="",
    geometry_identifier="",
    geometry_index=0,
):
    """Discover animation clips that can drive a loose ``.geo.json`` model."""
    absolute_file = os.path.abspath(filepath)
    try:
        source_mtime = os.path.getmtime(absolute_file)
    except OSError:
        source_mtime = 0.0
    cache_key = (
        os.path.normcase(absolute_file),
        source_mtime,
        os.path.normcase(os.path.abspath(preferred_root)) if preferred_root else "",
        str(geometry_identifier or "").casefold(),
        int(geometry_index or 0),
    )
    if cache_key in _GEOMETRY_ANIMATION_CLIP_CACHE:
        return [dict(clip) for clip in _GEOMETRY_ANIMATION_CLIP_CACHE[cache_key]]
    model_bones = geometry_bone_names(
        filepath,
        geometry_identifier=geometry_identifier,
        geometry_index=geometry_index,
    )
    if not model_bones:
        _GEOMETRY_ANIMATION_CLIP_CACHE[cache_key] = []
        return []
    root = _loose_resource_root(filepath, preferred_root)
    animation_root = os.path.join(root, "animations")
    # Never fall back to recursively scanning the full pack. Besides stalling
    # Blender's file browser, non-animation JSON cannot provide clip choices.
    if not os.path.isdir(animation_root):
        _GEOMETRY_ANIMATION_CLIP_CACHE[cache_key] = []
        return []
    clips = []
    seen = set()
    for candidate in iter_json_files(animation_root):
        try:
            data = load_json_file(candidate)
        except Exception:
            continue
        animations = data.get("animations", {}) if isinstance(data, dict) else {}
        if not isinstance(animations, dict):
            continue
        for identifier, animation in animations.items():
            if not isinstance(identifier, str) or not isinstance(animation, dict):
                continue
            key = identifier.casefold()
            if key in seen:
                continue
            bones = animation.get("bones", {})
            clip_bones = {
                str(name).casefold()
                for name in bones
            } if isinstance(bones, dict) else set()
            overlap = model_bones.intersection(clip_bones)
            if clip_bones and not overlap:
                continue
            seen.add(key)
            clips.append({
                "identifier": identifier,
                "filepath": candidate,
                "matching_bones": len(overlap),
                "bone_count": len(clip_bones),
            })
    clips.sort(key=lambda clip: (
        -int(clip["matching_bones"]),
        str(clip["identifier"]).casefold(),
        os.path.normcase(str(clip["filepath"])),
    ))
    _GEOMETRY_ANIMATION_CLIP_CACHE[cache_key] = [dict(clip) for clip in clips]
    return clips


def normalize_identifier(identifier):
    if not identifier:
        return ""
    text = str(identifier)
    if ":" not in text:
        return text
    left, right = text.split(":", 1)
    # Legacy geometry keys encode inheritance as child:parent.  That colon is
    # not a namespace separator, and the child remains the selectable ID.
    if left.lower().startswith(("geometry.", "geo.", "animation.", "controller.")):
        return left
    return right

def geometry_match_key(identifier):
    ident = normalize_identifier(identifier or "").replace("\\", "/").lower()
    for prefix in ("geometry.", "geometry/", "geo.", "geo/"):
        if ident.startswith(prefix):
            return ident[len(prefix):]
    return ident

def geometry_keys_match(candidate, target):
    if not candidate or not target:
        return False
    # Geometry identifiers are names, not suffix-search terms. Treating
    # ``geometry.vendor.boat`` as ``geometry.boat`` can silently import an
    # unrelated model from another installed pack.
    return candidate == target


def builtin_geometry_root():
    """Return the add-on's exact-ID fallbacks for engine-owned geometry."""
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "builtin_geometry")


def builtin_geometry_variant(identifier, texture_path):
    """Select the engine-owned bamboo renderer for a bamboo boat texture.

    Vanilla exposes one ``geometry.boat``/``geometry.chest_boat`` reference,
    while the renderer swaps to a raft model for the bamboo texture variant.
    Pack-authored exact geometry is still resolved before this built-in alias.
    """
    key = geometry_match_key(identifier)
    texture_name = os.path.splitext(os.path.basename(str(texture_path or "")))[0].lower()
    if key == "boat" and texture_name == "bamboo_raft":
        return "geometry.bamboo_raft"
    if key == "chest_boat" and texture_name == "chest_boat_bamboo":
        return "geometry.chest_bamboo_raft"
    return identifier


_BOAT_VARIANT_TEXTURE_KEYS = (
    "oak", "spruce", "birch", "jungle", "acacia",
    "darkoak", "mangrove", "bamboo", "cherry", "paleoak",
)


def builtin_boat_texture_variant(entity_identifier, textures, query_context):
    """Return the canonical boat texture reference for ``query.variant``."""
    entity_key = normalize_identifier(entity_identifier).lower()
    if entity_key not in {"boat", "chest_boat"}:
        return ""
    if not isinstance(textures, dict) or not isinstance(query_context, dict):
        return ""
    try:
        variant = int(query_context["variant"])
    except (KeyError, TypeError, ValueError):
        return ""
    if variant < 0 or variant >= len(_BOAT_VARIANT_TEXTURE_KEYS):
        return ""
    texture = textures.get(_BOAT_VARIANT_TEXTURE_KEYS[variant])
    return texture if isinstance(texture, str) else ""


def _engine_version_tuple(value):
    if isinstance(value, (list, tuple)):
        result = []
        for item in value[:4]:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                result.append(0)
        return tuple(result)
    return (0,)


def find_contextual_entity_file(root_dir, geometry_identifier, preferred_name=""):
    """Find the best client entity that owns an exact geometry reference."""
    target = geometry_match_key(geometry_identifier)
    if not target or not root_dir or not os.path.isdir(root_dir):
        return ""
    preferred = re.sub(r"[^a-z0-9]+", " ", str(preferred_name).lower()).strip()
    target_words = set(re.sub(r"[^a-z0-9]+", " ", target).split())
    matches = []
    for filepath in iter_json_files(root_dir):
        normalized = filepath.replace("\\", "/").lower()
        if "/entity/" not in normalized and not normalized.endswith(".entity.json"):
            continue
        try:
            data = load_json_file(filepath)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        entity = data.get("minecraft:client_entity")
        if not isinstance(entity, dict):
            continue
        description = entity.get("description", {})
        if not isinstance(description, dict):
            continue
        geometries = description.get("geometry", {})
        if not isinstance(geometries, dict) or not any(
            isinstance(ref, str)
            and geometry_keys_match(geometry_match_key(ref), target)
            for ref in geometries.values()
        ):
            continue
        entity_key = normalize_identifier(description.get("identifier", "")).lower()
        entity_words = set(entity_key.replace("_", " ").split())
        if preferred and preferred == entity_key.replace("_", " "):
            semantic_rank = 0
        elif preferred and entity_words and entity_words.issubset(set(preferred.split())):
            semantic_rank = 1
        elif entity_words and entity_words.issubset(target_words):
            semantic_rank = 2
        else:
            semantic_rank = 3
        version = _engine_version_tuple(description.get("min_engine_version", (0,)))
        matches.append((semantic_rank, tuple(-part for part in version), filepath))
    matches.sort()
    return matches[0][2] if matches else ""


def safe_name(name):
    name = normalize_identifier(name)
    name = name.replace("/", "_").replace("\\", "_").replace(":", "_")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "minecraft_asset"


def iter_json_files(root_dir):
    if not root_dir or not os.path.isdir(root_dir):
        return
    for root, _, files in os.walk(root_dir):
        for filename in files:
            if filename.lower().endswith(".json"):
                yield os.path.join(root, filename)


_RESOURCE_INDEX_CACHE = {}
_PACK_STACK_CACHE = {}
_RESOURCE_RESOLUTION_ISSUES = {}


def _stack_roots_for_lookup(root):
    """Expand a pack container while retaining its explicit child order."""
    if not root or not os.path.isdir(root):
        return []
    root = os.path.abspath(root)
    if is_resource_pack_root(root):
        return [root]
    children = _resource_pack_children(root)
    if children:
        return children
    basename = os.path.basename(os.path.normpath(root)).casefold()
    direct_names = {
        "models", "animations", "animation_controllers", "textures",
        "builtin_geometry", "materials",
    }
    if basename in direct_names or any(
        os.path.isdir(os.path.join(root, name)) for name in direct_names
    ):
        return [root]
    return []


def build_resource_pack_stack(roots):
    """Build or retrieve an ordered exact-ID stack; earlier roots are higher."""
    expanded = []
    seen = set()
    for root in roots or ():
        for candidate in _stack_roots_for_lookup(root):
            key = os.path.normcase(os.path.abspath(candidate))
            if key in seen:
                continue
            seen.add(key)
            expanded.append(os.path.abspath(candidate))
    key = tuple(os.path.normcase(path) for path in expanded)
    stack = _PACK_STACK_CACHE.get(key)
    if stack is None:
        stack = ResourcePackStack.from_roots(expanded)
        _PACK_STACK_CACHE[key] = stack
    return stack


def _remember_resolution(kind, roots, logical_id, result):
    key = (
        kind,
        tuple(os.path.normcase(os.path.abspath(root)) for root in roots if root),
        canonical_logical_id(logical_id),
    )
    _RESOURCE_RESOLUTION_ISSUES[key] = tuple(result.issues)


def resource_resolution_issues():
    """Return structured issues observed by legacy string-returning lookups."""
    return {
        key: issues for key, issues in _RESOURCE_RESOLUTION_ISSUES.items() if issues
    }


def resolve_geometry_resource(root_dir, geometry_identifier, source_pointer=""):
    roots = _stack_roots_for_lookup(root_dir)
    stack = build_resource_pack_stack(roots)
    result = stack.resolve_geometry_compatible(geometry_identifier, source_pointer)
    _remember_resolution("geometry", roots, geometry_identifier, result)
    return result



def _resource_index(root_dir):
    """Index geometry/animation/controller ids once per pack root."""
    key = os.path.normcase(os.path.abspath(root_dir or ""))
    cached = _RESOURCE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index = {"geometry": {}, "animation": {}, "controller": {}}
    roots = (
        ("models", "geometry"),
        ("animations", "animation"),
        ("animation_controllers", "controller"),
    )
    for folder, kind in roots:
        scan_root = os.path.join(root_dir, folder)
        if not os.path.isdir(scan_root):
            basename = os.path.basename(os.path.normpath(root_dir)).lower()
            # Built-in/direct resource roots are small and may point straight
            # at the relevant directory. Never index broad parents such as
            # Downloads when a resource subdirectory is absent.
            if basename in {folder.lower(), "builtin_geometry"}:
                scan_root = root_dir
            else:
                continue
        if not os.path.isdir(scan_root):
            continue
        for filepath in iter_json_files(scan_root):
            try:
                data = load_json_file(filepath)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if kind == "geometry":
                values = data.get("minecraft:geometry", [])
                if isinstance(values, list):
                    for geometry in values:
                        desc = geometry.get("description", {}) if isinstance(geometry, dict) else {}
                        identifier = desc.get("identifier", "") if isinstance(desc, dict) else ""
                        if identifier:
                            index[kind].setdefault(geometry_match_key(identifier), filepath)
                for identifier in data:
                    if str(identifier).startswith("geometry."):
                        child_identifier = str(identifier).partition(":")[0]
                        index[kind].setdefault(
                            geometry_match_key(child_identifier), filepath
                        )
            elif kind == "animation":
                values = data.get("animations", {})
                if isinstance(values, dict):
                    for identifier in values:
                        index[kind].setdefault(
                            normalize_identifier(identifier), filepath
                        )
            else:
                values = data.get("animation_controllers", {})
                if isinstance(values, dict):
                    for identifier in values:
                        index[kind].setdefault(
                            normalize_identifier(identifier), filepath
                        )
    _RESOURCE_INDEX_CACHE[key] = index
    return index


def clear_resource_index_cache():
    _RESOURCE_INDEX_CACHE.clear()
    _PACK_STACK_CACHE.clear()
    _RESOURCE_RESOLUTION_ISSUES.clear()


def find_geometry_file(root_dir, geometry_identifier):
    result = resolve_geometry_resource(root_dir, geometry_identifier)
    return result.resource.path if result.ok else ""


def find_animation_file(root_dir, animation_identifier):
    target = normalize_identifier(animation_identifier)
    return _resource_index(root_dir)["animation"].get(target, "")


def find_animation_controller_file(root_dir, controller_identifier):
    target = normalize_identifier(controller_identifier)
    return _resource_index(root_dir)["controller"].get(target, "")


def load_animation_controller(filepath, controller_identifier):
    target = normalize_identifier(controller_identifier)
    try:
        data = load_json_file(filepath)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    controllers = data.get("animation_controllers", {})
    if not isinstance(controllers, dict):
        return {}
    for key, controller in controllers.items():
        if normalize_identifier(key) == target and isinstance(controller, dict):
            return controller
    return {}


def controller_animation_aliases(controller):
    """Collect animation aliases referenced by an animation-controller's states."""
    aliases = []
    states = controller.get("states", {}) if isinstance(controller, dict) else {}
    if not isinstance(states, dict):
        return aliases
    for state in states.values():
        if not isinstance(state, dict):
            continue
        animations = state.get("animations", [])
        if isinstance(animations, (str, dict)):
            animations = [animations]
        if not isinstance(animations, list):
            continue
        for entry in animations:
            if isinstance(entry, str):
                if entry not in aliases:
                    aliases.append(entry)
            elif isinstance(entry, dict):
                for alias in entry.keys():
                    if isinstance(alias, str) and alias not in aliases:
                        aliases.append(alias)
    return aliases




def _dedupe_paths(paths):
    result = []
    seen = set()
    for path in paths:
        if not path:
            continue
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen or not os.path.isdir(path):
            continue
        seen.add(norm)
        result.append(path)
    return result


def is_resource_pack_root(path):
    if not path or not os.path.isdir(path):
        return False
    if os.path.isdir(os.path.join(path, "textures")):
        return True
    if os.path.isdir(os.path.join(path, "assets")) or os.path.isfile(os.path.join(path, "pack.mcmeta")):
        return True
    manifest = os.path.join(path, "manifest.json")
    if not os.path.isfile(manifest):
        return False
    try:
        data = load_json_file(manifest)
    except Exception:
        return False
    for module in data.get("modules", []):
        if (
            isinstance(module, dict)
            and str(module.get("type", "")).casefold() in {"resources", "client_data"}
        ):
            return True
    return False


def _resource_pack_children(folder):
    rp_dir = os.path.join(folder, "resource_packs")
    if not os.path.isdir(rp_dir):
        return []
    return [
        os.path.join(rp_dir, name)
        for name in sorted(os.listdir(rp_dir), key=str.casefold)
        if is_resource_pack_root(os.path.join(rp_dir, name))
    ]


def find_resource_pack_roots(asset_filepath="", preferred_root=""):
    roots = []

    def add_from_folder(folder):
        if not folder or not os.path.isdir(folder):
            return
        if is_resource_pack_root(folder):
            roots.append(folder)
        roots.extend(_resource_pack_children(folder))

    if preferred_root:
        add_from_folder(preferred_root)

    current = os.path.dirname(os.path.abspath(asset_filepath)) if asset_filepath else ""
    for _ in range(10):
        if not current:
            break
        add_from_folder(current)
        parent = os.path.dirname(current)
        if os.path.basename(parent).lower() == "resource_packs" and is_resource_pack_root(current):
            roots.insert(0, current)
        if os.path.basename(parent).lower() == "behavior_packs":
            world_root = os.path.dirname(parent)
            roots.extend(_resource_pack_children(world_root))
        if parent == current:
            break
        current = parent

    return _dedupe_paths(roots)


def find_resource_pack_root(asset_filepath="", preferred_root=""):
    roots = find_resource_pack_roots(asset_filepath, preferred_root)
    return roots[0] if roots else ""

def _extract_texture_path(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("path"), str):
            return value["path"]
        variations = value.get("variations")
        if isinstance(variations, list) and variations:
            return _extract_texture_path(variations[0])
    if isinstance(value, list) and value:
        return _extract_texture_path(value[0])
    return ""


def _iter_texture_definition_files(root):
    direct = (
        os.path.join(root, "textures", "terrain_texture.json"),
        os.path.join(root, "textures", "item_texture.json"),
    )
    for filepath in direct:
        if os.path.isfile(filepath):
            yield filepath
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"__MACOSX", ".git", "node_modules", "__pycache__"}]
        for filename in files:
            if filename in {"terrain_texture.json", "item_texture.json"}:
                yield os.path.join(current, filename)


def _resolve_texture_definition(search_roots, texture_name):
    if not texture_name or "/" in texture_name or "\\" in texture_name:
        return texture_name
    seen_files = set()
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for texture_file in _iter_texture_definition_files(root):
            if texture_file in seen_files:
                continue
            seen_files.add(texture_file)
            try:
                data = load_json_file(texture_file)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            texture_data = data.get("texture_data", {})
            entry = texture_data.get(texture_name)
            if not isinstance(entry, dict):
                continue
            resolved = _extract_texture_path(entry.get("textures"))
            if resolved:
                return resolved
    return texture_name

def asset_resource_roots(context, json_filepath=""):
    """Shared import order: active layers, worker layers, then source fallbacks."""
    def absolute(path):
        try:
            import bpy
            return bpy.path.abspath(path)
        except ImportError:
            return os.path.abspath(os.path.expanduser(path))

    search_roots = []
    scene = getattr(context, "scene", None)
    browser_root = scene.get('_mcbedrock_asset_browser_root') if hasattr(scene, 'get') else None
    if browser_root is not None:
        from .brarchive_pack import resolve_roots, selections
        root = absolute(browser_root) if browser_root else ''
        if not root and json_filepath:
            current = os.path.dirname(absolute(json_filepath))
            root = current
            while current:
                if is_resource_pack_root(current):
                    root = current
                    break
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
        roots = (_stack_roots_for_lookup(root) or [root]) if root else []
        return _dedupe_paths(resolve_roots(roots, selections(scene)))
    for pack in getattr(scene, "mcbedrock_active_packs", ()):
        path = getattr(pack, "path", "")
        if path:
            path = absolute(path)
            search_roots.extend(_stack_roots_for_lookup(path) or [path])
    if hasattr(scene, 'get'):
        try:
            worker_roots = json.loads(scene.get('_mcbedrock_resource_roots', '[]'))
            if isinstance(worker_roots, list):
                for root in worker_roots:
                    if isinstance(root, str) and root:
                        root = absolute(root)
                        search_roots.extend(_stack_roots_for_lookup(root) or [root])
        except (ValueError, TypeError):
            pass
    # Once a stack exists, a stale legacy path must not re-enable a removed pack.
    scene_path = (getattr(scene, "mcbedrock_resource_pack_path", "")
                  if scene and not getattr(scene, "mcbedrock_active_packs", ()) else "")
    if scene_path:
        try:
            scene_path = getattr(__import__("bpy"), "path").abspath(scene_path)
        except (ImportError, AttributeError):
            scene_path = os.path.abspath(os.path.expanduser(scene_path))
    search_roots.extend(find_resource_pack_roots(json_filepath, scene_path))
    from .brarchive_pack import resolve_roots, selections
    return _dedupe_paths(resolve_roots(search_roots, selections(scene)))


@contextmanager
def browser_pack_scope(context):
    """Browser imports use their selected pack, independently of schematic layers."""
    scene = context.scene
    key = '_mcbedrock_asset_browser_root'
    previous = scene.get(key)
    scene[key] = getattr(scene, 'mcbedrock_browser_root', '')
    try:
        yield
    finally:
        if previous is None:
            del scene[key]
        else:
            scene[key] = previous


def _loose_texture_stack(folder, logical_id):
    """The stack for a file outside any pack: the files beside it and the
    folders a texture reference can name (``textures/``, ``assets/``, the
    reference's own first folder), never everything below the folder. A
    particle saved in Downloads used to index, and parse, all of Downloads."""
    logical = canonical_logical_id(logical_id)
    folders = {"textures", "assets"}
    first = logical.split("/", 1)[0]
    if "/" in logical and ":" not in first:
        folders.add(first.casefold())
    return ResourcePackStack([PackLayer(folder, 1, os.path.basename(folder), frozenset(folders))])


def _texture_search_roots(context, json_filepath):
    search_roots = asset_resource_roots(context, json_filepath)
    addon_pack = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vanilla_pack")
    if os.path.isdir(addon_pack):
        search_roots.append(addon_pack)
    return _dedupe_paths(search_roots)


def resolve_texture_resource(
    context,
    raw_texture,
    json_filepath,
    source_pointer="",
):
    """Resolve an exact texture ID and retain its pack/source provenance."""
    logical_id = canonical_logical_id(raw_texture)
    if not logical_id:
        return ResolutionResult()
    if os.path.isabs(logical_id) and os.path.isfile(logical_id):
        path = os.path.abspath(logical_id)
        pack_root = find_resource_pack_root(path) or os.path.dirname(path)
        return ResolutionResult(resource=ResolvedResource(
            "texture", logical_id, path, pack_root, 1, source_pointer,
        ))

    roots = _texture_search_roots(context, json_filepath)
    if roots:
        stack = build_resource_pack_stack(roots)
    else:
        # A loose file gets one exact local directory, never an ancestor or
        # basename scan across Downloads/Desktop.
        local_root = os.path.dirname(os.path.abspath(json_filepath))
        roots = [local_root]
        stack = _loose_texture_stack(local_root, logical_id)
    result = stack.resolve_texture(logical_id, source_pointer)
    if result.resource is None and all(issue.code == 'not_found' for issue in result.issues):
        from .vanilla_bedrock_textures import find_texture
        fallback = find_texture(logical_id, getattr(getattr(context, 'scene', None),
                                                    'mcbedrock_vanilla_assets_path', ''))
        if fallback:
            path, pack_root = fallback
            result = ResolutionResult(resource=ResolvedResource(
                'texture', logical_id, path, pack_root, len(roots), source_pointer))
    _remember_resolution("texture", roots, logical_id, result)
    return result


def resolve_texture_path(
    context,
    raw_texture,
    json_filepath,
    source_pointer="",
):
    result = resolve_texture_resource(
        context, raw_texture, json_filepath, source_pointer
    )
    if result.ok:
        return result.resource.path
    return canonical_logical_id(raw_texture)


def normalize_reference_map(value, default_key="default"):
    """Normalize entity reference fields into {key: string} dicts.

    Packs use strings, lists, and dicts interchangeably for ``geometry``,
    ``textures``, and ``animations``. Non-string leaf values are dropped.
    """
    if isinstance(value, str):
        return {default_key: value}
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            if isinstance(item, str):
                key = default_key if index == 0 else f"{default_key}_{index}"
                result[key] = item
        return result
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items() if isinstance(item, str)}
    return {}


class BedrockClientEntityParser:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = load_json_file(filepath)
        if not isinstance(self.data, dict):
            self.data = {}
        
        self.entity = self.data.get("minecraft:client_entity", self.data.get("minecraft:attachable", {}))
        if not isinstance(self.entity, dict):
            self.entity = {}
        self.description = self.entity.get("description", {})
        if not isinstance(self.description, dict):
            self.description = {}
        else:
            self.description = dict(self.description)
        for field in ("geometry", "textures", "animations", "materials"):
            if field in self.description:
                self.description[field] = normalize_reference_map(self.description[field])

    def get_identifier(self):
        return self.description.get("identifier", os.path.splitext(os.path.basename(self.filepath))[0])

    def get_geometry_references(self):
        return list(self.description.get("geometry", {}).values())

    def get_geometry_reference_items(self):
        return list(self.description.get("geometry", {}).items())

    def get_texture_references(self):
        return list(self.description.get("textures", {}).values())

    def get_animation_references(self):
        return list(self.description.get("animations", {}).values())

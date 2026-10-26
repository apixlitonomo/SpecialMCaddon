import hashlib
import json
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .bedrock_assets import detect_json_asset, load_json_file, normalize_identifier
from .utils import fast_relpath, strip_formatting_codes, walk
from .archive_extractor import (
    DISCOVERABLE_ARCHIVE_EXTENSIONS,
    archive_rejection_reason,
    inspect_archive,
    user_cache_dir,
)


ARCHIVE_EXTENSIONS = DISCOVERABLE_ARCHIVE_EXTENSIONS
TEXTURE_EXTENSIONS = {".png", ".tga", ".jpg", ".jpeg"}
JSON_EXTENSIONS = {".json"}


@dataclass
class BrowserAsset:
    name: str
    asset_type: str
    source_path: str
    pack_name: str = ""
    identifier: str = ""
    status: str = "Ready"
    detail: str = ""
    importable: bool = True
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class PackContext:
    root: str
    name: str
    lang: Dict[str, str] = field(default_factory=dict)


def _asset_geometry_index(asset):
    """Return an asset's exact geometry-record selector."""
    metadata = getattr(asset, "metadata", {}) or {}
    direct_value = getattr(asset, "geometry_index", -1)
    try:
        return int(metadata.get("geometry_index", direct_value))
    except (TypeError, ValueError):
        return -1


def browser_asset_identity(asset):
    """Return the stable identity of one browser record.

    A geometry file can contain thousands of independently importable models.
    The record selector is therefore part of the identity; the source file alone
    is never sufficient to identify a Model card.
    """
    source_path = getattr(asset, "source_path", "") or ""
    normalized_path = (
        os.path.normcase(os.path.abspath(os.path.normpath(source_path)))
        if source_path
        else ""
    )
    return (
        normalized_path,
        str(getattr(asset, "asset_type", "") or ""),
        str(getattr(asset, "identifier", "") or ""),
        _asset_geometry_index(asset),
    )


def browser_asset_key(asset):
    """Serialize :func:`browser_asset_identity` for Blender string properties."""
    metadata = getattr(asset, 'metadata', None)
    if metadata is None:
        try:
            metadata = json.loads(getattr(asset, 'metadata_json', '{}'))
        except ValueError:
            metadata = {}
    if metadata and metadata.get('br_identity'):
        return 'br:' + metadata['br_identity']
    return json.dumps(browser_asset_identity(asset), ensure_ascii=True, separators=(",", ":"))


def browser_refresh_identity(asset):
    """Return the gallery-refresh dedupe identity for one browser record.

    Entity and attachable files are commonly versioned siblings which describe
    the same logical asset. Scanner order intentionally puts the preferred
    (newest) definition first, so gallery refresh collapses those siblings by
    identifier. Raw Model records remain exact: their source path and geometry
    index are required to distinguish independently importable geometries.
    """
    asset_type = str(getattr(asset, "asset_type", "") or "")
    identifier = str(getattr(asset, "identifier", "") or "").strip()
    if asset_type.casefold() in {"entity", "attachable"} and identifier:
        return ("logical", asset_type.casefold(), identifier.casefold())
    return ("exact",) + browser_asset_identity(asset)


def resolve_browser_asset_index(assets, preferred_index=-1, asset_key=""):
    """Resolve a browser record without retargeting to a sibling in its file."""
    try:
        preferred_index = int(preferred_index)
    except (TypeError, ValueError):
        preferred_index = -1

    if 0 <= preferred_index < len(assets):
        if not asset_key or browser_asset_key(assets[preferred_index]) == asset_key:
            return preferred_index

    if asset_key:
        for index, asset in enumerate(assets):
            if browser_asset_key(asset) == asset_key:
                return index
    return -1


def default_downloads_path():
    home = os.path.expanduser("~")
    candidate = os.path.join(home, "Downloads")
    return candidate if os.path.isdir(candidate) else ""


def _clean_minecraft_text(value):
    if not isinstance(value, str):
        return value
    value = strip_formatting_codes(value)
    value = value.replace("\t#", "")
    value = value.strip()
    return value


def _strip_asset_prefix(raw):
    raw = normalize_identifier(raw or "")
    raw = re.sub(r"^(controller\.animation|animation_controller|animation|geometry|particle|entity|model)[\.:/_-]+", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^(textures?|materials?)[\.:/_-]+", "", raw, flags=re.IGNORECASE)
    return raw

def _prettify_identifier(value):
    raw = _strip_asset_prefix(value or "")
    raw = raw.replace("\\", "/")
    if "/" in raw:
        raw = os.path.basename(raw)
        raw = os.path.splitext(raw)[0]
    elif os.path.splitext(raw.lower())[1] in {".json", ".png", ".tga", ".jpg", ".jpeg"}:
        raw = os.path.splitext(os.path.basename(raw))[0]
    raw = re.sub(r"[:_\-.]+", " ", raw).strip()
    return raw.title() if raw else "Minecraft Asset"


def _read_lang_file(filepath):
    result = {}
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.split("\t#", 1)[0]
                result[key.strip()] = _clean_minecraft_text(value)
    except Exception:
        pass
    return result


def _load_lang(root):
    lang = {}
    for current, _, files in walk(root):
        current_lower = current.replace("\\", "/").lower()
        if "/texts" not in current_lower:
            continue
        for filename in files:
            lower = filename.lower()
            if lower == "en_us.lang":
                lang.update(_read_lang_file(os.path.join(current, filename)))
        for filename in files:
            if filename.lower().endswith(".lang") and filename.lower() != "en_us.lang":
                for key, value in _read_lang_file(os.path.join(current, filename)).items():
                    lang.setdefault(key, value)
    return lang


def _resolve_lang(value, lang):
    if isinstance(value, str) and value in lang:
        return lang[value]
    return _clean_minecraft_text(value or "")


def _read_pack_name(root, lang):
    java_mod = os.path.join(root, ".java_mod_resource.json")
    if os.path.isfile(java_mod):
        try:
            data = load_json_file(java_mod)
            name = _clean_minecraft_text(data.get("name", ""))
            if name:
                return name
        except Exception:
            pass
    manifest = os.path.join(root, "manifest.json")
    if os.path.isfile(manifest):
        try:
            data = load_json_file(manifest)
            header = data.get("header", {})
            name = _resolve_lang(header.get("name", ""), lang)
            if name:
                return name
        except Exception:
            pass
    return os.path.basename(root.rstrip("\\/"))


def _find_pack_roots(root):
    roots = []
    metadata_names = {"manifest.json", "pack.mcmeta", ".java_mod_resource.json"}
    if any(os.path.isfile(os.path.join(root, name)) for name in metadata_names):
        roots.append(root)
    for current, dirs, files in walk(root):
        if metadata_names.intersection(files) and current not in roots:
            roots.append(current)
        dirs[:] = [d for d in dirs if d not in {"__MACOSX", ".git", "node_modules", "__pycache__"}]
    return roots or [root]


def _is_encrypted_bedrock_pack(root):
    contents = os.path.join(root, "contents.json")
    if not os.path.isfile(contents):
        return False
    try:
        with open(contents, "rb") as f:
            head = f.read(32).lstrip()
        return bool(head and head[:1] not in {b"{", b"["})
    except Exception:
        return False

def _safe_relpath(path, root):
    return fast_relpath(path, root)


def _add_if_new(assets, seen, asset):
    key = browser_asset_identity(asset)
    if key in seen:
        return
    seen.add(key)
    assets.append(asset)

def _geometry_match_key(identifier):
    ident = normalize_identifier(identifier or "").replace("\\", "/").lower()
    for prefix in ("geometry.", "geometry/", "geo.", "geo/"):
        if ident.startswith(prefix):
            return ident[len(prefix):]
    return ident


class _GeometryMap(dict):
    """Geometry reference -> entity identifier, built once per pack scan.

    Suffix lookups compare against every entry, so each entry's match key is
    normalized once instead of once per lookup (quadratic on large packs).
    """

    _match_keys = None

    def match_keys(self):
        if self._match_keys is None:
            self._match_keys = [
                (_geometry_match_key(candidate), entity_identifier)
                for candidate, entity_identifier in self.items()
            ]
        return self._match_keys


class _TextureMap(dict):
    """Entity texture reference -> entity identifier, built once per pack scan.

    A texture takes the first reference (in insertion order) that it ends
    with or that ends with it. Scanning every reference for every texture is
    quadratic; suffix indexes find the same first match directly.
    """

    _order = None
    _suffix_first = None

    def first_match(self, tex_path):
        if self._order is None:
            self._order = {reference: index for index, reference in enumerate(self)}
            suffix_first = {}
            for index, reference in enumerate(self):
                for start in range(len(reference) + 1):
                    suffix_first.setdefault(reference[start:], index)
            self._suffix_first = suffix_first
            self._items = list(self.items())
        # References that end with the texture path...
        best = self._suffix_first.get(tex_path)
        # ...and references the texture path ends with.
        for start in range(len(tex_path) + 1):
            index = self._order.get(tex_path[start:])
            if index is not None and (best is None or index < best):
                best = index
        return None if best is None else self._items[best]


def _texture_map_match(tex_map, tex_path):
    if isinstance(tex_map, _TextureMap):
        return tex_map.first_match(tex_path)
    for t_ref, e_id in tex_map.items():
        if tex_path.endswith(t_ref) or t_ref.endswith(tex_path):
            return t_ref, e_id
    return None


def _geo_map_lookup(geo_map, identifier):
    if not geo_map:
        return ""
    if identifier in geo_map:
        return geo_map[identifier]
    key = _geometry_match_key(identifier)
    if key in geo_map:
        return geo_map[key]
    if isinstance(geo_map, _GeometryMap):
        candidates = geo_map.match_keys()
    else:
        candidates = [(_geometry_match_key(candidate), entity) for candidate, entity in geo_map.items()]
    for candidate_key, entity_identifier in candidates:
        if candidate_key == key or candidate_key.endswith("." + key) or key.endswith("." + candidate_key):
            return entity_identifier
    return ""


def _java_asset_id(filepath, folder_name):
    rel = filepath.replace("\\", "/")
    lower = rel.lower()
    marker = "/assets/"
    if marker in lower:
        tail = rel[lower.rfind(marker) + len(marker):]
        parts = tail.split("/")
        if len(parts) >= 3 and parts[1].lower() == folder_name:
            asset_path = os.path.splitext("/".join(parts[2:]))[0]
            return f"{parts[0]}:{asset_path}"
    return os.path.splitext(os.path.basename(filepath))[0]


def _java_model_id(filepath):
    return _java_asset_id(filepath, "models")


def _java_particle_id(filepath):
    normalized = filepath.replace("\\", "/")
    lowered = normalized.casefold()
    for marker in (
        "/polytone/custom_particles/",
        "/polytone/particle_modifiers/",
    ):
        if marker in lowered and "/assets/" in lowered:
            assets_tail = normalized[lowered.rfind("/assets/") + len("/assets/"):]
            parts = assets_tail.split("/")
            relative = normalized[lowered.rfind(marker) + len(marker):]
            return f"{parts[0]}:{os.path.splitext(relative)[0]}"
    return _java_asset_id(filepath, "particles")


def _java_model_asset_type(filepath, root, logical_path=""):
    path = (logical_path or _safe_relpath(filepath, root)).replace("\\", "/").lower()
    marker = "/models/"
    if marker not in f"/{path}":
        return "Java Model"
    model_tail = f"/{path}".split(marker, 1)[1]
    segments = set(part for part in model_tail.split("/")[:-1] if part)
    if segments.intersection({"entities", "entity", "mobs", "mob"}):
        return "Java Entity Model"
    if segments.intersection({"particles", "particle", "effects", "effect"}):
        return "Java Particle Model"
    return "Java Model"


def _asset_section(asset):
    asset_type = asset.asset_type.lower()
    if "protected" in asset_type or "locked" in asset_type or asset.status in {"Unreadable", "Protected", "Encrypted"}:
        return 900
    if "pack" in asset_type:
        return 0
    if "entity" in asset_type:
        return 10
    if "particle" in asset_type:
        return 30
    if "model" in asset_type:
        return 20
    if "animation" in asset_type:
        return 40
    if "block" in asset_type or "item" in asset_type:
        return 50
    if "texture" in asset_type:
        return 80
    return 100


def _version_tuple(value):
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = re.findall(r"\d+", str(value or ""))
    values = [int(part) for part in parts[:4]]
    return tuple((values + [0, 0, 0, 0])[:4])


def _asset_version_sort_key(asset):
    if asset.asset_type not in {"Entity", "Attachable"}:
        return (0, 0, 0, 0)
    version = _version_tuple(asset.metadata.get("engine_version", ""))
    # Duplicate identifiers are collapsed by the UI after sorting. Newest
    # engine-specific definitions must therefore appear first.
    return tuple(-part for part in version)


def _asset_sort_key(asset):
    return (
        asset.pack_name.lower(),
        _asset_section(asset),
        asset.asset_type.lower(),
        asset.name.lower(),
        asset.identifier.lower(),
        _asset_version_sort_key(asset),
        browser_asset_identity(asset),
    )


def _asset_name_from_identifier(identifier, lang, prefixes=()):
    from .vfs import strip_obfuscation
    ident = strip_obfuscation(normalize_identifier(identifier))
    namespace_less = ident.split(":", 1)[-1]
    candidates = []
    for prefix in prefixes:
        candidates.append(f"{prefix}.{namespace_less}.name")
        candidates.append(f"{prefix}.{ident}.name")
        candidates.append(f"{prefix}.{identifier}.name")
    candidates.extend([
        f"entity.{namespace_less}.name",
        f"entity.{ident}.name",
        f"entity.{identifier}.name",
        f"item.spawn_egg.entity.{namespace_less}.name",
        f"item.spawn_egg.entity.{ident}.name",
        f"item.spawn_egg.entity.{identifier}.name",
        f"item.{namespace_less}.name",
        f"item.{ident}.name",
        f"item.{identifier}.name",
        f"tile.{namespace_less}.name",
        f"tile.{ident}.name",
        f"tile.{identifier}.name",
    ])
    for key in candidates:
        if key in lang:
            return lang[key]
    return _prettify_identifier(identifier)


def _animation_alias_detail(aliases):
    if not aliases:
        return ""
    clean = []
    for alias in aliases:
        if alias and alias not in clean:
            clean.append(alias)
    if not clean:
        return ""
    return "Aliases/references: " + ", ".join(clean[:6])


def _build_animation_aliases(json_files, pack, vfs=None, json_loader=None):
    json_loader = json_loader or load_json_file
    aliases = {}
    for filepath in json_files:
        if "entity" not in filepath.replace("\\", "/").lower():
            continue
        try:
            data = json_loader(filepath)
        except Exception:
            continue
        if detect_json_asset(filepath, data=data) != "client_entity":
            continue
        entity = data.get("minecraft:client_entity", {}) if isinstance(data, dict) else {}
        desc = entity.get("description", {}) if isinstance(entity, dict) else {}
        if not isinstance(desc, dict):
            continue
        entity_identifier = desc.get("identifier", "")
        entity_name = _asset_name_from_identifier(entity_identifier, pack.lang, prefixes=("entity",))
        animations = desc.get("animations", {}) or {}
        if not isinstance(animations, dict):
            continue
        for alias, ref in animations.items():
            if not isinstance(ref, str) or not ref.startswith("animation."):
                continue
            pretty_alias = _prettify_identifier(alias)
            display = f"{entity_name} {pretty_alias}"
            aliases.setdefault(ref, []).append(display)
    return aliases


def _build_entity_reverse_map(json_files, json_loader=None):
    json_loader = json_loader or load_json_file
    geo_map = _GeometryMap()
    tex_map = _TextureMap()
    for filepath in json_files:
        if "entity" not in filepath.replace("\\", "/").lower():
            continue
        try:
            data = json_loader(filepath)
        except Exception:
            continue
        if detect_json_asset(filepath, data=data) != "client_entity":
            continue
        entity = data.get("minecraft:client_entity", {}) if isinstance(data, dict) else {}
        desc = entity.get("description", {}) if isinstance(entity, dict) else {}
        if not isinstance(desc, dict):
            continue
        entity_identifier = desc.get("identifier", "")
        if not entity_identifier:
            continue
            
        geometries = desc.get("geometry", {})
        if isinstance(geometries, dict):
            for geo_ref in geometries.values():
                if isinstance(geo_ref, str):
                    geo_map[geo_ref] = entity_identifier
                    geo_map[_geometry_match_key(geo_ref)] = entity_identifier
                    
        textures = desc.get("textures", {})
        if isinstance(textures, dict):
            for tex_ref in textures.values():
                if isinstance(tex_ref, str):
                    tex_map[tex_ref.replace("\\", "/").lower()] = entity_identifier
    return geo_map, tex_map



def _geometry_cube_count(geometry):
    bones = geometry.get("bones", []) if isinstance(geometry, dict) else []
    if not isinstance(bones, list):
        return 0
    count = 0
    for bone in bones:
        if not isinstance(bone, dict):
            continue
        cubes = bone.get("cubes", [])
        if isinstance(cubes, list):
            count += sum(1 for cube in cubes if isinstance(cube, dict))
    return count
def _extract_json_assets(filepath, pack, root, animation_aliases=None, vfs=None, geo_map=None, json_loader=None):
    try:
        data = (json_loader or load_json_file)(filepath)
    except Exception:
        return [BrowserAsset(
            name=_prettify_identifier(filepath),
            asset_type="Locked/Unknown",
            source_path=filepath,
            pack_name=pack.name,
            status="Unreadable",
            detail="Could not read JSON metadata; this may be protected, encrypted, or malformed.",
            importable=False,
        )]

    rel = _safe_relpath(filepath, root)
    assets = []
    
    logical_name = None
    logical_path = ""
    if vfs:
        logical_path = vfs.resolve_logical(filepath)
        rel_path = fast_relpath(filepath, root).replace("\\", "/")
        if logical_path and logical_path != rel_path:
            logical_stem = os.path.splitext(os.path.basename(logical_path))[0]
            if logical_stem.endswith(".geo") or logical_stem.endswith(".entity"):
                logical_stem = os.path.splitext(logical_stem)[0]
            logical_name = _asset_name_from_identifier(f"geometry.{logical_stem}", pack.lang, prefixes=("entity", "item.spawn_egg.entity"))

    detected = detect_json_asset(filepath, data=data)

    if detected == "model":
        geometries = data.get("minecraft:geometry")
        if isinstance(geometries, list):
            geometry_total = len(geometries)
            for geo_index, geo in enumerate(geometries):
                if not isinstance(geo, dict):
                    continue
                identifier = geo.get("description", {}).get("identifier", "")
                cube_count = _geometry_cube_count(geo)
                bones = geo.get("bones", [])
                bone_count = len(bones) if isinstance(bones, list) else 0

                name = logical_name if geometry_total == 1 else None
                mapped_entity = _geo_map_lookup(geo_map, identifier)
                if not name and mapped_entity:
                    name = _asset_name_from_identifier(mapped_entity, pack.lang, prefixes=("entity", "item.spawn_egg.entity"))
                if not name:
                    name = _asset_name_from_identifier(identifier or f"geometry {geo_index + 1}", pack.lang, prefixes=("entity", "item.spawn_egg.entity"))

                assets.append(BrowserAsset(
                    name=name,
                    asset_type="Model",
                    source_path=filepath,
                    pack_name=pack.name,
                    identifier=identifier,
                    status="Ready" if cube_count else "No Geometry",
                    detail=f"Geometry JSON: {rel}; geometry {geo_index + 1}/{geometry_total}; {bone_count} bones; {cube_count} cubes",
                    importable=cube_count > 0,
                    metadata={"geometry_index": str(geo_index)},
                ))
        legacy_geometries = [(key, value) for key, value in data.items() if key.startswith("geometry.") and isinstance(value, dict)]
        legacy_total = len(legacy_geometries)
        for geo_index, (key, value) in enumerate(legacy_geometries):
            cube_count = _geometry_cube_count(value)
            bones = value.get("bones", [])
            bone_count = len(bones) if isinstance(bones, list) else 0
            name = logical_name if legacy_total == 1 else None
            mapped_entity = _geo_map_lookup(geo_map, key)
            if not name and mapped_entity:
                name = _asset_name_from_identifier(mapped_entity, pack.lang, prefixes=("entity", "item.spawn_egg.entity"))
            if not name:
                name = _asset_name_from_identifier(key, pack.lang, prefixes=("entity", "item.spawn_egg.entity"))

            assets.append(BrowserAsset(
                name=name,
                asset_type="Model",
                source_path=filepath,
                pack_name=pack.name,
                identifier=key,
                status="Ready" if cube_count else "No Geometry",
                detail=f"Legacy geometry JSON: {rel}; geometry {geo_index + 1}/{legacy_total}; {bone_count} bones; {cube_count} cubes",
                importable=cube_count > 0,
                metadata={"geometry_index": str(geo_index)},
            ))

    elif detected == "java_model":
        identifier = _java_model_id(filepath)
        asset_type = _java_model_asset_type(filepath, root, logical_path)
        element_count = len(data.get("elements", []) or []) if isinstance(data.get("elements", []), list) else 0
        texture_count = len(data.get("textures", {}) or {}) if isinstance(data.get("textures", {}), dict) else 0
        parent = data.get("parent", "") if isinstance(data.get("parent", ""), str) else ""
        detail_parts = [f"{asset_type} JSON: {rel}"]
        if parent:
            detail_parts.append(f"parent {parent}")
        detail_parts.append(f"{element_count} elements")
        detail_parts.append(f"{texture_count} textures")
        assets.append(BrowserAsset(
            name=logical_name or _prettify_identifier(identifier),
            asset_type=asset_type,
            source_path=filepath,
            pack_name=pack.name,
            identifier=identifier,
            detail="; ".join(detail_parts),
            importable=True,
        ))

    elif detected == "java_particle":
        try:
            from ..java_parser import JavaParticleParser
        except (ImportError, ValueError):
            from java_parser import JavaParticleParser
        parser = JavaParticleParser(filepath)
        metadata = parser.compatibility_metadata()
        identifier = metadata["identifier"] or _java_particle_id(filepath)
        texture_count = metadata["texture_count"]
        source_path = (
            metadata["sprite_filepath"]
            if os.path.isfile(metadata["sprite_filepath"])
            else filepath
        )
        detail_parts = [f"Java particle JSON: {rel}", f"{texture_count} textures"]
        if metadata["is_polytone"]:
            detail_parts.append("Polytone custom particle (safe MVEL subset)")
        if metadata["has_modifier"]:
            detail_parts.append("Polytone particle modifier")
        assets.append(BrowserAsset(
            name=logical_name or _prettify_identifier(identifier),
            asset_type="Java Particle",
            source_path=source_path,
            pack_name=pack.name,
            identifier=identifier,
            detail="; ".join(detail_parts),
            importable=True,
            metadata={
                "java_namespace": metadata["namespace"],
                "polytone_path": metadata["polytone_path"],
                "polytone_modifier_path": metadata["modifier_path"],
            },
        ))
    elif detected == "client_entity":
        entity = data.get("minecraft:client_entity", data.get("minecraft:attachable", {}))
        entity = entity if isinstance(entity, dict) else {}
        desc = entity.get("description", {})
        desc = desc if isinstance(desc, dict) else {}
        identifier = desc.get("identifier", "")
        geometry_count = len(desc.get("geometry", {}) or {})
        texture_count = len(desc.get("textures", {}) or {})
        animation_count = len(desc.get("animations", {}) or {})
        asset_type = "Attachable" if "minecraft:attachable" in data else "Entity"
        assets.append(BrowserAsset(
            name=_asset_name_from_identifier(identifier, pack.lang, prefixes=("entity", "item.spawn_egg.entity")),
            asset_type=asset_type,
            source_path=filepath,
            pack_name=pack.name,
            identifier=identifier,
            status="Ready" if geometry_count else "Special Renderer",
            detail=(
                f"{geometry_count} geometry refs, {texture_count} texture refs, "
                f"{animation_count} animation refs"
            ) if geometry_count else "Engine-owned/special renderer; no importable geometry references",
            metadata={"engine_version": str(desc.get("min_engine_version", "0"))},
        ))

    elif detected == "particle":
        effect = data.get("particle_effect", data.get("minecraft:particle_effect", data))
        if "description" not in effect or not isinstance(effect.get("description"), dict) or "identifier" not in effect["description"]:
            for val in data.values():
                if isinstance(val, dict) and "description" in val and "identifier" in val.get("description", {}):
                    effect = val
                    break
        desc = effect.get("description", {}) if isinstance(effect, dict) else {}
        identifier = desc.get("identifier", "")
        assets.append(BrowserAsset(
            name=_asset_name_from_identifier(identifier, pack.lang, prefixes=("particle",)),
            asset_type="Particle",
            source_path=filepath,
            pack_name=pack.name,
            identifier=identifier,
            detail=f"Particle JSON: {rel}",
        ))

    elif detected == "animation":
        animations = data.get("animations", {})
        if not isinstance(animations, dict):
            animations = {}
        for identifier in animations.keys():
            aliases = (animation_aliases or {}).get(identifier, [])
            detail = f"Animation JSON: {rel}"
            alias_detail = _animation_alias_detail(aliases)
            if alias_detail:
                detail = f"{detail}; {alias_detail}"
            name = _prettify_identifier(identifier)
            if aliases and " alias " not in aliases[0]:
                name = aliases[0]
            assets.append(BrowserAsset(
                name=name,
                asset_type="Animation",
                source_path=filepath,
                pack_name=pack.name,
                identifier=identifier,
                detail=detail,
            ))

    elif detected == "animation_controller":
        controllers = data.get("animation_controllers", {})
        if not isinstance(controllers, dict):
            controllers = {}
        for identifier in controllers.keys():
            assets.append(BrowserAsset(
                name=_prettify_identifier(identifier),
                asset_type="Animation Controller",
                source_path=filepath,
                pack_name=pack.name,
                identifier=identifier,
                detail=f"Controller JSON: {rel}",
                importable=False,
            ))

    elif "minecraft:block" in data:
        block = data.get("minecraft:block", {})
        block = block if isinstance(block, dict) else {}
        desc = block.get("description", {})
        desc = desc if isinstance(desc, dict) else {}
        identifier = desc.get("identifier", "")
        assets.append(BrowserAsset(
            name=_asset_name_from_identifier(identifier, pack.lang, prefixes=("tile",)),
            asset_type="Block",
            source_path=filepath,
            pack_name=pack.name,
            identifier=identifier,
            detail=f"Block JSON: {rel}",
            importable=False,
        ))

    elif "minecraft:item" in data:
        item = data.get("minecraft:item", {})
        item = item if isinstance(item, dict) else {}
        desc = item.get("description", {})
        desc = desc if isinstance(desc, dict) else {}
        identifier = desc.get("identifier", "")
        assets.append(BrowserAsset(
            name=_asset_name_from_identifier(identifier, pack.lang, prefixes=("item",)),
            asset_type="Item",
            source_path=filepath,
            pack_name=pack.name,
            identifier=identifier,
            detail=f"Item JSON: {rel}",
            importable=False,
        ))

    elif "minecraft:texture_set" in data or filepath.lower().endswith(".texture_set.json"):
        stem = os.path.basename(filepath).replace(".texture_set.json", "")
        texset = data.get("minecraft:texture_set", {})
        color = texset.get("color", "") if isinstance(texset, dict) else ""
        name = logical_name or _prettify_identifier(color or stem)
        status = "Ready"
        assets.append(BrowserAsset(
            name=name,
            asset_type="Texture Set",
            source_path=filepath,
            pack_name=pack.name,
            identifier=color or stem,
            status=status,
            detail=f"Texture set JSON: {rel}",
            importable=False,
        ))

    if not assets and filepath.lower().endswith("manifest.json"):
        header = data.get("header", {})
        name = _resolve_lang(header.get("name", ""), pack.lang) or pack.name
        desc = _resolve_lang(header.get("description", ""), pack.lang)
        assets.append(BrowserAsset(
            name=name,
            asset_type="Pack",
            source_path=filepath,
            pack_name=pack.name,
            identifier=header.get("uuid", ""),
            detail=desc or f"Pack manifest: {rel}",
            importable=False,
        ))

    return assets


def _texture_asset(filepath, pack, root, vfs=None, tex_map=None):
    rel = _safe_relpath(filepath, root)
    
    logical_name = None
    logical_path = ""
    if vfs:
        logical_path = vfs.resolve_logical(filepath)
        rel_path = fast_relpath(filepath, root).replace("\\", "/")
        if logical_path and logical_path != rel_path:
            logical_stem = os.path.splitext(os.path.basename(logical_path))[0]
            logical_name = _prettify_identifier(logical_stem)
            
    name = logical_name
    
    if not name and tex_map:
        tex_path = rel.replace("\\", "/").lower()
        if tex_path.endswith(".png") or tex_path.endswith(".tga") or tex_path.endswith(".jpg"):
            tex_path = os.path.splitext(tex_path)[0]
        match = _texture_map_match(tex_map, tex_path)
        if match is not None:
            e_id = match[1]
            e_name = _asset_name_from_identifier(e_id, pack.lang, prefixes=("entity", "item.spawn_egg.entity"))
            if e_name and e_name != _prettify_identifier(e_id):
                name = e_name + " Texture"
                
    name = name or _prettify_identifier(filepath)
    stem = os.path.splitext(os.path.basename(filepath))[0]
    status = "Ready"
    detail = f"Texture: {rel}"
    if status != "Ready":
        detail += "; no readable metadata name was exposed"
    return BrowserAsset(
        name=name,
        asset_type="Texture",
        source_path=filepath,
        pack_name=pack.name,
        identifier=rel.replace("\\", "/"),
        status=status,
        detail=detail,
        importable=True,
    )

def _java_texture_identifier(namespace, relative_path):
    path = os.path.splitext(str(relative_path).replace("\\", "/"))[0]
    if path.startswith("textures/"):
        path = path[len("textures/"):]
    return "%s:%s" % (namespace or "minecraft", path.lstrip("/"))


def _java_pack_texture_ids(pack_root):
    """Logical Java texture IDs physically overridden by one pack."""
    found = {}
    assets_root = os.path.join(pack_root, "assets")
    if not os.path.isdir(assets_root):
        return found
    for namespace in sorted(os.listdir(assets_root)):
        texture_root = os.path.join(assets_root, namespace, "textures")
        if not os.path.isdir(texture_root):
            continue
        for current, _dirs, files in walk(texture_root):
            for filename in files:
                if os.path.splitext(filename.lower())[1] not in TEXTURE_EXTENSIONS:
                    continue
                relative = fast_relpath(
                    os.path.join(current, filename),
                    os.path.join(assets_root, namespace),
                )
                found[_java_texture_identifier(namespace, relative)] = os.path.abspath(
                    os.path.join(current, filename)
                )
    return found


def _iter_java_blockstates(pack_roots):
    """Yield each logical blockstate once, honoring root precedence."""
    seen = set()
    for root in pack_roots:
        assets_root = os.path.join(root, "assets")
        if not os.path.isdir(assets_root):
            continue
        for namespace in sorted(os.listdir(assets_root)):
            state_root = os.path.join(assets_root, namespace, "blockstates")
            if not os.path.isdir(state_root):
                continue
            for current, _dirs, files in walk(state_root):
                for filename in sorted(files):
                    if not filename.lower().endswith(".json"):
                        continue
                    path = os.path.join(current, filename)
                    relative = os.path.splitext(fast_relpath(path, state_root))[0]
                    identifier = "%s:%s" % (
                        namespace,
                        relative.replace("\\", "/"),
                    )
                    key = identifier.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    yield identifier, path


def _blockstate_model_refs(definition):
    """Collect every model reference without assuming a particular state."""
    refs = []
    seen = set()

    def visit(value):
        if isinstance(value, dict):
            model = value.get("model")
            if isinstance(model, str) and model and model not in seen:
                seen.add(model)
                refs.append(model)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(definition)
    return refs


def _normalized_model_texture_ids(model, default_namespace):
    result = set()
    textures = model.get("textures", {}) if isinstance(model, dict) else {}
    if not isinstance(textures, dict):
        return result
    for reference in textures.values():
        if isinstance(reference, dict):
            # Minecraft 26.x texture slot: {"sprite": ..., "force_translucent": ...}
            reference = reference.get("sprite", "")
        if not isinstance(reference, str) or not reference or reference.startswith("#"):
            continue
        namespace, separator, path = reference.partition(":")
        if not separator:
            namespace, path = default_namespace, namespace
        result.add(_java_texture_identifier(namespace, path))
    return result


def _block_texture_ids(geometry_roots):
    """``[(blockstate id, blockstate path, texture ids)]`` for every blockstate.

    The texture ids cover every model a blockstate can show, with parents
    merged through the ordered roots.
    """
    try:
        from ..builders.schematic_geometry import merge_model_layered
    except (ImportError, ValueError):  # pragma: no cover - plain Python tests
        from builders.schematic_geometry import merge_model_layered

    blocks = []
    model_textures = {}
    for identifier, source_path in _iter_java_blockstates(geometry_roots):
        try:
            definition = load_json_file(source_path)
        except Exception:
            continue
        if not isinstance(definition, dict):
            continue
        block_namespace = identifier.partition(":")[0] or "minecraft"
        textures = set()
        for model_ref in _blockstate_model_refs(definition):
            model_namespace = (
                model_ref.partition(":")[0]
                if ":" in model_ref else block_namespace
            )
            cache_key = (model_namespace, model_ref)
            model_ids = model_textures.get(cache_key)
            if model_ids is None:
                merged = merge_model_layered(
                    model_ref, geometry_roots, block_namespace
                )
                model_ids = _normalized_model_texture_ids(
                    merged, model_namespace
                )
                model_textures[cache_key] = model_ids
            textures.update(model_ids)
        blocks.append((identifier, source_path, textures))
    return blocks


# Bump when ``_block_texture_ids`` records something different.
_BLOCK_TEXTURES_CACHE_VERSION = 1


def _model_graph_signature(geometry_roots):
    """Path, size and mtime of every blockstate and model file in the roots."""
    digest = hashlib.sha1()
    for root in geometry_roots:
        assets_root = os.path.join(root, "assets")
        try:
            namespaces = sorted(os.listdir(assets_root))
        except OSError:
            continue
        for namespace in namespaces:
            for folder in ("blockstates", "models"):
                stack = [os.path.join(assets_root, namespace, folder)]
                while stack:
                    current = stack.pop()
                    try:
                        with os.scandir(current) as listing:
                            entries = sorted(listing, key=lambda entry: entry.name)
                    except OSError:
                        continue
                    for entry in entries:
                        try:
                            if entry.is_dir():
                                stack.append(entry.path)
                                continue
                            stat = entry.stat()
                        except OSError:
                            continue
                        digest.update(
                            f"{entry.path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode(
                                "utf-8", "surrogatepass"
                            )
                        )
    return digest.hexdigest()


def _saved_block_texture_ids(geometry_roots):
    """``_block_texture_ids``, saved per user until a model file changes.

    Walking vanilla's ~1,100 blockstates and their parent models took about
    two seconds on every Java pack index. The model graph only depends on
    blockstate and model files, so a listing of those decides reuse.
    """
    key = json.dumps([_BLOCK_TEXTURES_CACHE_VERSION, list(geometry_roots)])
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()
    path = os.path.join(user_cache_dir("index"), "blocks_" + digest + ".json")
    signature = _model_graph_signature(geometry_roots)
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
        if payload.get("signature") == signature:
            return [
                (identifier, source_path, set(textures))
                for identifier, source_path, textures in payload["blocks"]
            ]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    blocks = _block_texture_ids(geometry_roots)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = "%s.%d.tmp" % (path, os.getpid())
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({
                "signature": signature,
                "blocks": [
                    [identifier, source_path, sorted(textures)]
                    for identifier, source_path, textures in blocks
                ],
            }, stream)
        os.replace(temporary, path)
    except OSError:
        pass
    return blocks


def synthesize_java_block_assets(
    pack_root, vanilla_root="", pack_name="", max_assets=25000,
    save_model_graph=False,
):
    """Create importable Java Block cards from texture overrides.

    The active pack supplies textures while the ordered model graph falls
    through to vanilla. Matching is entirely data-driven: no pack, mod, or
    block identifiers are special-cased. ``save_model_graph`` reuses the
    vanilla model graph across runs (see ``_saved_block_texture_ids``).
    """
    if not pack_root or not os.path.isdir(pack_root):
        return []
    try:
        from .vanilla_assets import build_pack_roots
    except (ImportError, ValueError):  # pragma: no cover - plain Python tests
        from importers.vanilla_assets import build_pack_roots

    overridden_paths = _java_pack_texture_ids(pack_root)
    overridden = set(overridden_paths)
    geometry_roots = build_pack_roots(vanilla_root, "")
    if not overridden or not geometry_roots:
        return []

    if save_model_graph:
        blocks = _saved_block_texture_ids(geometry_roots)
    else:
        blocks = _block_texture_ids(geometry_roots)
    cards = []
    for identifier, source_path, textures in blocks:
        matched = textures.intersection(overridden)
        if not matched:
            continue
        cards.append(BrowserAsset(
            name=_prettify_identifier(identifier),
            asset_type="Java Block",
            source_path=overridden_paths[sorted(matched)[0]],
            pack_name=pack_name or os.path.basename(pack_root.rstrip("\\/")),
            identifier=identifier,
            status="Ready",
            detail="Texture override applied to vanilla/model fallback: %s" % (
                ", ".join(sorted(matched)[:4])
            ),
            importable=True,
            metadata={
                "pack_root": os.path.abspath(pack_root),
                "vanilla_root": os.path.abspath(vanilla_root) if vanilla_root else "",
                "block_state": identifier,
                "blockstate_source": os.path.abspath(source_path),
                "overridden_textures": json.dumps(sorted(matched)),
                "geometry_policy": "VANILLA",
            },
        ))
        if len(cards) >= max_assets:
            break
    return cards


class _JsonPrefetch:
    """Run ``load_json_file`` ahead on a few threads, in the order the scan asks.

    Opening thousands of small files is mostly waiting on the disk (OneDrive
    especially), so a bounded window of upcoming loads runs in parallel.
    """

    def __init__(self, paths, window=64, workers=8):
        self._queue = list(paths)
        self._next = 0
        self._pending = {}
        self._window = window
        self._pool = ThreadPoolExecutor(max_workers=workers) if self._queue else None
        self._fill()

    def _fill(self):
        while self._next < len(self._queue) and len(self._pending) < self._window:
            path = self._queue[self._next]
            self._next += 1
            if path not in self._pending:
                self._pending[path] = self._pool.submit(load_json_file, path)

    def load(self, path):
        future = self._pending.pop(path, None)
        self._fill()
        if future is None:
            return load_json_file(path)
        return future.result()

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)


def _scan_pack_root(root, max_assets):
    lang = _load_lang(root)
    pack = PackContext(root=root, name=_read_pack_name(root, lang), lang=lang)
    assets = []
    seen = set()

    from .vfs import VirtualFileSystem
    vfs = VirtualFileSystem.get(root)

    if _is_encrypted_bedrock_pack(root):
        assets.append(BrowserAsset(
            name=pack.name,
            asset_type="Protected Pack",
            source_path=root,
            pack_name=pack.name,
            status="Encrypted",
            detail="Encrypted Bedrock contents.json detected. Use an already-decrypted resource pack folder for import; this add-on does not decrypt protected packs.",
            importable=False,
        ))

    all_files = []
    for current, dirs, files in walk(root):
        dirs[:] = [d for d in dirs if d not in {"__MACOSX", ".git", "node_modules", "__pycache__"}]
        for filename in files:
            all_files.append(os.path.join(current, filename))

    json_files = [f for f in all_files if f.endswith(".json")]
    # The entity passes read entity files first, then the main loop reads every
    # JSON file in walk order; read ahead in exactly that order.
    entity_files = [f for f in json_files if "entity" in f.replace("\\", "/").lower()]
    entity_set = set(entity_files)
    prefetch = _JsonPrefetch(entity_files + [
        f for f in all_files if f.lower().endswith(".json") and f not in entity_set
    ])
    # Share parsed documents across the metadata passes for this scan only.
    # A subsequent refresh always reads current files, and importers keep their
    # normal independent loading behavior.
    documents = {}

    def scan_json(filepath):
        if filepath not in documents:
            documents[filepath] = prefetch.load(filepath)
        return documents[filepath]

    try:
        animation_aliases = _build_animation_aliases(json_files, pack, vfs=vfs, json_loader=scan_json)
        geo_map, tex_map = _build_entity_reverse_map(json_files, json_loader=scan_json)

        for filepath in all_files:
            if len(assets) >= max_assets:
                break
            if filepath.lower().endswith(".json"):
                for asset in _extract_json_assets(filepath, pack, root, animation_aliases=animation_aliases, vfs=vfs, geo_map=geo_map, json_loader=scan_json):
                    _add_if_new(assets, seen, asset)
                documents.pop(filepath, None)
            if filepath.lower().endswith((".png", ".tga", ".jpg", ".jpeg")):
                _add_if_new(assets, seen, _texture_asset(filepath, pack, root, vfs=vfs, tex_map=tex_map))
    finally:
        prefetch.close()

    return assets

def _scan_archive(filepath):
    pack_name = os.path.splitext(os.path.basename(filepath))[0]
    inspection = inspect_archive(filepath)
    status = inspection.get("status", "Unreadable")
    detail = inspection.get("error", "")
    packs = inspection.get("resource_packs", [])
    archive_type = inspection.get("archive_type", "")

    if packs:
        first = packs[0]
        pack_name = _resolve_lang(first.get("name", ""), {}) or pack_name
        description = _resolve_lang(first.get("description", ""), {})
        count = len(packs)
        detail = description or (
            f"{count} resource-pack module{'s' if count != 1 else ''} available "
            f"from this .{archive_type} archive."
        )
        status = "Archive"

    reason = archive_rejection_reason(filepath)
    if reason:
        status = "Unsupported"
        detail = reason

    if status == "No Resource Packs" and not detail:
        detail = "The container has no visual resource-pack modules."

    return BrowserAsset(
        name=pack_name,
        asset_type="Unsupported Archive" if status == "Unsupported" else "Pack Archive",
        source_path=filepath,
        pack_name=pack_name,
        status=status,
        detail=detail,
        importable=False,
        metadata={
            "archive_type": archive_type,
            "resource_pack_count": str(len(packs)),
        },
    )


def scan_asset_folder(root, include_archives=True, max_assets=25000):
    if not root or not os.path.exists(root):
        return []

    assets = []
    seen_pack_roots = set()

    if os.path.isfile(root):
        ext = os.path.splitext(root.lower())[1]
        if ext in ARCHIVE_EXTENSIONS:
            return [_scan_archive(root)]
        pack_root = os.path.dirname(root)
        pack = PackContext(root=pack_root, name=os.path.basename(pack_root), lang=_load_lang(pack_root))
        return _extract_json_assets(root, pack, pack_root)

    for pack_root in _find_pack_roots(root):
        if pack_root in seen_pack_roots:
            continue
        seen_pack_roots.add(pack_root)
        assets.extend(_scan_pack_root(pack_root, max_assets=max_assets - len(assets)))
        if len(assets) >= max_assets:
            break

    if include_archives and len(assets) < max_assets:
        for current, dirs, files in walk(root):
            dirs[:] = [d for d in dirs if d not in {"__MACOSX", ".git", "node_modules", "__pycache__"}]
            for filename in files:
                if len(assets) >= max_assets:
                    break
                if os.path.splitext(filename.lower())[1] in ARCHIVE_EXTENSIONS:
                    assets.append(_scan_archive(os.path.join(current, filename)))

    assets.sort(key=_asset_sort_key)
    return assets



def _iter_archives_limited(root, max_depth=4):
    """Yield archive files under root without walking unlimited trees."""
    root = os.path.abspath(root)
    for current, dirs, files in walk(root):
        rel = os.path.relpath(current, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [
                d for d in dirs
                if d not in {"__MACOSX", ".git", "node_modules", "__pycache__"}
            ]
        for filename in files:
            if os.path.splitext(filename.lower())[1] in ARCHIVE_EXTENSIONS:
                yield os.path.join(current, filename)


def scan_asset_roots(roots: List[str], addon_dir: str, include_archives: bool = True, max_assets: int = 25000) -> List[BrowserAsset]:
    """Scan several pack roots and extracted archives as one asset library."""
    if not roots:
        return []

    scan_roots = []
    archive_paths = []
    seen_roots = set()
    seen_archives = set()
    diagnostic_assets = []

    for root in roots:
        if not root or not os.path.exists(root):
            continue
        root_abs = os.path.abspath(root)
        ext = os.path.splitext(root_abs.lower())[1]
        if os.path.isfile(root_abs) and ext in ARCHIVE_EXTENSIONS:
            archive_key = os.path.normcase(root_abs)
            if include_archives and archive_key not in seen_archives:
                archive_paths.append(root_abs)
                seen_archives.add(archive_key)
            continue
        if os.path.isdir(root_abs):
            root_key = os.path.normcase(root_abs)
            if root_key not in seen_roots:
                scan_roots.append(root_abs)
                seen_roots.add(root_key)
            if include_archives:
                for archive_path in _iter_archives_limited(root_abs):
                    archive_key = os.path.normcase(os.path.abspath(archive_path))
                    if archive_key not in seen_archives:
                        archive_paths.append(archive_path)
                        seen_archives.add(archive_key)

    if include_archives and archive_paths:
        from .archive_extractor import extract_archives

        for result in extract_archives(archive_paths, addon_dir):
            output_path = result.get("output_path", "")
            if not output_path or not os.path.isdir(output_path):
                diagnostic = _scan_archive(result.get("archive_path", ""))
                if result.get("error"):
                    diagnostic.detail = str(result["error"])
                diagnostic_assets.append(diagnostic)
                continue
            root_key = os.path.normcase(os.path.abspath(output_path))
            if root_key not in seen_roots:
                scan_roots.append(output_path)
                seen_roots.add(root_key)

    assets = diagnostic_assets[:max_assets]
    seen_assets = set()
    for root in scan_roots:
        if len(assets) >= max_assets:
            break
        remaining = max_assets - len(assets)
        for asset in scan_asset_folder(root, include_archives=False, max_assets=remaining):
            asset_key = browser_asset_identity(asset)

            if asset_key in seen_assets:
                continue
            seen_assets.add(asset_key)
            assets.append(asset)
            if len(assets) >= max_assets:
                break

    assets.sort(key=_asset_sort_key)
    return assets

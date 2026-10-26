import json
import os
from copy import deepcopy

from .bedrock_assets import (
    find_geometry_file,
    load_json_file,
    normalize_identifier,
)

KNOWN_GEOMETRY_SCHEMA_FAMILIES = (
    (1, 8, 0),
    (1, 12, 0),
    (1, 14, 0),
    (1, 16, 0),
    (1, 19, 30),
    (1, 21, 0),
)


def _version_tuple(value):
    """Return a comparable three-component Bedrock format version."""
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = str(value or "").split(".")
    result = []
    for part in parts[:3]:
        try:
            result.append(int(part))
        except (TypeError, ValueError):
            result.append(0)
    while len(result) < 3:
        result.append(0)
    return tuple(result)


def geometry_schema_family(value):
    """Return the newest known geometry schema applicable to a version."""
    version = _version_tuple(value)
    compatible = [
        item for item in KNOWN_GEOMETRY_SCHEMA_FAMILIES if item <= version
    ]
    return max(compatible) if compatible else KNOWN_GEOMETRY_SCHEMA_FAMILIES[0]


def _format_version_text(value, fallback="1.8.0"):
    version = _version_tuple(value or fallback)
    return ".".join(str(part) for part in version)


def _geometry_match_key(identifier):
    ident = normalize_identifier(identifier or "").replace("\\", "/").lower()
    for prefix in ("geometry.", "geometry/", "geo.", "geo/"):
        if ident.startswith(prefix):
            ident = ident[len(prefix):]
            break
    return ident

def _geometry_keys_match(candidate, target):
    if not candidate or not target:
        return False
    return candidate == target


def _geometry_compatibility_key(identifier):
    return "".join(char for char in _geometry_match_key(identifier) if char.isalnum())


def _resource_pack_root(filepath):
    current = os.path.abspath(os.path.dirname(filepath or ""))
    while current:
        if os.path.basename(current).casefold() == "models":
            return os.path.dirname(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return ""


def bones_with_cubes(geometry):
    """Names of bones that contribute cube geometry (not skeleton-only)."""
    bones = geometry.get("bones", []) if isinstance(geometry, dict) else []
    bones = bones if isinstance(bones, list) else []
    result = set()
    for bone in bones:
        if isinstance(bone, dict) and bone.get("cubes"):
            result.add(bone.get("name", ""))
    result.discard("")
    return result


class BedrockModelParser:
    def __init__(
        self, filepath=None, json_string=None, _inheritance_chain=()
    ):
        self.filepath = filepath
        self._inheritance_chain = set(_inheritance_chain)
        if filepath:
            self._inheritance_chain.add(os.path.abspath(filepath))
        if filepath:
            self.data = load_json_file(filepath)
        elif json_string:
            self.data = json.loads(json_string)
        else:
            raise ValueError("Must provide either filepath or json_string")
        if not isinstance(self.data, dict):
            self.data = {}

    def get_format_version(self):
        if "minecraft:geometry" in self.data:
            return _format_version_text(self.data.get("format_version"), "1.12.0")
        return _format_version_text(self.data.get("format_version"), "1.8.0")

    def get_schema_family(self):
        return geometry_schema_family(self.get_format_version())

    def get_geometries(self):
        """
        Extracts all geometry definitions from the file.
        Bedrock models can use either "minecraft:geometry" (1.12+) or just be keyed by "geometry.*" (1.8).
        Returns a list of geometry dictionaries containing bones and description.
        """
        geometries = []

        # Format 1.12.0+
        if "minecraft:geometry" in self.data:
            geos = self.data["minecraft:geometry"]
            if isinstance(geos, list):
                result = []
                for geo in geos:
                    if not isinstance(geo, dict):
                        continue
                    normalized = deepcopy(geo)
                    normalized["_mcbedrock_format_version"] = self.get_format_version()
                    normalized["_mcbedrock_schema_family"] = self.get_schema_family()
                    result.append(normalized)
                return result

        # Format 1.8.0 supports geometry.child:geometry.parent inheritance.
        # Resolve it before normalization so the child keeps base cubes and
        # independently authored overlay cubes (vanilla Sheep is canonical).
        raw = {
            key: value
            for key, value in self.data.items()
            if isinstance(key, str)
            and key.startswith("geometry.")
            and isinstance(value, dict)
        }
        resolved = {}

        def external_legacy(parent_key):
            pack_root = _resource_pack_root(self.filepath)
            if not pack_root:
                return None
            parent_path = find_geometry_file(pack_root, parent_key)
            if not parent_path:
                return None
            parent_path = os.path.abspath(parent_path)
            if parent_path in self._inheritance_chain:
                return None
            parser = BedrockModelParser(
                filepath=parent_path,
                _inheritance_chain=self._inheritance_chain,
            )
            try:
                geometry = parser.get_geometry(parent_key)
            except (OSError, ValueError, IndexError):
                return None
            description = geometry.get("description", {})
            result = {
                "bones": deepcopy(geometry.get("bones", [])),
                "texturewidth": description.get("texture_width", 64),
                "textureheight": description.get("texture_height", 64),
            }
            for field in (
                "visible_bounds_width",
                "visible_bounds_height",
                "visible_bounds_offset",
            ):
                if field in description:
                    result[field] = deepcopy(description[field])
            return result

        def resolve_legacy(raw_key, visiting=None):
            if raw_key in resolved:
                return deepcopy(resolved[raw_key])
            visiting = set(visiting or ())
            if raw_key in visiting:
                return deepcopy(raw.get(raw_key, {}))
            visiting.add(raw_key)
            child_key, separator, parent_key = raw_key.partition(":")
            child = deepcopy(raw.get(raw_key, {}))
            if not separator:
                resolved[raw_key] = child
                return deepcopy(child)
            if parent_key in raw:
                base = resolve_legacy(parent_key, visiting)
            else:
                base = external_legacy(parent_key)
                if base is None:
                    resolved[raw_key] = child
                    return deepcopy(child)
            merged = deepcopy(base)
            for field, value in child.items():
                if field == "bones":
                    continue
                merged[field] = deepcopy(value)
            base_bones = [
                deepcopy(bone) for bone in merged.get("bones", [])
                if isinstance(bone, dict)
            ]
            by_name = {
                str(bone.get("name", "")).lower(): bone for bone in base_bones
            }
            for child_bone in child.get("bones", []):
                if not isinstance(child_bone, dict):
                    continue
                name = str(child_bone.get("name", "")).lower()
                existing = by_name.get(name)
                if existing is None:
                    existing = deepcopy(child_bone)
                    existing.pop("reset", None)
                    base_bones.append(existing)
                    by_name[name] = existing
                    continue
                if bool(child_bone.get("reset", False)):
                    replacement = deepcopy(child_bone)
                    replacement.pop("reset", None)
                    base_bones[base_bones.index(existing)] = replacement
                    by_name[name] = replacement
                    continue
                base_cubes = list(existing.get("cubes", []))
                for field, value in child_bone.items():
                    if field != "cubes":
                        existing[field] = deepcopy(value)
                existing["cubes"] = base_cubes + deepcopy(
                    child_bone.get("cubes", [])
                )
            merged["bones"] = base_bones
            merged["_mcbedrock_inherits"] = parent_key
            resolved[raw_key] = merged
            return deepcopy(merged)

        for key in raw:
            child_key = key.partition(":")[0]
            value = resolve_legacy(key)
            bones = value.get("bones", [])
            normalized_geo = {
                "description": {
                    "identifier": child_key,
                    "texture_width": value.get("texturewidth", 64),
                    "texture_height": value.get("textureheight", 64),
                    "legacy_uv": True,
                    "inherits": value.get("_mcbedrock_inherits", ""),
                },
                "bones": bones,
                "_mcbedrock_format_version": self.get_format_version(),
                "_mcbedrock_schema_family": self.get_schema_family(),
            }
            geometries.append(normalized_geo)

        return geometries

    def get_geometry(self, identifier=None, index=0):
        geometries = self.get_geometries()
        if not geometries:
            raise ValueError("No Bedrock geometry definitions found")

        if identifier:
            target = _geometry_match_key(identifier)
            matches = []
            for geometry_index, geometry in enumerate(geometries):
                desc = geometry.get("description", {})
                geo_id = desc.get("identifier", "") if isinstance(desc, dict) else ""
                if _geometry_keys_match(_geometry_match_key(geo_id), target):
                    matches.append((geometry_index, geometry))
            if not matches:
                compatible = [
                    (geometry_index, geometry)
                    for geometry_index, geometry in enumerate(geometries)
                    if _geometry_compatibility_key(
                        geometry.get("description", {}).get("identifier", "")
                    ) == _geometry_compatibility_key(identifier)
                ]
                if len(compatible) == 1:
                    matches = compatible
            for geometry_index, geometry in matches:
                if geometry_index == index:
                    return geometry
            if matches:
                return matches[0][1]

        if index < 0 or index >= len(geometries):
            raise IndexError(f"Geometry index {index} out of range")
        return geometries[index]

    def get_default_texture_size(self, geometry):
        desc = geometry.get("description", {})
        return (
            float(desc.get("texture_width", desc.get("texturewidth", 64))),
            float(desc.get("texture_height", desc.get("textureheight", 64))),
        )

    def get_display_name(self, geometry):
        ident = geometry.get("description", {}).get("identifier", "")
        if ident:
            return ident
        if self.filepath:
            return os.path.splitext(os.path.basename(self.filepath))[0]
        return "bedrock_model"

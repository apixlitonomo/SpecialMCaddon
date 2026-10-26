"""Minecraft Java particle and Polytone resource parsing."""

import json
import os


def _normalized(path):
    return os.path.abspath(path).replace("\\", "/")


def _java_asset_parts(filepath):
    """Return ``(assets_root, namespace, kind, path)`` for a Java asset."""
    normalized = _normalized(filepath)
    lowered = normalized.casefold()
    marker = "/assets/"
    if marker not in lowered:
        return "", "minecraft", "", os.path.splitext(os.path.basename(filepath))[0]

    start = lowered.rfind(marker)
    assets_root = normalized[: start + len(marker) - 1]
    tail = normalized[start + len(marker):]
    parts = tail.split("/")
    if len(parts) < 2:
        return assets_root, "minecraft", "", os.path.splitext(parts[-1])[0]

    namespace = parts[0] or "minecraft"
    relative = "/".join(parts[1:])
    relative_lower = relative.casefold()
    for kind, kind_marker in (
        ("particle", "particles/"),
        ("polytone_custom", "polytone/custom_particles/"),
        ("polytone_modifier", "polytone/particle_modifiers/"),
    ):
        if relative_lower.startswith(kind_marker):
            value = relative[len(kind_marker):]
            if value.casefold().endswith(".json"):
                value = value[:-5]
            return assets_root, namespace, kind, value
    return assets_root, namespace, "", os.path.splitext(os.path.basename(filepath))[0]


def _read_json(filepath):
    if not filepath or not os.path.isfile(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _texture_values(data):
    raw = data.get("textures", data.get("values", [])) if isinstance(data, dict) else []
    if isinstance(raw, dict):
        raw = list(raw.values())
    elif isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    result = []
    for entry in raw:
        if isinstance(entry, str):
            value = entry
        elif isinstance(entry, dict):
            value = entry.get("texture", entry.get("sprite", entry.get("value", "")))
        else:
            value = ""
        value = str(value or "").strip()
        if value:
            result.append(value)
    return result


class JavaParticleParser:
    """Parse vanilla/modded Java sprite lists and paired Polytone definitions."""

    def __init__(self, filepath):
        self.filepath = os.path.abspath(filepath)
        self.assets_root, self.namespace, self.source_kind, self.particle_path = (
            _java_asset_parts(self.filepath)
        )
        self.name = os.path.basename(self.particle_path) or os.path.splitext(
            os.path.basename(filepath)
        )[0]

        source_data = _read_json(self.filepath)
        self.polytone_data = {}
        self.polytone_path = ""
        self.modifier_data = {}
        self.modifier_path = ""

        if self.source_kind == "polytone_custom":
            self.polytone_data = source_data
            self.polytone_path = self.filepath
            self.sprite_filepath = self._companion("particles")
            self.data = _read_json(self.sprite_filepath)
        elif self.source_kind == "polytone_modifier":
            self.modifier_data = source_data
            self.modifier_path = self.filepath
            self.sprite_filepath = self._companion("particles")
            self.data = _read_json(self.sprite_filepath)
        else:
            self.sprite_filepath = self.filepath
            self.data = source_data
            self.polytone_path = self._companion("polytone/custom_particles")
            self.polytone_data = _read_json(self.polytone_path)
            self.modifier_path = self._companion("polytone/particle_modifiers")
            self.modifier_data = _read_json(self.modifier_path)

        self.textures_list = _texture_values(self.data)

    def _companion(self, folder):
        if not self.assets_root:
            return ""
        return os.path.normpath(
            os.path.join(
                self.assets_root,
                self.namespace,
                *folder.split("/"),
                *self.particle_path.split("/"),
            )
            + ".json"
        )

    def get_identifier(self):
        return f"{self.namespace}:{self.particle_path}"

    def get_texture_references(self):
        """Return canonical texture IDs, preserving custom namespaces."""
        result = []
        for texture in self.textures_list:
            value = texture.replace("\\", "/").strip()
            if ":" not in value:
                value = f"{self.namespace}:{value}"
            result.append(value)
        return result

    def get_texture_paths(self):
        """Compatibility alias returning namespace-aware Java texture IDs."""
        return self.get_texture_references()

    def get_first_texture_path(self):
        paths = self.get_texture_references()
        return paths[0] if paths else ""

    def get_frame_count(self):
        return len(self.textures_list)

    def is_polytone(self):
        return bool(self.polytone_data)

    def has_polytone_modifier(self):
        return bool(self.modifier_data)

    def compatibility_metadata(self):
        """Small serializable summary used by the browser and Blender builder."""
        return {
            "identifier": self.get_identifier(),
            "namespace": self.namespace,
            "particle_path": self.particle_path,
            "sprite_filepath": self.sprite_filepath,
            "polytone_path": self.polytone_path if self.polytone_data else "",
            "modifier_path": self.modifier_path if self.modifier_data else "",
            "texture_count": self.get_frame_count(),
            "is_polytone": self.is_polytone(),
            "has_modifier": self.has_polytone_modifier(),
        }

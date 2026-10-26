"""Pure, source-backed Texture Set parsing for Bedrock resource packs.

Minecraft treats an invalid ``*.texture_set.json`` as a content error and
ignores the whole Texture Set. These helpers mirror that all-or-nothing
contract: callers either receive a validated definition or reasons why the
classic color texture must be used as the recoverable fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from typing import Any, Optional


IMAGE_EXTENSION_PRIORITY = (".tga", ".png", ".jpg", ".jpeg")
TEXTURE_SET_FIELDS = {
    "color",
    "normal",
    "heightmap",
    "metalness_emissive_roughness",
    "metalness_emissive_roughness_subsurface",
}


@dataclass(frozen=True)
class TextureSetProblem:
    """One validation failure in a Texture Set document."""

    field: str
    message: str


@dataclass(frozen=True)
class TextureSetLayer:
    """A validated image reference or uniform layer value."""

    name: str
    source: Any
    image_path: str = ""
    uniform: tuple[float, ...] = ()

    @property
    def is_image(self) -> bool:
        return bool(self.image_path)


@dataclass(frozen=True)
class TextureSetDefinition:
    """Validated ``minecraft:texture_set`` data from one resource pack."""

    source_path: str
    pack_root: str
    format_version: str
    layers: dict[str, TextureSetLayer]

    def layer(self, name: str) -> Optional[TextureSetLayer]:
        return self.layers.get(name)


@dataclass(frozen=True)
class TextureSetResolution:
    """Result of looking up and validating a texture's Texture Set."""

    metadata_path: str = ""
    definition: Optional[TextureSetDefinition] = None
    problems: tuple[TextureSetProblem, ...] = field(default_factory=tuple)

    @property
    def exists(self) -> bool:
        return bool(self.metadata_path)

    @property
    def valid(self) -> bool:
        return self.definition is not None and not self.problems


def _inside(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, candidate)) == root
    except (OSError, ValueError):
        return False


def find_resource_pack_root(path: str) -> str:
    """Return the owning pack directory without crossing filesystem roots."""
    current = os.path.abspath(os.path.dirname(path))
    while True:
        if os.path.isfile(os.path.join(current, "manifest.json")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # Unit fixtures and unpacked fragments may omit a manifest. The directory
    # immediately containing ``textures`` is still the only safe pack scope.
    absolute = os.path.normpath(os.path.abspath(path))
    drive, tail = os.path.splitdrive(absolute)
    parts = tail.lstrip(os.sep).split(os.sep)
    lowered = [part.casefold() for part in parts]
    if "textures" in lowered:
        index = lowered.index("textures")
        prefix = os.path.join(drive + os.sep, *parts[:index])
        return os.path.normpath(prefix)
    return os.path.abspath(os.path.dirname(path))


def resolve_texture_resource(
    reference: str,
    *,
    metadata_path: str,
    pack_root: str,
) -> str:
    """Resolve a declared image in its owning pack using Mojang priority.

    Relative names first use the Texture Set directory. Pack-qualified names
    such as ``textures/entity/foo`` use the pack root. Absolute paths and
    references escaping the pack are rejected.
    """
    if not isinstance(reference, str) or not reference.strip():
        return ""
    text = reference.strip().replace("\\", "/")
    if os.path.isabs(text) or text.startswith("/"):
        return ""
    normalized = os.path.normpath(text.replace("/", os.sep))
    if normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
        return ""

    pack_root = os.path.abspath(pack_root)
    metadata_dir = os.path.dirname(os.path.abspath(metadata_path))
    if normalized.casefold().startswith("textures" + os.sep):
        roots = (pack_root,)
    else:
        roots = (metadata_dir, pack_root)
    seen = set()
    for root in roots:
        candidate = os.path.abspath(os.path.join(root, normalized))
        if candidate in seen or not _inside(pack_root, candidate):
            continue
        seen.add(candidate)
        stem, extension = os.path.splitext(candidate)
        candidates = (candidate,) if extension else tuple(
            stem + suffix for suffix in IMAGE_EXTENSION_PRIORITY
        )
        for image_path in candidates:
            if _inside(pack_root, image_path) and os.path.isfile(image_path):
                return os.path.normpath(image_path)
    return ""


def _numeric_tuple(value: Any, channels: int) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != channels:
        return ()
    result = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            return ()
        number = float(component)
        if not math.isfinite(number) or not 0.0 <= number <= 255.0:
            return ()
        result.append(number / 255.0)
    return tuple(result)


def _scalar(value: Any) -> tuple[float, ...]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ()
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 255.0:
        return ()
    return (number / 255.0,)


def _hex_tuple(value: Any, channels: int) -> tuple[float, ...]:
    if not isinstance(value, str) or not value.startswith("#"):
        return ()
    digits = value[1:]
    allowed_lengths = {channels * 2}
    # Color additionally accepts RGB; alpha defaults to fully opaque.
    if channels == 4:
        allowed_lengths.add(6)
    if len(digits) not in allowed_lengths:
        return ()
    try:
        values = tuple(
            int(digits[index:index + 2], 16) / 255.0
            for index in range(0, len(digits), 2)
        )
    except ValueError:
        return ()
    if channels == 4 and len(values) == 3:
        values = values + (1.0,)
    return values


def _parse_layer(
    name: str,
    value: Any,
    *,
    metadata_path: str,
    pack_root: str,
) -> tuple[Optional[TextureSetLayer], Optional[TextureSetProblem]]:
    if isinstance(value, str) and not value.startswith("#"):
        image_path = resolve_texture_resource(
            value,
            metadata_path=metadata_path,
            pack_root=pack_root,
        )
        if not image_path:
            return None, TextureSetProblem(
                name,
                "referenced image is missing, unreadable, or outside this resource pack",
            )
        return TextureSetLayer(name=name, source=value, image_path=image_path), None

    channels = {
        "color": 4,
        "heightmap": 1,
        "metalness_emissive_roughness": 3,
        "metalness_emissive_roughness_subsurface": 4,
    }.get(name)
    if channels is None:
        return None, TextureSetProblem(name, "this layer must reference an image")
    if channels == 1:
        uniform = _scalar(value)
    else:
        uniform = _numeric_tuple(value, channels) or _hex_tuple(value, channels)
    if not uniform:
        return None, TextureSetProblem(
            name,
            f"expected an image reference or a valid {channels}-channel 0..255 value",
        )
    return TextureSetLayer(name=name, source=value, uniform=uniform), None


def load_texture_set(texture_path: str) -> TextureSetResolution:
    """Load and validate the Texture Set adjacent to ``texture_path``.

    An absent Texture Set is not an error. An existing invalid one returns no
    definition so callers can follow Minecraft's classic-color fallback.
    """
    if not texture_path:
        return TextureSetResolution()
    metadata_path = (
        os.path.splitext(os.path.abspath(texture_path))[0]
        + ".texture_set.json"
    )
    if not os.path.isfile(metadata_path):
        return TextureSetResolution()
    try:
        with open(metadata_path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return TextureSetResolution(
            metadata_path=metadata_path,
            problems=(TextureSetProblem("$", f"invalid JSON: {exc}"),),
        )
    if not isinstance(document, dict):
        return TextureSetResolution(
            metadata_path=metadata_path,
            problems=(TextureSetProblem("$", "document root must be an object"),),
        )

    problems = []
    format_version = document.get("format_version")
    if not isinstance(format_version, str) or not format_version.strip():
        problems.append(
            TextureSetProblem("format_version", "missing or invalid format version")
        )
        format_version = ""
    payload = document.get("minecraft:texture_set")
    if not isinstance(payload, dict):
        problems.append(
            TextureSetProblem("minecraft:texture_set", "missing texture-set object")
        )
        payload = {}
    for key in sorted(str(key) for key in payload if key not in TEXTURE_SET_FIELDS):
        problems.append(TextureSetProblem(key, "unexpected texture-set field"))
    if "color" not in payload:
        problems.append(TextureSetProblem("color", "required color layer is missing"))
    if "normal" in payload and "heightmap" in payload:
        problems.append(
            TextureSetProblem(
                "normal/heightmap", "normal and heightmap are mutually exclusive"
            )
        )
    if (
        "metalness_emissive_roughness" in payload
        and "metalness_emissive_roughness_subsurface" in payload
    ):
        problems.append(
            TextureSetProblem("MER/MERS", "MER and MERS are mutually exclusive")
        )

    pack_root = find_resource_pack_root(metadata_path)
    layers = {}
    for name in TEXTURE_SET_FIELDS:
        if name not in payload:
            continue
        layer, problem = _parse_layer(
            name,
            payload[name],
            metadata_path=metadata_path,
            pack_root=pack_root,
        )
        if problem:
            problems.append(problem)
        elif layer:
            layers[name] = layer

    if problems:
        return TextureSetResolution(
            metadata_path=metadata_path,
            problems=tuple(problems),
        )
    return TextureSetResolution(
        metadata_path=metadata_path,
        definition=TextureSetDefinition(
            source_path=metadata_path,
            pack_root=pack_root,
            format_version=format_version,
            layers=layers,
        ),
    )

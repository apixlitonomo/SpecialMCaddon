"""Minecraft tint colours for Java model faces (no bpy dependency).

Since 1.21.4 every item definition (``assets/<ns>/items/*.json``) lists one
tint source per ``tintindex`` for each model it shows. Reading those gives
grass, leaves, leather, potions and the rest their in-game default colours
from data instead of name lists. Models no item definition references fall
back to the schematic block-colour table.
"""

import glob
import os

try:
    from ..importers.bedrock_assets import load_json_file
except ImportError:  # Plain-Python unit tests without the bpy package root.
    from importers.bedrock_assets import load_json_file

_INDEX_CACHE = {}


def normalize_model_id(reference, default_namespace="minecraft"):
    reference = str(reference or "").replace("\\", "/").strip()
    namespace, separator, path = reference.partition(":")
    if not separator:
        namespace, path = default_namespace, namespace
    return f"{namespace.lower()}:{path.lower()}"


def _model_nodes(node, found):
    """Every ``minecraft:model`` node in an item definition tree."""
    if isinstance(node, dict):
        if (
            str(node.get("type", "")).split(":")[-1] == "model"
            and isinstance(node.get("model"), str)
        ):
            found.append(node)
        for value in node.values():
            _model_nodes(value, found)
    elif isinstance(node, list):
        for value in node:
            _model_nodes(value, found)


def item_tint_index(roots, overlays=False):
    """``model id -> tint source list`` from the ordered pack roots.

    The first root that defines an item wins, like the resource pack stack.
    ``overlays`` also reads a pack's version overlay folders (where packs such
    as Origin Realms keep 1.21.4+ item definitions), overlays first.
    """
    key = (overlays,) + tuple(os.path.normcase(os.path.abspath(str(root))) for root in roots if root)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index = {}
    seen_items = set()
    folders = []
    for root in roots:
        if not root or not os.path.isdir(str(root)):
            continue
        if overlays:
            folders.extend(sorted(os.path.join(str(root), name) for name in os.listdir(str(root))
                                  if os.path.isdir(os.path.join(str(root), name, "assets")))[::-1])
        folders.append(str(root))
    for root in folders:
        pattern = os.path.join(root, "assets", "*", "items", "**", "*.json")
        for path in glob.glob(pattern, recursive=True):
            relative = os.path.relpath(path, root).replace("\\", "/").lower()
            if relative in seen_items:
                continue
            seen_items.add(relative)
            namespace = relative.split("/")[1] if relative.count("/") >= 2 else "minecraft"
            try:
                data = load_json_file(path)
            except Exception:  # noqa: BLE001 - a broken file must not stop imports
                continue
            nodes = []
            _model_nodes(data.get("model") if isinstance(data, dict) else None, nodes)
            for node in nodes:
                tints = node.get("tints")
                if isinstance(tints, list) and tints:
                    index.setdefault(normalize_model_id(node["model"], namespace), tints)
    _INDEX_CACHE[key] = index
    return index


def _argb_to_rgb(value):
    try:
        value = int(value) & 0xFFFFFF
    except (TypeError, ValueError):
        return None
    return (
        ((value >> 16) & 255) / 255.0,
        ((value >> 8) & 255) / 255.0,
        (value & 255) / 255.0,
    )


def tint_source_color(source, grass_colormap=None):
    """sRGB colour (0-1) a tint source shows by default, or None.

    ``grass_colormap(temperature, downfall)`` samples colormap/grass.png.
    """
    if not isinstance(source, dict):
        return _argb_to_rgb(source) if isinstance(source, int) else None
    kind = str(source.get("type", "")).split(":")[-1]
    if kind == "grass":
        if grass_colormap is None:
            return None
        try:
            temperature = min(1.0, max(0.0, float(source.get("temperature", 0.5))))
            downfall = min(1.0, max(0.0, float(source.get("downfall", 1.0))))
        except (TypeError, ValueError):
            return None
        return grass_colormap(temperature, downfall)
    if kind == "constant":
        return _argb_to_rgb(source.get("value"))
    if "default" in source:
        return _argb_to_rgb(source.get("default"))
    return None


def grass_colormap_pixel(temperature, downfall):
    """Pixel (column, row-from-top) GrassColor.get samples."""
    downfall *= temperature
    return int((1.0 - temperature) * 255.0), int((1.0 - downfall) * 255.0)


def model_tint_colors(model_id, roots, grass_colormap=None):
    """Per-``tintindex`` default colours for one model (None where unknown)."""
    sources = item_tint_index(roots).get(normalize_model_id(model_id), ())
    return [tint_source_color(source, grass_colormap) for source in sources]


# Tint sources whose colour the game or a server picks at runtime. A constant
# is fixed by the pack itself (Origin Realms uses white for slot 0).
_FIXED_SOURCES = {"constant"}


def server_tinted_indices(model_id, roots):
    """``tintindex`` values a server or the game colours at runtime, or None
    when no item definition names this model (then every index counts)."""
    sources = item_tint_index(roots, overlays=True).get(normalize_model_id(model_id))
    if not sources:
        return None
    return {index for index, source in enumerate(sources)
            if not (isinstance(source, dict) and str(source.get("type", "")).split(":")[-1] in _FIXED_SOURCES)}

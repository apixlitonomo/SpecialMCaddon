"""Bake Java block models into reusable per-block face templates.

One block state is baked exactly once and then stamped at every world position
that uses it, so a schematic with 200k stone blocks resolves and merges model
JSON a single time. Output vertices are Blender-space, block-local, in metres
(one block = 1.0), ready to be offset by the block's world position.

Pure Python - no ``bpy`` - so the whole resolve/bake path is testable outside
Blender.
"""

import os

from .java_geometry import (
    JAVA_FACE_AXIS,
    JAVA_FACE_INDICES,
    apply_element_rotation_java,
    element_vertices_java,
    face_uv_corners,
    java_face_has_area,
    java_to_blender_point,
    java_to_mirrored_blender_point,
    rotate_point_java,
    uvlock_face,
)
from .java_resolver import (
    _namespace_from_model,
)

try:
    from ..importers.bedrock_assets import load_json_file
    from ..importers.blockstate_resolver import (
        as_roots,
        parse_block_state,
        resolve_model_path,
        resolve_block_models,
        resolve_texture_path,
        split_namespace,
        texture_slot_reference,
    )
except (ImportError, ValueError):  # pragma: no cover - plain Python test path
    from importers.bedrock_assets import load_json_file
    from importers.blockstate_resolver import (
        as_roots,
        parse_block_state,
        resolve_model_path,
        resolve_block_models,
        resolve_texture_path,
        split_namespace,
        texture_slot_reference,
    )


FACE_ORDER = ("north", "south", "east", "west", "up", "down")

# Java space: +X east, +Y up, +Z south.
FACE_DIRECTION_JAVA = {
    "north": (0, 0, -1),
    "south": (0, 0, 1),
    "east": (1, 0, 0),
    "west": (-1, 0, 0),
    "up": (0, 1, 0),
    "down": (0, -1, 0),
}
_DIRECTION_TO_FACE = {vector: name for name, vector in FACE_DIRECTION_JAVA.items()}

# ``JAVA_FACE_INDICES`` winds every quad outward in Java space; the mirrored
# schematic world axes flip handedness, so their loops (and UVs) are reversed.
_FACE_LOOP = {name: tuple(reversed(indices))
              for name, indices in JAVA_FACE_INDICES.items()}

# World-space neighbour offsets, keyed by face name (Minecraft coordinates).
FACE_OFFSET = dict(FACE_DIRECTION_JAVA)

# Blocks that fill their cell but must not hide their neighbours' faces.
NON_OCCLUDING_NAMES = frozenset((
    "minecraft:barrier",
    "minecraft:structure_void",
    "minecraft:light",
    "minecraft:slime_block",
    "minecraft:honey_block",
    "minecraft:tinted_glass",
))
NON_OCCLUDING_SUFFIXES = (
    "_glass", "_leaves", "_ice", "glass", "leaves", "ice", "water", "lava",
    "_shulker_box", "shulker_box", "_spawner", "spawner",
)

# Vanilla applies a biome colour to these; no biome data exists in a schematic,
# so a plains-like constant keeps grass and leaves from rendering grey.
GRASS_TINT = (0.5686, 0.7412, 0.3490)
FOLIAGE_TINT = (0.4667, 0.6706, 0.1843)
WATER_TINT = (0.2471, 0.4627, 0.8941)

_GRASS_TINT_NAMES = ("grass", "fern", "sugar_cane", "tall_grass", "large_fern")
_WATER_TINT_NAMES = ("water", "bubble_column", "cauldron")


def tint_color_for(identifier, colormaps=None, default=FOLIAGE_TINT):
    """Return the constant biome tint for a block name (``default`` if none)."""
    _namespace, path = split_namespace(identifier)
    if any(word in path for word in _WATER_TINT_NAMES):
        return WATER_TINT
    if "leaves" in path or "vine" in path:
        return (colormaps or {}).get('foliage', FOLIAGE_TINT)
    if any(word in path for word in _GRASS_TINT_NAMES):
        return (colormaps or {}).get('grass', GRASS_TINT)
    return default


class BakedFace:
    """One textured quad in block-local Blender space."""

    __slots__ = ("verts", "uvs", "texture", "cullface", "tint")

    def __init__(self, verts, uvs, texture, cullface, tint):
        self.verts = verts
        self.uvs = uvs
        self.texture = texture
        self.cullface = cullface
        self.tint = tint


class BakedModel:
    """Every face of one block state, plus how it occludes its neighbours."""

    __slots__ = ("state", "faces", "full_cube", "occludes", "resolved")

    def __init__(self, state, faces, full_cube, occludes, resolved):
        self.state = state
        self.faces = faces
        self.full_cube = full_cube
        self.occludes = occludes
        self.resolved = resolved


def _rotate_variant(point, x_angle, y_angle, origin=(8.0, 8.0, 8.0)):
    """Apply a blockstate variant rotation about ``origin``.

    Minecraft's ``x``/``y`` rotate the model clockwise when looking down the
    positive axis, which is the opposite sense to ``rotate_point_java``; hence
    the negated angles. X is applied before Y, matching vanilla.

    Vertices rotate about the block centre; direction vectors must rotate about
    zero instead, or the translation part turns a unit normal into a point.
    """
    if x_angle:
        point = rotate_point_java(point, -x_angle, "x", origin)
    if y_angle:
        point = rotate_point_java(point, -y_angle, "y", origin)
    return point


def _rotate_face_name(face_name, x_angle, y_angle):
    """Follow a face through a variant rotation, e.g. north + y90 -> east."""
    if not x_angle and not y_angle:
        return face_name
    vector = FACE_DIRECTION_JAVA.get(face_name)
    if vector is None:
        return face_name
    rotated = _rotate_variant(vector, x_angle, y_angle, origin=(0.0, 0.0, 0.0))
    # Re-quantise: the angles are multiples of 90, so components land on 0/+-1.
    key = tuple(int(round(component)) for component in rotated)
    return _DIRECTION_TO_FACE.get(key, face_name)


def _is_full_cube_element(element):
    from_v = element.get("from", [0, 0, 0])
    to_v = element.get("to", [16, 16, 16])
    try:
        from_v = [float(v) for v in from_v[:3]]
        to_v = [float(v) for v in to_v[:3]]
    except (TypeError, ValueError):
        return False
    if any(abs(v) > 0.001 for v in from_v):
        return False
    if any(abs(v - 16.0) > 0.001 for v in to_v):
        return False
    faces = element.get("faces")
    if not isinstance(faces, dict):
        return False
    return all(name in faces for name in FACE_ORDER)


def _occludes(identifier, full_cube):
    if not full_cube:
        return False
    if identifier in NON_OCCLUDING_NAMES:
        return False
    _namespace, path = split_namespace(identifier)
    return not path.endswith(NON_OCCLUDING_SUFFIXES)


def find_model_path(model_ref, pack_roots, namespace):
    """First root that supplies this model reference, or ""."""
    return resolve_model_path(pack_roots, model_ref, namespace)


def merge_model_layered(model_ref, pack_roots, namespace, _seen=None):
    """Merge a model with its ``parent`` chain, resolving across every root.

    Each parent is resolved through the whole chain, letting an override sit on
    top of vanilla geometry. The direct Java importer uses the same resource
    lookup, while keeping the explicitly selected file as its entry point.
    """
    roots = as_roots(pack_roots)
    seen = _seen if _seen is not None else set()
    path = find_model_path(model_ref, roots, namespace)
    if not path:
        return {}
    key = os.path.normcase(os.path.abspath(path))
    if key in seen:
        return {}
    seen.add(key)

    try:
        data = load_json_file(path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    own_namespace = _namespace_from_model(path) or namespace
    parent_ref = data.get("parent", "")
    parent_data = {}
    if parent_ref and not str(parent_ref).startswith("builtin/"):
        parent_data = merge_model_layered(parent_ref, roots, own_namespace, seen)
        if not parent_data:
            return {}  # Missing/cyclic parents are not intentional empty models.

    merged = dict(parent_data)
    textures = dict(parent_data.get("textures", {})
                    if isinstance(parent_data.get("textures"), dict) else {})
    for texture_key, reference in (data.get("textures", {}) if isinstance(data.get("textures"), dict) else {}).items():
        reference = texture_slot_reference(reference)
        if reference and not reference.startswith("#") and ":" not in reference:
            reference = own_namespace + ":" + reference
        textures[texture_key] = reference
    if textures:
        merged["textures"] = textures
    for name, value in data.items():
        if name == "textures":
            continue
        if name == "elements" and isinstance(value, list):
            merged[name] = value
        elif name not in merged or name in {"display", "gui_light", "overrides", "parent"}:
            merged[name] = value
    return merged


class ModelCache:
    """Bakes and remembers one :class:`BakedModel` per block state string.

    ``pack_roots`` is an ordered search chain - the active pack first, vanilla
    underneath - so an incomplete pack contributes what it has and inherits the
    rest instead of failing to resolve at all.

    ``mirrored`` keeps a schematic world's mirrored axes
    (``java_to_mirrored_blender_point``); a block imported on its own bakes
    like a Java model import instead.
    """

    def __init__(
        self,
        pack_roots,
        interpolation="Closest",
        model_roots=None,
        texture_roots=None,
        colormaps=None,
        mirrored=True,
    ):
        self.pack_roots = as_roots(pack_roots)
        self.model_roots = as_roots(
            self.pack_roots if model_roots is None else model_roots
        )
        self.texture_roots = as_roots(
            self.pack_roots if texture_roots is None else texture_roots
        )
        self.interpolation = interpolation
        self.colormaps = colormaps
        self.mirrored = mirrored
        self._baked = {}
        self._merged = {}
        self.missing_states = set()
        self.missing_textures = set()

    @property
    def pack_root(self):
        """First root, for messages that name a single pack."""
        return self.pack_roots[0] if self.pack_roots else ""

    # -- model JSON ---------------------------------------------------------

    def _merged_model(self, model_ref, namespace):
        key = (namespace, model_ref)
        cached = self._merged.get(key)
        if cached is not None:
            return cached
        data = merge_model_layered(model_ref, self.model_roots, namespace)
        self._merged[key] = data
        return data

    def _texture(self, texture_ref, textures, namespace):
        path = resolve_texture_path(
            self.texture_roots, texture_ref, textures, namespace
        )
        if path:
            return path
        self.missing_textures.add(str(texture_ref))
        return ""

    # -- baking -------------------------------------------------------------

    def _bake_variant(self, variant, identifier, namespace, tint_color, faces_out):
        data = self._merged_model(variant["model"], namespace)
        elements = data.get("elements") if isinstance(data, dict) else None
        if not isinstance(elements, list) or not elements:
            return False
        textures = data.get("textures") if isinstance(data.get("textures"), dict) else {}
        x_angle = variant.get("x", 0.0)
        y_angle = variant.get("y", 0.0)
        uvlock = variant.get("uvlock", False)
        full_cube = False
        to_blender = java_to_mirrored_blender_point if self.mirrored else java_to_blender_point
        face_loops = _FACE_LOOP if self.mirrored else JAVA_FACE_INDICES

        for element in elements:
            if not isinstance(element, dict):
                continue
            from_v = element.get("from", [0, 0, 0])
            to_v = element.get("to", [16, 16, 16])
            if not (isinstance(from_v, (list, tuple)) and isinstance(to_v, (list, tuple))):
                continue
            if len(from_v) < 3 or len(to_v) < 3:
                continue
            from_v = [float(v) for v in from_v[:3]]
            to_v = [float(v) for v in to_v[:3]]
            element_faces = element.get("faces")
            if not isinstance(element_faces, dict):
                continue
            if _is_full_cube_element(element):
                full_cube = True

            corners = apply_element_rotation_java(
                element_vertices_java(from_v, to_v), element.get("rotation")
            )
            if x_angle or y_angle:
                corners = [_rotate_variant(point, x_angle, y_angle) for point in corners]
            blender_corners = [to_blender(point) for point in corners]

            for face_name in FACE_ORDER:
                face = element_faces.get(face_name)
                if not isinstance(face, dict) or not java_face_has_area(face_name, from_v, to_v):
                    continue
                verts = tuple(blender_corners[index] for index in face_loops[face_name])
                if uvlock and (x_angle or y_angle):
                    # Minecraft recomputes the rectangle and its rotation so
                    # the texture stays world-aligned after the variant turn.
                    face = uvlock_face(face, face_name, from_v, to_v, x_angle, y_angle)
                uvs = face_uv_corners(
                    face, face_name, JAVA_FACE_AXIS[face_name], from_v, to_v
                )
                if self.mirrored:
                    # Reverse alongside the loop so each UV stays on its own corner.
                    uvs = list(reversed(uvs))
                cullface = face.get("cullface")
                if isinstance(cullface, str) and cullface:
                    cullface = _rotate_face_name(cullface, x_angle, y_angle)
                else:
                    cullface = None
                try:
                    tint_index = int(face.get("tintindex", -1))
                except (TypeError, ValueError):
                    tint_index = -1
                faces_out.append(BakedFace(
                    verts=verts,
                    uvs=uvs,
                    texture=self._texture(face.get("texture", ""), textures, namespace),
                    cullface=cullface,
                    tint=tint_color if tint_index >= 0 else None,
                ))
        return full_cube

    def get(self, state):
        """Return the :class:`BakedModel` for a block state string."""
        baked = self._baked.get(state)
        if baked is not None:
            return baked

        identifier, _properties = parse_block_state(state)
        namespace, _path = split_namespace(identifier)
        variants = resolve_block_models(self.model_roots, state)
        tint_color = tint_color_for(identifier, self.colormaps)

        faces = []
        full_cube = False
        for variant in variants:
            if self._bake_variant(variant, identifier, namespace, tint_color, faces):
                full_cube = True

        resolved = bool(variants) and all(
            bool(self._merged_model(variant["model"], namespace))
            for variant in variants
        )
        if not resolved:
            self.missing_states.add(state)

        baked = BakedModel(
            state=state,
            faces=faces,
            full_cube=full_cube,
            occludes=_occludes(identifier, full_cube),
            resolved=resolved,
        )
        self._baked[state] = baked
        return baked


CHUNK_SIZE = 16


def build_chunk_arrays(world, chunk_key, chunk, cache, cull_faces, use_tint, stats):
    """Collect one chunk's visible faces into flat, chunk-local mesh arrays.

    Returns ``(verts, faces, uvs, face_keys)`` where ``face_keys`` are
    ``(texture_path, tint)`` pairs the caller maps onto material slots.
    """
    cx, cy, cz = chunk_key
    base_x, base_y, base_z = cx * CHUNK_SIZE, cy * CHUNK_SIZE, cz * CHUNK_SIZE
    verts = []
    faces = []
    uvs = []
    face_keys = []
    get_block = world.get_block
    get_model = cache.get

    for (lx, ly, lz), state in chunk.items():
        world_x, world_y, world_z = base_x + lx, base_y + ly, base_z + lz
        baked = get_model(state)
        model_faces = baked.faces
        if not model_faces:
            if baked.resolved:
                stats["empty_models"] += 1  # Fluids and other modelless blocks.
                continue
            model_faces = placeholder_faces()
            stats["placeholders"] += 1

        # Blender axes: Minecraft Y is up, so world (x, y, z) -> (x, z, y).
        offset_x = float(lx)
        offset_y = float(lz)
        offset_z = float(ly)

        for face in model_faces:
            cullface = face.cullface
            if cull_faces and cullface:
                dx, dy, dz = FACE_OFFSET[cullface]
                neighbour = get_block(world_x + dx, world_y + dy, world_z + dz)
                if neighbour is not None and get_model(neighbour).occludes:
                    stats["culled_faces"] += 1
                    continue
            base = len(verts)
            for vx, vy, vz in face.verts:
                verts.append((vx + offset_x, vy + offset_y, vz + offset_z))
            faces.append((base, base + 1, base + 2, base + 3))
            uvs.append(face.uvs)
            face_keys.append((face.texture, face.tint if use_tint else None))
        stats["blocks"] += 1

    return verts, faces, uvs, face_keys


def new_build_stats():
    return {
        "blocks": 0, "culled_faces": 0, "placeholders": 0, "empty_models": 0,
        "chunks": 0, "faces": 0, "vertices": 0,
    }


def banner_lines(content, width=72):
    """Wrap content in a hash box so it cannot be missed in the console."""
    width = max(width, max((len(line) for line in content), default=0) + 4)
    out = ["#" * width]
    for line in content:
        out.append("#  %s%s#" % (line, " " * (width - len(line) - 4)))
    out.append("#" * width)
    return out


def no_models_message(pack_roots, vanilla_note):
    """The message for 'this pack cannot describe block shapes at all'."""
    searched = as_roots(pack_roots) or ["(nothing - no valid asset root set)"]
    lines = [
        "CANNOT BUILD GEOMETRY - NO BLOCK MODELS FOUND",
        "",
        "Searched these asset roots, in order:",
    ]
    lines.extend("   %d. %s" % (i + 1, root) for i, root in enumerate(searched))
    lines.extend([
        "",
        "Vanilla lookup: %s" % (vanilla_note or "not attempted"),
        "",
        "'Bare Bones' and most texture packs override TEXTURES ONLY. They",
        "ship no blockstates/ or models/ folders, so they cannot describe",
        "block shapes on their own and nothing can be built from them.",
        "",
        "FIX - either of these works:",
        "  * Load a FULL extracted Vanilla Java resource pack, or",
        "  * Set 'Vanilla Assets' in the Schematic Importer panel to",
        r"    .minecraft\versions\<version>\<version>.jar",
        "",
        "The jar is unpacked once into the add-on cache and reused.",
        "Your texture pack still applies on top of it.",
    ])
    return lines


def unresolved_message(placeholders, missing_states):
    """The message for 'built, but some blocks are magenta placeholders'."""
    lines = [
        "%d BLOCKS COULD NOT BE RESOLVED - shown as magenta cubes" % placeholders,
        "",
        "%d block states were not found in any asset root." % len(missing_states),
        "This usually means the vanilla assets are older or newer than the",
        "schematic, or the resource pack is incomplete.",
        "",
        "Set 'Vanilla Assets' in the Schematic Importer panel to a Minecraft",
        "client .jar matching the schematic's Minecraft version.",
    ]
    if missing_states:
        lines.append("")
        lines.append("First unresolved states:")
        lines.extend("   %s" % state for state in list(missing_states)[:6])
    return lines


def placeholder_faces():
    """A plain unit cube, used when the pack cannot supply a model."""
    corners = [java_to_mirrored_blender_point(p) for p in element_vertices_java(
        [0.0, 0.0, 0.0], [16.0, 16.0, 16.0]
    )]
    faces = []
    for face_name in FACE_ORDER:
        faces.append(BakedFace(
            verts=tuple(corners[index] for index in _FACE_LOOP[face_name]),
            uvs=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
            texture="",
            cullface=face_name,
            tint=None,
        ))
    return faces


def pack_has_block_models(pack_roots):
    """True when any root in the chain carries a blockstates directory."""
    for pack_root in as_roots(pack_roots):
        assets = os.path.join(pack_root or "", "assets")
        if not os.path.isdir(assets):
            continue
        try:
            namespaces = os.listdir(assets)
        except OSError:
            continue
        for namespace in namespaces:
            if os.path.isdir(os.path.join(assets, namespace, "blockstates")):
                return True
    return False


# Blocks every complete Java pack defines; if none resolve, the chain is broken.
PROBE_STATES = (
    "minecraft:stone",
    "minecraft:dirt",
    "minecraft:oak_planks",
    "minecraft:cobblestone",
)


def probe_block_models(pack_roots):
    """Return the staple blocks that resolve to real geometry across the chain.

    Cheaper and far more honest than checking that a directory exists: a pack
    can ship an empty ``blockstates/`` folder, or one holding a single override.
    """
    roots = as_roots(pack_roots)
    if not roots:
        return []
    cache = ModelCache(roots)
    return [state for state in PROBE_STATES if cache.get(state).faces]

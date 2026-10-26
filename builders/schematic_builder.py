"""Turn a decoded schematic into textured Blender geometry.

One mesh object per 16x16x16 chunk, all parented to a single empty so the whole
build can be moved as a unit. Chunks come straight from the importer's spatial
hash, which keeps individual meshes small enough for the viewport to cull and
avoids one multi-million-vertex object.

Faces are dropped two ways: a model face carrying ``cullface`` is skipped when
the neighbouring block fills that side, and blocks the pack cannot resolve fall
back to a marked placeholder cube instead of silently vanishing.
"""

import os
import time

import bpy

from .bedrock_builder import _ensure_unique_collection
from .java_materials import _apply_tint, _make_java_material
from .schematic_geometry import (
    CHUNK_SIZE,
    ModelCache,
    PROBE_STATES,
    banner_lines,
    build_chunk_arrays,
    new_build_stats,
    no_models_message,
    probe_block_models,
    unresolved_message,
)
from .selection import deselect_all_objects

try:
    from ..importers.vanilla_assets import (
        addon_cache_dir,
        build_pack_roots,
        resolve_vanilla_root,
    )
except (ImportError, ValueError):  # pragma: no cover - plain Python test path
    from importers.vanilla_assets import (
        addon_cache_dir,
        build_pack_roots,
        resolve_vanilla_root,
    )



_LOG_PREFIX = "[Sparkle][Litematic]"
PLACEHOLDER_COLOR = (0.85, 0.15, 0.75, 1.0)


class SchematicBuildError(RuntimeError):
    """Raised when the scene is not set up well enough to build geometry."""


def _print_banner(content):
    """Print an unmissable boxed message to the system console."""
    print("")
    for line in banner_lines(content):
        print(line)
    print("")


def _no_models_banner(roots, vanilla_note):
    _print_banner(no_models_message(roots, vanilla_note))


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def _material_key(texture_path, tint):
    return (texture_path or "", tuple(round(c, 4) for c in tint) if tint else None)


def _pack_colormaps(roots):
    """Sample the pack's plains colors for schematics without biome data."""
    from ..importers.blockstate_resolver import resolve_texture_path
    colors = {}
    for kind in ('grass', 'foliage'):
        path = resolve_texture_path(roots, 'minecraft:colormap/' + kind)
        if not path:
            continue
        image = None
        try:
            # A private image avoids changing an already imported texture's settings.
            image = bpy.data.images.load(path, check_existing=False)
            image.colorspace_settings.name = 'Non-Color'
            width, height = image.size
            if width != 256 or height != 256:
                print('%s Ignoring invalid %s colormap: %s' % (_LOG_PREFIX, kind, path))
                continue
            # Plains: temperature .8, rainfall .4. PNG rows are top-down;
            # Blender's pixel buffer is bottom-up.
            x, y = int((1 - .8) * 255), int((1 - .8 * .4) * 255)
            pixel = ((height - 1 - y) * width + x) * image.channels
            colors[kind] = tuple(image.pixels[pixel:pixel + 3])
        except (OSError, RuntimeError, ValueError) as exc:
            print('%s Cannot read %s colormap %s: %s' % (_LOG_PREFIX, kind, path, exc))
        finally:
            if image is not None:
                bpy.data.images.remove(image)
    return colors


class MaterialCache:
    """One Blender material per (texture, tint) pair, shared across chunks."""

    def __init__(self, context, interpolation="Closest"):
        self.context = context
        self.interpolation = interpolation
        self._materials = {}

    def get(self, texture_path, tint):
        key = _material_key(texture_path, tint)
        material = self._materials.get(key)
        if material is not None:
            return material
        if texture_path:
            name = os.path.splitext(os.path.basename(texture_path))[0]
        else:
            name = "mc_missing_block"
        if tint:
            name = "%s_tinted" % name
        material = _make_java_material(
            name, texture_path, self.context, interpolation=self.interpolation
        )
        if not texture_path:
            material.diffuse_color = PLACEHOLDER_COLOR
            bsdf = material.node_tree.nodes.get("Principled BSDF")
            if bsdf and "Base Color" in bsdf.inputs:
                bsdf.inputs["Base Color"].default_value = PLACEHOLDER_COLOR
        elif tint:
            _apply_tint(material, tint)
        material["mcbedrock_source_format"] = "java_block"
        self._materials[key] = material
        return material


# ---------------------------------------------------------------------------
# Geometry assembly
# ---------------------------------------------------------------------------

def _make_chunk_object(context, name, chunk_key, verts, faces, uvs, face_keys,
                       materials, collection):
    mesh = bpy.data.meshes.new("%s_MeshData" % name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    uv_layer = mesh.uv_layers.new(name="UVMap")
    uv_data = uv_layer.data
    for polygon_index, polygon in enumerate(mesh.polygons):
        face_uv = uvs[polygon_index] if polygon_index < len(uvs) else [(0.0, 0.0)] * 4
        for corner, loop_index in enumerate(polygon.loop_indices):
            uv_data[loop_index].uv = face_uv[corner]

    # Only bind the slots this chunk actually uses; a global slot list would put
    # hundreds of unused materials on every object.
    slot_index = {}
    for key in face_keys:
        if key not in slot_index:
            slot_index[key] = len(slot_index)
            mesh.materials.append(materials.get(key[0], key[1]))
    for polygon_index, polygon in enumerate(mesh.polygons):
        if polygon_index < len(face_keys):
            polygon.material_index = slot_index[face_keys[polygon_index]]

    obj = bpy.data.objects.new(name, mesh)
    cx, cy, cz = chunk_key
    obj.location = (cx * CHUNK_SIZE, cz * CHUNK_SIZE, cy * CHUNK_SIZE)
    collection.objects.link(obj)
    obj["mcbedrock_asset_type"] = "java_schematic_chunk"
    obj["mcbedrock_chunk"] = "%d,%d,%d" % chunk_key
    return obj


def _clean_name(name):
    raw = str(name or "Schematic").strip()
    for char in "\\/:*?\"<>|":
        raw = raw.replace(char, "_")
    return " ".join(raw.split()) or "Schematic"


def _orient_blueprint(collection, root):
    """Use (x, -z, y) for Axiom without a mirrored root transform.

    The shared legacy builder swaps Y/Z, which reflects the entire blueprint.
    Convert every newly built datablock and local transform together so display
    entities, chunk offsets, UV corners and rig rest poses stay aligned.
    """
    from mathutils import Matrix
    conversion = Matrix.Diagonal((1, -1, 1, 1))
    converted = {}
    for obj in collection.objects:
        if obj == root:
            continue
        obj.matrix_basis = conversion @ obj.matrix_basis @ conversion
        obj.matrix_parent_inverse = conversion @ obj.matrix_parent_inverse @ conversion
        if obj.type in {'MESH', 'ARMATURE'}:
            original = obj.data
            data = converted.get(original)
            if data is None:
                data = original.copy()
                data.transform(conversion)
                if obj.type == 'MESH':
                    data.flip_normals()
                    data.update()
                converted[original] = data
            obj.data = data
    for original in converted:
        if original.users == 0:
            if isinstance(original, bpy.types.Mesh):
                bpy.data.meshes.remove(original)
            else:
                bpy.data.armatures.remove(original)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def resolve_asset_roots(pack_root, vanilla_path=""):
    """Build the ordered asset search chain: active packs, then Vanilla.

    Returns ``(roots, vanilla_note)``. Missing vanilla assets are not fatal here
    - a complete pack alone is enough - so the caller decides how to react.
    """
    if isinstance(pack_root, (str, bytes, os.PathLike)):
        pack_roots = [pack_root] if pack_root else []
    else:
        pack_roots = list(pack_root or ())
    if bpy:
        pack_roots = [bpy.path.abspath(root) for root in pack_roots if root]
    vanilla_path = bpy.path.abspath(vanilla_path or "") if bpy else (vanilla_path or "")
    try:
        vanilla_root, note = resolve_vanilla_root(vanilla_path, addon_cache_dir())
    except Exception as exc:  # A broken jar must not sink an otherwise fine pack.
        vanilla_root, note = "", "vanilla lookup failed: %s" % exc
    return build_pack_roots(pack_roots, vanilla_root), note


def build_schematic(context, world, pack_root, cull_faces=True, use_tint=True,
                    interpolation="Closest", vanilla_path=""):
    """Build every chunk of ``world`` as textured meshes. Returns a stats dict."""
    if world is None or not (world.chunks or any(r.entities for r in world.regions)):
        raise SchematicBuildError("No blocks or entities loaded - import a schematic first")

    roots, vanilla_note = resolve_asset_roots(pack_root, vanilla_path)
    if not roots:
        _no_models_banner(roots, vanilla_note)
        raise SchematicBuildError(
            "No usable asset root. Set a Java resource pack, or point "
            "'Vanilla Assets' at a Minecraft client .jar. See the console."
        )

    print("%s Asset roots (first match wins):" % _LOG_PREFIX)
    for index, root in enumerate(roots):
        print("%s   %d. %s" % (_LOG_PREFIX, index + 1, root))
    print("%s Vanilla: %s" % (_LOG_PREFIX, vanilla_note))

    resolved_probes = probe_block_models(roots)
    if not resolved_probes:
        # Probes are diagnostic only: custom-only packs and display-only files
        # need not contain stone, grass, dirt or oak planks.
        print("%s No standard probe blocks; resolving the schematic's own assets" % _LOG_PREFIX)

    started = time.time()
    name = _clean_name(world.name)
    collection = _ensure_unique_collection(context, name)
    model_cache = ModelCache(roots, interpolation=interpolation, colormaps=_pack_colormaps(roots))
    materials = MaterialCache(context, interpolation=interpolation)
    stats = new_build_stats()

    root = bpy.data.objects.new("%s_Root" % name, None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 2.0
    collection.objects.link(root)
    root["mcbedrock_asset_type"] = "java_schematic"
    root["mcbedrock_source_path"] = world.filepath

    total_chunks = len(world.chunks)
    window_manager = context.window_manager
    window_manager.progress_begin(0, max(1, total_chunks))
    print("%s Building %d chunks (%d/%d probe blocks resolved)"
          % (_LOG_PREFIX, total_chunks, len(resolved_probes), len(PROBE_STATES)))

    try:
        for index, chunk_key in enumerate(sorted(world.chunks)):
            window_manager.progress_update(index)
            verts, faces, uvs, face_keys = build_chunk_arrays(
                world, chunk_key, world.chunks[chunk_key], model_cache,
                cull_faces, use_tint, stats,
            )
            if not faces:
                continue
            obj = _make_chunk_object(
                context, "%s_chunk_%d_%d_%d" % ((name,) + chunk_key), chunk_key,
                verts, faces, uvs, face_keys, materials, collection,
            )
            obj.parent = root
            obj.matrix_parent_inverse = root.matrix_world.inverted()
            stats["chunks"] += 1
            stats["faces"] += len(faces)
            stats["vertices"] += len(verts)
            if total_chunks > 20 and index % max(1, total_chunks // 10) == 0:
                print("%s   %d/%d chunks" % (_LOG_PREFIX, index, total_chunks))
    finally:
        window_manager.progress_end()

    from .schematic_entities import EntityBuilder
    stats.update(EntityBuilder(context, roots, collection, root, model_cache,
                               materials, interpolation).build(world))
    if os.path.splitext(world.filepath)[1].lower() == '.bp':
        _orient_blueprint(collection, root)
    stats["seconds"] = time.time() - started
    stats["missing_states"] = sorted(model_cache.missing_states)
    stats["missing_textures"] = sorted(model_cache.missing_textures)
    stats["collection"] = collection.name
    stats["root"] = root
    stats["asset_roots"] = list(roots)
    stats["vanilla_note"] = vanilla_note
    stats["probes_resolved"] = len(resolved_probes)

    deselect_all_objects(context)
    root.select_set(True)
    context.view_layer.objects.active = root
    return stats


class _SingleBlockWorld:
    """Minimal world adapter for the shared chunk geometry pipeline."""

    def __init__(self, state):
        self.state = state
        self.name = state.split(":", 1)[-1]
        self.filepath = ""
        self.chunks = {(0, 0, 0): {(0, 0, 0): state}}

    def get_block(self, x, y, z):
        return self.state if (x, y, z) == (0, 0, 0) else None


def build_single_block(
    context,
    state,
    pack_root,
    vanilla_path="",
    interpolation="Closest",
    vanilla_geometry=False,
):
    """Build one Java block with independently selected geometry/textures.

    Asset-browser cards synthesized from texture overrides set
    ``vanilla_geometry`` so pack-authored replacement models cannot leak into a
    texture-only import. The active pack still has first priority for textures.
    """
    roots, vanilla_note = resolve_asset_roots(pack_root, vanilla_path)
    if not roots:
        raise SchematicBuildError(
            "No usable Java asset root. Set an active pack and vanilla assets."
        )

    world = _SingleBlockWorld(state)
    name = _clean_name(state.split(":", 1)[-1])
    collection = _ensure_unique_collection(context, name)
    model_roots = roots
    if vanilla_geometry:
        if isinstance(pack_root, (str, bytes, os.PathLike)):
            active_roots = [pack_root] if pack_root else []
        else:
            active_roots = list(pack_root or ())
        active_keys = {
            os.path.normcase(os.path.abspath(root))
            for root in active_roots if root
        }
        model_roots = [
            root for root in roots
            if os.path.normcase(os.path.abspath(root)) not in active_keys
        ]
        if not model_roots:
            raise SchematicBuildError(
                "Texture-only block import requires vanilla model assets"
            )
    model_cache = ModelCache(
        roots,
        interpolation=interpolation,
        model_roots=model_roots,
        texture_roots=roots,
        colormaps=_pack_colormaps(roots),
        # One block is an import, not a world: match Java model imports.
        mirrored=False,
    )
    materials = MaterialCache(context, interpolation=interpolation)
    stats = new_build_stats()
    verts, faces, uvs, face_keys = build_chunk_arrays(
        world,
        (0, 0, 0),
        world.chunks[(0, 0, 0)],
        model_cache,
        False,
        True,
        stats,
    )
    if not faces:
        raise SchematicBuildError(
            "The selected block resolved to no renderable model faces."
        )

    root = bpy.data.objects.new("%s_Root" % name, None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 1.25
    collection.objects.link(root)
    root["mcbedrock_asset_type"] = "java_block"
    root["mcbedrock_block_state"] = state
    root["mcbedrock_pack_root"] = roots[0] if roots else ""
    root["mcbedrock_geometry_policy"] = (
        "VANILLA" if vanilla_geometry else "LAYERED"
    )

    obj = _make_chunk_object(
        context,
        "%s_Block" % name,
        (0, 0, 0),
        verts,
        faces,
        uvs,
        face_keys,
        materials,
        collection,
    )
    obj["mcbedrock_asset_type"] = "java_block_mesh"
    obj["mcbedrock_block_state"] = state
    obj.parent = root
    obj.matrix_parent_inverse = root.matrix_world.inverted()

    stats.update({
        "chunks": 1,
        "faces": len(faces),
        "vertices": len(verts),
        "collection": collection.name,
        "root": root,
        "asset_roots": list(roots),
        "vanilla_note": vanilla_note,
        "missing_states": sorted(model_cache.missing_states),
        "missing_textures": sorted(model_cache.missing_textures),
    })
    deselect_all_objects(context)
    root.select_set(True)
    context.view_layer.objects.active = root
    return stats


def print_build_summary(stats):
    print("=" * 68)
    print("%s Build complete in %.2fs" % (_LOG_PREFIX, stats.get("seconds", 0.0)))
    for index, root in enumerate(stats.get("asset_roots") or []):
        print("  Asset root %d      : %s" % (index + 1, root))
    print("  Vanilla source    : %s" % stats.get("vanilla_note", ""))
    print("  Collection        : %s" % stats.get("collection", ""))
    print("  Chunk objects     : %d" % stats.get("chunks", 0))
    print("  Blocks built      : %d" % stats.get("blocks", 0))
    print("  Faces             : %d" % stats.get("faces", 0))
    print("  Vertices          : %d" % stats.get("vertices", 0))
    print("  Faces culled      : %d" % stats.get("culled_faces", 0))
    print("  Placeholder blocks: %d" % stats.get("placeholders", 0))
    print("  Modelless blocks  : %d (fluids, air-likes)" % stats.get("empty_models", 0))
    print("  Entities imported : %d" % stats.get("entities", 0))
    print("  Entity markers    : %d" % stats.get("entity_markers", 0))
    for warning in stats.get("entity_warnings", [])[:20]:
        print("  Entity warning    : %s" % warning)
    missing_states = stats.get("missing_states") or []
    if missing_states:
        print("  Unresolved states : %d" % len(missing_states))
        for state in missing_states[:25]:
            print("      %s" % state)
        if len(missing_states) > 25:
            print("      ... and %d more" % (len(missing_states) - 25))
    missing_textures = stats.get("missing_textures") or []
    if missing_textures:
        print("  Missing textures  : %d" % len(missing_textures))
        for texture in missing_textures[:15]:
            print("      %s" % texture)
    print("=" * 68)

    # Partial resolution still builds, but the magenta cubes need explaining.
    placeholders = stats.get("placeholders", 0)
    if placeholders:
        _print_banner(unresolved_message(
            placeholders, stats.get("missing_states") or []
        ))

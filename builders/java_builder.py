import os

import bpy

from .bedrock_builder import _ensure_unique_collection
from .selection import deselect_all_objects
from .java_geometry import (
    JAVA_COPLANAR_LIFT,
    apply_element_rotation_java,
    build_extruded_sprite_mesh,
    element_lift_ranks,
    element_seen_from_behind,
    element_vertices_java,
    java_blender_face,
    java_face_has_area,
    java_to_blender_point,
    java_to_mirrored_blender_point,
    lift_element_bounds,
)
from .java_resolver import (
    _find_pack_root,
    _model_id_from_path,
    _merge_java_model,
    _namespace_from_model,
    _resolve_texture_path,
    resolve_builtin_entity_texture,
)
from .java_materials import (
    _apply_tint,
    _first_resolved_java_texture,
    _make_java_material,
)
from .java_tints import grass_colormap_pixel, model_tint_colors, server_tinted_indices

# Minecraft stores element faces in an EnumMap, so they bake in Direction
# order. With coplanar faces the later one draws on top.
JAVA_FACE_ORDER = ("down", "up", "north", "south", "west", "east")

# Vanilla block/cross.json, for cross-template models whose parent is missing.
_CROSS_ELEMENTS = (
    {
        "from": [0.8, 0, 8], "to": [15.2, 16, 8],
        "rotation": {"origin": [8, 8, 8], "axis": "y", "angle": 45, "rescale": True},
        "faces": {
            "north": {"uv": [0, 0, 16, 16], "texture": "#cross"},
            "south": {"uv": [0, 0, 16, 16], "texture": "#cross"},
        },
    },
    {
        "from": [8, 0, 0.8], "to": [8, 16, 15.2],
        "rotation": {"origin": [8, 8, 8], "axis": "y", "angle": 45, "rescale": True},
        "faces": {
            "west": {"uv": [0, 0, 16, 16], "texture": "#cross"},
            "east": {"uv": [0, 0, 16, 16], "texture": "#cross"},
        },
    },
)


_GRASS_COLORMAPS = {}


def _grass_colormap(roots):
    """Sampler for the stack's colormap/grass.png (GrassColor.get), or None."""
    from ..importers.blockstate_resolver import resolve_texture_path

    path = resolve_texture_path(roots, "minecraft:colormap/grass")
    if not path:
        return None
    if path in _GRASS_COLORMAPS:
        return _GRASS_COLORMAPS[path]
    sampler = None
    image = None
    try:
        import numpy

        # A private copy keeps an already imported texture's settings intact.
        image = bpy.data.images.load(path, check_existing=False)
        image.colorspace_settings.name = "Non-Color"
        width, height = int(image.size[0]), int(image.size[1])
        channels = int(image.channels)
        pixels = numpy.empty(width * height * channels, dtype=numpy.float32)
        image.pixels.foreach_get(pixels)
        pixels = pixels.reshape(height, width, channels)

        def sampler(temperature, downfall):
            column, row = grass_colormap_pixel(temperature, downfall)
            column = min(width - 1, max(0, column))
            row = min(height - 1, max(0, row))
            red, green, blue = pixels[height - 1 - row, column, :3]
            return (float(red), float(green), float(blue))
    except (ImportError, RuntimeError, ValueError):
        sampler = None
    finally:
        if image is not None:
            bpy.data.images.remove(image)
    _GRASS_COLORMAPS[path] = sampler
    return sampler


TINT_ATTRIBUTE = "mc_tint_index"


def _model_tints(model_id, roots):
    """Default colour per ``tintindex`` from item definitions (may be empty)."""
    return model_tint_colors(model_id, roots, _grass_colormap(roots))


def _block_tint_fallback(model_id):
    """Block colour for a tinted block model no item definition covers."""
    path = str(model_id).partition(":")[2] or str(model_id)
    if not path.startswith("block/"):
        return None
    from .schematic_geometry import tint_color_for

    return tint_color_for(path.rsplit("/", 1)[-1], default=None)


def _generated_model_kind(data):
    """Pick the fallback for a model without elements from its parent.

    Without elements Minecraft renders ``builtin/generated`` items as an
    extruded sprite. A cross-template block whose parent could not be
    loaded gets the vanilla cross shape instead.
    """
    parent = str(data.get("parent", "") if isinstance(data, dict) else "").lower()
    if "cross" in parent.replace("\\", "/").rsplit("/", 1)[-1]:
        return "cross"
    return "sprite"


def _image_alpha_grid(image, frame_height):
    width = int(image.size[0])
    total_height = int(image.size[1])
    frame_height = max(1, min(int(frame_height), total_height))
    channels = max(1, int(getattr(image, "channels", 4) or 4))
    if channels < 4:
        return [[True for _column in range(width)] for _row in range(frame_height)]
    import numpy

    pixels = numpy.empty(width * total_height * channels, dtype=numpy.float32)
    image.pixels.foreach_get(pixels)
    alpha = pixels.reshape(total_height, width, channels)[..., 3] > 0.01
    # Blender stores rows bottom-up; the top frame is the sprite's first frame.
    return [
        alpha[total_height - 1 - row].tolist()
        for row in range(frame_height)
    ]


def _generated_layer_paths(textures, pack_root, namespace):
    """Resolved ``layer0``..``layerN`` textures in item/generated draw order."""
    layers = sorted(
        (int(key[5:]), key) for key in textures
        if key.startswith("layer") and key[5:].isdigit()
    )
    paths = []
    for _index, key in layers:
        path = _resolve_texture_path(textures[key], textures, pack_root, namespace)
        if path:
            paths.append(path)
    return paths


def _composited_layer_image(paths, name, tints=(), runtime=()):
    """Stack every item layer into one packed image, like item/generated.

    Layer ``i`` is multiplied by tint ``i`` (leather, potions, spawn eggs) and
    later layers draw over earlier ones (armor trims, potion bottles). Returns
    ``(image, share)``: ``share`` is a gray image of how much of each pixel
    comes from the ``runtime`` layers a server colours (None without any).
    ``image`` is None when there is nothing to stack or tint, a layer
    animates, or a layer's size does not scale evenly onto the largest one.
    """
    tints = list(tints or ())[:len(paths)]
    runtime = {index for index in runtime if index < len(paths)}
    if len(paths) < 2 and not any(tints) and not runtime:
        return None, None
    import numpy

    frames = []
    for index, path in enumerate(paths):
        if os.path.isfile(f"{path}.mcmeta"):
            return None, None
        try:
            image = bpy.data.images.load(path, check_existing=True)
        except RuntimeError:
            return None, None
        width, height = int(image.size[0]), int(image.size[1])
        if width <= 0 or height <= 0:
            return None, None
        pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
        image.pixels.foreach_get(pixels)
        frame = pixels.reshape(height, width, 4)
        tint = tints[index] if index < len(tints) else None
        if tint:
            # Item tints multiply the stored (sRGB) texel, as the game does.
            frame = frame.copy()
            frame[..., :3] *= numpy.asarray(tint[:3], dtype=numpy.float32)
        frames.append(frame)
    width = max(frame.shape[1] for frame in frames)
    height = max(frame.shape[0] for frame in frames)
    premultiplied = numpy.zeros((height, width, 3), dtype=numpy.float32)
    coverage = numpy.zeros((height, width, 1), dtype=numpy.float32)
    tinted = numpy.zeros((height, width, 1), dtype=numpy.float32)
    for index, frame in enumerate(frames):
        if width % frame.shape[1] or height % frame.shape[0]:
            return None, None
        frame = numpy.repeat(frame, height // frame.shape[0], axis=0)
        frame = numpy.repeat(frame, width // frame.shape[1], axis=1)
        alpha = frame[..., 3:4]
        premultiplied = frame[..., :3] * alpha + premultiplied * (1.0 - alpha)
        tinted = (alpha if index in runtime else 0.0) + tinted * (1.0 - alpha)
        coverage = alpha + coverage * (1.0 - alpha)
    colour = numpy.divide(
        premultiplied, coverage,
        out=numpy.zeros_like(premultiplied), where=coverage > 0.0,
    )
    result = numpy.concatenate((colour, coverage), axis=2)
    image = bpy.data.images.new(f"{name}_layers", width, height, alpha=True)
    image.pixels.foreach_set(result.ravel())
    image.alpha_mode = "STRAIGHT"
    image.pack()
    share = None
    if runtime:
        fraction = numpy.divide(tinted, coverage, out=numpy.zeros_like(tinted), where=coverage > 0.0)
        share = bpy.data.images.new(f"{name}_tint_share", width, height, alpha=False)
        share.colorspace_settings.name = "Non-Color"
        share.pixels.foreach_set(numpy.concatenate((fraction, fraction, fraction, numpy.ones_like(fraction)), axis=2).ravel())
        share.pack()
    return image, share


def _tint_by_share(material, share):
    """Drive the tint's strength by ``share`` (1 where a pixel is all dye)."""
    from .java_materials import TINT_NODE
    nodes = material.node_tree.nodes
    mix = nodes.get(TINT_NODE)
    image_node = next((n for n in nodes if n.type == "TEX_IMAGE" and n.image), None)
    if mix is None or image_node is None:
        return
    node = nodes.new("ShaderNodeTexImage")
    node.name = node.label = "Minecraft Tint Share"
    node.image = share
    node.interpolation = image_node.interpolation
    node.location = (image_node.location.x, image_node.location.y - 280)
    if image_node.inputs["Vector"].links:
        material.node_tree.links.new(image_node.inputs["Vector"].links[0].from_socket, node.inputs["Vector"])
    factor = mix.inputs["Factor"] if "Factor" in mix.inputs else mix.inputs[0]
    material.node_tree.links.new(node.outputs["Color"], factor)


def _build_generated_java_model(context, filepath, data, pack_root, namespace, model_id, interpolation="Closest", display_name=""):
    textures = data.get("textures", {}) if isinstance(data.get("textures"), dict) else {}
    layer_paths = _generated_layer_paths(textures, pack_root, namespace)
    texture_path = layer_paths[0] if layer_paths else _first_resolved_java_texture(textures, pack_root, namespace)
    if not texture_path:
        texture_path = resolve_builtin_entity_texture(data, filepath, pack_root, namespace)
    name = _clean_import_name(display_name, os.path.splitext(os.path.basename(filepath))[0])
    group_col = _ensure_unique_collection(context, name)

    width = 1.0
    height = 1.0
    runtime = server_tinted_indices(model_id, pack_root) or set()
    layer_tints = list(_model_tints(model_id, pack_root)) if layer_paths else []
    # A layer a server colours keeps its default on the material's tint node,
    # not baked into the image, so a new tint replaces it.
    runtime_tint = next((layer_tints[i] for i in sorted(runtime) if i < len(layer_tints) and layer_tints[i]), None)
    composite, share = _composited_layer_image(
        layer_paths, name, [None if i in runtime else tint for i, tint in enumerate(layer_tints)], runtime,
    ) if layer_paths else (None, None)
    image = composite
    frame_height = 16
    if image is None and texture_path:
        try:
            image = bpy.data.images.load(texture_path, check_existing=True)
        except Exception:
            image = None
    if image and image.size and image.size[0] > 0 and image.size[1] > 0:
        frame_height = min(int(image.size[0]), int(image.size[1]))

    alpha_grid = _image_alpha_grid(image, frame_height) if image else []
    verts, faces, loop_uvs = build_extruded_sprite_mesh(
        alpha_grid,
        width=width,
        height=height,
        depth=0.0625,
    )
    if not faces:
        # No readable texture: one upright quad facing the front view.
        half_w = width / 2.0
        verts = [(-half_w, 0.0, 0.0), (half_w, 0.0, 0.0), (half_w, 0.0, height), (-half_w, 0.0, height)]
        faces = [(0, 1, 2, 3)]
        loop_uvs = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]]

    mesh_data = bpy.data.meshes.new(f"{name}_MeshData")
    mesh_data.from_pydata(verts, [], faces)
    mesh_data.update()
    uv_layer = mesh_data.uv_layers.new(name="UVMap")
    for poly_index, polygon in enumerate(mesh_data.polygons):
        face_uv = loop_uvs[poly_index] if poly_index < len(loop_uvs) else [(0, 0)] * 4
        for i, loop_index in enumerate(polygon.loop_indices):
            uv_layer.data[loop_index].uv = face_uv[i]

    mesh_obj = bpy.data.objects.new(f"{name}_Mesh", mesh_data)
    context.collection.objects.link(mesh_obj)
    group_col.objects.link(mesh_obj)
    for col in list(mesh_obj.users_collection):
        if col != group_col:
            col.objects.unlink(mesh_obj)

    mat_name = os.path.splitext(os.path.basename(texture_path))[0] if texture_path else name
    material = _make_java_material(
        mat_name, texture_path, context, interpolation=interpolation, image=composite,
    )
    if share is not None:
        # The dye layer a server colours: tint only its share of each pixel.
        _apply_tint(material, runtime_tint or (1.0, 1.0, 1.0))
        _tint_by_share(material, share)
        material[TINT_ATTRIBUTE] = min(runtime)
    mesh_obj.data.materials.append(material)
    mesh_obj["mcbedrock_asset_type"] = "java_model"
    mesh_obj["mcbedrock_identifier"] = model_id
    mesh_obj["mcbedrock_source_path"] = filepath
    mesh_obj["mcbedrock_generated_fallback"] = True
    mesh_obj["mcbedrock_item_layers"] = len(layer_paths)
    if display_name:
        mesh_obj["mcbedrock_display_name"] = display_name
    deselect_all_objects(context)
    mesh_obj.select_set(True)
    context.view_layer.objects.active = mesh_obj
    return {"mesh": mesh_obj, "collection": group_col, "model_id": model_id}



def _clean_import_name(name, fallback):
    raw = str(name or "").strip()
    if not raw:
        raw = str(fallback or "minecraft_asset").strip()
    raw = raw.replace("\\", "_").replace("/", "_").replace(":", "_")
    raw = raw.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    raw = " ".join(raw.split())
    return raw or "minecraft_asset"


def build_java_model_from_file(context, filepath, interpolation="Closest", display_name="",
                               resource_roots=None, vanilla_path=None, mirrored=False):
    """Build a Java block/item model as one mesh.

    ``mirrored`` bakes in the schematic world's mirrored axes, for models
    placed inside a Litematic/WorldEdit build; imports keep the game's
    handedness.
    """
    from ..importers.blockstate_resolver import as_roots
    from ..importers.bedrock_assets import asset_resource_roots
    from .schematic_builder import resolve_asset_roots
    if resource_roots is None:
        resource_roots = asset_resource_roots(context, filepath)
    roots = as_roots(resource_roots)
    roots.append(_find_pack_root(filepath))
    if vanilla_path is None:
        vanilla_path = getattr(context.scene, "mcbedrock_vanilla_assets_path", "")
    pack_root, _note = resolve_asset_roots(roots, vanilla_path)
    model_id = _model_id_from_path(filepath)
    data = _merge_java_model(filepath, pack_root)
    textures = data.get("textures", {}) if isinstance(data.get("textures"), dict) else {}
    namespace = _namespace_from_model(filepath)
    elements = data.get("elements", [])
    if (not isinstance(elements, list) or not elements) and _generated_model_kind(data) == "cross":
        elements = [dict(element) for element in _CROSS_ELEMENTS]
        if "cross" not in textures:
            fallback = next((
                textures[key] for key in ("cross", "plant", "layer0", "texture", "all", "particle")
                if isinstance(textures.get(key), str) and textures[key]
            ), next((value for value in textures.values() if isinstance(value, str)), ""))
            textures = dict(textures, cross=fallback)
    if not isinstance(elements, list) or not elements:
        return _build_generated_java_model(context, filepath, data, pack_root, namespace, model_id, interpolation=interpolation, display_name=display_name)

    name = _clean_import_name(display_name, os.path.splitext(os.path.basename(filepath))[0])
    group_col = _ensure_unique_collection(context, name)

    verts = []
    faces = []
    loop_uvs = []
    material_keys = []
    face_tints = []
    resolved_textures = {}
    tint_colors = _model_tints(model_id, pack_root)
    block_tint = _block_tint_fallback(model_id)
    runtime_tints = server_tinted_indices(model_id, pack_root)

    to_blender = java_to_mirrored_blender_point if mirrored else java_to_blender_point

    # Elements bake in file order. Blockbench "groups" are editor metadata:
    # the game ignores them (and their rotations), so geometry does too.
    baked = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        from_v = element.get("from", [0, 0, 0])
        to_v = element.get("to", [16, 16, 16])
        if not (isinstance(from_v, list) and isinstance(to_v, list) and len(from_v) >= 3 and len(to_v) >= 3):
            continue
        try:
            from_v = [float(v) for v in from_v[:3]]
            to_v = [float(v) for v in to_v[:3]]
        except (TypeError, ValueError):
            continue
        faces_data = element.get("faces", {}) if isinstance(element.get("faces"), dict) else {}
        element_faces = [
            (face_name, faces_data[face_name]) for face_name in JAVA_FACE_ORDER
            if isinstance(faces_data.get(face_name), dict)
            and java_face_has_area(face_name, from_v, to_v)
        ]
        # Rotate in Java space first (correct handedness), then convert to Blender.
        corners = [
            to_blender(p) for p in apply_element_rotation_java(
                element_vertices_java(from_v, to_v), element.get("rotation"))
        ]
        loops = [java_blender_face(face_name, face, from_v, to_v, mirrored)[0]
                 for face_name, face in element_faces]
        baked.append((element, from_v, to_v, element_faces, corners, loops))

    # Minecraft draws coplanar quads in model order and the later one wins.
    # A later element that overlays an earlier face grows a hair along its
    # own normals instead, so it stays one closed box with no cracks.
    ranks = element_lift_ranks([
        (corners, loops, element_seen_from_behind(from_v, to_v))
        for _element, from_v, to_v, _faces, corners, loops in baked
    ])
    for (element, from_v, to_v, element_faces, corners, _loops), rank in zip(baked, ranks):
        if rank:
            lifted_from, lifted_to = lift_element_bounds(from_v, to_v, JAVA_COPLANAR_LIFT * rank)
            corners = [
                to_blender(p) for p in apply_element_rotation_java(
                    element_vertices_java(lifted_from, lifted_to), element.get("rotation"))
            ]
        base = len(verts)
        verts.extend(corners)
        for face_name, face in element_faces:
            # UVs come from the authored bounds; only the geometry is lifted.
            indices, uvs = java_blender_face(face_name, face, from_v, to_v, mirrored)
            faces.append(tuple(base + idx for idx in indices))
            loop_uvs.append(uvs)
            texture_ref = str(face.get("texture", "") or "")
            if texture_ref not in resolved_textures:
                resolved_textures[texture_ref] = _resolve_texture_path(texture_ref, textures, pack_root, namespace)
            try:
                tint_index = int(face.get("tintindex", -1))
            except (TypeError, ValueError):
                tint_index = -1
            tint = None
            if tint_index >= 0:
                tint = (
                    tint_colors[tint_index]
                    if tint_index < len(tint_colors) and tint_colors[tint_index]
                    else block_tint
                )
            # A slot the server colours at runtime gets its own material even
            # without a known colour, so Posterize can tint just those faces.
            runtime = tint_index >= 0 and (runtime_tints is None or tint_index in runtime_tints)
            material_keys.append((resolved_textures[texture_ref], tuple(tint) if tint else None,
                                  tint_index if runtime else -1))
            face_tints.append(tint_index if runtime else -1)

    if not verts or not faces:
        raise ValueError("Java model did not contain importable faces")

    mesh_data = bpy.data.meshes.new(f"{name}_MeshData")
    mesh_data.from_pydata(verts, [], faces)
    mesh_data.update()
    uv_layer = mesh_data.uv_layers.new(name="UVMap")
    for poly_index, polygon in enumerate(mesh_data.polygons):
        face_uv = loop_uvs[poly_index] if poly_index < len(loop_uvs) else [(0, 0)] * 4
        for i, loop_index in enumerate(polygon.loop_indices):
            uv_layer.data[loop_index].uv = face_uv[i]

    mesh_obj = bpy.data.objects.new(f"{name}_Mesh", mesh_data)
    context.collection.objects.link(mesh_obj)
    group_col.objects.link(mesh_obj)
    for col in list(mesh_obj.users_collection):
        if col != group_col:
            col.objects.unlink(mesh_obj)

    # One material per (texture, tint): tinted overlays such as grass sides
    # must not recolour the untinted faces that share their texture.
    unique_keys = list(dict.fromkeys(material_keys)) or [("", None, -1)]
    for path, tint, runtime in unique_keys:
        mat_name = os.path.splitext(os.path.basename(path))[0] if path else name
        if tint or runtime >= 0:
            mat_name = f"{mat_name}_tinted"
        material = _make_java_material(mat_name, path, context, interpolation=interpolation)
        if path and (tint or runtime >= 0):
            _apply_tint(material, tint or (1.0, 1.0, 1.0))
        if runtime >= 0:
            material[TINT_ATTRIBUTE] = runtime
        mesh_obj.data.materials.append(material)
    slots = {key: index for index, key in enumerate(unique_keys)}
    for idx, poly in enumerate(mesh_obj.data.polygons):
        key = material_keys[idx] if idx < len(material_keys) else ("", None, -1)
        poly.material_index = slots.get(key, 0)
    if any(index >= 0 for index in face_tints):
        # Which faces a server colours at runtime (Posterize's Server tint).
        attribute = mesh_obj.data.attributes.new(TINT_ATTRIBUTE, "INT", "FACE")
        attribute.data.foreach_set("value", face_tints)

    mesh_obj["mcbedrock_asset_type"] = "java_model"
    mesh_obj["mcbedrock_identifier"] = model_id
    mesh_obj["mcbedrock_source_path"] = filepath
    if display_name:
        mesh_obj["mcbedrock_display_name"] = display_name
    deselect_all_objects(context)
    mesh_obj.select_set(True)
    context.view_layer.objects.active = mesh_obj
    return {"mesh": mesh_obj, "collection": group_col, "model_id": model_id}

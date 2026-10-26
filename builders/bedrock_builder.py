import math
import os

import bpy
from mathutils import Euler, Vector

from ..importers.bedrock_assets import safe_name

# Pure UV helper re-exported for callers that build cube faces.
from .bedrock_uv import build_cube_faces

# Bedrock model/entity/animation imports are routed through the MCblend-inspired
# pipeline. These are the canonical implementations; import them up front so they
# are the names this module exposes.
from .bedrock_mcblend import (
    apply_bedrock_animations as apply_bedrock_animations,
    apply_evaluated_animation_tracks as apply_evaluated_animation_tracks,
    build_bedrock_model as build_bedrock_model,
    build_bedrock_model_from_file as build_bedrock_model_from_file,
    import_client_entity as import_client_entity,
)


def bedrock_to_blender(point):
    x, y, z = _coerce_vector(point)
    return Vector((x, z, y)) / 16.0


def bedrock_rotation_to_euler(rotation):
    x, y, z = _coerce_vector(rotation)
    return Euler((math.radians(x), math.radians(z), math.radians(-y)), "XZY")


def _coerce_float(value, default=0.0):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _coerce_vector(value, default=(0.0, 0.0, 0.0)):
    if isinstance(value, (int, float, str)):
        return (_coerce_float(value),) * 3
    if isinstance(value, (list, tuple)):
        vals = [_coerce_float(v) for v in value[:3]]
        while len(vals) < 3:
            vals.append(0.0)
        return tuple(vals)
    return tuple(default)


def _image_alpha_usable(image):
    try:
        if getattr(image, "channels", 4) < 4:
            return False
        pixels = image.pixels
        total = len(pixels) // 4
        if total == 0:
            return False
        stride = max(1, total // 2048)
        for i in range(0, total, stride):
            if pixels[i * 4 + 3] > 0.01:
                return True
        return False
    except Exception:
        return True


def _make_model_material(name, texture_path="", interpolation="Closest", image=None):
    mat = bpy.data.materials.new(name=f"Mat_{safe_name(name)}")
    mat.use_nodes = True
    mat.use_backface_culling = False
    mat.diffuse_color = (1.0, 1.0, 1.0, 1.0)

    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = "CLIP"
        except TypeError:
            mat.blend_method = "CLIP"
    if hasattr(mat, "surface_render_method"):
        try:
            mat.surface_render_method = "DITHERED"
        except TypeError:
            try:
                mat.surface_render_method = "DITHERED"
            except TypeError:
                pass
    if hasattr(mat, "alpha_threshold"):
        mat.alpha_threshold = 0.5
    if hasattr(mat, "show_transparent_back"):
        mat.show_transparent_back = True
    if hasattr(mat, "use_screen_refraction"):
        mat.use_screen_refraction = False

    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        for node in nodes:
            if node.type == "BSDF_PRINCIPLED":
                bsdf = node
                break

    if bsdf:
        # "A little rough specular", as on mobs in the trailers (see
        # bedrock_mcblend.materials).
        from .trailer_materials import ROUGHNESS, SPECULAR
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = SPECULAR
        elif "Specular" in bsdf.inputs:
            bsdf.inputs["Specular"].default_value = SPECULAR
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = ROUGHNESS
        if "Alpha" in bsdf.inputs:
            bsdf.inputs["Alpha"].default_value = 1.0
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = 0.0
        if "Emission Color" in bsdf.inputs:
            bsdf.inputs["Emission Color"].default_value = (0.0, 0.0, 0.0, 1.0)

    if (image is not None or (texture_path and os.path.isfile(texture_path))) and bsdf:
        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.location = (-300, 300)
        img = image
        if img is None:
            # Reuse an already-loaded image with the same path to avoid duplicate datablocks.
            img = bpy.data.images.get(os.path.basename(texture_path))
            if not img or bpy.path.abspath(img.filepath) != bpy.path.abspath(texture_path):
                img = bpy.data.images.load(texture_path, check_existing=True)
        tex_node.image = img
        if hasattr(tex_node.image, "alpha_mode"):
            tex_node.image.alpha_mode = "STRAIGHT"
        tex_node.interpolation = interpolation
        mat.node_tree.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
        alpha_usable = _image_alpha_usable(tex_node.image)
        if not alpha_usable and hasattr(tex_node.image, "alpha_mode"):
            tex_node.image.alpha_mode = "CHANNEL_PACKED"
        if "Alpha" in bsdf.inputs and alpha_usable:
            mat.node_tree.links.new(tex_node.outputs["Alpha"], bsdf.inputs["Alpha"])
    elif bsdf:
        bsdf.inputs["Base Color"].default_value = (1.0, 0.0, 1.0, 1.0)

    return mat


def _clean_import_name(name, fallback):
    raw = str(name or "").strip()
    if not raw:
        raw = str(fallback or "minecraft_asset").strip()
    raw = raw.replace("\\", "_").replace("/", "_").replace(":", "_")
    raw = raw.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    raw = " ".join(raw.split())
    return raw or "minecraft_asset"


def _ensure_unique_collection(context, name):
    col_name = name
    if col_name in bpy.data.collections:
        i = 1
        while f"{col_name}.{i:03d}" in bpy.data.collections:
            i += 1
        col_name = f"{col_name}.{i:03d}"
    collection = bpy.data.collections.new(col_name)
    context.collection.children.link(collection)
    return collection

import os
from ..importers.bedrock_assets import load_json_file
from ..java_texture_animation import iter_action_fcurves, mcmeta_frame_location_y
from .bedrock_builder import _make_model_material
from .java_resolver import _resolve_texture_path


def _texture_node(material):
    if not material or not material.use_nodes:
        return None
    for node in material.node_tree.nodes:
        if node.bl_idname == "ShaderNodeTexImage":
            return node
    return None


def _apply_java_texture_animation(material, texture_path, context):
    if not texture_path or not os.path.isfile(texture_path):
        return
    meta_path = f"{texture_path}.mcmeta"
    if not os.path.isfile(meta_path):
        return
    meta = load_json_file(meta_path)
    animation = meta.get("animation") if isinstance(meta, dict) else None
    if animation is None:
        return
    if not isinstance(animation, dict):
        animation = {}

    image_node = _texture_node(material)
    image = image_node.image if image_node else None
    if not image or not image.size:
        return
    width, height = int(image.size[0]), int(image.size[1])
    if width <= 0 or height <= width:
        return

    frame_height = int(animation.get("height") or width)
    if frame_height <= 0:
        frame_height = width
    frame_count = max(1, height // frame_height)
    if frame_count <= 1:
        return

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    texcoord = nodes.new(type="ShaderNodeTexCoord")
    mapping = nodes.new(type="ShaderNodeMapping")
    texcoord.location = (image_node.location.x - 640, image_node.location.y)
    mapping.location = (image_node.location.x - 420, image_node.location.y)
    links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], image_node.inputs["Vector"])
    mapping.inputs["Scale"].default_value[1] = 1.0 / frame_count

    fps = getattr(context.scene.render, "fps", 24) or 24
    frametime = int(animation.get("frametime") or 1)
    step = max(1, round((frametime / 20.0) * fps))
    frames = animation.get("frames")
    if isinstance(frames, list) and frames:
        sequence = []
        for item in frames:
            if isinstance(item, dict):
                idx = int(item.get("index", 0) or 0)
                repeat = max(1, int(item.get("time", frametime) or frametime))
                sequence.extend([idx] * repeat)
            else:
                sequence.append(int(item or 0))
    else:
        sequence = list(range(frame_count))

    start = int(context.scene.frame_start or 1)
    for offset, frame_index in enumerate(sequence):
        frame_index = max(0, min(frame_count - 1, frame_index))
        mapping.inputs["Location"].default_value[1] = mcmeta_frame_location_y(frame_index, frame_count)
        mapping.inputs["Location"].keyframe_insert("default_value", frame=start + offset * step, index=1)

    material["mcbedrock_animated_texture"] = os.path.basename(meta_path)
    if material.node_tree.animation_data and material.node_tree.animation_data.action:
        for fcurve in iter_action_fcurves(material.node_tree.animation_data.action):
            for keyframe in fcurve.keyframe_points:
                keyframe.interpolation = "CONSTANT"
            try:
                cycles = fcurve.modifiers.new(type="CYCLES")
                cycles.mode_before = "REPEAT"
                cycles.mode_after = "REPEAT"
            except RuntimeError:
                pass


def _make_java_material(name, texture_path, context, interpolation="Closest", image=None):
    material = _make_model_material(name, texture_path, interpolation=interpolation, image=image)
    # Java model faces are authored as independently visible quads. Keep that
    # source-format default explicit instead of inheriting Bedrock's culling.
    material.use_backface_culling = False
    if hasattr(material, "show_transparent_back"):
        material.show_transparent_back = True
    material["mcbedrock_source_format"] = "java"
    # Java PNG alpha is authored opacity, including fully transparent textures.
    # Bedrock's all-zero-alpha recovery would make invisible pane faces opaque.
    image_node = _texture_node(material)
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if image_node and image_node.image and bsdf and "Alpha" in bsdf.inputs:
        image_node.image.alpha_mode = "STRAIGHT"
        material.node_tree.links.new(image_node.outputs["Alpha"], bsdf.inputs["Alpha"])
    if hasattr(material, "blend_method"):
        material.blend_method = "HASHED"
    _apply_java_texture_animation(material, texture_path, context)
    return material


def _apply_tint(material, color):
    """Multiply the texture colour by a constant biome tint."""
    if not material or not material.use_nodes:
        return
    # Pack colors and vanilla defaults are sRGB; shader sockets are linear.
    color = tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in color)
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    image_node = None
    for node in nodes:
        if node.bl_idname == "ShaderNodeTexImage":
            image_node = node
            break
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        for node in nodes:
            if node.type == "BSDF_PRINCIPLED":
                bsdf = node
                break
    if bsdf is None:
        return
    if image_node is None:
        bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
        return

    mix = None
    try:
        mix = nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.inputs["Factor"].default_value = 1.0
        color_inputs = [s for s in mix.inputs if s.type == "RGBA"]
        if len(color_inputs) < 2:
            raise RuntimeError("unexpected ShaderNodeMix sockets")
        links.new(image_node.outputs["Color"], color_inputs[0])
        color_inputs[1].default_value = (color[0], color[1], color[2], 1.0)
        output = next(s for s in mix.outputs if s.type == "RGBA")
    except Exception:
        if mix is not None:
            nodes.remove(mix)
        mix = nodes.new("ShaderNodeMixRGB")  # Pre-4.x / fallback path.
        mix.blend_type = "MULTIPLY"
        mix.inputs[0].default_value = 1.0
        links.new(image_node.outputs["Color"], mix.inputs[1])
        mix.inputs[2].default_value = (color[0], color[1], color[2], 1.0)
        output = mix.outputs[0]
    mix.location = (image_node.location.x + 200, image_node.location.y)
    links.new(output, bsdf.inputs["Base Color"])


def _first_resolved_java_texture(textures, pack_root, namespace):
    if not isinstance(textures, dict):
        return ""
    preferred = ("layer0", "texture", "particle", "all", "default", "0")
    keys = [key for key in preferred if key in textures]
    keys.extend(key for key in textures.keys() if key not in keys)
    for key in keys:
        path = _resolve_texture_path(textures.get(key, ""), textures, pack_root, namespace)
        if path:
            return path
    return ""

"""Minecraft material helpers shared by the rewritten Bedrock importer."""

import json
import os

import bpy

from ...importers.bedrock_assets import safe_name
from ...importers.bedrock_compatibility import material_fallback_issue
from ...importers.texture_sets import load_texture_set


_RENDER_METHODS = {"opaque", "alpha_test", "blend", "emissive_alpha_test"}


def _try_set_enum(obj, attr, values):
    if not hasattr(obj, attr):
        return False
    for value in values:
        try:
            setattr(obj, attr, value)
            return True
        except (TypeError, ValueError):
            continue
    return False


def _set_surface_render_method(mat, render_method):
    if render_method == "blend":
        _try_set_enum(mat, "surface_render_method", ("BLENDED", "DITHERED"))
    elif render_method in {"alpha_test", "emissive_alpha_test"}:
        _try_set_enum(mat, "surface_render_method", ("CLIPPED", "DITHERED", "BLENDED"))
    else:
        _try_set_enum(mat, "surface_render_method", ("OPAQUE", "DITHERED"))


def _set_legacy_blend_method(mat, render_method):
    if render_method == "blend":
        _try_set_enum(mat, "blend_method", ("BLEND", "HASHED"))
    elif render_method in {"alpha_test", "emissive_alpha_test"}:
        _try_set_enum(mat, "blend_method", ("CLIP", "HASHED", "BLEND"))
    else:
        _try_set_enum(mat, "blend_method", ("OPAQUE",))


def _texture_set_location(field):
    if field == "$":
        return "$"
    if field == "format_version":
        return "$.format_version"
    return f"$.minecraft:texture_set.{field}"


def _layer_record(layer):
    return {
        "source": layer.source,
        "image_path": layer.image_path,
        "uniform": list(layer.uniform),
    }


def _declared_texture_source(source_texture):
    """Resolve an optional Texture Set without inspecting image pixels."""
    resolution = load_texture_set(source_texture)
    source = {
        "source_texture": source_texture,
        "color": None,
        "mers": None,
        "issues": [],
        "record": {"source_texture": source_texture, "mode": "classic"},
    }
    if not resolution.exists:
        return source
    if not resolution.valid:
        source["issues"] = [
            material_fallback_issue(
                f"Texture Set field {problem.field!r} is invalid: {problem.message}",
                fallback=f"classic color texture {source_texture!r}",
                source_path=resolution.metadata_path,
                source_location=_texture_set_location(problem.field),
            ).as_dict()
            for problem in resolution.problems
        ]
        source["record"] = {
            "source_texture": source_texture,
            "metadata_path": resolution.metadata_path,
            "mode": "classic_fallback",
            "problems": [
                {"field": problem.field, "message": problem.message}
                for problem in resolution.problems
            ],
        }
        return source

    definition = resolution.definition
    source["color"] = definition.layer("color")
    source["mers"] = (
        definition.layer("metalness_emissive_roughness_subsurface")
        or definition.layer("metalness_emissive_roughness")
    )
    source["record"] = {
        "source_texture": source_texture,
        "metadata_path": definition.source_path,
        "format_version": definition.format_version,
        "mode": "texture_set",
        "layers": {
            name: _layer_record(layer)
            for name, layer in definition.layers.items()
        },
    }
    return source


def _new_image_node(
    nodes, path, interpolation, name, location, *, non_color=False,
    packed_alpha=False,
):
    node = nodes.new("ShaderNodeTexImage")
    node.name = name
    node.label = os.path.basename(path)
    node.location = location
    node.image = bpy.data.images.load(path, check_existing=True)
    if hasattr(node.image, "alpha_mode"):
        node.image.alpha_mode = (
            "CHANNEL_PACKED" if non_color or packed_alpha else "STRAIGHT"
        )
    node.interpolation = interpolation or "Closest"
    # Entity atlases use clamped edge sampling; Blender nodes default to REPEAT.
    node.extension = "CLIP"
    if non_color:
        try:
            node.image.colorspace_settings.name = "Non-Color"
        except (AttributeError, TypeError, ValueError):
            pass
    return node


def _new_value_node(nodes, name, value, location):
    node = nodes.new("ShaderNodeValue")
    node.name = name
    node.location = location
    node.outputs[0].default_value = float(value)
    return node


def _separate_channels(nodes, links, socket, name, location):
    try:
        separate = nodes.new("ShaderNodeSeparateColor")
        outputs = (
            separate.outputs.get("Red"),
            separate.outputs.get("Green"),
            separate.outputs.get("Blue"),
            separate.outputs.get("Alpha"),
        )
    except RuntimeError:
        separate = nodes.new("ShaderNodeSeparateRGB")
        outputs = (
            separate.outputs.get("R"),
            separate.outputs.get("G"),
            separate.outputs.get("B"),
            None,
        )
    separate.name = name
    separate.location = location
    links.new(socket, separate.inputs[0])
    return outputs


def _max_sockets(nodes, links, first, second, name, location):
    if first is None:
        return second
    if second is None:
        return first
    maximum = nodes.new("ShaderNodeMath")
    maximum.name = name
    maximum.operation = "MAXIMUM"
    maximum.location = location
    links.new(first, maximum.inputs[0])
    links.new(second, maximum.inputs[1])
    return maximum.outputs[0]


_MATERIAL_GROUP_VERSION = 4
# Texels below this alpha are dropped and every other texel draws with its own
# alpha, as Blockbench's texture shader does (``if (color.a < 0.01) discard``).
BLOCKBENCH_ALPHA_DISCARD = 0.01


def _group_socket(group, name, in_out, socket_type):
    interface = getattr(group, "interface", None)
    if interface is not None:
        return interface.new_socket(
            name=name, in_out=in_out, socket_type=socket_type
        )
    sockets = group.inputs if in_out == "INPUT" else group.outputs
    return sockets.new(socket_type, name)


def _material_semantic_group(surface_mode, threshold, legacy_emission_mode):
    """Return reusable MCBlend-style Color/Coverage/Emission semantics."""
    legacy = legacy_emission_mode == "inverse_alpha"
    if surface_mode == "blend":
        family = "Blend"
    elif surface_mode == "cutout":
        family = "Emissive Cutout" if legacy else "Cutout"
    else:
        family = "Emissive Opaque" if legacy else "Opaque"
    name = f"MCBedrock {family} v{_MATERIAL_GROUP_VERSION}"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        return existing

    group = bpy.data.node_groups.new(name=name, type="ShaderNodeTree")
    for socket_name, socket_type in (
        ("Color", "NodeSocketColor"),
        ("Alpha", "NodeSocketFloat"),
        ("MERS Emission", "NodeSocketFloat"),
        ("Cutout Threshold", "NodeSocketFloat"),
        ("Fullbright", "NodeSocketFloat"),
        ("Light Multiplier", "NodeSocketFloat"),
    ):
        _group_socket(group, socket_name, "INPUT", socket_type)
    for socket_name, socket_type in (
        ("Color", "NodeSocketColor"),
        ("Coverage", "NodeSocketFloat"),
        ("Emission Color", "NodeSocketColor"),
    ):
        _group_socket(group, socket_name, "OUTPUT", socket_type)

    nodes = group.nodes
    links = group.links
    group_input = nodes.new("NodeGroupInput")
    group_input.location = (-520, 0)
    group_output = nodes.new("NodeGroupOutput")
    group_output.location = (420, 0)
    links.new(group_input.outputs["Color"], group_output.inputs["Color"])

    declared_emission = group_input.outputs["MERS Emission"]
    emission_factor = declared_emission
    if legacy:
        inverse = nodes.new("ShaderNodeMath")
        inverse.name = "Legacy Inverse Alpha Emission"
        inverse.operation = "SUBTRACT"
        inverse.inputs[0].default_value = 1.0
        links.new(group_input.outputs["Alpha"], inverse.inputs[1])
        emission_union = nodes.new("ShaderNodeMath")
        emission_union.name = "Emission Union"
        emission_union.operation = "MAXIMUM"
        links.new(declared_emission, emission_union.inputs[0])
        links.new(inverse.outputs[0], emission_union.inputs[1])
        emission_factor = emission_union.outputs[0]

    # Coverage follows Blockbench, the reference editor for Bedrock models:
    # every texel draws with its own alpha and only texels under 1% are
    # dropped, whatever the declared material. The game reads alpha per
    # material (a hard cut, or a glow mask for entity_emissive_alpha), which
    # turned near-invisible hologram texels into solid squares. Tint masks
    # never reach here: their layers pass full coverage.
    alpha_test = nodes.new("ShaderNodeMath")
    alpha_test.name = "Alpha Test"
    alpha_test.label = "Alpha >= 1% (Blockbench)"
    alpha_test.operation = "GREATER_THAN"
    alpha_test.inputs[1].default_value = BLOCKBENCH_ALPHA_DISCARD - 1.0e-4
    links.new(group_input.outputs["Alpha"], alpha_test.inputs[0])
    blockbench = nodes.new("ShaderNodeMath")
    blockbench.name = "Blockbench Coverage"
    blockbench.operation = "MULTIPLY"
    links.new(group_input.outputs["Alpha"], blockbench.inputs[0])
    links.new(alpha_test.outputs[0], blockbench.inputs[1])
    if surface_mode == "blend":
        coverage_socket = blockbench.outputs[0]
    else:
        # RTX texture sets can declare emission where the color is clear.
        emission_test = nodes.new("ShaderNodeMath")
        emission_test.name = "Declared Emission Coverage"
        emission_test.operation = "GREATER_THAN"
        emission_test.inputs[1].default_value = 0.0
        links.new(declared_emission, emission_test.inputs[0])

        coverage_union = nodes.new("ShaderNodeMath")
        coverage_union.name = "Alpha or Declared Emission"
        coverage_union.operation = "MAXIMUM"
        links.new(blockbench.outputs[0], coverage_union.inputs[0])
        links.new(emission_test.outputs[0], coverage_union.inputs[1])
        coverage_socket = coverage_union.outputs[0]
    links.new(coverage_socket, group_output.inputs["Coverage"])

    fullbright_factor = nodes.new("ShaderNodeMath")
    fullbright_factor.name = "Covered Fullbright"
    fullbright_factor.operation = "MULTIPLY"
    links.new(coverage_socket, fullbright_factor.inputs[0])
    links.new(group_input.outputs["Light Multiplier"], fullbright_factor.inputs[1])

    inverse_fullbright = nodes.new("ShaderNodeMath")
    inverse_fullbright.name = "Lit Emission Weight"
    inverse_fullbright.operation = "SUBTRACT"
    inverse_fullbright.inputs[0].default_value = 1.0
    links.new(group_input.outputs["Fullbright"], inverse_fullbright.inputs[1])
    lit_emission = nodes.new("ShaderNodeMath")
    lit_emission.operation = "MULTIPLY"
    links.new(emission_factor, lit_emission.inputs[0])
    links.new(inverse_fullbright.outputs[0], lit_emission.inputs[1])
    unlit_emission = nodes.new("ShaderNodeMath")
    unlit_emission.operation = "MULTIPLY"
    links.new(fullbright_factor.outputs[0], unlit_emission.inputs[0])
    links.new(group_input.outputs["Fullbright"], unlit_emission.inputs[1])
    selected_emission = nodes.new("ShaderNodeMath")
    selected_emission.name = "Per-Layer Emission Factor"
    selected_emission.operation = "ADD"
    links.new(lit_emission.outputs[0], selected_emission.inputs[0])
    links.new(unlit_emission.outputs[0], selected_emission.inputs[1])

    emission_color = nodes.new("ShaderNodeMixRGB")
    emission_color.name = "Per-Layer Emission Color"
    emission_color.blend_type = "MULTIPLY"
    emission_color.inputs[0].default_value = 1.0
    links.new(group_input.outputs["Color"], emission_color.inputs[1])
    links.new(selected_emission.outputs[0], emission_color.inputs[2])
    links.new(emission_color.outputs[0], group_output.inputs["Emission Color"])

    group["mcbedrock_surface_mode"] = surface_mode
    group["mcbedrock_cutout_threshold_input"] = True
    group["mcbedrock_legacy_emission_mode"] = legacy_emission_mode
    return group


def _semantic_node(
    nodes,
    links,
    surface_mode,
    threshold,
    legacy_emission_mode,
    color_socket,
    alpha_socket,
    mers_emission_socket,
    name,
    location,
    ignore_lighting=False,
    light_color_multiplier=1.0,
):
    node = nodes.new("ShaderNodeGroup")
    node.name = name
    node.label = name
    node.location = location
    node.node_tree = _material_semantic_group(
        surface_mode, threshold, legacy_emission_mode
    )
    links.new(color_socket, node.inputs["Color"])
    links.new(alpha_socket, node.inputs["Alpha"])
    node.inputs["Cutout Threshold"].default_value = float(threshold)
    node.inputs["Fullbright"].default_value = 1.0 if ignore_lighting else 0.0
    node.inputs["Light Multiplier"].default_value = max(
        0.0, float(light_color_multiplier)
    )
    if mers_emission_socket is not None:
        links.new(mers_emission_socket, node.inputs["MERS Emission"])
    else:
        node.inputs["MERS Emission"].default_value = 0.0
    return node


def _mix_color_sockets(nodes, links, bottom, top, factor, name, location):
    mix = nodes.new("ShaderNodeMixRGB")
    mix.name = name
    mix.blend_type = "MIX"
    mix.location = location
    links.new(factor, mix.inputs[0])
    links.new(bottom, mix.inputs[1])
    links.new(top, mix.inputs[2])
    return mix.outputs[0]


def _mix_value_sockets(nodes, links, bottom, top, factor, name, location):
    inverse = nodes.new("ShaderNodeMath")
    inverse.operation = "SUBTRACT"
    inverse.inputs[0].default_value = 1.0
    links.new(factor, inverse.inputs[1])
    bottom_weighted = nodes.new("ShaderNodeMath")
    bottom_weighted.operation = "MULTIPLY"
    links.new(bottom, bottom_weighted.inputs[0])
    links.new(inverse.outputs[0], bottom_weighted.inputs[1])
    top_weighted = nodes.new("ShaderNodeMath")
    top_weighted.operation = "MULTIPLY"
    links.new(top, top_weighted.inputs[0])
    links.new(factor, top_weighted.inputs[1])
    result = nodes.new("ShaderNodeMath")
    result.name = name
    result.operation = "ADD"
    result.location = location
    links.new(bottom_weighted.outputs[0], result.inputs[0])
    links.new(top_weighted.outputs[0], result.inputs[1])
    return result.outputs[0]


def _two_sided_uv_sources(nodes, links, pair_mode):
    """Return primary/alternate authored UV sources for a plane pair."""
    front_uv = nodes.new("ShaderNodeUVMap")
    front_uv.name = "Bedrock Front UV"
    front_uv.uv_map = "UVMap"
    back_uv = nodes.new("ShaderNodeUVMap")
    back_uv.name = "Bedrock Back UV"
    back_uv.uv_map = "UVMap_Back"
    if pair_mode == "composite":
        # Implicit box planes are view-independent. Both atlas candidates are
        # sampled and material coverage decides which texels survive.
        return front_uv.outputs["UV"], back_uv.outputs["UV"]
    if pair_mode == "directional":
        geometry = nodes.new("ShaderNodeNewGeometry")
        geometry.name = "Bedrock Face Direction"
        selector = nodes.new("ShaderNodeMixRGB")
        selector.name = "Bedrock Two-Sided UV"
        selector.label = "Directional Front / Back UV"
        selector.blend_type = "MIX"
        selector.use_clamp = False
        links.new(geometry.outputs["Backfacing"], selector.inputs[0])
        links.new(front_uv.outputs["UV"], selector.inputs[1])
        links.new(back_uv.outputs["UV"], selector.inputs[2])
        return selector.outputs[0], None
    return front_uv.outputs["UV"], None


def _wire_backface_cull(material, nodes, links, alpha_socket):
    """Zero coverage on back faces so Cycles culls like EEVEE's flag does."""
    geometry = nodes.new("ShaderNodeNewGeometry")
    geometry.name = "Bedrock Backface Cull Geometry"
    geometry.location = (-220, -420)
    front = nodes.new("ShaderNodeMath")
    front.name = "Bedrock Front Face Mask"
    front.label = "1 - Backfacing"
    front.operation = "SUBTRACT"
    front.inputs[0].default_value = 1.0
    front.location = (-40, -420)
    culled = nodes.new("ShaderNodeMath")
    culled.name = "Bedrock Backface Cull"
    culled.label = "Coverage x Front Face"
    culled.operation = "MULTIPLY"
    culled.location = (140, -380)
    links.new(geometry.outputs["Backfacing"], front.inputs[1])
    links.new(alpha_socket, culled.inputs[0])
    links.new(front.outputs[0], culled.inputs[1])
    follow_backface_culling_setting(material)
    return culled.outputs[0]


def follow_backface_culling_setting(material):
    """Make the shader's backface cut follow Material > Backface Culling.

    The cut makes Cycles cull, which ignores that setting; on its own it kept
    back faces hidden after the setting was turned off. Backfacing now passes
    through a multiply driven by the setting (a SUM driver, so no Python needs
    to run). Returns True when the material was changed; materials without the
    cut, or already following the setting, are left alone.
    """
    tree = material.node_tree if material is not None and material.use_nodes else None
    front = tree.nodes.get("Bedrock Front Face Mask") if tree else None
    geometry = tree.nodes.get("Bedrock Backface Cull Geometry") if tree else None
    if front is None or geometry is None or tree.nodes.get("Bedrock Backface Cull Setting"):
        return False
    setting = tree.nodes.new("ShaderNodeMath")
    setting.name = "Bedrock Backface Cull Setting"
    setting.label = "Backfacing x Backface Culling"
    setting.operation = "MULTIPLY"
    setting.location = (front.location.x - 90, front.location.y - 140)
    tree.links.new(geometry.outputs["Backfacing"], setting.inputs[0])
    tree.links.new(setting.outputs[0], front.inputs[1])
    driver = setting.inputs[1].driver_add("default_value").driver
    driver.type = "SUM"
    variable = driver.variables.new()
    variable.name = "culling"
    variable.type = "SINGLE_PROP"
    variable.targets[0].id_type = "MATERIAL"
    variable.targets[0].id = material
    variable.targets[0].data_path = "use_backface_culling"
    setting.inputs[1].default_value = float(material.use_backface_culling)
    return True


def _wire_texture_uv_transform(
    nodes, links, texture_nodes, offset, scale, index, source_socket=None,
):
    if not texture_nodes:
        return
    identity = (
        tuple(offset) == (0.0, 0.0)
        and tuple(scale) == (1.0, 1.0)
    )
    if source_socket is None and identity:
        return
    if source_socket is None:
        uv_map = nodes.new("ShaderNodeUVMap")
        uv_map.name = f"Bedrock UV Source {index}"
        uv_map.uv_map = "UVMap"
        source_socket = uv_map.outputs["UV"]
    if identity:
        target_socket = source_socket
    else:
        mapping = nodes.new("ShaderNodeMapping")
        mapping.name = f"Bedrock Static UV Animation {index}"
        mapping.vector_type = "POINT"
        mapping.inputs["Location"].default_value = (
            float(offset[0]), float(offset[1]), 0.0
        )
        mapping.inputs["Scale"].default_value = (
            float(scale[0]), float(scale[1]), 1.0
        )
        links.new(source_socket, mapping.inputs["Vector"])
        target_socket = mapping.outputs["Vector"]
    for texture_node in texture_nodes:
        vector_input = texture_node.inputs.get("Vector")
        if vector_input is None:
            continue
        for existing in list(vector_input.links):
            links.remove(existing)
        links.new(target_socket, vector_input)


def _wire_static_uv_transform(nodes, links, offset, scale):
    """Apply one statically evaluated render-controller UV transform."""
    texture_nodes = [node for node in nodes if node.type == "TEX_IMAGE"]
    if not texture_nodes:
        return
    vector_input = texture_nodes[0].inputs.get("Vector")
    source_socket = (
        vector_input.links[0].from_socket
        if vector_input is not None and vector_input.links
        else None
    )
    if source_socket is None:
        uv_map = nodes.new("ShaderNodeUVMap")
        uv_map.name = "Bedrock Animated UV Source"
        uv_map.uv_map = "UVMap"
        uv_map.location = (-1200, 760)
        source_socket = uv_map.outputs["UV"]

    mapping = nodes.new("ShaderNodeMapping")
    mapping.name = "Bedrock Static UV Animation"
    mapping.vector_type = "POINT"
    mapping.location = (-980, 760)
    mapping.inputs["Location"].default_value = (
        float(offset[0]), float(offset[1]), 0.0
    )
    mapping.inputs["Scale"].default_value = (
        float(scale[0]), float(scale[1]), 1.0
    )
    links.new(source_socket, mapping.inputs["Vector"])
    for texture_node in texture_nodes:
        target = texture_node.inputs.get("Vector")
        if target is None:
            continue
        for existing in list(target.links):
            links.remove(existing)
        links.new(mapping.outputs["Vector"], target)


def make_model_material(
    name,
    texture_path="",
    interpolation="Closest",
    render_method="alpha_test",
    texture_paths=None,
    two_sided_uv=False,
    uv_pair_mode="",
    backface_culling=None,
    declared_material="",
    ignore_lighting=False,
    light_color_multiplier=1.0,
    uv_offset=(0.0, 0.0),
    uv_scale=(1.0, 1.0),
    material_issues=None,
    surface_mode=None,
    cutout_threshold=None,
    legacy_emission_mode=None,
    material_layers=None,
    force_backface_culling=False,
    force_two_sided=False,
    surface_override=None,
):
    """Build one material from an ordered stack of Bedrock controller passes.

    ``surface_override`` ("cutout", "blend", "opaque") sets the material's own
    mode whatever the top pass is: Java Edition entities draw their skin as a
    cutout and mix glow and overlay passes over it in the shader.
    """
    issues = [
        dict(issue)
        for issue in (material_issues or [])
        if isinstance(issue, dict)
    ]

    def normalize_layer(raw_layer, fallback_paths):
        raw_layer = raw_layer if isinstance(raw_layer, dict) else {}
        behavior = raw_layer.get("behavior", raw_layer.get("material", {}))
        behavior = dict(behavior) if isinstance(behavior, dict) else {}
        method = str(
            behavior.get("render_method", raw_layer.get("render_method", render_method))
            or "alpha_test"
        )
        if method not in _RENDER_METHODS:
            issues.append(material_fallback_issue(
                f"Unsupported render mode {method!r}",
                fallback="alpha_test",
                source_location="$.material.render_method",
            ).as_dict())
            method = "alpha_test"
        mode = behavior.get("surface_mode")
        if mode is None:
            mode = {
                "opaque": "opaque",
                "blend": "blend",
                "alpha_test": "cutout",
                "emissive_alpha_test": "cutout",
            }[method]
        mode = str(mode).lower()
        if mode not in {"opaque", "cutout", "blend"}:
            mode = "cutout"
        legacy = behavior.get("legacy_emission_mode")
        if legacy is None:
            legacy = (
                "inverse_alpha" if method == "emissive_alpha_test" else "none"
            )
        legacy = str(legacy).lower()
        if legacy not in {"none", "inverse_alpha"}:
            legacy = "none"
        threshold = behavior.get("cutout_threshold")
        if threshold is None:
            threshold = 1.0e-4 if legacy == "inverse_alpha" and mode == "cutout" else 0.5
        threshold = max(0.0, min(1.0, float(threshold)))
        paths = list(raw_layer.get("texture_paths", fallback_paths) or ())
        resolved_cull = bool(
            behavior.get(
                "cull_backfaces",
                behavior.get("culling") == "back",
            )
        )
        resolved_culling = str(
            behavior.get("culling", "back" if resolved_cull else "none")
        )
        return {
            "texture_paths": paths,
            "surface_mode": mode,
            "cutout_threshold": threshold,
            "legacy_emission_mode": legacy,
            "alpha_role": str(behavior.get("alpha_role", "coverage")),
            "lighting": str(behavior.get("lighting", "lit")),
            "emission": str(behavior.get("emission", "none")),
            "provenance": list(behavior.get("provenance", ())),
            "culling": resolved_culling,
            "cull_backfaces": resolved_cull,
            "declared_material": str(
                behavior.get("name", raw_layer.get("declared_material", declared_material))
                or ""
            ),
            "ignore_lighting": bool(
                behavior.get("lighting") == "unlit" or
                behavior.get("ignore_lighting", raw_layer.get("ignore_lighting", ignore_lighting))
            ),
            "light_color_multiplier": max(0.0, float(
                behavior.get(
                    "light_color_multiplier",
                    raw_layer.get("light_color_multiplier", light_color_multiplier),
                )
            )),
            "uv_offset": tuple(float(value) for value in behavior.get(
                "uv_offset", raw_layer.get("uv_offset", uv_offset)
            )[:2]),
            "uv_scale": tuple(float(value) for value in behavior.get(
                "uv_scale", raw_layer.get("uv_scale", uv_scale)
            )[:2]),
            "issues": list(behavior.get("issues", ())),
            "render_method": method,
        }

    fallback_paths = list(texture_paths or ())
    if texture_path and texture_path not in fallback_paths:
        fallback_paths.insert(0, texture_path)
    controller_stack = material_layers is not None
    if controller_stack:
        layers = [normalize_layer(layer, ()) for layer in material_layers]
    else:
        layers = [normalize_layer({
            "texture_paths": fallback_paths,
            "behavior": {
                "surface_mode": surface_mode,
                "cutout_threshold": cutout_threshold,
                "legacy_emission_mode": legacy_emission_mode,
                "cull_backfaces": backface_culling,
                "name": declared_material,
                "ignore_lighting": ignore_lighting,
                "light_color_multiplier": light_color_multiplier,
                "uv_offset": uv_offset,
                "uv_scale": uv_scale,
                "issues": issues,
                "render_method": render_method,
            },
        }, fallback_paths)]
    layers = [layer for layer in layers if layer["texture_paths"]]

    # Mcblend searches the reversed controller stack for its first opaque
    # material. That opaque pass replaces the complete stack. Without an
    # opaque pass, global render mode and sidedness come from the first layer.
    if controller_stack:
        opaque_layer = next((
            layer for layer in layers if layer["surface_mode"] == "opaque"
        ), None)
        if opaque_layer is not None:
            layers = [opaque_layer]
    empty_controller_stack = controller_stack and not layers
    if layers:
        first_layer = layers[0]
        final_surface_mode = first_layer["surface_mode"]
        final_render_method = first_layer["render_method"]
    elif empty_controller_stack:
        first_layer = None
        final_surface_mode = "blend"
        final_render_method = "blend"
    else:
        first_layer = None
        final_render_method = str(render_method or "alpha_test")
        final_surface_mode = {
            "opaque": "opaque",
            "blend": "blend",
            "alpha_test": "cutout",
            "emissive_alpha_test": "cutout",
        }.get(final_render_method, "cutout")
    if surface_override in ("cutout", "blend", "opaque"):
        final_surface_mode = surface_override
        final_render_method = {"cutout": "alpha_test", "blend": "blend", "opaque": "opaque"}[surface_override]
    if controller_stack:
        requested_culling = bool(first_layer and first_layer["cull_backfaces"])
    else:
        requested_culling = (
            True
            if backface_culling is None
            else bool(backface_culling)
        )
    # A declared material culls as it does in the game: entity_alphatest and
    # everything built on it carry DisableCulling, so their cubes draw both
    # sides and show their inner walls through transparent texels; entity,
    # entity_emissive and the like cull. Geometry imported without a client
    # entity has no material, and its closed cubes stay culled. Cards and
    # faces seen through their own cube are two-sided per face either way,
    # and an explicit backface_culling=False from a caller opts out.
    effective_culling = (
        not bool(force_two_sided)
        # Java Edition entities layer textures too (glow, overlays) but
        # never cull; their callers pass an explicit False.
        and backface_culling is not False
        and (requested_culling if controller_stack and first_layer
             else backface_culling is None or bool(backface_culling))
    )
    mat = bpy.data.materials.new(name=f"Mat_{safe_name(name)}")
    mat.use_nodes = True
    mat.use_backface_culling = effective_culling
    mat.diffuse_color = (1.0, 1.0, 1.0, 1.0)
    mat["mcbedrock_requested_render_method"] = str(render_method or "alpha_test")
    mat["mcbedrock_render_method"] = final_render_method
    mat["mcbedrock_surface_mode"] = final_surface_mode
    mat["mcbedrock_empty_material_stack"] = empty_controller_stack
    mat["mcbedrock_cutout_threshold"] = min(
        (layer["cutout_threshold"] for layer in layers if layer["surface_mode"] == "cutout"),
        default=0.0,
    )
    mat["mcbedrock_legacy_emission_mode"] = (
        "inverse_alpha" if any(
            layer["legacy_emission_mode"] == "inverse_alpha" for layer in layers
        ) else "none"
    )
    mat["mcbedrock_declared_material"] = str(
        first_layer["declared_material"] if first_layer else declared_material or ""
    )
    mat["mcbedrock_requested_backface_culling"] = requested_culling
    mat["mcbedrock_backface_culling"] = effective_culling
    mat["mcbedrock_culling_policy"] = "resolved_material_with_topology_override"
    mat["mcbedrock_ignore_lighting"] = any(
        layer["ignore_lighting"] for layer in layers
    )
    mat["mcbedrock_light_color_multiplier"] = max(
        (layer["light_color_multiplier"] for layer in layers), default=1.0
    )
    mat["mcbedrock_uv_offset"] = tuple(float(value) for value in uv_offset[:2])
    mat["mcbedrock_uv_scale"] = tuple(float(value) for value in uv_scale[:2])

    _set_legacy_blend_method(mat, final_render_method)
    _set_surface_render_method(mat, final_render_method)
    # Blender 4.5 can reset culling while changing the surface render method.
    mat.use_backface_culling = effective_culling
    if hasattr(mat, "alpha_threshold"):
        mat.alpha_threshold = (
            mat["mcbedrock_cutout_threshold"]
            if final_surface_mode == "cutout" else 0.0
        )
    if hasattr(mat, "show_transparent_back"):
        mat.show_transparent_back = not mat.use_backface_culling
    if hasattr(mat, "use_screen_refraction"):
        mat.use_screen_refraction = False

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = nodes.get("Principled BSDF") or next(
        (node for node in nodes if node.type == "BSDF_PRINCIPLED"), None
    )
    if bsdf:
        # Element's look for Minecraft models: "a little rough specular"
        # (bramstout.nl), the soft sheen on mobs in the trailers, not flat
        # matte. Same recipe as the optional trailer world materials.
        from ..trailer_materials import ROUGHNESS, SPECULAR
        specular = bsdf.inputs.get("Specular IOR Level")
        if specular is None:
            specular = bsdf.inputs.get("Specular")
        if specular is not None:
            specular.default_value = SPECULAR
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = ROUGHNESS
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = 0.0
        if "Alpha" in bsdf.inputs:
            bsdf.inputs["Alpha"].default_value = 1.0
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = 1.0
        if "Emission Color" in bsdf.inputs:
            bsdf.inputs["Emission Color"].default_value = (0.0, 0.0, 0.0, 1.0)

    # A face seen from behind keeps Blender's normal, turned toward the viewer.
    # Forcing the authored outward normal (so both sides lit alike) left it
    # facing away from the camera, which Cycles shades black: the inside of
    # every double-sided box seen through a transparent texel.
    mat["mcbedrock_two_sided_lighting"] = False

    # A pair selector reads the mesh's UVMap_Back. A card whose two sides map
    # the same texels gets none, and a selector reading a missing UV map
    # samples (0, 0) everywhere: Superman's wire cards drew one teal texel.
    resolved_pair_mode = str(
        (uv_pair_mode or "directional") if two_sided_uv else "none"
    )
    source_specs = []
    all_paths = []
    pass_records = []
    for pass_index, layer in enumerate(layers):
        pass_paths = []
        seen_in_pass = set()
        for path in layer["texture_paths"]:
            if not path or not os.path.isfile(path):
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in seen_in_pass:
                continue
            seen_in_pass.add(key)
            pass_paths.append(path)
            all_paths.append(path)
            source = _declared_texture_source(path)
            issues.extend(source["issues"])
            source_specs.append({
                "source": source,
                "behavior": layer,
                "pass_index": pass_index,
            })
        issues.extend(
            dict(issue) for issue in layer["issues"] if isinstance(issue, dict)
        )
        pass_records.append({
            "index": pass_index,
            "texture_paths": pass_paths,
            "surface_mode": layer["surface_mode"],
            "cutout_threshold": layer["cutout_threshold"],
            "legacy_emission_mode": layer["legacy_emission_mode"],
            "alpha_role": layer["alpha_role"],
            "lighting": layer["lighting"],
            "emission": layer["emission"],
            "provenance": layer["provenance"],
            "culling": layer["culling"],
            "cull_backfaces": layer["cull_backfaces"],
            "declared_material": layer["declared_material"],
            "ignore_lighting": layer["ignore_lighting"],
            "light_color_multiplier": layer["light_color_multiplier"],
            "uv_offset": layer["uv_offset"],
            "uv_scale": layer["uv_scale"],
        })

    sources = [spec["source"] for spec in source_specs]
    mat["mcbedrock_texture_layers"] = json.dumps(all_paths)
    mat["mcbedrock_material_passes"] = json.dumps(pass_records)
    mat["mcbedrock_texture_sets"] = json.dumps(
        [source["record"] for source in sources]
    )
    valid_set_path = next((
        source["record"].get("metadata_path", "")
        for source in sources
        if source["record"].get("mode") == "texture_set"
    ), "")
    if valid_set_path:
        mat["mcbedrock_texture_set"] = valid_set_path

    if source_specs and bsdf:
        effective_pair_mode = resolved_pair_mode
        uv_sources = (
            _two_sided_uv_sources(nodes, links, effective_pair_mode)
            if effective_pair_mode != "none" else (None, None)
        )
        primary_uv_source, alternate_uv_source = uv_sources
        color_output = None
        coverage_output = None
        emission_color_output = None
        metallic_output = None
        roughness_output = None
        subsurface_output = None
        semantic_group_name = ""
        transformed_uv = False

        # Controller passes arrive top-to-bottom, while the node
        # accumulator composites each new source over what already exists.
        # Traverse controller passes bottom-to-top but retain texture order
        # within each pass so the first controller remains visually on top.
        composite_specs = (
            sorted(
                source_specs,
                key=lambda item: item["pass_index"],
                reverse=True,
            )
            if controller_stack else source_specs
        )
        for layer_index, spec in enumerate(composite_specs):
            source = spec["source"]
            behavior = spec["behavior"]
            color_layer = source["color"]
            x = -1100
            y = 500 - layer_index * 360
            texture_nodes = []
            alternate_texture_nodes = []
            if color_layer is None or color_layer.is_image:
                color_path = (
                    color_layer.image_path if color_layer is not None
                    else source["source_texture"]
                )
                color_node = _new_image_node(
                    nodes, color_path, interpolation,
                    "Base Color Texture" if layer_index == 0 else f"Texture Layer {layer_index}",
                    (x, y),
                    # Channel packed: Cycles premultiplies straight 8-bit images,
                    # and texels at a few percent alpha came out black.
                    packed_alpha=True,
                )
                texture_nodes.append(color_node)
                layer_color = color_node.outputs["Color"]
                layer_alpha = color_node.outputs["Alpha"]
                if alternate_uv_source is not None:
                    alternate_node = _new_image_node(
                        nodes, color_path, interpolation,
                        f"Alternate Plane Texture {layer_index}",
                        (x, y + 120),
                    )
                    alternate_texture_nodes.append(alternate_node)
                    layer_color = _mix_color_sockets(
                        nodes, links,
                        alternate_node.outputs["Color"], color_node.outputs["Color"],
                        color_node.outputs["Alpha"],
                        f"Plane Candidate Composite {layer_index}", (x + 300, y + 80),
                    )
                    layer_alpha = _max_sockets(
                        nodes, links, color_node.outputs["Alpha"],
                        alternate_node.outputs["Alpha"],
                        f"Plane Coverage Union {layer_index}", (x + 300, y + 20),
                    )
            else:
                rgba = tuple(color_layer.uniform)
                color_node = nodes.new("ShaderNodeRGB")
                color_node.name = f"Texture Set Color {layer_index}"
                color_node.location = (x, y)
                color_node.outputs[0].default_value = rgba
                alpha_node = _new_value_node(
                    nodes, f"Texture Set Alpha {layer_index}", rgba[3], (x, y - 100)
                )
                layer_color = color_node.outputs[0]
                layer_alpha = alpha_node.outputs[0]

            channels = (None, None, None, None)
            mers = source["mers"]
            if mers is not None:
                if mers.is_image:
                    mers_node = _new_image_node(
                        nodes, mers.image_path, interpolation,
                        "MERS Texture" if layer_index == 0 else f"MERS Texture Layer {layer_index}",
                        (x, y - 150), non_color=True,
                    )
                    texture_nodes.append(mers_node)
                    channels = _separate_channels(
                        nodes, links, mers_node.outputs["Color"],
                        "MERS Channels" if layer_index == 0 else f"MERS Channels Layer {layer_index}",
                        (x + 220, y - 150),
                    )
                    if channels[3] is None:
                        channels = channels[:3] + (mers_node.outputs["Alpha"],)
                    if not mat.get("mcbedrock_emissive_texture"):
                        mat["mcbedrock_emissive_texture"] = mers.image_path
                else:
                    values = list(mers.uniform) + [None] * 4
                    channels = tuple(
                        _new_value_node(
                            nodes, f"Texture Set MERS {layer_index}.{channel}", value,
                            (x + channel * 110, y - 150),
                        ).outputs[0] if value is not None else None
                        for channel, value in enumerate(values[:4])
                    )

            _wire_texture_uv_transform(
                nodes, links, texture_nodes,
                behavior["uv_offset"], behavior["uv_scale"], layer_index,
                source_socket=primary_uv_source,
            )
            _wire_texture_uv_transform(
                nodes, links, alternate_texture_nodes,
                behavior["uv_offset"], behavior["uv_scale"],
                f"{layer_index}_alternate",
                source_socket=alternate_uv_source,
            )
            transformed_uv = transformed_uv or (
                behavior["uv_offset"] != (0.0, 0.0)
                or behavior["uv_scale"] != (1.0, 1.0)
            )
            if behavior["alpha_role"] == "tint_mask":
                layer_alpha = _new_value_node(
                    nodes,
                    f"Tint Mask Full Coverage {layer_index}",
                    1.0,
                    (-650, y - 80),
                ).outputs[0]

            semantic = _semantic_node(
                nodes, links,
                behavior["surface_mode"], behavior["cutout_threshold"],
                behavior["legacy_emission_mode"], layer_color, layer_alpha,
                channels[1], f"Bedrock Material Semantics {layer_index}",
                (-520, y),
                ignore_lighting=behavior["ignore_lighting"],
                light_color_multiplier=behavior["light_color_multiplier"],
            )
            semantic_group_name = semantic.node_tree.name
            layer_coverage = semantic.outputs["Coverage"]
            layer_emission_color = semantic.outputs["Emission Color"]
            layer_metallic = channels[0]
            if layer_metallic is None:
                layer_metallic = _new_value_node(
                    nodes, f"Default Metallic {layer_index}", 0.0,
                    (x + 100, y - 260)
                ).outputs[0]
            layer_roughness = channels[2]
            if layer_roughness is None:
                layer_roughness = _new_value_node(
                    nodes, f"Default Roughness {layer_index}", 1.0,
                    (x + 210, y - 260)
                ).outputs[0]
            layer_subsurface = channels[3]
            if layer_subsurface is None:
                layer_subsurface = _new_value_node(
                    nodes, f"Default Subsurface {layer_index}", 0.0,
                    (x + 320, y - 260)
                ).outputs[0]

            if color_output is None:
                color_output = semantic.outputs["Color"]
                coverage_output = layer_coverage
                emission_color_output = layer_emission_color
                metallic_output = layer_metallic
                roughness_output = layer_roughness
                subsurface_output = layer_subsurface
            else:
                color_output = _mix_color_sockets(
                    nodes, links, color_output, semantic.outputs["Color"],
                    layer_coverage, f"Alpha Over Layer {layer_index}", (0, y)
                )
                emission_color_output = _mix_color_sockets(
                    nodes, links, emission_color_output, layer_emission_color,
                    layer_coverage, f"Emission Over Layer {layer_index}", (0, y - 60)
                )
                metallic_output = _mix_value_sockets(
                    nodes, links, metallic_output, layer_metallic, layer_coverage,
                    f"Metallic Over Layer {layer_index}", (0, y - 120)
                )
                roughness_output = _mix_value_sockets(
                    nodes, links, roughness_output, layer_roughness, layer_coverage,
                    f"Roughness Over Layer {layer_index}", (0, y - 180)
                )
                subsurface_output = _mix_value_sockets(
                    nodes, links, subsurface_output, layer_subsurface, layer_coverage,
                    f"Subsurface Over Layer {layer_index}", (0, y - 240)
                )
                coverage_output = _max_sockets(
                    nodes, links, coverage_output, layer_coverage,
                    f"Layer Coverage Union {layer_index}", (0, y - 300)
                )

        links.new(color_output, bsdf.inputs["Base Color"])
        if "Alpha" in bsdf.inputs:
            if effective_culling:
                coverage_output = _wire_backface_cull(
                    mat, nodes, links, coverage_output
                )
            links.new(coverage_output, bsdf.inputs["Alpha"])
        # MCBlend entity materials are neutral diffuse surfaces. Bedrock MERS
        # G remains authoritative for declared emission coverage, but RTX
        # metalness/roughness/subsurface data must not make ordinary entity
        # skins metallic or translucent in Blender's Principled shader.
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = 0.0
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 1.0
        subsurface_input = bsdf.inputs.get("Subsurface Weight")
        if subsurface_input is None:
            subsurface_input = bsdf.inputs.get("Subsurface")
        if subsurface_input is not None:
            subsurface_input.default_value = 0.0
        mat["mcbedrock_mers_alpha_usage"] = "unused_data"
        mat["mcbedrock_mers_surface_usage"] = "emission_only"
        if "Emission Color" in bsdf.inputs:
            links.new(emission_color_output, bsdf.inputs["Emission Color"])
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = 1.0

        mat["mcbedrock_material_group"] = semantic_group_name
        mat["mcbedrock_alpha_cutout_threshold"] = mat["mcbedrock_cutout_threshold"]
        mat["mcbedrock_color_alpha_usage"] = (
            "tint_mask_full_coverage"
            if any(layer["alpha_role"] == "tint_mask" for layer in layers)
            else "ordered_controller_coverage"
        )
        mat["mcbedrock_declared_emissive_alpha"] = any(
            layer["legacy_emission_mode"] == "inverse_alpha" for layer in layers
        )
        mat["mcbedrock_static_uv_animation"] = transformed_uv
    elif bsdf:
        if empty_controller_stack:
            bsdf.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 0.0)
            if "Alpha" in bsdf.inputs:
                bsdf.inputs["Alpha"].default_value = 0.0
            mat["mcbedrock_color_alpha_usage"] = "empty_stack_transparent"
        else:
            bsdf.inputs["Base Color"].default_value = (1.0, 0.0, 1.0, 1.0)
        mat["mcbedrock_static_uv_animation"] = False

    mat["mcbedrock_two_sided_uv"] = bool(two_sided_uv)
    mat["mcbedrock_uv_pair_mode"] = resolved_pair_mode
    mat["mcbedrock_material_issues"] = json.dumps(issues)
    return mat

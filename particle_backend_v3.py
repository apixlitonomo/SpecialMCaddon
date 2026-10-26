"""Compact Blender Hair Curves + Geometry Nodes particle renderer."""

from __future__ import annotations

import hashlib
import json
import math
import os

import bpy
from mathutils import Matrix, Quaternion, Vector


MINIMUM_BLENDER_VERSION = (4, 5, 0)


def _require_backend_support():
    from .builder import ParticleBackendUnavailable

    if bpy.app.version < MINIMUM_BLENDER_VERSION:
        raise ParticleBackendUnavailable(
            "requires Blender 4.5 or newer"
        )
    hair_curves = getattr(bpy.data, "hair_curves", None)
    if hair_curves is None or not hasattr(hair_curves, "new"):
        raise ParticleBackendUnavailable(
            "Hair Curves datablocks are unavailable"
        )



def _stable_fraction(seed, effect_identifier, particle_id):
    payload = f"{seed}:{effect_identifier}:{particle_id}".encode("utf-8")
    integer = int.from_bytes(hashlib.sha256(payload).digest()[:8], "little")
    return integer / float(1 << 64)


def _blender_vector(value):
    return Vector((float(value.x), -float(value.z), float(value.y)))


def _state_direction(state, min_speed_threshold=0.01, previous=None):
    custom = state.get("custom_direction")
    if custom is not None and custom.length >= 0.0001:
        return tuple(_blender_vector(custom.normalized()))

    velocity = state.get("velocity")
    threshold = max(0.0, float(min_speed_threshold or 0.0))
    if velocity is not None and velocity.length >= threshold:
        if velocity.length >= 0.0001:
            return tuple(_blender_vector(velocity.normalized()))
    if previous is not None and Vector(previous).length >= 0.0001:
        return tuple(previous)

    spawn_direction = state.get("spawn_direction")
    if spawn_direction is not None and spawn_direction.length >= 0.0001:
        return tuple(_blender_vector(spawn_direction.normalized()))
    return (1.0, 0.0, 0.0)


def _state_rotation(state, facing_mode, emitter_rotation=None):
    spin = Quaternion((0.0, 0.0, 1.0), math.radians(float(state.get("rot_z", 0.0))))
    if facing_mode.startswith("direction_") or facing_mode in {
        "lookat_direction",
        "rotate_xyz",
        "lookat_xyz",
        "rotate_y",
        "lookat_y",
    }:
        return tuple(float(value) for value in spin.to_euler("XYZ"))

    base = {
        # Bedrock XY maps to Blender XZ; XZ maps to Blender XY.
        "emitter_transform_xy": Quaternion((1.0, 0.0, 0.0), math.pi / 2.0),
        "emitter_transform_xz": Quaternion(),
        "emitter_transform_yz": Quaternion((0.0, 1.0, 0.0), math.pi / 2.0),
    }.get(facing_mode, Quaternion())
    rotation = base @ spin
    if emitter_rotation is not None:
        rotation = emitter_rotation @ rotation
    return tuple(float(value) for value in rotation.to_euler("XYZ"))


def _preview_frame_for_particles(particles):
    """Choose the earliest frame with useful visible particle coverage."""
    visible_scores = {}
    first_history_frame = None
    for particle in particles:
        for frame, state in particle.history.items():
            frame = int(frame)
            first_history_frame = (
                frame
                if first_history_frame is None
                else min(first_history_frame, frame)
            )
            color = state.get("color", (1.0, 1.0, 1.0, 1.0))
            alpha = float(color[3]) if len(color) > 3 else 1.0
            scale_x = abs(float(state.get("scale_x", 0.0)))
            scale_y = abs(float(state.get("scale_y", 0.0)))
            if alpha > 1.0e-5 and scale_x > 1.0e-5 and scale_y > 1.0e-5:
                coverage = max(0.0, alpha) * scale_x * scale_y
                visible_scores[frame] = visible_scores.get(frame, 0.0) + coverage
    if visible_scores:
        peak = max(visible_scores.values())
        meaningful = max(1.0e-8, peak * 0.2)
        return min(
            frame
            for frame, coverage in visible_scores.items()
            if coverage >= meaningful
        )
    return first_history_frame or 1


def _write_attribute(curves, name, data_type, values, domain="POINT"):
    attribute = curves.attributes.new(name, data_type, domain)
    if data_type in {"FLOAT", "INT", "BOOLEAN"}:
        attribute.data.foreach_set("value", values)
    elif data_type == "FLOAT_VECTOR":
        flattened = [component for value in values for component in value]
        attribute.data.foreach_set("vector", flattened)
    else:
        raise ValueError(f"Unsupported cache attribute type: {data_type}")
    return attribute


def _converted_emitter_transform(matrix):
    """Convert a captured Blender emitter transform into cache coordinates."""
    location = matrix.translation
    origin = (float(location.x), -float(location.y), float(location.z))
    reflection = Matrix.Diagonal((1.0, -1.0, 1.0))
    rotation = (
        reflection @ matrix.to_quaternion().to_matrix() @ reflection
    ).to_quaternion()
    return origin, rotation


def _build_curve_cache(
    name,
    particles,
    effect_identifier,
    facing_mode,
    seed,
    direction_config=None,
    uv_correction=(1.0, 1.0),
    emitter_matrices=None,
    world_space=False,
):
    ordered = [
        particle
        for particle in sorted(particles, key=lambda item: int(item.id))
        if particle.history
    ]
    sizes = [len(particle.history) for particle in ordered]
    curves = bpy.data.hair_curves.new(name)
    if sizes:
        curves.add_curves(sizes=sizes)

    time_positions = []
    sampled_positions = []
    frames = []
    alive = []
    origins = []
    curve_particle_ids = []
    curve_random_values = []
    births = []
    deaths = []
    scale_x = []
    scale_y = []
    colors_r = []
    colors_g = []
    colors_b = []
    colors_a = []
    uv_offset_x = []
    uv_offset_y = []
    uv_scale_x = []
    uv_scale_y = []
    rotations = []
    directions = []
    correction_x, correction_y = uv_correction
    direction_config = direction_config if isinstance(direction_config, dict) else {}
    try:
        min_speed_threshold = float(
            direction_config.get("min_speed_threshold", 0.01)
        )
    except (TypeError, ValueError):
        min_speed_threshold = 0.01

    for particle in ordered:
        stable_random = _stable_fraction(
            seed,
            effect_identifier,
            int(particle.id),
        )
        history_frames = sorted(particle.history)
        birth_frame = int(history_frames[0])
        emitter_origin = (0.0, 0.0, 0.0)
        emitter_rotation = None
        if (
            world_space
            and emitter_matrices
            and 0 <= birth_frame < len(emitter_matrices)
        ):
            emitter_origin, emitter_rotation = _converted_emitter_transform(
                emitter_matrices[birth_frame]
            )
        curve_particle_ids.append(int(particle.id))
        curve_random_values.append(stable_random)
        births.append(float(history_frames[0]))
        deaths.append(float(history_frames[-1]))
        previous_direction = None
        for frame in history_frames:
            state = particle.history[frame]
            point_emitter_rotation = emitter_rotation
            point_emitter_origin = emitter_origin
            if (
                world_space
                and emitter_matrices
                and 0 <= frame < len(emitter_matrices)
                and (
                    facing_mode.startswith("emitter_transform_")
                    or getattr(particle, "local_space_rotation", False)
                )
            ):
                current_origin, point_emitter_rotation = (
                    _converted_emitter_transform(emitter_matrices[frame])
                )
                if getattr(particle, "local_space_position", False):
                    point_emitter_origin = current_origin
            position = _blender_vector(state["pos"])
            color = tuple(state.get("color", (1.0, 1.0, 1.0, 1.0)))
            uv_offset = tuple(state.get("uv_offset", (0.0, 0.0)))
            uv_scale = tuple(state.get("uv_scale", (1.0, 1.0)))
            direction = _state_direction(
                state,
                min_speed_threshold,
                previous_direction,
            )
            previous_direction = direction

            time_positions.append((float(frame), 0.0, 0.0))
            sampled_positions.append(tuple(position))
            frames.append(float(frame))
            alive.append(True)
            origins.append(point_emitter_origin)
            scale_x.append(float(state.get("scale_x", 1.0)))
            scale_y.append(float(state.get("scale_y", 1.0)))
            colors_r.append(float(color[0]))
            colors_g.append(float(color[1]))
            colors_b.append(float(color[2]))
            colors_a.append(float(color[3]))
            uv_offset_x.append(float(uv_offset[0]) * correction_x)
            uv_offset_y.append(
                1.0 - (1.0 - float(uv_offset[1])) * correction_y
            )
            uv_scale_x.append(float(uv_scale[0]) * correction_x)
            uv_scale_y.append(float(uv_scale[1]) * correction_y)
            rotations.append(
                _state_rotation(state, facing_mode, point_emitter_rotation)
            )
            directions.append(direction)

    if time_positions:
        curves.points.foreach_set(
            "position",
            [component for position in time_positions for component in position],
        )
    _write_attribute(curves, "mc_position", "FLOAT_VECTOR", sampled_positions)
    _write_attribute(curves, "mc_frame", "FLOAT", frames)
    _write_attribute(curves, "mc_alive", "BOOLEAN", alive)
    _write_attribute(curves, "mc_emitter_origin", "FLOAT_VECTOR", origins)
    _write_attribute(
        curves, "mc_particle_id", "INT", curve_particle_ids, "CURVE"
    )
    _write_attribute(
        curves, "mc_random", "FLOAT", curve_random_values, "CURVE"
    )
    _write_attribute(curves, "mc_birth", "FLOAT", births, "CURVE")
    _write_attribute(curves, "mc_death", "FLOAT", deaths, "CURVE")
    _write_attribute(curves, "mc_scale_x", "FLOAT", scale_x)
    _write_attribute(curves, "mc_scale_y", "FLOAT", scale_y)
    _write_attribute(curves, "mc_r", "FLOAT", colors_r)
    _write_attribute(curves, "mc_g", "FLOAT", colors_g)
    _write_attribute(curves, "mc_b", "FLOAT", colors_b)
    _write_attribute(curves, "mc_alpha", "FLOAT", colors_a)
    _write_attribute(curves, "uv_offset_x", "FLOAT", uv_offset_x)
    _write_attribute(curves, "uv_offset_y", "FLOAT", uv_offset_y)
    _write_attribute(curves, "uv_scale_x", "FLOAT", uv_scale_x)
    _write_attribute(curves, "uv_scale_y", "FLOAT", uv_scale_y)
    _write_attribute(curves, "mc_rotation", "FLOAT_VECTOR", rotations)
    _write_attribute(curves, "mc_direction", "FLOAT_VECTOR", directions)
    return curves, len(ordered), len(time_positions)

def _interface_socket(interface, name, in_out, socket_type, default=None):
    socket = interface.new_socket(
        name=name,
        in_out=in_out,
        socket_type=socket_type,
    )
    if default is not None and hasattr(socket, "default_value"):
        socket.default_value = default
    return socket


def set_modifier_input_value(modifier, socket_or_identifier, value):
    """Set a Geometry Nodes modifier input across Blender API generations.

    Blender 5.0 moved per-modifier interface values out of ID properties and
    into ``modifier.properties.inputs.<identifier>.value``. Blender 4.x uses
    ``modifier[identifier]``. Prefer the current API when it is available and
    retain the old path for supported 4.x releases.
    """
    identifier = str(
        getattr(socket_or_identifier, "identifier", socket_or_identifier)
        or ""
    )
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    socket_state = getattr(inputs, identifier, None) if inputs else None
    if socket_state is not None and hasattr(socket_state, "value"):
        socket_state.value = value
        return True
    modifier[identifier] = value
    return True


def get_modifier_input_value(modifier, socket_or_identifier):
    """Read a Geometry Nodes modifier input across Blender API generations."""
    identifier = str(
        getattr(socket_or_identifier, "identifier", socket_or_identifier)
        or ""
    )
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    socket_state = getattr(inputs, identifier, None) if inputs else None
    if socket_state is not None and hasattr(socket_state, "value"):
        return socket_state.value
    return modifier[identifier]


def _named_attribute(nodes, name, data_type="FLOAT"):
    node = nodes.new("GeometryNodeInputNamedAttribute")
    node.data_type = data_type
    node.inputs["Name"].default_value = name
    return node.outputs["Attribute"]


def _make_geometry_nodes(
    cache_object,
    material,
    facing_mode,
    *,
    world_space=False,
    baked_animation_scale=1.0,
    initial_animation_scale=1.0,
    initial_particle_scale=1.0,
    initial_speed=1.0,
    initial_density=1.0,
    initial_start_frame=1,
    initial_end_frame=250,
    loop_start_frame=0,
    loop_frame_count=0,
):
    group = bpy.data.node_groups.new(
        f"GN_{cache_object.name}",
        "GeometryNodeTree",
    )
    interface = group.interface
    geometry_input = _interface_socket(
        interface, "Geometry", "INPUT", "NodeSocketGeometry"
    )
    _interface_socket(
        interface, "Geometry", "OUTPUT", "NodeSocketGeometry"
    )
    animation_scale = _interface_socket(
        interface, "Animation Scale", "INPUT", "NodeSocketFloat",
        initial_animation_scale
    )
    particle_scale = _interface_socket(
        interface, "Particle Scale", "INPUT", "NodeSocketFloat",
        initial_particle_scale
    )
    speed = _interface_socket(
        interface, "Speed", "INPUT", "NodeSocketFloat", initial_speed
    )
    density = _interface_socket(
        interface, "Density", "INPUT", "NodeSocketFloat", initial_density
    )
    start_frame = _interface_socket(
        interface, "Start Frame", "INPUT", "NodeSocketFloat",
        float(initial_start_frame)
    )
    end_frame = _interface_socket(
        interface, "End Frame", "INPUT", "NodeSocketFloat",
        float(initial_end_frame)
    )
    for socket in (animation_scale, particle_scale, speed):
        if hasattr(socket, "min_value"):
            socket.min_value = 0.001
    if hasattr(density, "min_value"):
        density.min_value = 0.0
        density.max_value = 1.0

    nodes = group.nodes
    links = group.links
    group_input = nodes.new("NodeGroupInput")
    group_output = nodes.new("NodeGroupOutput")

    carriers = nodes.new("GeometryNodeCurveToPoints")
    carriers.mode = "COUNT"
    carriers.inputs["Count"].default_value = 1
    links.new(group_input.outputs["Geometry"], carriers.inputs["Curve"])
    original_index = nodes.new("GeometryNodeInputIndex")
    store_curve_index = nodes.new("GeometryNodeStoreNamedAttribute")
    store_curve_index.data_type = "INT"
    store_curve_index.domain = "POINT"
    store_curve_index.label = "Preserve cache curve index"
    store_curve_index.inputs["Name"].default_value = "mc_cache_curve_index"
    links.new(
        carriers.outputs["Points"], store_curve_index.inputs["Geometry"]
    )
    links.new(
        original_index.outputs["Index"], store_curve_index.inputs["Value"]
    )
    indexed_carriers = store_curve_index.outputs["Geometry"]
    carrier_index = _named_attribute(
        nodes, "mc_cache_curve_index", "INT"
    )

    # Map the scene range to cache frame 1 while speed scales elapsed time.
    scene_time = nodes.new("GeometryNodeInputSceneTime")
    elapsed = nodes.new("ShaderNodeMath")
    elapsed.operation = "SUBTRACT"
    links.new(scene_time.outputs["Frame"], elapsed.inputs[0])
    links.new(group_input.outputs["Start Frame"], elapsed.inputs[1])
    scaled_elapsed = nodes.new("ShaderNodeMath")
    scaled_elapsed.operation = "MULTIPLY"
    links.new(elapsed.outputs[0], scaled_elapsed.inputs[0])
    links.new(group_input.outputs["Speed"], scaled_elapsed.inputs[1])
    scaled_time = nodes.new("ShaderNodeMath")
    scaled_time.operation = "ADD"
    scaled_time.label = "Effective cache frame"
    if loop_start_frame > 0 and loop_frame_count > 0:
        loop_offset = nodes.new("ShaderNodeMath")
        loop_offset.operation = "MODULO"
        loop_offset.inputs[1].default_value = float(loop_frame_count)
        loop_offset.label = "Loop cache frame offset"
        links.new(scaled_elapsed.outputs[0], loop_offset.inputs[0])
        links.new(loop_offset.outputs[0], scaled_time.inputs[0])
        scaled_time.inputs[1].default_value = float(loop_start_frame)
    else:
        links.new(scaled_elapsed.outputs[0], scaled_time.inputs[0])
        scaled_time.inputs[1].default_value = 1.0

    # Curve to Points propagates these constant-per-track attributes onto
    # each carrier. Reading them in carrier context keeps particle age
    # independent per track; nesting Sample Curve here collapses the factor
    # to a loop-global age in Blender's field evaluation.
    birth = _named_attribute(nodes, "mc_birth")
    death = _named_attribute(nodes, "mc_death")
    random_value = _named_attribute(nodes, "mc_random")
    lifetime = nodes.new("ShaderNodeMath")
    lifetime.operation = "SUBTRACT"
    links.new(death, lifetime.inputs[0])
    links.new(birth, lifetime.inputs[1])
    safe_lifetime = nodes.new("ShaderNodeMath")
    safe_lifetime.operation = "MAXIMUM"
    safe_lifetime.inputs[1].default_value = 0.0001
    links.new(lifetime.outputs[0], safe_lifetime.inputs[0])
    age = nodes.new("ShaderNodeMath")
    age.operation = "SUBTRACT"
    links.new(scaled_time.outputs[0], age.inputs[0])
    links.new(birth, age.inputs[1])
    normalized_age = nodes.new("ShaderNodeMath")
    normalized_age.operation = "DIVIDE"
    links.new(age.outputs[0], normalized_age.inputs[0])
    links.new(safe_lifetime.outputs[0], normalized_age.inputs[1])
    factor = nodes.new("ShaderNodeClamp")
    factor.inputs["Min"].default_value = 0.0
    factor.inputs["Max"].default_value = 1.0
    links.new(normalized_age.outputs[0], factor.inputs["Value"])

    before_birth = nodes.new("ShaderNodeMath")
    before_birth.operation = "LESS_THAN"
    links.new(scaled_time.outputs[0], before_birth.inputs[0])
    links.new(birth, before_birth.inputs[1])
    after_death = nodes.new("ShaderNodeMath")
    after_death.operation = "GREATER_THAN"
    links.new(scaled_time.outputs[0], after_death.inputs[0])
    links.new(death, after_death.inputs[1])
    outside_lifetime = nodes.new("FunctionNodeBooleanMath")
    outside_lifetime.operation = "OR"
    links.new(before_birth.outputs[0], outside_lifetime.inputs[0])
    links.new(after_death.outputs[0], outside_lifetime.inputs[1])
    before_range = nodes.new("ShaderNodeMath")
    before_range.operation = "LESS_THAN"
    links.new(scene_time.outputs["Frame"], before_range.inputs[0])
    links.new(group_input.outputs["Start Frame"], before_range.inputs[1])
    after_range = nodes.new("ShaderNodeMath")
    after_range.operation = "GREATER_THAN"
    links.new(scene_time.outputs["Frame"], after_range.inputs[0])
    links.new(group_input.outputs["End Frame"], after_range.inputs[1])
    outside_range = nodes.new("FunctionNodeBooleanMath")
    outside_range.operation = "OR"
    links.new(before_range.outputs[0], outside_range.inputs[0])
    links.new(after_range.outputs[0], outside_range.inputs[1])
    outside_time = nodes.new("FunctionNodeBooleanMath")
    outside_time.operation = "OR"
    links.new(outside_lifetime.outputs[0], outside_time.inputs[0])
    links.new(outside_range.outputs[0], outside_time.inputs[1])
    inside_density = nodes.new("ShaderNodeMath")
    inside_density.operation = "LESS_THAN"
    links.new(random_value, inside_density.inputs[0])
    links.new(group_input.outputs["Density"], inside_density.inputs[1])
    outside_density = nodes.new("FunctionNodeBooleanMath")
    outside_density.operation = "NOT"
    links.new(inside_density.outputs[0], outside_density.inputs[0])
    delete_selection = nodes.new("FunctionNodeBooleanMath")
    delete_selection.operation = "OR"
    links.new(outside_time.outputs[0], delete_selection.inputs[0])
    links.new(outside_density.outputs[0], delete_selection.inputs[1])

    def sample_field(name, data_type="FLOAT"):
        sample = nodes.new("GeometryNodeSampleCurve")
        sample.mode = "FACTOR"
        sample.data_type = data_type
        sample.label = f"Sample {name}"
        links.new(group_input.outputs["Geometry"], sample.inputs["Curves"])
        links.new(
            _named_attribute(nodes, name, data_type),
            sample.inputs["Value"],
        )
        links.new(factor.outputs["Result"], sample.inputs["Factor"])
        links.new(carrier_index, sample.inputs["Curve Index"])
        return sample

    sampled_position = sample_field("mc_position", "FLOAT_VECTOR").outputs["Value"]
    if world_space:
        origin_sample = sample_field("mc_emitter_origin", "FLOAT_VECTOR")
        from_origin = nodes.new("ShaderNodeVectorMath")
        from_origin.operation = "SUBTRACT"
        links.new(sampled_position, from_origin.inputs[0])
        links.new(origin_sample.outputs["Value"], from_origin.inputs[1])
        live_scale_ratio = nodes.new("ShaderNodeMath")
        live_scale_ratio.operation = "DIVIDE"
        live_scale_ratio.inputs[1].default_value = max(
            0.001,
            float(baked_animation_scale),
        )
        links.new(
            group_input.outputs["Animation Scale"],
            live_scale_ratio.inputs[0],
        )
        scaled_offset = nodes.new("ShaderNodeVectorMath")
        scaled_offset.operation = "SCALE"
        links.new(from_origin.outputs["Vector"], scaled_offset.inputs[0])
        links.new(live_scale_ratio.outputs[0], scaled_offset.inputs[3])
        restored_origin = nodes.new("ShaderNodeVectorMath")
        restored_origin.operation = "ADD"
        restored_origin.label = "World-space animation scale"
        links.new(origin_sample.outputs["Value"], restored_origin.inputs[0])
        links.new(scaled_offset.outputs["Vector"], restored_origin.inputs[1])
        sampled_position = restored_origin.outputs["Vector"]

    set_position = nodes.new("GeometryNodeSetPosition")
    links.new(indexed_carriers, set_position.inputs["Geometry"])
    links.new(sampled_position, set_position.inputs["Position"])

    sampled = {
        name: sample_field(name)
        for name in (
            "mc_scale_x",
            "mc_scale_y",
            "mc_r",
            "mc_g",
            "mc_b",
            "mc_alpha",
            "uv_offset_x",
            "uv_offset_y",
            "uv_scale_x",
            "uv_scale_y",
        )
    }
    sampled["mc_rotation"] = sample_field("mc_rotation", "FLOAT_VECTOR")
    sampled["mc_direction"] = sample_field("mc_direction", "FLOAT_VECTOR")

    carrier_geometry = set_position.outputs["Geometry"]
    for name in (
        "mc_r",
        "mc_g",
        "mc_b",
        "mc_alpha",
        "uv_offset_x",
        "uv_offset_y",
        "uv_scale_x",
        "uv_scale_y",
    ):
        store = nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type = "FLOAT"
        store.domain = "POINT"
        store.inputs["Name"].default_value = name
        links.new(carrier_geometry, store.inputs["Geometry"])
        links.new(sampled[name].outputs["Value"], store.inputs["Value"])
        carrier_geometry = store.outputs["Geometry"]

    delete_geometry = nodes.new("GeometryNodeDeleteGeometry")
    delete_geometry.domain = "POINT"
    links.new(carrier_geometry, delete_geometry.inputs["Geometry"])
    links.new(delete_selection.outputs[0], delete_geometry.inputs["Selection"])

    grid = nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value = 1.0
    grid.inputs["Size Y"].default_value = 1.0
    grid.inputs["Vertices X"].default_value = 2
    grid.inputs["Vertices Y"].default_value = 2
    set_material = nodes.new("GeometryNodeSetMaterial")
    set_material.inputs["Selection"].default_value = True
    set_material.inputs["Material"].default_value = material
    store_uv = nodes.new("GeometryNodeStoreNamedAttribute")
    store_uv.data_type = "FLOAT2"
    store_uv.domain = "CORNER"
    store_uv.label = "Preserve billboard UVMap"
    store_uv.inputs["Name"].default_value = "UVMap"
    links.new(grid.outputs["Mesh"], store_uv.inputs["Geometry"])
    links.new(grid.outputs["UV Map"], store_uv.inputs["Value"])

    links.new(store_uv.outputs["Geometry"], set_material.inputs["Geometry"])

    combine_scale = nodes.new("ShaderNodeCombineXYZ")
    links.new(sampled["mc_scale_x"].outputs["Value"], combine_scale.inputs["X"])
    links.new(sampled["mc_scale_y"].outputs["Value"], combine_scale.inputs["Y"])
    combine_scale.inputs["Z"].default_value = 1.0
    scale_multiplier = group_input.outputs["Particle Scale"]
    if not world_space:
        compensate_scale = nodes.new("ShaderNodeMath")
        compensate_scale.operation = "DIVIDE"
        links.new(group_input.outputs["Particle Scale"], compensate_scale.inputs[0])
        links.new(group_input.outputs["Animation Scale"], compensate_scale.inputs[1])
        scale_multiplier = compensate_scale.outputs[0]
    scale_vector = nodes.new("ShaderNodeVectorMath")
    scale_vector.operation = "SCALE"
    links.new(combine_scale.outputs["Vector"], scale_vector.inputs[0])
    links.new(scale_multiplier, scale_vector.inputs[3])

    instance = nodes.new("GeometryNodeInstanceOnPoints")
    links.new(delete_geometry.outputs["Geometry"], instance.inputs["Points"])
    links.new(set_material.outputs["Geometry"], instance.inputs["Instance"])
    links.new(scale_vector.outputs["Vector"], instance.inputs["Scale"])

    authored_rotation = sampled["mc_rotation"].outputs["Value"]
    active_camera = nodes.new("GeometryNodeInputActiveCamera")
    camera_info = nodes.new("GeometryNodeObjectInfo")
    camera_info.transform_space = "RELATIVE"
    links.new(active_camera.outputs["Active Camera"], camera_info.inputs["Object"])
    position = nodes.new("GeometryNodeInputPosition")
    to_camera = nodes.new("ShaderNodeVectorMath")
    to_camera.operation = "SUBTRACT"
    links.new(camera_info.outputs["Location"], to_camera.inputs[0])
    links.new(position.outputs["Position"], to_camera.inputs[1])
    camera_direction = to_camera.outputs["Vector"]
    camera_back = nodes.new("FunctionNodeTransformDirection")
    camera_back.inputs["Direction"].default_value = (0.0, 0.0, 1.0)
    links.new(camera_info.outputs["Transform"], camera_back.inputs["Transform"])

    def flatten_world_up(direction_output):
        separate = nodes.new("ShaderNodeSeparateXYZ")
        flatten = nodes.new("ShaderNodeCombineXYZ")
        links.new(direction_output, separate.inputs["Vector"])
        links.new(separate.outputs["X"], flatten.inputs["X"])
        links.new(separate.outputs["Y"], flatten.inputs["Y"])
        flatten.inputs["Z"].default_value = 0.0
        return flatten.outputs["Vector"]

    def add_local_spin(base_rotation):
        spin_rotation = nodes.new("FunctionNodeEulerToRotation")
        links.new(authored_rotation, spin_rotation.inputs["Euler"])
        rotate = nodes.new("FunctionNodeRotateRotation")
        rotate.rotation_space = "LOCAL"
        links.new(base_rotation, rotate.inputs["Rotation"])
        links.new(spin_rotation.outputs["Rotation"], rotate.inputs["Rotate By"])
        return rotate.outputs["Rotation"]
    def no_camera_vertical_rotation():
        fallback_euler = nodes.new("ShaderNodeCombineXYZ")
        fallback_euler.label = "No-camera vertical fallback"
        fallback_euler.inputs["X"].default_value = math.pi / 2.0
        fallback_rotation = nodes.new("FunctionNodeEulerToRotation")
        fallback_rotation.label = "No-camera vertical fallback"
        links.new(
            fallback_euler.outputs["Vector"],
            fallback_rotation.inputs["Euler"],
        )
        return add_local_spin(fallback_rotation.outputs["Rotation"])

    camera_scale_length = nodes.new("ShaderNodeVectorMath")
    camera_scale_length.operation = "LENGTH"
    links.new(camera_info.outputs["Scale"], camera_scale_length.inputs[0])
    has_active_camera = nodes.new("ShaderNodeMath")
    has_active_camera.operation = "GREATER_THAN"
    has_active_camera.inputs[1].default_value = 0.5
    links.new(camera_scale_length.outputs["Value"], has_active_camera.inputs[0])

    def camera_or_vertical(camera_rotation):
        switch = nodes.new("GeometryNodeSwitch")
        switch.input_type = "ROTATION"
        switch.label = "Active camera or upright fallback"
        links.new(has_active_camera.outputs[0], switch.inputs["Switch"])
        links.new(no_camera_vertical_rotation(), switch.inputs["False"])
        links.new(camera_rotation, switch.inputs["True"])
        return switch.outputs["Output"]

    final_rotation = authored_rotation
    if facing_mode == "rotate_xyz":
        final_rotation = camera_or_vertical(
            add_local_spin(camera_info.outputs["Rotation"])
        )
    elif facing_mode in {"lookat_xyz", "lookat_y", "rotate_y"}:
        direction_output = (
            camera_back.outputs["Direction"]
            if facing_mode == "rotate_y"
            else camera_direction
        )
        if facing_mode in {"lookat_y", "rotate_y"}:
            direction_output = flatten_world_up(direction_output)
        align = nodes.new("FunctionNodeAlignEulerToVector")
        align.axis = "Z"
        align.pivot_axis = "Y"
        align.inputs["Factor"].default_value = 1.0
        links.new(authored_rotation, align.inputs["Rotation"])
        links.new(direction_output, align.inputs["Vector"])
        final_rotation = camera_or_vertical(align.outputs["Rotation"])
    elif facing_mode.startswith("direction_"):
        axes = nodes.new("FunctionNodeAxesToRotation")
        axes.primary_axis = {
            "direction_x": "X",
            "direction_y": "Y",
            "direction_z": "Z",
        }.get(facing_mode, "X")
        axes.secondary_axis = {
            "direction_x": "Y",
            "direction_y": "X",
            "direction_z": "Y",
        }.get(facing_mode, "Y")
        axes.inputs["Secondary Axis"].default_value = (0.0, 0.0, 1.0)
        links.new(
            sampled["mc_direction"].outputs["Value"],
            axes.inputs["Primary Axis"],
        )
        final_rotation = add_local_spin(axes.outputs["Rotation"])
    elif facing_mode == "lookat_direction":
        axes = nodes.new("FunctionNodeAxesToRotation")
        axes.primary_axis = "Z"
        axes.secondary_axis = "Y"
        links.new(camera_direction, axes.inputs["Primary Axis"])
        links.new(
            sampled["mc_direction"].outputs["Value"],
            axes.inputs["Secondary Axis"],
        )
        final_rotation = camera_or_vertical(
            add_local_spin(axes.outputs["Rotation"])
        )
    links.new(final_rotation, instance.inputs["Rotation"])

    realize = nodes.new("GeometryNodeRealizeInstances")
    links.new(instance.outputs["Instances"], realize.inputs["Geometry"])
    links.new(realize.outputs["Geometry"], group_output.inputs["Geometry"])

    modifier = cache_object.modifiers.new(
        name="Bedrock Particle Cache",
        type="NODES",
    )
    modifier.node_group = group
    for socket, value in (
        (geometry_input, None),
        (animation_scale, initial_animation_scale),
        (particle_scale, initial_particle_scale),
        (speed, initial_speed),
        (density, initial_density),
        (start_frame, initial_start_frame),
        (end_frame, initial_end_frame),
    ):
        if value is not None:
            set_modifier_input_value(modifier, socket, value)
    return modifier, group

def _positive_dimension(value):
    return (
        float(value)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
        else None
    )


def _billboard_uv_description(parser):
    billboard = parser.components.get("minecraft:particle_appearance_billboard")
    if billboard is None:
        billboard = parser.components.get(
            "minecraft:particle_appearance_stretched_billboard", {}
        )
    uv = billboard.get("uv", {}) if isinstance(billboard, dict) else {}
    has_pixel_region = isinstance(uv, dict) and (
        "flipbook" in uv or ("uv" in uv and "uv_size" in uv)
    )
    return uv, has_pixel_region


def _image_dimensions(texture_path):
    if not texture_path or not os.path.isfile(texture_path):
        return None
    try:
        image = bpy.data.images.load(texture_path, check_existing=True)
        dimensions = int(image.size[0]), int(image.size[1])
    except (RuntimeError, ValueError, OSError):
        return None
    return dimensions if dimensions[0] > 0 and dimensions[1] > 0 else None


def _uniform_texture_density_scale(
    declared_width,
    declared_height,
    actual_width,
    actual_height,
):
    """Accept uniformly upscaled resource-pack textures without noise."""
    if declared_width is None or declared_height is None:
        return False
    scale_x = float(actual_width) / float(declared_width)
    scale_y = float(actual_height) / float(declared_height)
    return (
        scale_x > 0.0
        and scale_y > 0.0
        and abs(scale_x - scale_y)
        <= 1.0e-6 * max(scale_x, scale_y, 1.0)
    )

def _single_axis_texture_density_scale(declared, actual):
    """Accept a lone logical atlas dimension scaled by whole pixels.

    Bedrock permits authors to declare just the axis used by their UV math.
    High-resolution packs then scale that logical axis by an integer while the
    omitted axis continues to use normalized coordinates. That is compatible data,
    not a size mismatch.
    """
    if declared is None:
        return False
    scale = float(actual) / float(declared)
    return (
        scale >= 1.0
        and abs(scale - round(scale)) <= 1.0e-6 * max(scale, 1.0)
    )


def _declared_dimension_compatible(
    declared,
    other_declared,
    actual,
    uniform_scale,
):
    if declared is None or declared == 1:
        return True
    if abs(float(declared) - float(actual)) <= 0.001:
        return True
    if uniform_scale:
        return True
    return (other_declared is None or other_declared == 1) and (
        _single_axis_texture_density_scale(declared, actual)
    )

def _uv_image_policy(parser, texture_path):
    """Return neutral scaling plus declared/image mismatch diagnostics.

    Simulation normalizes UVs using the authored coordinate grid, with omitted
    axes defaulting to 1. Physical image size does not change that grid.
    """
    uv, has_pixel_region = _billboard_uv_description(parser)
    dimensions = _image_dimensions(texture_path)
    if not has_pixel_region or dimensions is None:
        return (1.0, 1.0), []

    actual_width, actual_height = dimensions
    declared_width = _positive_dimension(uv.get("texture_width", 1))
    declared_height = _positive_dimension(uv.get("texture_height", 1))
    warnings = []
    identifier = parser.get_identifier()
    uniform_scale = _uniform_texture_density_scale(
        declared_width,
        declared_height,
        actual_width,
        actual_height,
    )
    width_compatible = _declared_dimension_compatible(
        declared_width, declared_height, actual_width, uniform_scale
    )
    height_compatible = _declared_dimension_compatible(
        declared_height, declared_width, actual_height, uniform_scale
    )
    if not width_compatible:
        warnings.append(
            f"{identifier}: declared texture_width {declared_width:g} differs "
            f"from image width {actual_width}; authored value retained"
        )
    if not height_compatible:
        warnings.append(
            f"{identifier}: declared texture_height {declared_height:g} differs "
            f"from image height {actual_height}; authored value retained"
        )
    return (1.0, 1.0), warnings

def _rescale_float_attribute(curves, name, transform):
    attribute = curves.attributes.get(name)
    if attribute is None:
        return
    values = [0.0] * len(attribute.data)
    attribute.data.foreach_get("value", values)
    attribute.data.foreach_set("value", [transform(value) for value in values])


def update_cache_texture_uv(render_objects, texture_path):
    """Rebuild missing-dimension UV normalization for a new cache texture."""
    dimensions = _image_dimensions(texture_path)
    if dimensions is None:
        raise ValueError(f"Unable to read texture dimensions: {texture_path}")
    actual_width, actual_height = dimensions
    warnings = []
    for obj in render_objects:
        if not bool(obj.get("mcbedrock_particle_cache", False)):
            continue
        if not bool(obj.get("mcbedrock_uv_pixel_region", False)):
            continue
        curves = getattr(obj, "data", None)
        if curves is None or not hasattr(curves, "attributes"):
            continue

        declared_width = _positive_dimension(
            obj.get("mcbedrock_uv_declared_width", 0.0)
        )
        declared_height = _positive_dimension(
            obj.get("mcbedrock_uv_declared_height", 0.0)
        )
        old_width = _positive_dimension(
            obj.get("mcbedrock_uv_image_width", 0.0)
        )
        old_height = _positive_dimension(
            obj.get("mcbedrock_uv_image_height", 0.0)
        )
        ratio_x = (
            1.0
            if declared_width is not None or old_width is None
            else old_width / actual_width
        )
        ratio_y = (
            1.0
            if declared_height is not None or old_height is None
            else old_height / actual_height
        )

        _rescale_float_attribute(
            curves, "uv_offset_x", lambda value: value * ratio_x
        )
        _rescale_float_attribute(
            curves, "uv_scale_x", lambda value: value * ratio_x
        )
        _rescale_float_attribute(
            curves,
            "uv_offset_y",
            lambda value: 1.0 - (1.0 - value) * ratio_y,
        )
        _rescale_float_attribute(
            curves, "uv_scale_y", lambda value: value * ratio_y
        )
        curves.update_tag()
        obj["mcbedrock_uv_correction_x"] = 1.0
        obj["mcbedrock_uv_correction_y"] = 1.0
        obj["mcbedrock_uv_image_width"] = actual_width
        obj["mcbedrock_uv_image_height"] = actual_height

        identifier = str(obj.get("mcbedrock_effect_identifier", obj.name))
        uniform_scale = _uniform_texture_density_scale(
            declared_width,
            declared_height,
            actual_width,
            actual_height,
        )
        width_compatible = _declared_dimension_compatible(
            declared_width, declared_height, actual_width, uniform_scale
        )
        height_compatible = _declared_dimension_compatible(
            declared_height, declared_width, actual_height, uniform_scale
        )
        if not width_compatible:
            warnings.append(
                f"{identifier}: declared texture_width {declared_width:g} "
                f"differs from image width {actual_width}; authored value retained"
            )
        if not height_compatible:
            warnings.append(
                f"{identifier}: declared texture_height {declared_height:g} "
                f"differs from image height {actual_height}; authored value retained"
            )
    return warnings

def build_gn_cache(
    *,
    context,
    emitter,
    collection,
    effects,
    filepath,
    texture_override,
    force_loop,
    interpolation,
    molang_context,
    parent_target,
    world_space,
    emitter_matrices,
    resolve_texture,
    create_material,
    create_billboard,
):
    """Create one Hair Curves/GN cache object per simulated effect."""
    del force_loop, molang_context, parent_target, create_billboard
    _require_backend_support()


    seed = int(emitter.get("mcbedrock_seed", 0))
    if not seed:
        source = os.path.normcase(os.path.abspath(filepath))
        seed = int.from_bytes(
            hashlib.sha256(source.encode("utf-8")).digest()[:4],
            "little",
        ) & 0x7FFFFFFF
        emitter["mcbedrock_seed"] = seed

    created_objects = []
    created_groups = []
    created_materials = []
    render_objects = []
    warnings = []
    playback_start = int(getattr(emitter, "mcbedrock_start_frame", 1))
    preview_frame = playback_start
    preview_coverage_score = -1.0
    total_particles = 0
    total_samples = 0
    empty_effect_count = 0
    try:
        for effect_identifier, effect in effects.items():
            parser = effect["parser"]
            particles = effect["particles"]
            active_particles = [particle for particle in particles if particle.history]
            timing = effect.get("timing", {})
            warm_loop_start = int(
                timing.get("warm_loop_start_frame", 0) or 0
            )
            warm_loop_end = int(
                timing.get("warm_loop_end_frame", 0) or 0
            )
            loop_frame_count = int(timing.get("cycle_frames", 0) or 0)
            maximum_cached_frame = (
                max(max(particle.history) for particle in active_particles)
                if active_particles
                else 0
            )
            has_cached_warm_loop = bool(
                active_particles
                and timing.get("is_looping", False)
                and warm_loop_start > 0
                and loop_frame_count > 0
                and warm_loop_end == warm_loop_start + loop_frame_count - 1
                and warm_loop_end <= maximum_cached_frame
            )
            available_loop_frames = max(
                0,
                maximum_cached_frame - warm_loop_start + 1,
            )
            cache_loop_frame_count = (
                (available_loop_frames // loop_frame_count)
                * loop_frame_count
                if has_cached_warm_loop
                else 0
            )
            effect_cache_preview_frame = (
                warm_loop_start
                if has_cached_warm_loop
                else (
                    _preview_frame_for_particles(active_particles)
                    if active_particles
                    else 1
                )
            )
            effect_scene_preview_frame = (
                playback_start
                if has_cached_warm_loop
                else playback_start + effect_cache_preview_frame - 1
            )
            effect_preview_score = 0.0
            for particle in active_particles:
                state = particle.history.get(effect_cache_preview_frame)
                if state is None:
                    continue
                color = state.get("color", (1.0, 1.0, 1.0, 1.0))
                alpha = float(color[3]) if len(color) > 3 else 1.0
                scale_x = abs(float(state.get("scale_x", 0.0)))
                scale_y = abs(float(state.get("scale_y", 0.0)))
                if alpha > 1.0e-5 and scale_x > 1.0e-5 and scale_y > 1.0e-5:
                    effect_preview_score += (
                        max(0.0, alpha) * scale_x * scale_y
                    )
            if effect_preview_score > preview_coverage_score:
                preview_frame = effect_scene_preview_frame
                preview_coverage_score = effect_preview_score
            short_name = (
                effect_identifier.split(":")[-1]
                if ":" in effect_identifier
                else effect_identifier
            )
            if texture_override and os.path.isfile(texture_override):
                texture_path = texture_override
            elif effect.get("texture_path") and os.path.isfile(
                effect["texture_path"]
            ):
                texture_path = effect["texture_path"]
            else:
                texture_path = resolve_texture(
                    context,
                    parser,
                    parser.filepath,
                )
            if not os.path.isfile(texture_path):
                warnings.append(
                    f"Texture not found: '{parser.get_texture_path()}'"
                )
            uv_correction, uv_warnings = _uv_image_policy(
                parser,
                texture_path,
            )
            warnings.extend(uv_warnings)
            curves, particle_count, sample_count = _build_curve_cache(
                f"ParticleCache_{short_name}",
                active_particles,
                effect_identifier,
                parser.get_billboard_mode(),
                seed,
                parser.get_billboard_direction(),
                uv_correction,
                emitter_matrices,
                world_space,
            )
            cache_object = bpy.data.objects.new(
                f"ParticleCache_{short_name}",
                curves,
            )
            collection.objects.link(cache_object)
            created_objects.append(cache_object)
            if not world_space:
                cache_object.parent = emitter
            cache_object["mcbedrock_particle_cache"] = True
            cache_object["mcbedrock_effect_identifier"] = effect_identifier
            cache_object["mcbedrock_particle_count"] = particle_count
            cache_object["mcbedrock_sample_count"] = sample_count
            is_empty_preview = not active_particles
            cache_object["mcbedrock_empty_preview"] = is_empty_preview
            empty_reason = next(
                (
                    str(item.get("code", ""))
                    for item in effect.get("diagnostics", ())
                    if isinstance(item, dict)
                    and (
                        str(item.get("code", "")).startswith(
                            "particle.preview.empty"
                        )
                        or str(item.get("code", ""))
                        == "particle.preview.manual_emitter"
                    )
                ),
                "",
            )
            cache_object["mcbedrock_empty_preview_reason"] = empty_reason
            if is_empty_preview:
                empty_effect_count += 1
            cache_object["mcbedrock_cache_schema"] = 2
            cache_object["mcbedrock_world_space"] = bool(world_space)
            effect_diagnostics = list(effect.get("diagnostics", ()) or ())
            cache_object["mcbedrock_diagnostics_json"] = json.dumps(
                effect_diagnostics,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            cache_object["mcbedrock_diagnostic_count"] = len(
                effect_diagnostics
            )
            cache_object["mcbedrock_preview_limitation_count"] = sum(
                1
                for item in effect_diagnostics
                if isinstance(item, dict)
                and (
                    str(item.get("category", "")).casefold()
                    == "preview_environment"
                    or str(item.get("presentation", "")).casefold()
                    in {"metadata", "stored", "stored_only"}
                    or str(item.get("code", "")) in {
                        "particle.world.collision_unavailable",
                        "particle.world.block_unavailable",
                        "particle.event.sound_unavailable",
                    }
                )
            )
            cache_object["mcbedrock_baked_animation_scale"] = float(
                emitter.mcbedrock_animation_scale
            )
            timing = effect.get("timing", {})
            cache_object["mcbedrock_timing_warm_loop_cached"] = bool(
                has_cached_warm_loop
            )
            cache_object["mcbedrock_cache_loop_frame_count"] = int(
                cache_loop_frame_count
            )
            for key in (
                "fps",
                "active_frames",
                "sleep_frames",
                "cycle_frames",
                "warm_loop_start_frame",
                "warm_loop_end_frame",
            ):
                cache_object["mcbedrock_timing_" + key] = int(
                    timing.get(key, 0) or 0
                )
            for key in (
                "active_time_seconds",
                "sleep_time_seconds",
                "cycle_time_seconds",
                "maximum_particle_lifetime_seconds",
            ):
                cache_object["mcbedrock_timing_" + key] = float(
                    timing.get(key, 0.0) or 0.0
                )
            cache_object["mcbedrock_timing_is_looping"] = bool(
                timing.get("is_looping", False)
            )
            uv_description, has_pixel_region = _billboard_uv_description(parser)
            declared_width = _positive_dimension(
                uv_description.get("texture_width", 1)
            )
            declared_height = _positive_dimension(
                uv_description.get("texture_height", 1)
            )
            image_dimensions = _image_dimensions(texture_path) or (0, 0)
            cache_object["mcbedrock_uv_pixel_region"] = has_pixel_region
            cache_object["mcbedrock_uv_declared_width"] = declared_width or 0.0
            cache_object["mcbedrock_uv_declared_height"] = declared_height or 0.0
            cache_object["mcbedrock_uv_correction_x"] = uv_correction[0]
            cache_object["mcbedrock_uv_correction_y"] = uv_correction[1]
            cache_object["mcbedrock_uv_image_width"] = image_dimensions[0]
            cache_object["mcbedrock_uv_image_height"] = image_dimensions[1]
            texture_resolution = effect.get("texture_resolution", {})
            cache_object["mcbedrock_texture_logical_id"] = str(
                texture_resolution.get(
                    "logical_id", parser.get_texture_path()
                )
            )
            cache_object["mcbedrock_texture_source_kind"] = str(
                texture_resolution.get("source_kind", "")
            )
            cache_object["mcbedrock_texture_pack_root"] = str(
                texture_resolution.get("pack_root", "")
            )
            cache_object["mcbedrock_texture_priority"] = int(
                texture_resolution.get("priority", 0) or 0
            )
            cache_object["mcbedrock_resolved_texture"] = str(
                texture_path or ""
            )
            material = create_material(
                cache_object,
                texture_path,
                parser.get_material_type(),
                is_lit=parser.has_lighting(),
                interpolation=interpolation,
                attribute_source="GEOMETRY",
            )
            created_materials.append(material)
            _modifier, group = _make_geometry_nodes(
                cache_object,
                material,
                parser.get_billboard_mode(),
                world_space=world_space,
                baked_animation_scale=cache_object[
                    "mcbedrock_baked_animation_scale"
                ],
                initial_animation_scale=max(
                    0.001, float(emitter.mcbedrock_animation_scale)
                ),
                initial_particle_scale=max(
                    0.001, float(emitter.mcbedrock_particle_scale)
                ),
                initial_speed=max(
                    0.001, float(emitter.mcbedrock_speed_scale)
                ),
                initial_density=max(
                    0.0, min(1.0, float(emitter.mcbedrock_density))
                ),
                initial_start_frame=int(emitter.mcbedrock_start_frame),
                initial_end_frame=int(emitter.mcbedrock_end_frame),
                loop_start_frame=(
                    warm_loop_start if has_cached_warm_loop else 0
                ),
                loop_frame_count=(
                    cache_loop_frame_count if has_cached_warm_loop else 0
                ),
            )
            created_groups.append(group)
            render_objects.append(cache_object)
            total_particles += particle_count
            total_samples += sample_count
    except Exception:
        for obj in reversed(created_objects):
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data and data.users == 0:
                bpy.data.hair_curves.remove(data)
        for group in reversed(created_groups):
            if group.users == 0:
                bpy.data.node_groups.remove(group)
        for material in reversed(created_materials):
            if material is not None and material.users == 0:
                bpy.data.materials.remove(material)
        raise

    emitter["mcbedrock_preview_frame"] = int(preview_frame)
    emitter["mcbedrock_empty_effect_count"] = int(empty_effect_count)
    root_timing = next(
        (
            dict(effect.get("timing", {}))
            for effect in effects.values()
            if effect.get("timing")
        ),
        {},
    )
    for key in (
        "fps",
        "active_frames",
        "sleep_frames",
        "cycle_frames",
        "warm_loop_start_frame",
        "warm_loop_end_frame",
    ):
        emitter["mcbedrock_timing_" + key] = int(
            root_timing.get(key, 0) or 0
        )
    for key in (
        "active_time_seconds",
        "sleep_time_seconds",
        "cycle_time_seconds",
        "maximum_particle_lifetime_seconds",
    ):
        emitter["mcbedrock_timing_" + key] = float(
            root_timing.get(key, 0.0) or 0.0
        )
    emitter["mcbedrock_timing_is_looping"] = bool(
        root_timing.get("is_looping", False)
    )
    return {
        "render_objects": tuple(render_objects),
        "warning": " | ".join(warnings) or None,
        "particle_count": total_particles,
        "sample_count": total_samples,
        "empty_effect_count": empty_effect_count,
        "preview_frame": int(preview_frame),
    }

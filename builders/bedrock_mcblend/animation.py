"""Bedrock animation application for the MCblend-inspired importer."""

import math
import re

import bpy
from mathutils import Matrix

from ...importers.bedrock_assets import load_json_file, load_json_shared, normalize_identifier, safe_name
from .bedrock_animation import (
    AnimationPlaybackState,
    BedrockLocalPose,
    bedrock_local_pose_matrix,
    animation_relative_to_entity,
    animation_wall_duration,
    animation_loop_mode,
    animation_duration,
    animation_uses_movement_queries,
    authored_clip_sample_time,
    animation_has_root_motion,
    animation_uses_molang,
    channel_is_time_varying,
    resolve_pose_bone_name,
    sample_animation_channel,
)


def _stored_source_rest_matrix(bone):
    values = bone.get("mcbedrock_source_rest_matrix")
    if values is None:
        return None
    try:
        if len(values) != 16:
            return None
        return Matrix(tuple(
            tuple(float(values[row * 4 + column]) for column in range(4))
            for row in range(4)
        ))
    except (TypeError, ValueError, IndexError):
        return None


def _bone_pose_context(pose_bone):
    pose_bone.rotation_mode = "XZY"
    display_matrix = pose_bone.bone.matrix_local
    source_matrix = _stored_source_rest_matrix(pose_bone.bone)
    if source_matrix is None:
        # Backward compatibility for armatures imported before aligned rigs
        # began recording their authored absolute rest matrices.
        local_matrix = (
            pose_bone.parent.bone.matrix_local.inverted() @ display_matrix
            if pose_bone.parent else display_matrix
        )
        return (
            local_matrix.to_3x3().normalized(),
            display_matrix.to_3x3().normalized(),
        )

    if pose_bone.parent:
        source_parent = _stored_source_rest_matrix(pose_bone.parent.bone)
        # A synthetic entity_root has no authored Bedrock orientation.
        source_parent_basis = (
            source_parent.to_3x3().normalized()
            if source_parent is not None else Matrix.Identity(3)
        )
    else:
        source_parent_basis = Matrix.Identity(3)
    display_basis = display_matrix.to_3x3().normalized()
    rest_basis = source_parent_basis.inverted_safe() @ display_basis
    entity_basis = display_basis
    return rest_basis, entity_basis


def _set_pose_transform(
    pose_bone,
    rest_basis,
    entity_basis,
    position=(0.0, 0.0, 0.0),
    rotation=(0.0, 0.0, 0.0),
    scale=(1.0, 1.0, 1.0),
    relative_to_entity=False,
):
    """Apply one complete source-space TRS delta to a Blender pose bone."""
    pose = BedrockLocalPose(
        position=tuple(position),
        rotation=tuple(rotation),
        scale=tuple(scale),
    )
    reflected = bool(
        pose_bone.bone.get("mcbedrock_native_reflected", "")
        and not (
            pose_bone.parent
            and pose_bone.parent.bone.get("mcbedrock_native_reflected", "")
        )
    )
    reflection = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))

    def source_matrix(source_pose):
        matrix = Matrix(bedrock_local_pose_matrix(source_pose))
        return reflection @ matrix @ reflection if reflected else matrix

    if relative_to_entity:
        # ``relative_to.rotation = entity`` changes the rotation basis only;
        # position and scale retain the bone's local rest basis.
        rest = rest_basis.to_4x4()
        entity = entity_basis.to_4x4()
        position_matrix = source_matrix(
            BedrockLocalPose(position=pose.position)
        )
        rotation_matrix = source_matrix(
            BedrockLocalPose(rotation=pose.rotation)
        )
        scale_matrix = source_matrix(
            BedrockLocalPose(scale=pose.scale)
        )
        local_matrix = (
            rest.inverted_safe() @ position_matrix @ rest
            @ entity.inverted_safe() @ rotation_matrix @ entity
            @ rest.inverted_safe() @ scale_matrix @ rest
        )
    else:
        rest = rest_basis.to_4x4()
        # Blockbench (and the game) add animated angles to the bone's rest
        # angles and build one rotation: R(rest + anim). Relative to the rest
        # pose that is R(rest + anim) @ S @ R(rest)^-1, which differs from
        # R(anim) @ R(rest) whenever the two turn about different axes.
        rest_rotation = tuple(
            float(value) for value in
            (pose_bone.bone.get("mcbedrock_rest_rotation") or (0.0, 0.0, 0.0))
        )
        combined = BedrockLocalPose(
            position=pose.position,
            rotation=tuple(
                rest_rotation[axis] + float(pose.rotation[axis]) for axis in range(3)
            ),
            scale=pose.scale,
        )
        delta = (
            source_matrix(combined)
            @ source_matrix(BedrockLocalPose(rotation=rest_rotation)).inverted_safe()
        )
        local_matrix = rest.inverted_safe() @ delta @ rest

    location, rotation_quaternion, local_scale = local_matrix.decompose()
    pose_bone.rotation_mode = "XZY"
    pose_bone.location = location
    pose_bone.rotation_euler = rotation_quaternion.to_euler(
        "XZY", pose_bone.rotation_euler
    )
    pose_bone.scale = local_scale

    # A rotated nonuniform scale can produce shear, which Blender F-curves
    # cannot encode losslessly as location/rotation/scale. Preserve the best
    # decomposed pose and expose a machine-readable compatibility marker.
    reconstructed = (
        Matrix.Translation(location)
        @ rotation_quaternion.to_matrix().to_4x4()
        @ Matrix.Diagonal((*local_scale, 1.0))
    )
    has_shear = any(
        abs(local_matrix[row][column] - reconstructed[row][column]) > 1.0e-5
        for row in range(4)
        for column in range(4)
    )
    if has_shear:
        pose_bone["mcbedrock_animation_visual_only_shear"] = True
    return has_shear


def _reset_pose_bones(armature):
    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "XZY"
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)


def _iter_script_statements(client_description, script_name):
    scripts = (
        client_description.get("scripts", {})
        if isinstance(client_description, dict)
        else {}
    )
    values = scripts.get(script_name, []) if isinstance(scripts, dict) else []
    if isinstance(values, (str, dict)):
        values = [values]

    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item)

    yield from walk(values)


_BONE_QUERY_ASSIGNMENT = re.compile(
    r"(?:variable|v)\.([A-Za-z0-9_.]+)\s*=\s*"
    r"(?:query|q)\.(bone_origin|bone_rotation|bone_aabb)\s*"
    r"\(\s*['\\\"]([^'\\\"]+)['\\\"]\s*\)",
    re.IGNORECASE,
)


def _find_pose_bone(armature, bone_name):
    if not armature or getattr(armature, "type", "") != "ARMATURE":
        return None
    wanted = str(bone_name or "").lower()
    return next(
        (bone for bone in armature.pose.bones if bone.name.lower() == wanted),
        None,
    )


def _bedrock_pivot_from_pose_bone(pose_bone):
    pivot = pose_bone.bone.get("mcbedrock_pivot", (0.0, 0.0, 0.0))
    return (
        float(pivot[0]) * 16.0,
        float(pivot[2]) * 16.0,
        float(pivot[1]) * 16.0,
    )


def _static_channel_value(pose_bone, channel, default):
    """Current additive animation-channel value for MoLang ``this``.

    Bedrock starts each frame from the geometry bind pose, then accumulates
    animation channels in authored order.  Bind pivots/rotations are therefore
    not the initial value of ``this``.  Keeping the accumulator on the pose
    bone also lets consecutive unconditional/controller animations compose.
    """
    key = f"mcbedrock_static_{channel}"
    value = pose_bone.get(key, default)
    try:
        return tuple(float(value[index]) for index in range(3))
    except (TypeError, ValueError, IndexError):
        return tuple(default)


def _baked_source_channel_value(pose_bone, channel, default):
    """Source-channel accumulator already folded into the current rest rig."""
    value = pose_bone.get(f"mcbedrock_baked_source_{channel}", default)
    try:
        return tuple(float(value[index]) for index in range(3))
    except (TypeError, ValueError, IndexError):
        return tuple(default)


def _position_this_values(pose_bone, current):
    """Return Bedrock's current local bone origin for a position channel."""
    parent = pose_bone.parent
    own_pivot = _bedrock_pivot_from_pose_bone(pose_bone)
    # Bedrock entity animation roots are expressed relative to the renderer's
    # canonical model origin at Y=24. ``entity_root`` is importer-only, so a
    # source root parented to it must retain that same coordinate basis. This
    # lets source setup channels such as ``target - this`` cancel the authored
    # root origin instead of becoming a second, additive translation.
    source_root = (
        parent is None
        or parent.bone.get("mcbedrock_synthetic_root", False)
    )
    if source_root:
        parent_pivot = (0.0, 24.0, 0.0)
    else:
        # Explicit source children remain local to their authored parent.
        parent_pivot = (
            _bedrock_pivot_from_pose_bone(parent)
            if "mcbedrock_pivot" in parent.bone
            else (0.0, 24.0, 0.0)
        )
    return tuple(
        own_pivot[index] - parent_pivot[index] + float(current[index])
        for index in range(3)
    )


def _add_channel_values(left, right):
    return tuple(float(left[index]) + float(right[index]) for index in range(3))


def _bone_aabb_bedrock(armature, pose_bone):
    points = []
    wanted = pose_bone.name.lower()
    for child in armature.children:
        if getattr(child, "type", "") != "MESH":
            continue
        group = next(
            (
                candidate
                for candidate in child.vertex_groups
                if candidate.name.lower() == wanted
            ),
            None,
        )
        if group is None:
            continue
        for vertex in child.data.vertices:
            if any(
                membership.group == group.index and membership.weight > 0.0
                for membership in vertex.groups
            ):
                point = vertex.co
                points.append((
                    float(point.x) * 16.0,
                    float(point.z) * 16.0,
                    float(point.y) * 16.0,
                ))
    if not points:
        points = [_bedrock_pivot_from_pose_bone(pose_bone)]
    return (
        tuple(min(point[axis] for point in points) for axis in range(3)),
        tuple(max(point[axis] for point in points) for axis in range(3)),
    )


def _populate_bone_query_assignment(statement, evaluation_context, armature):
    """Flatten MoLang vector/struct bone queries into scalar context keys."""
    variables = evaluation_context.setdefault("v", {})
    handled = False
    for match in _BONE_QUERY_ASSIGNMENT.finditer(str(statement or "")):
        variable_name, query_name, bone_name = match.groups()
        pose_bone = _find_pose_bone(armature, bone_name)
        if pose_bone is None:
            continue
        handled = True
        variables[variable_name] = 0.0
        if query_name.lower() == "bone_origin":
            value = _bedrock_pivot_from_pose_bone(pose_bone)
            for axis, component in zip("xyz", value):
                variables[f"{variable_name}.{axis}"] = component
        elif query_name.lower() == "bone_rotation":
            value = pose_bone.bone.get(
                "mcbedrock_rest_rotation",
                (0.0, 0.0, 0.0),
            )
            for axis, component in zip("xyz", value):
                variables[f"{variable_name}.{axis}"] = float(component)
        else:
            minimum, maximum = _bone_aabb_bedrock(armature, pose_bone)
            for prefix, value in (("min", minimum), ("max", maximum)):
                for axis, component in zip("xyz", value):
                    variables[f"{variable_name}.{prefix}.{axis}"] = component
    return handled


def _new_animation_context(
    client_description=None,
    query_context=None,
    armature=None,
):
    from ...molang_parser import execute_statements

    query = {
        "anim_time": 0.0,
        "life_time": 0.0,
        "delta_time": 0.0,
        "frame_alpha": 0.0,
        "shake_time": 0.0,
        "target_x_rotation": 0.0,
        "target_y_rotation": 0.0,
        "body_y_rotation": 0.0,
        "is_moving": 0.0,
        "ground_speed": 0.0,
        "modified_move_speed": 0.0,
        "modified_distance_moved": 0.0,
        "model_scale": 0.0625,
        "is_baby": 0.0,
        "is_on_ground": 1.0,
        "is_riding": 0.0,
        "vertical_speed": 0.0,
        "head_x_rotation": lambda _index=0: 0.0,
        "head_y_rotation": lambda _index=0: 0.0,
        # The camera a few blocks in front, as for the entity preview.
        "distance_from_camera": 16.0,
        "rotation_to_camera": lambda _index=0: 0.0,
    }
    explicit_query_keys = set()
    if isinstance(query_context, dict):
        query.update(query_context)
        explicit_query_keys.update(query_context)
        default_keys = query.pop("_mcbedrock_default_keys", ())
        explicit_query_keys.difference_update(default_keys)
        explicit_query_keys.discard("_mcbedrock_default_keys")
    properties = query.pop("properties", {})
    if isinstance(properties, dict):
        query["property"] = lambda name="": properties.get(str(name), 0.0)
    evaluation_context = {
        "q": query,
        "v": {},
        "_mcbedrock_explicit_query_keys": explicit_query_keys,
        "_mcbedrock_armature": armature,
    }
    for statement in _iter_script_statements(client_description, "initialize"):
        _populate_bone_query_assignment(
            statement,
            evaluation_context,
            armature,
        )
        execute_statements(statement, evaluation_context)
    return evaluation_context


def _update_animation_context(
    evaluation_context,
    client_description,
    time_value,
    fps,
    preview_shake=False,
    preview_motion=False,
    animation=None,
):
    from ...molang_parser import execute_statements

    query = evaluation_context.setdefault("q", {})
    query["life_time"] = float(time_value)
    query["delta_time"] = 1.0 / float(max(1, fps))
    query["frame_alpha"] = 0.0
    if preview_motion:
        evaluation_context.setdefault("v", {}).setdefault("gliding_speed_value", 1.0)
        explicit = set(evaluation_context.get("_mcbedrock_explicit_query_keys", ()))
        speed = float(query.get("ground_speed", 0.0) or 0.0)
        if "ground_speed" not in explicit and abs(speed) <= 1.0e-8:
            speed = 1.0
            query["ground_speed"] = speed
        if "modified_move_speed" not in explicit:
            query["modified_move_speed"] = speed
        # Bedrock locomotion expressions measure travel in model pixels while
        # ground speed is expressed in blocks per second. One block is 16
        # model pixels; omitting that conversion makes procedural walk cycles
        # (notably horses) play at roughly one sixteenth their authored rate.
        preview_distance = float(time_value) * speed * 16.0
        if "modified_distance_moved" not in explicit:
            query["modified_distance_moved"] = preview_distance
        if "distance_moved" not in explicit:
            query["distance_moved"] = preview_distance
        if "is_moving" not in explicit:
            query["is_moving"] = abs(speed) > 1.0e-8
    armature = evaluation_context.get("_mcbedrock_armature")
    for statement in _iter_script_statements(client_description, "pre_animation"):
        _populate_bone_query_assignment(
            statement,
            evaluation_context,
            armature,
        )
        execute_statements(statement, evaluation_context)

    return evaluation_context




def apply_static_bedrock_pose(
    armature,
    filepath,
    animation_filter,
    client_description=None,
    query_context=None,
    static_neutral=False,
    sample_time=0.0,
):
    """Apply one zero-duration animation without creating an Action.

    Static-neutral sampling keeps source position assembly at time zero while
    omitting time-varying rotation and scale motion.
    """
    data = load_json_shared(filepath)
    animations = data.get("animations", {}) if isinstance(data, dict) else {}
    animation = animations.get(animation_filter) if isinstance(animations, dict) else None
    if not isinstance(animation, dict):
        return False
    if not armature or armature.type != "ARMATURE":
        return False

    evaluation_context = _new_animation_context(
        client_description,
        query_context,
        armature=armature,
    )
    _update_animation_context(
        evaluation_context,
        client_description,
        float(sample_time),
        24,
        preview_shake=False,
        animation=animation,
    )
    pose_bone_names = list(armature.pose.bones.keys())
    applied = False
    evaluation_context.setdefault("q", {})["anim_time"] = float(sample_time)
    bones = animation.get("bones", {})
    for source_name, channels in bones.items() if isinstance(bones, dict) else []:
        resolved_name = resolve_pose_bone_name(source_name, pose_bone_names)
        pose_bone = armature.pose.bones.get(resolved_name) if resolved_name else None
        if not pose_bone or not isinstance(channels, dict):
            continue
        rest_basis, entity_basis = _bone_pose_context(pose_bone)
        source_pose = {
            channel: _static_channel_value(
                pose_bone,
                channel,
                (1.0, 1.0, 1.0) if channel == "scale" else (0.0, 0.0, 0.0),
            )
            for channel in ("position", "rotation", "scale")
        }
        bone_applied = False
        for channel in ("position", "rotation", "scale"):
            if channel not in channels:
                continue
            if (
                static_neutral
                and channel in {"rotation", "scale"}
                and channel_is_time_varying(channels[channel])
            ):
                continue
            default = (1.0, 1.0, 1.0) if channel == "scale" else (0.0, 0.0, 0.0)
            current = source_pose[channel]
            this_values = (
                _position_this_values(pose_bone, current)
                if channel == "position"
                else current
            )
            contribution = sample_animation_channel(
                channels[channel],
                float(sample_time),
                default=default,
                evaluation_context=evaluation_context,
                this_values=this_values,
            )
            combined = (
                contribution
                if channel == "scale"
                else _add_channel_values(current, contribution)
            )
            source_pose[channel] = combined
            pose_bone[f"mcbedrock_static_{channel}"] = list(combined)
            bone_applied = True
        if bone_applied:
            _set_pose_transform(
                pose_bone,
                rest_basis,
                entity_basis,
                **source_pose,
                relative_to_entity=animation_relative_to_entity(channels),
            )
            applied = True
    if applied:
        armature["mcbedrock_static_animation_pose"] = animation_filter
        armature["mcbedrock_static_animation_phase"] = float(sample_time)
    return applied


def _iter_action_fcurves(action):
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return fcurves
    curves = []
    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            channelbag = getattr(strip, "channelbag", None)
            if channelbag and getattr(channelbag, "fcurves", None) is not None:
                curves.extend(channelbag.fcurves)
            for channelbag in getattr(strip, "channelbags", []) or []:
                if getattr(channelbag, "fcurves", None) is not None:
                    curves.extend(channelbag.fcurves)
    return curves


def apply_bedrock_animations(
    context,
    armature,
    filepath,
    animation_filter="",
    append_nla=False,
    client_description=None,
    query_context=None,
    procedural_preview_duration=1.0,
):
    data = load_json_file(filepath)
    animations = data.get("animations", {}) if isinstance(data, dict) else {}
    if not isinstance(animations, dict) or not animations:
        raise ValueError("No Bedrock animations found in JSON")
    if not armature or armature.type != "ARMATURE":
        raise ValueError("Select or import an armature before importing animation JSON")
    if animation_filter == "__NONE__":
        armature.animation_data_clear()
        return []

    fps = max(1, int(context.scene.render.fps or 24))
    created_actions = []
    filter_name = "" if animation_filter in ("", "__ALL__") else animation_filter
    armature.animation_data_create()

    for animation_name, animation in animations.items():
        if filter_name and normalize_identifier(animation_name) != normalize_identifier(filter_name):
            continue
        if not isinstance(animation, dict):
            continue

        loop_mode = animation_loop_mode(animation)
        loops = loop_mode == "loop"
        source_duration = animation_duration(animation)
        procedural = source_duration <= 1.0e-8 and animation_uses_molang(animation)
        evaluation_context = _new_animation_context(
            client_description,
            query_context,
            armature=armature,
        )
        preview_motion = animation_uses_movement_queries(
            animation, client_description
        )
        duration = (
            max(
                4.0 if preview_motion else 0.0,
                max(0.0, float(procedural_preview_duration)),
            )
            if procedural
            else source_duration
        )
        frame_count = max(0, int(math.ceil(duration * fps)))
        playback = AnimationPlaybackState(animation)
        preview_shake = False

        action = bpy.data.actions.new(name=f"MC_{safe_name(animation_name)}")
        armature.animation_data.action = action
        _reset_pose_bones(armature)

        bones = animation.get("bones", {})
        if not isinstance(bones, dict):
            continue

        pose_bone_names = list(armature.pose.bones.keys())
        animated = []
        for source_name, channels in bones.items():
            resolved_name = resolve_pose_bone_name(source_name, pose_bone_names)
            pose_bone = armature.pose.bones.get(resolved_name) if resolved_name else None
            if not pose_bone or not isinstance(channels, dict):
                continue
            rest_basis, entity_basis = _bone_pose_context(pose_bone)
            channel_names = [
                channel for channel in ("position", "rotation", "scale")
                if channel in channels
            ]
            if channel_names:
                animated.append((
                    source_name,
                    pose_bone,
                    channels,
                    channel_names,
                    rest_basis,
                    entity_basis,
                ))

        action_has_shear = False
        for frame_index in range(frame_count + 1):
            time_value = min(duration, frame_index / float(fps)) if duration else 0.0
            _update_animation_context(
                evaluation_context,
                client_description,
                time_value,
                fps,
                preview_shake=preview_shake,
                preview_motion=preview_motion,
                animation=animation,
            )
            sample_time = authored_clip_sample_time(animation, time_value)
            if loops and duration > 1.0e-8 and frame_index == frame_count:
                sample_time = 0.0
            evaluation_context.setdefault("q", {})["anim_time"] = sample_time
            animation_weight = playback.blend_weight(evaluation_context)
            frame_number = 1 + frame_index
            for (
                _source_name,
                pose_bone,
                channels,
                channel_names,
                rest_basis,
                entity_basis,
            ) in animated:
                source_pose = {
                    "position": (0.0, 0.0, 0.0),
                    "rotation": (0.0, 0.0, 0.0),
                    "scale": (1.0, 1.0, 1.0),
                }
                for channel in channel_names:
                    default = (1.0, 1.0, 1.0) if channel == "scale" else (0.0, 0.0, 0.0)
                    baked_source = _baked_source_channel_value(
                        pose_bone, channel, default
                    )
                    vec = sample_animation_channel(
                        channels[channel],
                        sample_time,
                        default=default,
                        loop_length=0.0,
                        evaluation_context=evaluation_context,
                        this_values=(
                            _position_this_values(pose_bone, baked_source)
                            if channel == "position"
                            else baked_source
                        ),
                    )
                    if channel == "scale":
                        vec = tuple(1.0 + (value - 1.0) * animation_weight for value in vec)
                    else:
                        vec = tuple(value * animation_weight for value in vec)
                    source_pose[channel] = vec
                action_has_shear = _set_pose_transform(
                    pose_bone,
                    rest_basis,
                    entity_basis,
                    **source_pose,
                    relative_to_entity=animation_relative_to_entity(channels),
                ) or action_has_shear
                for data_path in ("location", "rotation_euler", "scale"):
                    pose_bone.keyframe_insert(
                        data_path=data_path,
                        frame=frame_number,
                    )

        action["mcbedrock_visual_only_shear"] = action_has_shear

        for fcurve in _iter_action_fcurves(action):
            for keyframe in fcurve.keyframe_points:
                keyframe.interpolation = "LINEAR"
            if loops:
                try:
                    fcurve.modifiers.new(type="CYCLES")
                except Exception:
                    pass

        action["mcbedrock_animation_source"] = filepath
        action["mcbedrock_animation_name"] = animation_name
        action["mcbedrock_bake_fps"] = float(fps)
        authored_roots = {
            bone.name for bone in armature.data.bones if bone.parent is None
        }
        action["mcbedrock_has_root_motion"] = animation_has_root_motion(bones, authored_roots)
        action["mcbedrock_root_motion_preserved"] = True
        action["mcbedrock_procedural_preview"] = procedural
        action["mcbedrock_movement_preview"] = preview_motion
        action["mcbedrock_preview_duration"] = float(duration)
        action["mcbedrock_loop_mode"] = loop_mode
        action["mcbedrock_loop_detection"] = (
            "explicit"
            if "loop" in animation and animation.get("loop") is not None
            else ("inferred_seam" if loops else "default_once")
        )
        action["mcbedrock_time_mode"] = "authored_clip"
        action["mcbedrock_authored_time_zero"] = True
        action["mcbedrock_runtime_anim_time_update"] = False
        created_actions.append(action)

        if append_nla:
            track = armature.animation_data.nla_tracks.new()
            track.name = safe_name(animation_name)
            start = int(action.frame_range[0]) if action.frame_range[0] else 1
            track.strips.new(action.name, start, action)
            track.mute = True

    if created_actions:
        armature.animation_data.action = created_actions[0] if filter_name else None
    return created_actions


def arrange_nla_tracks_for_preview(context, armature):
    """Lay imported direct clips out consecutively as a playable preview."""
    animation_data = getattr(armature, "animation_data", None)
    if animation_data is None:
        return (0, 0)
    cursor = 1.0
    first = None
    last = None
    for track in animation_data.nla_tracks:
        track.mute = False
        for strip in track.strips:
            duration = max(1.0, float(strip.frame_end - strip.frame_start))
            strip.extrapolation = "NOTHING"
            strip.blend_type = "REPLACE"
            strip.frame_start = cursor
            strip.frame_end = cursor + duration
            first = cursor if first is None else first
            last = float(strip.frame_end)
            cursor = last + 1.0
    animation_data.action = None
    if first is not None and last is not None:
        context.scene.frame_start = int(first)
        context.scene.frame_end = max(int(first), int(math.ceil(last)))
        context.scene.frame_set(int(first))
        armature["mcbedrock_nla_preview"] = "sequential_unmuted"
        return int(first), int(math.ceil(last))
    return (0, 0)


def apply_evaluated_animation_tracks(
    context,
    armature,
    controller,
    animations,
    alias_table=None,
    duration=10.0,
    query_context=None,
    action_name="animation_controller",
):
    """Evaluate a Bedrock controller and bake its blended output to one Action."""
    from ...animation_controller import (
        evaluate_animation_pose,
        evaluate_controller_timeline,
        resolve_animation_weights,
    )
    if not armature or armature.type != "ARMATURE":
        raise ValueError("Select or import an armature before baking an animation controller")
    animations = animations.get("animations", animations) if isinstance(animations, dict) else {}
    if not isinstance(animations, dict) or not animations:
        raise ValueError("No Bedrock animation definitions supplied for controller baking")

    fps = max(1, int(context.scene.render.fps or 24))
    timeline = evaluate_controller_timeline(
        controller,
        duration=duration,
        fps=fps,
        context=query_context,
    )
    action = bpy.data.actions.new(name=f"MC_{safe_name(action_name)}")
    armature.animation_data_create()
    armature.animation_data.action = action
    _reset_pose_bones(armature)

    animated_channels = {}
    for animation in animations.values():
        bones = animation.get("bones", {}) if isinstance(animation, dict) else {}
        if not isinstance(bones, dict):
            continue
        for bone_name, channels in bones.items():
            if isinstance(channels, dict):
                animated_channels.setdefault(str(bone_name), set()).update(
                    channel for channel in ("position", "rotation", "scale") if channel in channels
                )

    pose_bone_names = list(armature.pose.bones.keys())
    bone_cache = {}
    for source_name in animated_channels:
        resolved = resolve_pose_bone_name(source_name, pose_bone_names)
        pose_bone = armature.pose.bones.get(resolved) if resolved else None
        if not pose_bone:
            continue
        rest_basis, entity_basis = _bone_pose_context(pose_bone)
        bone_cache[source_name] = (pose_bone, rest_basis, entity_basis)

    action_has_shear = False
    animation_runtime = {}
    animation_context = _new_animation_context(
        None,
        query_context if isinstance(query_context, dict) else None,
        armature=armature,
    )
    for frame_index, controller_frame in enumerate(timeline):
        weights = resolve_animation_weights(controller_frame.animations, alias_table)
        for inactive_id in set(animation_runtime).difference(weights):
            animation_runtime.pop(inactive_id, None)
        if callable(query_context):
            supplied = query_context(controller_frame.time, frame_index)
            if isinstance(supplied, dict):
                animation_context.setdefault("q", {}).update(supplied)
        _update_animation_context(
            animation_context, None, controller_frame.time, fps
        )
        sampled_pose = evaluate_animation_pose(
            animations,
            weights,
            controller_frame.state_time,
            runtime=animation_runtime,
            delta_time=0.0 if frame_index == 0 else 1.0 / float(fps),
            context=animation_context,
        )
        frame_number = 1 + frame_index
        for source_name, channels in animated_channels.items():
            cached = bone_cache.get(source_name)
            if not cached:
                continue
            pose_bone, rest_basis, entity_basis = cached
            values = sampled_pose.get(source_name, {})
            source_pose = {
                "position": values.get("position", (0.0, 0.0, 0.0)),
                "rotation": values.get("rotation", (0.0, 0.0, 0.0)),
                "scale": values.get("scale", (1.0, 1.0, 1.0)),
            }
            action_has_shear = _set_pose_transform(
                pose_bone,
                rest_basis,
                entity_basis,
                **source_pose,
                relative_to_entity=bool(values.get("__rotation_relative_to_entity__")),
            ) or action_has_shear
            for data_path in ("location", "rotation_euler", "scale"):
                pose_bone.keyframe_insert(data_path=data_path, frame=frame_number)

    action["mcbedrock_visual_only_shear"] = action_has_shear

    for fcurve in _iter_action_fcurves(action):
        for keyframe in fcurve.keyframe_points:
            keyframe.interpolation = "LINEAR"

    action["mcbedrock_time_mode"] = "runtime_controller"
    action["mcbedrock_authored_time_zero"] = False
    action["mcbedrock_runtime_anim_time_update"] = True
    action["mcbedrock_controller_name"] = action_name
    action["mcbedrock_controller_baked"] = True
    action["mcbedrock_controller_duration"] = float(duration)
    action["mcbedrock_controller_fps"] = fps
    action["mcbedrock_root_motion_preserved"] = True
    return action

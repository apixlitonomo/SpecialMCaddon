"""Loss-preserving Blockbench project animation export.

The original ``.bbmodel`` remains the source of truth for geometry, textures,
display settings, and project metadata. Blender actions are sampled back into
the matching Blockbench animation entries so unsupported project fields survive
the round trip unchanged.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import re
import tempfile
import uuid

try:
    import bpy
    from mathutils import Matrix
except ImportError:  # pragma: no cover - pure helper tests
    bpy = None
    Matrix = None

try:
    from ..importers.bbmodel import _version_before_5, load_bbmodel
except ImportError:  # pragma: no cover - top-level pure helper tests
    from importers.bbmodel import _version_before_5, load_bbmodel
from .bedrock_project import bedrock_geometry_project



TRANSFORM_CHANNELS = ("position", "rotation", "scale")
CHANNEL_DEFAULTS = {
    "position": (0.0, 0.0, 0.0),
    "rotation": (0.0, 0.0, 0.0),
    "scale": (1.0, 1.0, 1.0),
}


def blockbench_channel_value(channel, bedrock_value, before_v5=False):
    """Convert a Bedrock animation vector into bbmodel editor coordinates."""
    value = [float(component) for component in bedrock_value[:3]]
    if not before_v5:
        if channel in {"position", "rotation"}:
            value[0] *= -1.0
        if channel == "rotation":
            value[1] *= -1.0
    return [_clean_number(component) for component in value]


def blockbench_loop_mode(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"loop", "true", "repeat", "cycle", "1"}:
        return "loop"
    if normalized in {"hold", "hold_on_last_frame"}:
        return "hold"
    return "once"


def _clean_number(value, tolerance=1.0e-8):
    value = float(value)
    if abs(value) <= tolerance:
        return 0.0
    rounded = round(value, 6)
    return int(rounded) if float(rounded).is_integer() else rounded


def _json_property(owner, name, default):
    raw = owner.get(name) if owner is not None else None
    if not isinstance(raw, str) or not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value


def _project_group_map(project):
    """Return ``bone name -> group UUID`` for old imported Blender files."""
    result = {}
    for group in project.get("groups", []) if isinstance(project, dict) else []:
        if not isinstance(group, dict) or not group.get("uuid"):
            continue
        name = str(group.get("name") or "").strip()
        if name:
            result[name] = str(group["uuid"])

    def collect(items):
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("uuid") and item.get("name"):
                result.setdefault(str(item["name"]), str(item["uuid"]))
            collect(item.get("children", []))

    collect(project.get("outliner", []))
    return result


def _bone_group_map(armature, project):
    by_group = _json_property(
        armature, "mcbedrock_bbmodel_bone_by_group", {})
    result = {
        str(bone_name): str(group_uuid)
        for group_uuid, bone_name in by_group.items()
        if bone_name and group_uuid
    }
    if result:
        return result
    return _project_group_map(project)


def _root_bone_names(project, bone_map, armature):
    """Return authored bones represented by top-level Blockbench groups."""
    top_level_groups = {
        str(item.get("uuid"))
        for item in project.get("outliner", [])
        if isinstance(item, dict) and item.get("uuid")
    }
    roots = {
        bone_name
        for bone_name, group_uuid in bone_map.items()
        if str(group_uuid) in top_level_groups
    }
    if roots:
        return roots
    return {
        bone_name for bone_name in bone_map
        if armature.data.bones.get(bone_name) is not None
        and armature.data.bones[bone_name].parent is None
    }


def _iter_action_fcurves(action):
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return list(fcurves)
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


def bbmodel_actions(armature):
    """Collect imported, active, and NLA actions associated with an armature."""
    if bpy is None or armature is None:
        return []
    source = os.path.normcase(os.path.abspath(str(
        armature.get("mcbedrock_source_path", "") or ""
    )))
    result = []

    def add(action):
        if action is not None and action not in result:
            result.append(action)

    animation_data = getattr(armature, "animation_data", None)
    if animation_data is not None:
        add(animation_data.action)
        for track in animation_data.nla_tracks:
            for strip in track.strips:
                add(strip.action)
    for action in bpy.data.actions:
        action_source = str(
            action.get("mcbedrock_bbmodel_source", "") or "")
        if source and action_source:
            if os.path.normcase(os.path.abspath(action_source)) == source:
                add(action)
    return result


def _action_frame_range(action, scene):
    curves = _iter_action_fcurves(action)
    keyed = [
        float(point.co[0])
        for curve in curves
        for point in curve.keyframe_points
    ]
    if keyed:
        return min(keyed), max(keyed)
    frame_range = getattr(action, "frame_range", (scene.frame_start, scene.frame_end))
    start, end = (float(frame_range[0]), float(frame_range[1]))
    if end < start:
        start, end = end, start
    return start, end


def _sample_frames(action, scene):
    start, end = _action_frame_range(action, scene)
    first = int(math.floor(start))
    last = int(math.ceil(end))
    frames = {float(frame) for frame in range(first, last + 1)}
    frames.update(
        float(point.co[0])
        for curve in _iter_action_fcurves(action)
        for point in curve.keyframe_points
        if start - 1.0e-6 <= float(point.co[0]) <= end + 1.0e-6
    )
    return start, sorted(frames)


def _evaluated_local_matrix(pose_bone):
    bone = pose_bone.bone
    kwargs = {
        "matrix": pose_bone.matrix,
        "matrix_local": bone.matrix_local,
        "invert": True,
    }
    if pose_bone.parent is not None:
        kwargs["parent_matrix"] = pose_bone.parent.matrix
        kwargs["parent_matrix_local"] = pose_bone.parent.bone.matrix_local
    return bone.convert_local_to_pose(**kwargs)


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


def _source_rest_basis(pose_bone):
    display_matrix = pose_bone.bone.matrix_local
    source_matrix = _stored_source_rest_matrix(pose_bone.bone)
    if source_matrix is None:
        local_matrix = (
            pose_bone.parent.bone.matrix_local.inverted() @ display_matrix
            if pose_bone.parent else display_matrix
        )
        return local_matrix.to_3x3().normalized()
    source_parent = (
        _stored_source_rest_matrix(pose_bone.parent.bone)
        if pose_bone.parent else None
    )
    source_parent_basis = (
        source_parent.to_3x3().normalized()
        if source_parent is not None else Matrix.Identity(3)
    )
    display_basis = display_matrix.to_3x3().normalized()
    return source_parent_basis.inverted_safe() @ display_basis


def _source_pose(
    pose_bone,
    local_matrix,
    compatible_euler=None,
    outer_matrix=None,
):
    rest_basis = _source_rest_basis(pose_bone)
    rest = rest_basis.to_4x4()
    source = rest @ local_matrix @ rest.inverted_safe()
    reflected = bool(
        pose_bone.bone.get("mcbedrock_native_reflected", "")
        and not (
            pose_bone.parent
            and pose_bone.parent.bone.get("mcbedrock_native_reflected", "")
        )
    )
    if reflected:
        reflection = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))
        source = reflection @ source @ reflection
    if outer_matrix is not None:
        source = outer_matrix @ source
    location, quaternion, scale = source.decompose()
    # Blender 4.5 requires an Euler when the optional continuity hint is passed.
    euler = (quaternion.to_euler("XZY") if compatible_euler is None
             else quaternion.to_euler("XZY", compatible_euler))
    position = (
        location.x * 16.0,
        location.z * 16.0,
        location.y * 16.0,
    )
    rotation = (
        math.degrees(euler.x),
        -math.degrees(euler.z),
        math.degrees(euler.y),
    )
    bedrock_scale = (scale.x, scale.z, scale.y)
    return {
        "position": position,
        "rotation": rotation,
        "scale": bedrock_scale,
    }, euler


def _channel_is_nondefault(samples, channel, tolerance=1.0e-5):
    default = CHANNEL_DEFAULTS[channel]
    return any(
        any(abs(float(value[index]) - default[index]) > tolerance
            for index in range(3))
        for _time_value, value in samples
    )


def _keyframe(channel, time_value, value):
    return {
        "channel": channel,
        "data_points": [{
            "x": value[0],
            "y": value[1],
            "z": value[2],
        }],
        "uuid": str(uuid.uuid4()),
        "time": _clean_number(time_value),
        "color": -1,
        "interpolation": "linear",
    }


def _reset_action_pose(armature):
    for pose_bone in armature.pose.bones:
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)
    armature.location = (0.0, 0.0, 0.0)
    armature.rotation_euler = (0.0, 0.0, 0.0)
    armature.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    armature.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
    armature.scale = (1.0, 1.0, 1.0)


def _apply_action_frame(armature, action, frame, *, reset=True):
    """Evaluate F-curves independently of NLA/tweak-mode state."""
    if reset:
        _reset_action_pose(armature)
    resolved = {}
    for fcurve in _iter_action_fcurves(action):
        data_path = str(getattr(fcurve, "data_path", "") or "")
        if not data_path:
            continue
        if "." in data_path:
            owner_path, property_name = data_path.rsplit(".", 1)
        else:
            owner_path, property_name = "", data_path
        key = (owner_path, property_name)
        if key not in resolved:
            try:
                owner = (
                    armature.path_resolve(owner_path)
                    if owner_path else armature
                )
                resolved[key] = (owner, getattr(owner, property_name))
            except (AttributeError, ValueError):
                resolved[key] = None
        target = resolved[key]
        if target is None:
            continue
        owner, property_value = target
        value = float(fcurve.evaluate(frame))
        try:
            property_value[int(fcurve.array_index)] = value
        except (IndexError, TypeError):
            try:
                setattr(owner, property_name, value)
            except (AttributeError, TypeError):
                continue
    armature.update_tag(refresh={'OBJECT'})


def _sample_action(
    armature,
    action,
    bone_map,
    scene,
    before_v5,
    root_bones=(),
    overlay_action=None,
):
    scene_fps = (
        float(scene.render.fps or 24)
        / float(scene.render.fps_base or 1.0)
    )
    try:
        fps = max(
            1.0, float(action.get("mcbedrock_bake_fps", scene_fps))
        )
    except (TypeError, ValueError):
        fps = max(1.0, scene_fps)
    start, frames = _sample_frames(action, scene)
    action_start, action_end = _action_frame_range(action, scene)
    if overlay_action is not None:
        overlay_start, overlay_frames = _sample_frames(overlay_action, scene)
        start = min(start, overlay_start)
        last = max(frames[-1], overlay_frames[-1])
        frames = sorted(
            set(frames)
            | set(overlay_frames)
            | {
                float(frame)
                for frame in range(
                    int(math.floor(start)),
                    int(math.ceil(last)) + 1,
                )
            }
        )
    samples = {
        bone_name: {channel: [] for channel in TRANSFORM_CHANNELS}
        for bone_name in bone_map
        if armature.pose.bones.get(bone_name) is not None
    }
    root_bones = set(root_bones)
    previous_eulers = {}
    object_vertical_origin = None
    for frame in frames:
        whole = int(math.floor(frame))
        scene.frame_set(whole, subframe=frame - whole)
        action_frame = action_start + (frame - start)
        action_span = action_end - action_start
        if action_span > 1.0e-8:
            if blockbench_loop_mode(
                action.get("mcbedrock_loop_mode", "once")
            ) == "loop":
                action_frame = action_start + (
                    (frame - start) % action_span
                )
            else:
                action_frame = min(action_end, action_frame)
        _apply_action_frame(armature, action, action_frame)
        if overlay_action is not None:
            _apply_action_frame(
                armature, overlay_action, frame, reset=False)
        bpy.context.view_layer.update()
        evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        object_animation = armature.matrix_basis.copy()
        if object_vertical_origin is None:
            object_vertical_origin = float(object_animation.translation.z)
        object_animation.translation.z -= object_vertical_origin
        time_value = max(0.0, (frame - start) / fps)
        for bone_name, channels in samples.items():
            evaluated_bone = evaluated.pose.bones.get(bone_name)
            source_bone = armature.pose.bones.get(bone_name)
            if evaluated_bone is None or source_bone is None:
                continue
            local_matrix = _evaluated_local_matrix(evaluated_bone)
            pose, euler = _source_pose(
                source_bone,
                local_matrix,
                previous_eulers.get(bone_name),
                (
                    object_animation
                    if bone_name in root_bones else None
                ),
            )
            previous_eulers[bone_name] = euler
            for channel in TRANSFORM_CHANNELS:
                value = blockbench_channel_value(
                    channel, pose[channel], before_v5)
                channels[channel].append((time_value, value))
    return samples, max(0.0, (frames[-1] - start) / fps) if frames else 0.0


def _replace_clip_animation(
    clip,
    action,
    samples,
    bone_map,
    duration,
    loop_action=None,
):
    animators = clip.setdefault("animators", {})
    if not isinstance(animators, dict):
        animators = {}
        clip["animators"] = animators
    for bone_name, channels in samples.items():
        group_uuid = bone_map[bone_name]
        animator = animators.get(group_uuid)
        if not isinstance(animator, dict):
            animator = {"name": bone_name, "type": "bone", "keyframes": []}
            animators[group_uuid] = animator
        existing = animator.get("keyframes", [])
        preserved = [
            keyframe for keyframe in existing
            if not isinstance(keyframe, dict)
            or str(keyframe.get("channel", "")).lower()
            not in TRANSFORM_CHANNELS
        ] if isinstance(existing, list) else []
        generated = []
        for channel in TRANSFORM_CHANNELS:
            values = channels[channel]
            if not _channel_is_nondefault(values, channel):
                continue
            generated.extend(
                _keyframe(channel, time_value, value)
                for time_value, value in values
            )
        animator["name"] = str(animator.get("name") or bone_name)
        animator.setdefault("type", "bone")
        animator["keyframes"] = preserved + generated
    clip["length"] = _clean_number(duration)
    clip["loop"] = blockbench_loop_mode(
        (loop_action or action).get("mcbedrock_loop_mode", "once")
    )


def _action_clip_name(action):
    stored = str(action.get("mcbedrock_bbmodel_clip_name", "") or "")
    if stored:
        return stored
    name = str(action.name or "animation.blender")
    return name[3:] if name.startswith("MC_") else name


def _source_clip(project, action):
    animations = project.setdefault("animations", [])
    if not isinstance(animations, list):
        animations = []
        project["animations"] = animations
    wanted_uuid = str(
        action.get("mcbedrock_bbmodel_animation_uuid", "") or "")
    wanted_name = str(
        action.get("mcbedrock_animation_name", "") or "")
    for clip in animations:
        if not isinstance(clip, dict):
            continue
        if wanted_uuid and str(clip.get("uuid", "")) == wanted_uuid:
            return clip
    for clip in animations:
        if not isinstance(clip, dict):
            continue
        if wanted_name and str(clip.get("name", "")) == wanted_name:
            return clip
    return None


def _next_blender_edit_name(project):
    used = {
        str(animation.get("name", "")).strip().casefold()
        for animation in project.get("animations", [])
        if isinstance(animation, dict)
    }
    highest = 0
    for name in used:
        match = re.fullmatch(r"blender edit\s+(\d+)", name)
        if match:
            highest = max(highest, int(match.group(1)))
    index = highest + 1
    while f"blender edit {index}" in used:
        index += 1
    return f"Blender Edit {index}"


def _new_variant_clip(project, action):
    animations = project.setdefault("animations", [])
    if not isinstance(animations, list):
        animations = []
        project["animations"] = animations
    source = _source_clip(project, action)
    clip = deepcopy(source) if isinstance(source, dict) else {
        "loop": "once",
        "length": 0,
        "snapping": _clean_number(
            action.get(
                "mcbedrock_bbmodel_snapping",
                action.get("mcbedrock_bake_fps", 24),
            )
        ),
        "animators": {},
    }
    for animation in animations:
        if isinstance(animation, dict):
            animation["selected"] = False
    clip["uuid"] = str(uuid.uuid4())
    clip["name"] = _next_blender_edit_name(project)
    clip["selected"] = True
    clip["saved"] = True
    clip["path"] = ""
    animations.append(clip)
    return clip


def _group_ids(project):
    return {
        str(group.get("uuid"))
        for group in project.get("groups", [])
        if isinstance(group, dict) and group.get("uuid")
    }


def _element_ids(project):
    return {
        str(element.get("uuid"))
        for element in project.get("elements", project.get("cubes", []))
        if isinstance(element, dict) and element.get("uuid")
    }


def _compatible_existing_project(existing, source):
    """Accept a prior export, including a synthetic root added by this add-on."""
    if _element_ids(existing) != _element_ids(source):
        return False
    if _group_ids(existing) == _group_ids(source):
        return True
    synthetic = str(
        existing.get("mcbedrockparticles_synthetic_root_group", "") or ""
    )
    return (
        bool(synthetic)
        and not _group_ids(source)
        and _group_ids(existing) == {synthetic}
    )


def _ensure_synthetic_root_group(project, armature):
    """Make an otherwise groupless project animation-capable."""
    if _group_ids(project):
        return {}
    roots = [
        bone.name
        for bone in armature.data.bones
        if bone.parent is None
    ]
    if len(roots) != 1:
        return {}

    stored = str(
        project.get("mcbedrockparticles_synthetic_root_group", "") or ""
    )
    if stored:
        return {roots[0]: stored}

    group_uuid = str(uuid.uuid4())
    children = project.get("outliner", [])
    if not isinstance(children, list):
        children = []
    if not children:
        children = sorted(_element_ids(project))
    group = {
        "uuid": group_uuid,
        "name": roots[0],
        "origin": [0, 0, 0],
        "rotation": [0, 0, 0],
        "visibility": True,
        "export": True,
    }
    groups = project.get("groups", [])
    if not isinstance(groups, list):
        groups = []
    project["groups"] = [*groups, deepcopy(group)]
    project["outliner"] = [{
        "uuid": group_uuid,
        "name": roots[0],
        "children": deepcopy(children),
    }]
    project["mcbedrockparticles_synthetic_root_group"] = group_uuid
    return {roots[0]: group_uuid}


def _has_blender_variant(project):
    return any(
        isinstance(animation, dict)
        and re.fullmatch(
            r"blender edit\s+\d+",
            str(animation.get("name", "")).strip(),
            flags=re.IGNORECASE,
        )
        for animation in project.get("animations", [])
    )


def export_bbmodel_animations(
    armature,
    filepath,
    *,
    source_path="",
    actions=None,
    scene=None,
    overlay_action=None,
):
    """Write Blender actions into a copy of the original Blockbench project."""
    if bpy is None:
        raise RuntimeError("Blockbench animation export requires Blender")
    if armature is None or getattr(armature, "type", "") != "ARMATURE":
        raise ValueError("Select the armature imported from a .bbmodel project")
    source_path = source_path or str(
        armature.get("mcbedrock_source_path", "") or "")
    if not source_path or not os.path.isfile(source_path):
        raise ValueError("The original .bbmodel source file could not be found")
    filepath = os.path.abspath(filepath)
    try:
        source_project = load_bbmodel(source_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"The original .bbmodel is unreadable: {source_path} ({exc})"
        ) from exc
    project = source_project
    if (
        os.path.normcase(filepath) != os.path.normcase(
            os.path.abspath(source_path)
        )
        and os.path.isfile(filepath)
    ):
        try:
            existing = load_bbmodel(filepath)
            if (
                _has_blender_variant(existing)
                and _compatible_existing_project(existing, source_project)
            ):
                project = existing
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    bone_map = _bone_group_map(armature, project)
    if not bone_map:
        bone_map = _ensure_synthetic_root_group(project, armature)
    if not bone_map:
        raise ValueError(
            "No Blockbench bone UUID mapping was found, and the project "
            "could not be recovered as a single-root model"
        )
    root_bones = _root_bone_names(project, bone_map, armature)
    actions = list(actions) if actions is not None else bbmodel_actions(armature)
    if not actions:
        raise ValueError("No Blender actions were found for this model")
    scene = scene or bpy.context.scene
    before_v5 = _version_before_5(project)

    animation_data = armature.animation_data_create()
    original_action = animation_data.action
    original_frame = float(scene.frame_current_final)
    original_matrix_basis = armature.matrix_basis.copy()
    track_mutes = [
        (track, bool(track.mute)) for track in animation_data.nla_tracks
    ]
    exported_names = []
    try:
        for track, _mute in track_mutes:
            track.mute = True
        for action in actions:
            animation_data.action = None
            samples, duration = _sample_action(
                armature,
                action,
                bone_map,
                scene,
                before_v5,
                root_bones,
                overlay_action,
            )
            clip = _new_variant_clip(project, action)
            _replace_clip_animation(
                clip, action, samples, bone_map, duration, overlay_action)
            exported_names.append(clip["name"])
    finally:
        animation_data.action = original_action
        for track, mute in track_mutes:
            track.mute = mute
        whole = int(math.floor(original_frame))
        scene.frame_set(whole, subframe=original_frame - whole)
        armature.matrix_basis = original_matrix_basis
        bpy.context.view_layer.update()

    directory = os.path.dirname(filepath) or os.getcwd()
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".mcbedrock_bbmodel_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(project, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        load_bbmodel(temporary)
        os.replace(temporary, filepath)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return {
        "filepath": filepath,
        "actions": len(actions),
        "animations": len(project.get("animations", [])),
        "bones": len(bone_map),
        "variants": exported_names,
    }

# Native Bedrock imports do not have an existing .bbmodel to preserve. Build a
# deterministic project from their source geometry, then use the same tested
# action sampler and atomic writer as ordinary Blockbench round trips.
_export_existing_bbmodel_animations = export_bbmodel_animations


def export_bbmodel_animations(
    armature,
    filepath,
    *,
    source_path="",
    actions=None,
    scene=None,
    overlay_action=None,
):
    source_path = source_path or str(
        armature.get("mcbedrock_source_path", "") or ""
    ) if armature is not None else ""
    if source_path.lower().endswith(".bbmodel"):
        return _export_existing_bbmodel_animations(
            armature,
            filepath,
            source_path=source_path,
            actions=actions,
            scene=scene,
            overlay_action=overlay_action,
        )
    if not source_path or not os.path.isfile(source_path):
        raise ValueError("The original Bedrock geometry source file could not be found")

    try:
        from ..importers.parse_bedrock_geometry import BedrockModelParser
    except ImportError:  # pragma: no cover - top-level pure helper tests
        from importers.parse_bedrock_geometry import BedrockModelParser
    try:
        geometry = None
        stored_geometry = armature.get("mcbedrock_export_geometry", "")
        if isinstance(stored_geometry, str) and stored_geometry:
            candidate = json.loads(stored_geometry)
            if isinstance(candidate, dict):
                geometry = candidate
        if geometry is None:
            parser = BedrockModelParser(filepath=source_path)
            identifier = str(
                armature.get("mcbedrock_identifier", "") or ""
            )
            geometry = parser.get_geometry(identifier=identifier or None)
        hidden_parts = ()
        stored_hidden = armature.get("mcbedrock_export_hidden_parts", "")
        if isinstance(stored_hidden, str) and stored_hidden:
            candidate = json.loads(stored_hidden)
            if isinstance(candidate, list):
                hidden_parts = candidate
        hidden_parts = set(hidden_parts)
        hidden_parts.update(
            _mesh_hidden_geometry_bones(armature, geometry)
        )
        project = bedrock_geometry_project(
            geometry,
            source_path=source_path,
            texture_path=str(armature.get("mcbedrock_texture", "") or ""),
            project_path=filepath,
            hidden_parts=hidden_parts,
        )
    except (OSError, ValueError, json.JSONDecodeError, IndexError) as exc:
        raise ValueError(
            f"The original Bedrock geometry is unreadable: {source_path} ({exc})"
        ) from exc

    descriptor, temporary = tempfile.mkstemp(
        prefix="mcbedrock_geometry_", suffix=".bbmodel"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(project, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return _export_existing_bbmodel_animations(
            armature,
            filepath,
            source_path=temporary,
            actions=actions,
            scene=scene,
            overlay_action=overlay_action,
        )
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass

def _mesh_hidden_geometry_bones(armature, geometry):
    """Infer non-rendered source parts from the mesh actually built in Blender."""
    if bpy is None or armature is None or not isinstance(geometry, dict):
        return set()
    weighted = set()
    for obj in bpy.data.objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        try:
            owner = obj.find_armature()
        except (AttributeError, RuntimeError):
            owner = None
        if owner is not armature:
            continue
        group_names = {
            group.index: str(group.name).casefold()
            for group in obj.vertex_groups
        }
        for vertex in obj.data.vertices:
            for membership in vertex.groups:
                if membership.weight > 0.0 and membership.group in group_names:
                    weighted.add(group_names[membership.group])
    return {
        str(bone["name"])
        for bone in geometry.get("bones", ())
        if isinstance(bone, dict)
        and bone.get("name")
        and (
            bone.get("cubes")
            or bone.get("poly_mesh")
            or bone.get("texture_meshes")
        )
        and str(bone["name"]).casefold() not in weighted
    }

"""Pure Bedrock animation helpers (no bpy dependency)."""

import re
from dataclasses import dataclass


try:
    from ...molang_parser import evaluate as evaluate_molang
except ImportError:  # pragma: no cover - plain Python test path
    from molang_parser import evaluate as evaluate_molang

from ..bone_math import bedrock_rotation_matrix, bedrock_to_blender
from .uv import coerce_vector

@dataclass(frozen=True)
class BedrockLocalPose:
    """One complete authored local bone delta in Bedrock coordinates."""

    position: tuple = (0.0, 0.0, 0.0)
    rotation: tuple = (0.0, 0.0, 0.0)
    scale: tuple = (1.0, 1.0, 1.0)


def _matrix_multiply(left, right):
    return [
        [
            sum(float(left[row][index]) * float(right[index][column]) for index in range(4))
            for column in range(4)
        ]
        for row in range(4)
    ]


def _orientation_matrix(orientation):
    """Return a translation-free 4x4 orientation matrix from row data."""
    try:
        rows = [
            [float(orientation[row][column]) for column in range(3)]
            for row in range(3)
        ]
    except (TypeError, ValueError, IndexError):
        rows = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    return [
        [rows[0][0], rows[0][1], rows[0][2], 0.0],
        [rows[1][0], rows[1][1], rows[1][2], 0.0],
        [rows[2][0], rows[2][1], rows[2][2], 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _orientation_inverse(orientation):
    """Invert the 3x3 part of an affine, translation-free matrix."""
    matrix = _orientation_matrix(orientation)
    a, b, c = matrix[0][:3]
    d, e, f = matrix[1][:3]
    g, h, i = matrix[2][:3]
    determinant = (
        a * (e * i - f * h)
        - b * (d * i - f * g)
        + c * (d * h - e * g)
    )
    if abs(determinant) <= 1.0e-12:
        return _orientation_matrix(None)
    inverse = 1.0 / determinant
    return [
        [(e * i - f * h) * inverse, (c * h - b * i) * inverse, (b * f - c * e) * inverse, 0.0],
        [(f * g - d * i) * inverse, (a * i - c * g) * inverse, (c * d - a * f) * inverse, 0.0],
        [(d * h - e * g) * inverse, (b * g - a * h) * inverse, (a * e - b * d) * inverse, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def bedrock_local_pose_matrix(pose):
    """Build ``T @ R @ S`` in Blender model axes without using ``bpy``.

    Bedrock Y is Blender Z and Bedrock Z is Blender Y. Applying that basis
    conversion to the complete matrix is important: assigning a Bedrock
    scale vector directly to a Blender pose bone makes source Y scale act on
    Blender's bone-display Y axis instead of the model's vertical axis.
    """
    if not isinstance(pose, BedrockLocalPose):
        pose = BedrockLocalPose()
    position = bedrock_to_blender(coerce_vector(pose.position))
    rotation = bedrock_rotation_matrix(coerce_vector(pose.rotation))
    source_scale = coerce_vector(pose.scale, (1.0, 1.0, 1.0))
    blender_scale = (source_scale[0], source_scale[2], source_scale[1])
    matrix = [list(row) for row in rotation]
    for row in range(3):
        for column in range(3):
            matrix[row][column] *= blender_scale[column]
        matrix[row][3] = float(position[row])
    matrix[3] = [0.0, 0.0, 0.0, 1.0]
    return matrix


def bedrock_pose_matrix_in_rest_basis(pose, rest_orientation):
    """Map a complete Bedrock local pose into a Blender bone rest basis."""
    rest = _orientation_matrix(rest_orientation)
    rest_inverse = _orientation_inverse(rest_orientation)
    return _matrix_multiply(
        rest_inverse,
        _matrix_multiply(bedrock_local_pose_matrix(pose), rest),
    )


def _animation_scalar(value, context, default=0.0):
    """Evaluate a numeric/MoLang animation property."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            try:
                return float(evaluate_molang(value, context or {}, default))
            except (TypeError, ValueError):
                return float(default)
    return float(default)


def animation_loop_mode(animation):
    value = animation.get("loop", False) if isinstance(animation, dict) else False
    normalized = str(value).strip().lower()
    if value is True or normalized in {"true", "loop", "repeat", "cycle", "1"}:
        return "loop"
    if normalized in {"hold", "hold_on_last_frame"}:
        return "hold"
    if animation_has_seamless_cycle(animation):
        return "loop"
    return "once"


def animation_has_seamless_cycle(animation, tolerance=1.0e-4):
    """Infer a loop only when every moving numeric channel closes its seam."""
    if not isinstance(animation, dict):
        return False
    if "loop" in animation and animation.get("loop") is not None:
        return False
    if animation_uses_molang(animation):
        return False
    duration = animation_duration(animation)
    if duration <= 1.0e-8:
        return False
    bones = animation.get("bones", {})
    if not isinstance(bones, dict):
        return False
    moving = False
    for channels in bones.values():
        if not isinstance(channels, dict):
            continue
        for channel in ("position", "rotation", "scale"):
            value = channels.get(channel)
            if not channel_is_time_varying(value):
                continue
            moving = True
            default = (
                (1.0, 1.0, 1.0)
                if channel == "scale"
                else (0.0, 0.0, 0.0)
            )
            first = sample_animation_channel(value, 0.0, default=default)
            last = sample_animation_channel(value, duration, default=default)
            for index in range(3):
                delta = abs(first[index] - last[index])
                if channel == "rotation":
                    delta = min(delta % 360.0, (-delta) % 360.0)
                if delta > tolerance:
                    return False
    return moving


def animation_wall_duration(animation, context=None):
    """Duration of one deterministic preview pass including authored delays."""
    duration = animation_duration(animation)
    duration += max(
        0.0, _animation_scalar(animation.get("start_delay", 0.0), context or {})
    )
    if animation_loop_mode(animation) == "loop":
        duration += max(
            0.0, _animation_scalar(animation.get("loop_delay", 0.0), context or {})
        )
    return duration


def animation_relative_to_entity(channels):
    relative = channels.get("relative_to", {}) if isinstance(channels, dict) else {}
    return (
        isinstance(relative, dict)
        and str(relative.get("rotation", "")).strip().lower() == "entity"
    )


@dataclass
class AnimationPlaybackState:
    """Stateful Bedrock clock shared by direct and controller baking."""

    animation: dict
    anim_time: float = 0.0
    start_remaining: object = None
    loop_remaining: float = 0.0
    finished: bool = False

    def step(self, delta_time, context=None):
        context = context if isinstance(context, dict) else {}
        query = context.setdefault("q", {})
        if not isinstance(query, dict):
            query = {}
            context["q"] = query
        delta = max(0.0, float(delta_time or 0.0))
        if self.start_remaining is None:
            self.start_remaining = max(
                0.0, _animation_scalar(self.animation.get("start_delay", 0.0), context)
            )
        if self.start_remaining > 0.0:
            if delta < self.start_remaining:
                self.start_remaining -= delta
                query["delta_time"] = 0.0
                query["anim_time"] = 0.0
                return 0.0, 0.0
            delta -= self.start_remaining
            self.start_remaining = 0.0
        if self.loop_remaining > 0.0:
            if delta < self.loop_remaining:
                self.loop_remaining -= delta
                query["delta_time"] = 0.0
                query["anim_time"] = self.anim_time
                return self.anim_time, self.blend_weight(context)
            delta -= self.loop_remaining
            self.loop_remaining = 0.0
            self.anim_time = 0.0
        query["delta_time"] = delta
        query["anim_time"] = self.anim_time
        update = self.animation.get("anim_time_update")
        self.anim_time = max(
            0.0,
            _animation_scalar(update, context, self.anim_time + delta)
            if isinstance(update, str) and update.strip()
            else self.anim_time + delta,
        )
        duration = animation_duration(self.animation)
        mode = animation_loop_mode(self.animation)
        if duration > 0.0 and self.anim_time >= duration:
            if mode == "loop":
                overflow = self.anim_time % duration
                delay = max(
                    0.0, _animation_scalar(self.animation.get("loop_delay", 0.0), context)
                )
                if delay > 0.0:
                    self.anim_time = duration
                    self.loop_remaining = delay
                else:
                    self.anim_time = overflow
            else:
                self.anim_time = duration
                self.finished = mode == "once"
        query["anim_time"] = self.anim_time
        return self.anim_time, self.blend_weight(context)

    def blend_weight(self, context=None):
        return max(
            0.0,
            _animation_scalar(self.animation.get("blend_weight", 1.0), context or {}, 1.0),
        )

def _coerce_animation_vector(
    value,
    default,
    evaluation_context=None,
    this_values=None,
):
    """Coerce a Bedrock vector, evaluating MoLang string components."""
    if evaluation_context is None:
        return coerce_vector(value, default)

    defaults = tuple(default)
    if isinstance(value, (int, float, str)):
        values = [value, value, value]
    elif isinstance(value, (list, tuple)):
        values = list(value[:3])
    else:
        return defaults

    this_components = tuple(this_values if this_values is not None else defaults)
    result = []
    for index in range(3):
        component = values[index] if index < len(values) else defaults[index]
        if isinstance(component, (int, float)):
            result.append(float(component))
            continue
        if isinstance(component, str):
            try:
                result.append(float(component))
            except ValueError:
                sentinel = object()
                previous_this = evaluation_context.get("this", sentinel)
                evaluation_context["this"] = float(this_components[index])
                try:
                    result.append(
                        float(evaluate_molang(component, evaluation_context, defaults[index]))
                    )
                finally:
                    if previous_this is sentinel:
                        evaluation_context.pop("this", None)
                    else:
                        evaluation_context["this"] = previous_this
            continue
        result.append(float(defaults[index]))
    return tuple(result)


def _channel_keyframes_detailed(
    channel_value,
    default=(0.0, 0.0, 0.0),
    evaluation_context=None,
    this_values=None,
):
    """Return time/pre/post/interpolation tuples for a Bedrock channel."""
    if not isinstance(channel_value, dict):
        value = _coerce_animation_vector(
            channel_value, default, evaluation_context, this_values
        )
        return [(0.0, value, value, "linear")]

    frames = []
    for key, value in channel_value.items():
        try:
            time_value = float(key)
        except (TypeError, ValueError):
            continue
        lerp_mode = "linear"
        if isinstance(value, dict):
            lerp_mode = str(value.get("lerp_mode", lerp_mode)).lower()
            pre_value = _coerce_animation_vector(
                value.get("pre", value.get("post", default)),
                default,
                evaluation_context,
                this_values,
            )
            post_value = _coerce_animation_vector(
                value.get("post", value.get("pre", default)),
                default,
                evaluation_context,
                this_values,
            )
        else:
            pre_value = post_value = _coerce_animation_vector(
                value, default, evaluation_context, this_values
            )
        frames.append((time_value, pre_value, post_value, lerp_mode))
    return sorted(frames, key=lambda item: item[0])


_TIME_INPUT_RE = re.compile(
    r"\b(?:q|query)\.(?:anim_time|life_time|delta_time)\b",
    re.IGNORECASE,
)


def _contains_time_input(value):
    if isinstance(value, str):
        return _TIME_INPUT_RE.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_time_input(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_time_input(item) for item in value)
    return False


def _freeze_authored_value(value):
    if isinstance(value, dict):
        return tuple(sorted(
            (str(key), _freeze_authored_value(item))
            for key, item in value.items()
            if key != "lerp_mode"
        ))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_authored_value(item) for item in value)
    return value


def channel_is_time_varying(channel_value):
    """True when an animation channel changes as playback time advances.

    Explicit time queries and differing keyframe payloads are motion.
    Runtime state queries without time remain eligible for a deterministic
    preview context.
    """
    if _contains_time_input(channel_value):
        return True
    if not isinstance(channel_value, dict):
        return False

    frames = []
    for key, payload in channel_value.items():
        try:
            time_value = float(key)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            pre = payload.get("pre", payload.get("post"))
            post = payload.get("post", payload.get("pre"))
        else:
            pre = post = payload
        frames.append((
            time_value, _freeze_authored_value(pre), _freeze_authored_value(post)
        ))
    if not frames:
        return False
    frames.sort(key=lambda item: item[0])
    first = frames[0][1:]
    return any(frame[1:] != first for frame in frames) or first[0] != first[1]


def iter_channel_keyframes(channel_value, default=(0.0, 0.0, 0.0)):
    """Compatibility iterator exposing each key's post value."""
    return [
        (time_value, post_value, lerp_mode)
        for time_value, _pre_value, post_value, lerp_mode
        in _channel_keyframes_detailed(channel_value, default)
    ]


def _catmullrom(p0, p1, p2, p3, amount):
    t2 = amount * amount
    t3 = t2 * amount
    return tuple(
        0.5 * (
            (2.0 * p1[index])
            + (-p0[index] + p2[index]) * amount
            + (2.0 * p0[index] - 5.0 * p1[index] + 4.0 * p2[index] - p3[index]) * t2
            + (-p0[index] + 3.0 * p1[index] - 3.0 * p2[index] + p3[index]) * t3
        )
        for index in range(3)
    )


def sample_animation_channel(
    channel_value,
    time_value,
    default=(0.0, 0.0, 0.0),
    loop_length=0.0,
    evaluation_context=None,
    this_values=None,
):
    """Sample linear, discontinuous, or Catmull-Rom Bedrock keyframes."""
    frames = _channel_keyframes_detailed(
        channel_value,
        default,
        evaluation_context=evaluation_context,
        this_values=this_values,
    )
    if not frames:
        return tuple(default)

    sample_time = max(0.0, float(time_value or 0.0))
    if loop_length and loop_length > 0.0:
        sample_time %= float(loop_length)

    first_time, first_pre, first_post, _first_mode = frames[0]
    if sample_time < first_time:
        return first_pre
    if abs(sample_time - first_time) <= 1e-8:
        return first_post

    for index in range(1, len(frames)):
        right_time, right_pre, right_post, _right_mode = frames[index]
        if abs(sample_time - right_time) <= 1e-8:
            return right_post
        if sample_time > right_time:
            continue

        left_time, _left_pre, left_post, left_mode = frames[index - 1]
        span = right_time - left_time
        amount = 0.0 if span <= 1e-8 else (sample_time - left_time) / span
        if left_mode == "catmullrom":
            p0 = frames[index - 2][2] if index >= 2 else left_post
            p3 = frames[index + 1][1] if index + 1 < len(frames) else right_pre
            return _catmullrom(p0, left_post, right_pre, p3, amount)
        return tuple(
            left_post[axis] + (right_pre[axis] - left_post[axis]) * amount
            for axis in range(3)
        )
    return frames[-1][2]




def animation_uses_molang(animation):
    """Return True when an animation channel contains a non-numeric string."""
    bones = animation.get("bones", {}) if isinstance(animation, dict) else {}

    def uses_expression(value):
        if isinstance(value, str):
            try:
                float(value)
            except ValueError:
                return True
            return False
        if isinstance(value, dict):
            return any(uses_expression(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(uses_expression(item) for item in value)
        return False

    return uses_expression(bones)


def animation_duration(animation):
    """Return declared duration, extended to include any later bone key."""
    if not isinstance(animation, dict):
        return 0.0
    try:
        duration = max(0.0, float(animation.get("animation_length", 0.0) or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    bones = animation.get("bones", {})
    if not isinstance(bones, dict):
        return duration
    for channels in bones.values():
        if not isinstance(channels, dict):
            continue
        for channel in ("position", "rotation", "scale"):
            value = channels.get(channel)
            if not isinstance(value, dict):
                continue
            for key in value:
                try:
                    duration = max(duration, float(key))
                except (TypeError, ValueError):
                    continue
    return duration


_MOVEMENT_QUERY_RE = re.compile(
    r"\b(?:q|query)\.(?:modified_distance_moved|distance_moved|"
    r"modified_move_speed|ground_speed)\b",
    re.IGNORECASE,
)


_VARIABLE_REFERENCE_RE = re.compile(
    r"\b(?:v|variable)\.([A-Za-z_][A-Za-z0-9_.]*)\b",
    re.IGNORECASE,
)
_VARIABLE_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:v|variable)\.([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def animation_uses_movement_queries(animation, client_description=None):
    """Whether a direct clip depends on travel queries, including scripts."""
    def contains(value):
        if isinstance(value, str):
            return _MOVEMENT_QUERY_RE.search(value) is not None
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains(item) for item in value)
        return False

    bones = animation.get("bones", {}) if isinstance(animation, dict) else {}
    if contains(bones):
        return True
    required = {
        match.group(1).casefold()
        for match in _VARIABLE_REFERENCE_RE.finditer(str(bones))
    }
    scripts = (
        client_description.get("scripts", {})
        if isinstance(client_description, dict) else {}
    )
    statements = scripts.get("pre_animation", ()) if isinstance(scripts, dict) else ()
    if isinstance(statements, str):
        statements = (statements,)

    def iter_strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from iter_strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from iter_strings(item)

    assignments = {}
    for statement in iter_strings(statements):
        match = _VARIABLE_ASSIGNMENT_RE.match(statement)
        if match:
            assignments[match.group(1).casefold()] = match.group(2)
    visited = set()
    while required:
        name = required.pop()
        if name in visited:
            continue
        visited.add(name)
        expression = assignments.get(name, "")
        if _MOVEMENT_QUERY_RE.search(expression):
            return True
        required.update(
            match.group(1).casefold()
            for match in _VARIABLE_REFERENCE_RE.finditer(expression)
            if match.group(1).casefold() not in visited
        )
    return False


def authored_clip_sample_time(animation, elapsed):
    """Direct clips use authored wall time, never ``anim_time_update``."""
    duration = animation_duration(animation)
    value = max(0.0, float(elapsed or 0.0))
    return min(value, duration) if duration > 0.0 else value


def animation_has_meaningful_structural_transform(animation):
    """True when time zero changes position/rotation/scale from identity."""
    bones = animation.get("bones", {}) if isinstance(animation, dict) else {}
    if not isinstance(bones, dict):
        return False
    context = {
        "q": {"anim_time": 0.0, "life_time": 0.0, "delta_time": 0.0},
        "v": {},
    }
    for channels in bones.values():
        if not isinstance(channels, dict):
            continue
        for channel in ("position", "rotation", "scale"):
            if channel not in channels:
                continue
            neutral = (1.0, 1.0, 1.0) if channel == "scale" else (0.0, 0.0, 0.0)
            value = sample_animation_channel(
                channels[channel], 0.0, default=neutral,
                evaluation_context=context, this_values=neutral,
            )
            if any(abs(value[index] - neutral[index]) > 1.0e-6 for index in range(3)):
                return True
    return False


def neutral_animation_phase(animation):
    """Choose the earliest authored phase with the smallest transform score."""
    if not isinstance(animation, dict):
        return 0.0
    duration = animation_duration(animation)
    candidates = {0.0}
    bones = animation.get("bones", {})
    for channels in bones.values() if isinstance(bones, dict) else ():
        if not isinstance(channels, dict):
            continue
        for channel in ("position", "rotation", "scale"):
            value = channels.get(channel)
            if not isinstance(value, dict):
                continue
            for key in value:
                try:
                    candidate = max(0.0, float(key))
                except (TypeError, ValueError):
                    continue
                if duration <= 0.0 or candidate < duration - 1.0e-8:
                    candidates.add(candidate)

    def score(time_value):
        context = {
            "q": {"anim_time": time_value, "life_time": time_value, "delta_time": 0.0},
            "v": {},
        }
        total = 0.0
        for channels in bones.values() if isinstance(bones, dict) else ():
            if not isinstance(channels, dict):
                continue
            for channel in ("position", "rotation", "scale"):
                if channel not in channels:
                    continue
                neutral = (1.0, 1.0, 1.0) if channel == "scale" else (0.0, 0.0, 0.0)
                value = sample_animation_channel(
                    channels[channel], time_value, default=neutral,
                    evaluation_context=context, this_values=neutral,
                )
                weight = 1.0 / 16.0 if channel == "position" else 1.0
                total += sum(
                    ((value[index] - neutral[index]) * weight) ** 2
                    for index in range(3)
                )
        return total

    return min(sorted(candidates), key=lambda value: (round(score(value), 12), value))
def channel_uses_catmullrom(keyframes):
    return any(str(lerp).lower() == "catmullrom" for _time, _vec, lerp in keyframes)


def should_smooth_channel(channel, keyframes):
    """Scale/position stay linear; rotation may use catmullrom Bezier easing."""
    if channel in {"scale", "position"}:
        return False
    return channel_uses_catmullrom(keyframes)




def resolve_pose_bone_name(animation_bone_name, pose_bone_names):
    """Map Bedrock animation bone names onto Blender pose bones."""
    if not animation_bone_name:
        return ""
    if animation_bone_name in pose_bone_names:
        return animation_bone_name
    lower = animation_bone_name.lower()
    for name in pose_bone_names:
        if name.lower() == lower:
            return name
    return ""


def resolve_animation_reference(animation_filter, alias_table):
    """Resolve scripts.animate alias to animation.* id when needed."""
    if not animation_filter or animation_filter in ("__NONE__", "__ALL__"):
        return animation_filter
    if not isinstance(alias_table, dict):
        return animation_filter
    resolved = alias_table.get(animation_filter, animation_filter)
    if isinstance(resolved, str):
        return resolved
    return animation_filter


def bedrock_position_to_blender(vec):
    x, y, z = (float(vec[0]), float(vec[1]), float(vec[2]))
    return (x / 16.0, z / 16.0, y / 16.0)


def root_motion_bone_names(animation_bones, authored_roots=()):
    """Return animated bones that are explicit roots in the source rig."""
    animated = {str(name).casefold(): str(name) for name in animation_bones}
    return {
        animated[str(root).casefold()]
        for root in authored_roots
        if str(root).casefold() in animated
    }


def animation_has_root_motion(bones_data, authored_roots=()):
    """True when an authored root has a non-zero position channel."""
    if not isinstance(bones_data, dict):
        return False
    roots = root_motion_bone_names(bones_data.keys(), authored_roots)
    for bone_name in roots:
        channels = bones_data.get(bone_name)
        if not isinstance(channels, dict) or "position" not in channels:
            continue
        keyframes = iter_channel_keyframes(
            channels["position"], (0.0, 0.0, 0.0)
        )
        if len(keyframes) < 2:
            continue
        first = bedrock_position_to_blender(keyframes[0][1])
        last = bedrock_position_to_blender(keyframes[-1][1])
        delta = sum(
            (last[axis] - first[axis]) ** 2
            for axis in range(3)
        ) ** 0.5
        if delta > 0.01:
            return True
    return False
    return False

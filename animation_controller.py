"""Bedrock animation-controller parsing and deterministic state evaluation.

This module has no Blender dependency.  It converts a controller into a
frame-by-frame list of weighted animation aliases that the Blender importer can
bake into ordinary Actions.
"""

from dataclasses import dataclass, field
import re

try:
    from .molang_parser import evaluate, execute_statements
except ImportError:
    from molang_parser import evaluate, execute_statements


def load_animation_controllers(data):
    controllers = data.get("animation_controllers", {}) if isinstance(data, dict) else {}
    return controllers if isinstance(controllers, dict) else {}


def _statement_list(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int, float))]
    return []


_MOLANG_SCOPE_REF_RE = re.compile(
    r"\b(?:q|query|v|variable)\.([A-Za-z_][A-Za-z0-9_.]*)"
)


def _known_evaluate(expression, context):
    """Evaluate a controller expression without coercing missing refs to zero."""
    text = str(expression or "").strip()
    if not text:
        return None
    if re.search(r"\b(?:q|query)\.property\s*\(", text, re.IGNORECASE):
        # Property values are only known when the caller resolves the exact
        # property token; the generic MoLang callable cannot prove that here.
        return None
    for name in _MOLANG_SCOPE_REF_RE.findall(text):
        scope = "q" if text.lower().find("query.") >= 0 else "v"
        # Re-check the prefix for this individual match; query/variable may be
        # mixed in one expression.
        match = re.search(rf"\b(?:q|query|v|variable)\.{re.escape(name)}", text, re.IGNORECASE)
        prefix = match.group(0).split(".", 1)[0].lower() if match else scope
        bucket = context.get("q" if prefix in {"q", "query"} else "v", {})
        if not isinstance(bucket, dict) or name not in bucket:
            return None
    try:
        return float(evaluate(expression, context, 0.0))
    except (TypeError, ValueError):
        return None


def _animation_weights(state, context):
    animations = state.get("animations", []) if isinstance(state, dict) else []
    if isinstance(animations, (str, dict)):
        animations = [animations]
    if not isinstance(animations, list):
        return {}
    weights = {}
    for entry in animations:
        if isinstance(entry, str):
            weights[entry] = 1.0
        elif isinstance(entry, dict):
            for alias, expression in entry.items():
                if not isinstance(alias, str):
                    continue
                value = _known_evaluate(expression, context)
                if value is not None:
                    weights[alias] = max(0.0, value)
    return weights


def _blend_weights(previous, current, amount):
    amount = max(0.0, min(1.0, float(amount)))
    result = {}
    for alias in set(previous) | set(current):
        weight = previous.get(alias, 0.0) * (1.0 - amount) + current.get(alias, 0.0) * amount
        if weight > 1e-8:
            result[alias] = weight
    return result


@dataclass
class ControllerFrame:
    time: float
    state: str
    animations: dict
    state_time: float
    transition_progress: float = 1.0
    transitions_determined: bool = True


@dataclass
class AnimationControllerEvaluator:
    controller: dict
    context: dict = field(default_factory=dict)
    state: str = field(init=False, default="")
    state_time: float = field(init=False, default=0.0)
    time: float = field(init=False, default=0.0)
    _entered: bool = field(init=False, default=False)
    _unresolved_transition: bool = field(init=False, default=False)
    _previous_weights: dict = field(init=False, default_factory=dict)
    _blend_duration: float = field(init=False, default=0.0)
    _blend_elapsed: float = field(init=False, default=0.0)

    def __post_init__(self):
        self.controller = self.controller if isinstance(self.controller, dict) else {}
        self.context = self.context if isinstance(self.context, dict) else {}
        states = self.states
        requested = self.controller.get("initial_state", "default")
        self.state = requested if requested in states else (next(iter(states), ""))

    @property
    def states(self):
        states = self.controller.get("states", {})
        return states if isinstance(states, dict) else {}

    def _runtime_context(self, supplied):
        if isinstance(supplied, dict):
            for key, value in supplied.items():
                if isinstance(value, dict) and isinstance(self.context.get(key), dict):
                    self.context[key].update(value)
                else:
                    self.context[key] = value
        query = self.context.setdefault("q", {})
        if not isinstance(query, dict):
            query = {}
            self.context["q"] = query
        query["life_time"] = self.time
        query["anim_time"] = self.state_time
        return self.context

    def _run_statements(self, key, state):
        for statement in _statement_list(state.get(key)):
            execute_statements(statement, self.context)

    def _enter_current(self):
        state = self.states.get(self.state, {})
        if isinstance(state, dict):
            self._run_statements("on_entry", state)
        self._entered = True

    def _transition_target(self, state):
        self._unresolved_transition = False
        transitions = state.get("transitions", []) if isinstance(state, dict) else []
        if isinstance(transitions, dict):
            transitions = [transitions]
        if not isinstance(transitions, list):
            return ""
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            for target, condition in transition.items():
                value = _known_evaluate(condition, self.context)
                if value is None:
                    self._unresolved_transition = True
                    continue
                if target in self.states and value is not None and value != 0.0:
                    return target
        return ""

    def step(self, delta_time=0.0, context=None):
        delta = max(0.0, float(delta_time or 0.0))
        self.time += delta
        self.state_time += delta
        self._blend_elapsed += delta
        self._runtime_context(context)

        if not self._entered:
            self._enter_current()

        current_state = self.states.get(self.state, {})
        target = self._transition_target(current_state)
        if target and target != self.state:
            current_weights = _animation_weights(current_state, self.context)
            self._run_statements("on_exit", current_state)
            self._previous_weights = current_weights
            self.state = target
            self.state_time = 0.0
            self._runtime_context(None)
            self._entered = False
            self._enter_current()
            current_state = self.states.get(self.state, {})
            try:
                self._blend_duration = max(0.0, float(current_state.get("blend_transition", 0.0) or 0.0))
            except (TypeError, ValueError):
                self._blend_duration = 0.0
            self._blend_elapsed = 0.0

        weights = _animation_weights(current_state, self.context)
        if self._blend_duration > 0.0 and self._blend_elapsed < self._blend_duration:
            progress = self._blend_elapsed / self._blend_duration
            weights = _blend_weights(self._previous_weights, weights, progress)
        else:
            progress = 1.0
            self._previous_weights = {}

        return ControllerFrame(
            time=self.time,
            state=self.state,
            animations=weights,
            transitions_determined=not self._unresolved_transition,
            state_time=self.state_time,
            transition_progress=progress,
        )


def evaluate_controller_timeline(controller, duration, fps=24, context=None):
    fps = max(1, int(fps or 24))
    frame_count = max(1, int(round(max(0.0, float(duration or 0.0)) * fps)) + 1)
    evaluator = AnimationControllerEvaluator(controller, context={} if callable(context) else dict(context or {}))
    frames = []
    for frame_index in range(frame_count):
        current_time = frame_index / float(fps)
        supplied = context(current_time, frame_index) if callable(context) else None
        frames.append(evaluator.step(0.0 if frame_index == 0 else 1.0 / fps, supplied))
    return frames


def resolve_animation_weights(weights, alias_table):
    """Map controller aliases to concrete animation identifiers and combine duplicates."""
    aliases = alias_table if isinstance(alias_table, dict) else {}
    resolved = {}
    for alias, weight in (weights or {}).items():
        animation_id = aliases.get(alias, alias)
        if isinstance(animation_id, str):
            resolved[animation_id] = resolved.get(animation_id, 0.0) + float(weight)
    return resolved

def sample_animation_channel(
    channel_value, time, default=(0.0, 0.0, 0.0), loop_length=0.0, evaluation_context=None
):
    """Use the same Bedrock sampler as direct Action baking."""
    try:
        from .builders.bedrock_mcblend.bedrock_animation import sample_animation_channel as sample
    except (ImportError, ValueError):
        from builders.bedrock_mcblend.bedrock_animation import sample_animation_channel as sample
    return sample(
        channel_value,
        time,
        default=default,
        loop_length=loop_length,
        evaluation_context=evaluation_context,
    )

def evaluate_animation_pose(
    animations, weights, time, runtime=None, delta_time=0.0, context=None
):
    """Sample and weight Bedrock channels, honoring animation-level semantics."""
    try:
        from .builders.bedrock_mcblend.bedrock_animation import (
            AnimationPlaybackState, animation_relative_to_entity,
        )
    except ImportError:
        from builders.bedrock_mcblend.bedrock_animation import (
            AnimationPlaybackState, animation_relative_to_entity,
        )
    animations = animations if isinstance(animations, dict) else {}
    pose = {}
    context = context if isinstance(context, dict) else {"q": {}}
    stateful = runtime is not None
    runtime = runtime if isinstance(runtime, dict) else {}
    for animation_id, weight in (weights or {}).items():
        animation = animations.get(animation_id)
        if not isinstance(animation, dict):
            continue
        playback = runtime.setdefault(animation_id, AnimationPlaybackState(animation))
        if not stateful:
            playback.anim_time = max(0.0, float(time or 0.0))
        sample_time, own_weight = playback.step(
            delta_time if stateful else 0.0, context
        )
        weight = float(weight) * own_weight
        if weight <= 1.0e-8:
            continue
        bones = animation.get("bones", {})
        if not isinstance(bones, dict):
            continue
        for bone_name, channels in bones.items():
            if not isinstance(channels, dict):
                continue
            if animation.get("override_previous_animation"):
                pose[str(bone_name)] = {}
            blended = pose.setdefault(str(bone_name), {})
            relative_to_entity = animation_relative_to_entity(channels)
            for channel in ("position", "rotation", "scale"):
                if channel not in channels:
                    continue
                default = (1.0, 1.0, 1.0) if channel == "scale" else (0.0, 0.0, 0.0)
                value = sample_animation_channel(
                    channels[channel],
                    sample_time,
                    default=default,
                    loop_length=0.0,
                    evaluation_context=context,
                )
                if channel == "rotation" and relative_to_entity:
                    blended["__rotation_relative_to_entity__"] = True
                if channel == "scale":
                    base = blended.get(channel, (1.0, 1.0, 1.0))
                    blended[channel] = tuple(base[i] + (value[i] - 1.0) * weight for i in range(3))
                else:
                    base = blended.get(channel, (0.0, 0.0, 0.0))
                    blended[channel] = tuple(base[i] + value[i] * weight for i in range(3))
    return pose

import random
import math
import mathutils

from .molang_parser import evaluate, execute_statements
from .particle_core import CurveEvaluationError, EventContext, EventExecutor, SourceRef, evaluate_curve
from .particle_core.semantics import (
    EmitterEventScheduler,
    EntityAABB,
    LocalSpacePolicy,
    sample_emitter_shape,
)

def _expression_text(value):
    if isinstance(value, dict):
        return value.get("expression", "")
    if isinstance(value, list):
        return value
    return value or ""

class _EventRandom:
    def __init__(self, rng):
        self.rng = rng

    def sample(self, *_tokens):
        return self.rng.random()




class _PreviewScope(dict):
    """Dict-compatible host scope with an explicit preview-only fallback."""

    def __init__(self, fallback, on_missing):
        super().__init__()
        self.fallback = fallback
        self.on_missing = on_missing

    def __contains__(self, key):
        # MoLang's case-insensitive lookup probes membership before indexing.
        return True

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        self.on_missing(str(key))
        return self.fallback

    def get(self, key, default=None):
        del default
        return self[key]


class ParticleState:
    def __init__(self, id_val, rng):
        self.id = id_val
        self.age = 0.0
        self.lifetime = 1.0
        self.position = mathutils.Vector((0.0, 0.0, 0.0))
        self.velocity = mathutils.Vector((0.0, 0.0, 0.0))
        self.spawn_direction = mathutils.Vector((0.0, 0.0, 1.0))
        self.size = [0.1, 0.1]
        self.color = [1.0, 1.0, 1.0, 1.0]
        self.uv_offset = [0.0, 0.0]
        self.uv_scale = [1.0, 1.0]
        self.random1 = rng.random()
        self.random2 = rng.random()
        self.random3 = rng.random()
        self.random4 = rng.random()
        self.active = True
        self.rotation = 0.0
        self.rotation_rate = 0.0
        self.custom_vars = {}

        # For exporting frames
        self.history = {} # frame_number -> {pos, scale_x, scale_y, rot_z}
        
        # Sub-emitter event tracking
        self.triggered_events = [] # list of {"time": float, "event_name": str, "matrix": mathutils.Matrix}
        self.fired_timeline_events = set()
        self.expiration_fired = False
        self.local_space_position = False
        self.local_space_rotation = False
        self.local_space_velocity = False
        self.spawn_frame = 0
        self.kill_plane_previous = None

class BedrockSimulator:
    _RUNTIME_VARIABLE_NAMES = frozenset({
        "emitter_age",
        "emitter_lifetime",
        "emitter_random_1",
        "emitter_random_2",
        "emitter_random_3",
        "emitter_random_4",
        "particle_age",
        "particle_lifetime",
        "particle_random_1",
        "particle_random_2",
        "particle_random_3",
        "particle_random_4",
    })

    def __init__(self, parser, fps=24, duration_frames=100, force_loop=False, emitter_matrices=None, start_frame=1, static_emitter_matrix=None, initial_context=None, seed=0, min_engine_version=None, texture_dimensions=None):
        self.parser = parser
        self.fps = fps
        self.dt = 1.0 / fps
        self.duration_frames = duration_frames
        self.emitter_matrices = emitter_matrices
        self.start_frame = start_frame
        self.static_emitter_matrix = static_emitter_matrix
        self.force_loop = force_loop
        self.initial_context = initial_context if isinstance(initial_context, dict) else {}
        self.preview_missing_context = bool(
            self.initial_context.get("_preview_missing_context", False)
        )
        self.seed = int(seed or 0)
        self.rng = random.Random(self.seed)
        self.min_engine_version = min_engine_version or self.initial_context.get("min_engine_version") or "1.26.30"
        dimensions = texture_dimensions if isinstance(texture_dimensions, (list, tuple)) else ()
        self.texture_dimensions = tuple(
            float(dimensions[index])
            if index < len(dimensions)
            and isinstance(dimensions[index], (int, float))
            and not isinstance(dimensions[index], bool)
            and math.isfinite(float(dimensions[index]))
            and float(dimensions[index]) > 0.0
            else None
            for index in range(2)
        )

        self.particles = []
        self.all_triggered_events = []
        self.lifetime_events = self.parser.components.get("minecraft:particle_lifetime_events", {})
        self.diagnostics = []
        self.molang_diagnostics = []
        self._warning_codes = set()
        self.collision_provider = self.initial_context.get("collision_provider")
        self.block_provider = self.initial_context.get("block_provider")
        self.entity_aabb_provider = self.initial_context.get("entity_aabb_provider")
        self.entity_aabb_value = self.initial_context.get("entity_aabb")
        self.entity_based_emitter = bool(
            self.initial_context.get(
                "emitter_is_entity", self.emitter_matrices is not None
            )
        )
        self.emitter_expired = False
        self.emitter_expiration_fired = False
        self.current_frame = start_frame
        self.emitter_loop_index = 0
        self.emitter_total_distance = 0.0
        self.current_active_time = None
        self.current_sleep_time = None
        self.timing_metadata = {}
        self._current_loop_start_frame = int(start_frame)
        self._completed_loop_windows = []

        
        self.emitter_age = 0.0
        self.anim_time = (start_frame - 1) * self.dt
        self.next_particle_id = 0
        self.emitter_custom_vars = {}
        self.particle_scale = 1.0
        self.animation_scale = 1.0
        
        init_comp = self.parser.components.get("minecraft:emitter_initialization", {})
        self.creation_expr = init_comp.get("creation_expression", "")
        self.per_update_expr = init_comp.get("per_update_expression", "")
        particle_init = self.parser.components.get("minecraft:particle_initialization", {})
        self.particle_update_expr = _expression_text(particle_init.get("per_update_expression", ""))
        self.particle_render_expr = _expression_text(particle_init.get("per_render_expression", ""))

        self.spawn_fraction = 0.0

        self.rate_mode, self.rate_amt, self.rate_max = self.parser.get_rate()
        self.emitter_lifetime = self.parser.get_emitter_lifetime()
        self.activation_expr = None
        self.emitter_expiration_expr = 0.0
        emitter_lifetime_expression = self.parser.components.get("minecraft:emitter_lifetime_expression")
        if isinstance(emitter_lifetime_expression, dict):
            self.activation_expr = emitter_lifetime_expression.get("activation_expression", 1.0)
            self.emitter_expiration_expr = emitter_lifetime_expression.get("expiration_expression", 0.0)
        particle_lifetime_expression = self.parser.components.get("minecraft:particle_lifetime_expression", {})
        self.particle_expiration_expr = particle_lifetime_expression.get("expiration_expression", 0.0) if isinstance(particle_lifetime_expression, dict) else 0.0
        self.shape_type, self.shape_data = self.parser.get_shape()
        self.local_space = LocalSpacePolicy.from_component(
            self.parser.components.get("minecraft:emitter_local_space", {})
        )
        if self.local_space.invalid_rotation_without_position:
            self._warn_once(
                "particle.local_space.invalid_rotation",
                "emitter_local_space rotation=true requires position=true; rotation inheritance was disabled",
            )
        emitter_events = self.parser.components.get(
            "minecraft:emitter_lifetime_events", {}
        )
        self.emitter_events = EmitterEventScheduler(
            emitter_events if isinstance(emitter_events, dict) else {}
        )
        self.has_lifetime_once = (
            "minecraft:emitter_lifetime_once" in self.parser.components
        )

        self.is_looping = force_loop or ('minecraft:emitter_lifetime_looping' in self.parser.components)
        if 'minecraft:emitter_lifetime_looping' in self.parser.components:
            loop_data = self.parser.components['minecraft:emitter_lifetime_looping']
            self.sleep_time_expr = loop_data.get('sleep_time', 0.0)
        else:
            self.sleep_time_expr = 0.0

        self.motion_override_mode = "SOURCE"
        self.vertical_speed_override = 1.0
        self.vertical_acceleration_override = 0.0
        self.lifetime_override_mode = "SOURCE"
        self.collision_override_mode = "SOURCE"
        self._refresh_emitter_randoms()

    def _refresh_emitter_randoms(self):
        self.emitter_random_1 = self.rng.random()
        self.emitter_random_2 = self.rng.random()
        self.emitter_random_3 = self.rng.random()
        self.emitter_random_4 = self.rng.random()

    def apply_emission_override(self, controls):
        """Apply a root-emitter bake override without rewriting source JSON.

        Particle motion, lifetime, events, and child effects remain authored by
        the source. Only the root emitter's production cadence is replaced.
        """
        controls = controls if isinstance(controls, dict) else {}
        self.motion_override_mode = str(
            controls.get("motion_mode", "SOURCE")
        ).upper()
        self.vertical_speed_override = float(
            controls.get("vertical_speed", 1.0)
        )
        self.vertical_acceleration_override = float(
            controls.get("vertical_acceleration", 0.0)
        )
        self.lifetime_override_mode = str(
            controls.get("lifetime_mode", "SOURCE")
        ).upper()
        self.collision_override_mode = str(
            controls.get("collision_mode", "SOURCE")
        ).upper()
        mode = str(controls.get("mode", "SOURCE")).upper()
        if mode == "SOURCE":
            return False
        if mode == "BURST":
            count = max(0, int(controls.get("burst_count", 10)))
            self.rate_mode = "instant"
            self.rate_amt = count
            self.rate_max = count
            self.emitter_lifetime = 0.0
        elif mode == "CONTINUOUS":
            rate = max(0.0, float(controls.get("rate", 10.0)))
            maximum = max(1, int(controls.get("max_active", 100)))
            duration = max(
                self.dt,
                float(controls.get("duration_frames", self.duration_frames))
                / max(1.0, float(self.fps)),
            )
            self.rate_mode = "steady"
            self.rate_amt = rate
            self.rate_max = maximum
            self.emitter_lifetime = duration + self.dt
            self.is_looping = False
            self.sleep_time_expr = 0.0
            self.has_lifetime_once = True
            self.activation_expr = None
            self.emitter_expiration_expr = 0.0
        else:
            raise ValueError(f"Unknown particle emission mode: {mode}")
        return True

    def _vertical_override_values(self):
        mode = str(getattr(
            self, "motion_override_mode", "SOURCE"
        )).upper()
        speed = float(getattr(self, "vertical_speed_override", 1.0))
        acceleration = float(getattr(
            self, "vertical_acceleration_override", 0.0
        ))
        if mode == "RISE":
            return abs(speed), abs(acceleration)
        if mode == "FALL":
            return -abs(speed), -abs(acceleration)
        if mode == "CUSTOM":
            return speed, acceleration
        return None

    @staticmethod
    def _bedrock_to_blender_vector(value):
        return mathutils.Vector((value.x, value.z, value.y))

    @staticmethod
    def _blender_to_bedrock_vector(value):
        return mathutils.Vector((value.x, value.z, value.y))

    def _emitter_matrix_at(self, frame, fraction_offset=0.0):
        if self.static_emitter_matrix is not None:
            return self.static_emitter_matrix
        if not self.emitter_matrices:
            return None
        index = max(0, min(int(frame), len(self.emitter_matrices) - 1))
        current = self.emitter_matrices[index]
        if (
            current is None
            or index <= 0
            or not fraction_offset
            or self.dt <= 0.0
        ):
            return current
        previous = self.emitter_matrices[index - 1]
        if previous is None:
            return current
        alpha = max(0.0, min(1.0, float(fraction_offset) / self.dt))
        loc_previous, rot_previous, scale_previous = previous.decompose()
        loc_current, rot_current, scale_current = current.decompose()
        return mathutils.Matrix.LocRotScale(
            loc_previous.lerp(loc_current, alpha),
            rot_previous.slerp(rot_current, alpha),
            scale_previous.lerp(scale_current, alpha),
        )

    def _emitter_position(self, frame=None):
        matrix = self._emitter_matrix_at(
            self.current_frame if frame is None else frame
        )
        if matrix is None:
            return mathutils.Vector((0.0, 0.0, 0.0))
        return self._blender_to_bedrock_vector(matrix.translation)

    def _emitter_velocity(self, frame=None):
        frame = self.current_frame if frame is None else int(frame)
        if self.dt <= 0.0 or self.static_emitter_matrix is not None:
            return mathutils.Vector((0.0, 0.0, 0.0))
        current = self._emitter_position(frame)
        previous = self._emitter_position(max(0, frame - 1))
        return (current - previous) / self.dt

    def _particle_matrix(self, particle):
        return self._emitter_matrix_at(self.current_frame)

    def _point_to_world(self, particle, value):
        vector = value.copy()
        if not getattr(particle, "local_space_position", False):
            return vector
        matrix = self._particle_matrix(particle)
        if matrix is None:
            return vector
        blender = self._bedrock_to_blender_vector(vector)
        if getattr(particle, "local_space_rotation", False):
            blender = matrix @ blender
        else:
            blender += matrix.translation
        return self._blender_to_bedrock_vector(blender)

    def _point_to_local(self, particle, value):
        vector = value.copy()
        if not getattr(particle, "local_space_position", False):
            return vector
        matrix = self._particle_matrix(particle)
        if matrix is None:
            return vector
        blender = self._bedrock_to_blender_vector(vector)
        if getattr(particle, "local_space_rotation", False):
            blender = matrix.inverted_safe() @ blender
        else:
            blender -= matrix.translation
        return self._blender_to_bedrock_vector(blender)

    def _vector_to_world(self, particle, value):
        vector = value.copy()
        if (
            not getattr(particle, "local_space_position", False)
            or not getattr(particle, "local_space_rotation", False)
        ):
            return vector
        matrix = self._particle_matrix(particle)
        if matrix is None:
            return vector
        blender = matrix.to_3x3() @ self._bedrock_to_blender_vector(vector)
        return self._blender_to_bedrock_vector(blender)

    def _vector_to_local(self, particle, value):
        vector = value.copy()
        if (
            not getattr(particle, "local_space_position", False)
            or not getattr(particle, "local_space_rotation", False)
        ):
            return vector
        matrix = self._particle_matrix(particle)
        if matrix is None:
            return vector
        blender = (
            matrix.to_3x3().inverted_safe()
            @ self._bedrock_to_blender_vector(vector)
        )
        return self._blender_to_bedrock_vector(blender)

    @classmethod
    def _store_custom_variables(cls, target, ctx):
        values = ctx.get("v", {}) if isinstance(ctx, dict) else {}
        if not isinstance(values, dict):
            return
        for name, value in values.items():
            if str(name).casefold() not in cls._RUNTIME_VARIABLE_NAMES:
                target[name] = value

    def _emitter_relative_position(self, world_position, frame=None):
        matrix = self._emitter_matrix_at(
            self.current_frame if frame is None else frame
        )
        if matrix is None:
            return world_position.copy()
        blender_world = self._bedrock_to_blender_vector(world_position)
        blender_local = matrix.inverted_safe() @ blender_world
        return self._blender_to_bedrock_vector(blender_local)

    def _emitter_relative_to_world_position(self, relative, frame=None):
        matrix = self._emitter_matrix_at(
            self.current_frame if frame is None else frame
        )
        if matrix is None:
            return relative.copy()
        blender_world = matrix @ self._bedrock_to_blender_vector(relative)
        return self._blender_to_bedrock_vector(blender_world)

    def _resolve_entity_aabb(self, ctx):
        value = self.entity_aabb_value
        if callable(self.entity_aabb_provider):
            try:
                value = self.entity_aabb_provider(context=ctx)
            except TypeError:
                value = self.entity_aabb_provider(ctx)
        result = EntityAABB.from_value(value)
        if value is not None and result is None:
            self._warn_once(
                "particle.shape.entity_aabb_invalid",
                "The entity AABB provider returned invalid bounds; the documented emitter-point fallback was used",
            )
        return result

    def _cache_lifetime_window(self):
        ctx = self.get_context()
        active_time = self.evaluate_val(self.emitter_lifetime, ctx)
        sleep_time = (
            self.evaluate_val(self.sleep_time_expr, ctx)
            if self.is_looping
            else 0.0
        )
        self.current_active_time = max(0.0, float(active_time))
        self.current_sleep_time = max(0.0, float(sleep_time))
        cycle_time = (
            self.current_active_time + self.current_sleep_time
            if self.is_looping
            else self.current_active_time
        )
        active_frames = (
            int(round(self.current_active_time * self.fps))
            if math.isfinite(self.current_active_time)
            else 0
        )
        sleep_frames = (
            int(round(self.current_sleep_time * self.fps))
            if math.isfinite(self.current_sleep_time)
            else 0
        )
        cycle_frames = (
            int(round(cycle_time * self.fps))
            if math.isfinite(cycle_time)
            else 0
        )
        self.timing_metadata.update({
            "fps": int(self.fps),
            "is_looping": bool(self.is_looping),
            "active_time_seconds": (
                self.current_active_time
                if math.isfinite(self.current_active_time)
                else 0.0
            ),
            "sleep_time_seconds": (
                self.current_sleep_time
                if math.isfinite(self.current_sleep_time)
                else 0.0
            ),
            "cycle_time_seconds": (
                cycle_time if math.isfinite(cycle_time) else 0.0
            ),
            "active_frames": active_frames,
            "sleep_frames": sleep_frames,
            "cycle_frames": cycle_frames,
        })

    def _roll_completed_loop(self):
        if (
            not self.is_looping
            or self.emitter_expired
            or self.current_active_time is None
        ):
            return
        cycle_time = self.current_active_time + self.current_sleep_time
        if cycle_time <= 1.0e-12:
            return
        loop_budget = 1024
        while (
            self.emitter_age + 1.0e-9 >= cycle_time
            and loop_budget > 0
        ):
            completed_end = int(self.current_frame) - 1
            if completed_end >= self._current_loop_start_frame:
                self._completed_loop_windows.append({
                    "start_frame": self._current_loop_start_frame,
                    "end_frame": completed_end,
                    "active_time_seconds": self.current_active_time,
                    "sleep_time_seconds": self.current_sleep_time,
                })
            self._current_loop_start_frame = int(self.current_frame)
            self.emitter_age = round(
                max(0.0, self.emitter_age - cycle_time), 12
            )
            self.has_spawned_instant = False
            self.spawn_fraction = 0.0
            self.emitter_loop_index += 1
            self.emitter_events.begin_loop(self.emitter_loop_index)
            self._refresh_emitter_randoms()
            self._cache_lifetime_window()
            self._trigger_events(
                None, self.emitter_events.timeline_events(0.0, 0.0)
            )
            cycle_time = (
                self.current_active_time + self.current_sleep_time
            )
            if cycle_time <= 1.0e-12:
                break
            loop_budget -= 1

    def _finalize_timing_metadata(self):
        cycle_frames = int(self.timing_metadata.get("cycle_frames", 0))
        if not self.is_looping or cycle_frames <= 0:
            self.timing_metadata.update({
                "warm_loop_start_frame": 0,
                "warm_loop_end_frame": 0,
                "completed_loop_count": len(self._completed_loop_windows),
            })
            return
        maximum_lifetime = max(
            (
                max(0.0, float(particle.lifetime))
                for particle in self.particles
            ),
            default=0.0,
        )
        cycle_time = max(
            1.0e-12,
            float(self.timing_metadata.get("cycle_time_seconds", 0.0)),
        )
        warmup_frame = self.start_frame + int(
            math.ceil(maximum_lifetime * self.fps)
        )
        completed = next((
            window for window in self._completed_loop_windows
            if int(window["start_frame"]) >= warmup_frame
            and int(window["end_frame"]) >= int(window["start_frame"])
        ), None)
        if completed is None:
            warm_start = warm_end = 0
        else:
            warm_start = int(completed["start_frame"])
            warm_end = int(completed["end_frame"])
            cycle_frames = warm_end - warm_start + 1
            active_time = float(completed["active_time_seconds"])
            sleep_time = float(completed["sleep_time_seconds"])
            self.timing_metadata.update({
                "active_time_seconds": active_time,
                "sleep_time_seconds": sleep_time,
                "cycle_time_seconds": active_time + sleep_time,
                "active_frames": int(round(active_time * self.fps)),
                "sleep_frames": int(round(sleep_time * self.fps)),
                "cycle_frames": cycle_frames,
            })
        self.timing_metadata.update({
            "maximum_particle_lifetime_seconds": maximum_lifetime,
            "warm_loop_start_frame": warm_start,
            "warm_loop_end_frame": warm_end,
            "completed_loop_count": len(self._completed_loop_windows),
        })

    def _expire_emitter(self):
        if self.emitter_expired and self.emitter_expiration_fired:
            return
        self.emitter_expired = True
        if not self.emitter_expiration_fired:
            self.emitter_expiration_fired = True
            self._trigger_events(
                None, self.emitter_events.expiration_events()
            )

    def enable_standalone_manual_preview(self):
        """Emulate external manual triggers for an isolated preview.

        Manual emitters are driven by the game rather than by an authored
        spawn rate. In a standalone DCC preview there is no game caller, so
        provide a conservative one-trigger-per-second cadence through the
        clip. The first trigger is immediate, which keeps short previews
        useful, while the authored max-particle cap remains authoritative.
        """
        if self.rate_mode != "manual":
            return False
        self.rate_mode = "steady"
        self.rate_amt = 1.0
        self.rate_max = max(1, int(self.evaluate_val(
            self.rate_max, self.get_context()
        )))

        self.spawn_fraction = 1.0
        self._cache_lifetime_window()
        self._warn_once(
            "particle.preview.manual_emission",
            "Standalone preview emulates external manual triggers at one per second",
            category="preview_environment",
            presentation="metadata",
        )
        return True


    def _warn_once(
        self,
        code,
        message,
        *,
        category=None,
        presentation=None,
    ):
        if code in self._warning_codes:
            return
        self._warning_codes.add(code)
        diagnostic = {
            "severity": "warning",
            "code": code,
            "message": message,
        }
        if category:
            diagnostic["category"] = str(category)
        if presentation:
            diagnostic["presentation"] = str(presentation)
        self.diagnostics.append(diagnostic)

    def _expire_particle(self, particle):
        if not particle.active:
            return
        particle.active = False
        if not particle.expiration_fired:
            particle.expiration_fired = True
            if self.lifetime_events:
                self._trigger_events(
                    particle,
                    self.lifetime_events.get("expiration_event"),
                )

    @staticmethod
    def _vector(value, fallback):
        if isinstance(value, mathutils.Vector):
            return value.copy()
        try:
            return mathutils.Vector(value)
        except (TypeError, ValueError):
            return fallback.copy()

    def _trigger_collision_events(self, particle, collision, speed):
        events = collision.get("events", ())
        if isinstance(events, dict):
            events = [events]
        if not isinstance(events, list):
            return
        for item in events:
            if not isinstance(item, dict):
                continue
            if speed < self.evaluate_val(item.get("min_speed", 2.0), self.get_context(particle)):
                continue
            event_name = item.get("event")
            if isinstance(event_name, dict):
                event_name = event_name.get("event")
            self._trigger_events(particle, event_name)

    def _apply_collision(self, particle, collision, ctx):
        if not callable(self.collision_provider):
            self._warn_once(
                "particle.world.collision_unavailable",
                "Collision requires world geometry; standalone preview leaves motion uncollided",
                category="preview_environment",
                presentation="metadata",
            )
            return
        radius = max(0.0, self.evaluate_val(collision.get("collision_radius", 0.0), ctx))
        world_end = self._point_to_world(particle, particle.position)
        world_start = getattr(particle, "_previous_world_position", None)
        if world_start is None:
            world_start = self._point_to_world(
                particle, particle.position - particle.velocity * self.dt
            )
        hit = self.collision_provider(
            start=world_start.copy(),
            end=world_end.copy(),
            radius=radius,
            context=ctx,
        )
        if not hit:
            return
        world_velocity = self._vector_to_world(particle, particle.velocity)
        speed = world_velocity.length
        self._trigger_collision_events(particle, collision, speed)
        if collision.get("expire_on_contact", False):
            self._expire_particle(particle)
            return
        if not isinstance(hit, dict):
            hit = {}
        if "position" in hit:
            hit_position = self._vector(hit.get("position"), world_end)
            particle.position = self._point_to_local(particle, hit_position)
        if "velocity" in hit:
            world_velocity = self._vector(hit.get("velocity"), world_velocity)
            particle.velocity = self._vector_to_local(particle, world_velocity)
            return
        normal = self._vector(hit.get("normal"), mathutils.Vector((0.0, 1.0, 0.0)))
        if normal.length <= 1.0e-8:
            return
        normal.normalize()
        restitution = max(0.0, self.evaluate_val(collision.get("coefficient_of_restitution", 0.0), ctx))
        normal_speed = world_velocity.dot(normal)
        if normal_speed < 0.0:
            world_velocity -= (1.0 + restitution) * normal_speed * normal
        drag = min(1.0, max(0.0, self.evaluate_val(collision.get("collision_drag", 0.0), ctx)))
        normal_component = world_velocity.project(normal)
        world_velocity = (
            normal_component
            + (world_velocity - normal_component) * (1.0 - drag)
        )
        particle.velocity = self._vector_to_local(particle, world_velocity)

    def _apply_block_expiration(self, particle, ctx):
        inside = self.parser.components.get("minecraft:particle_expire_if_in_blocks")
        outside = self.parser.components.get("minecraft:particle_expire_if_not_in_blocks")
        if inside is None and outside is None:
            return
        if not callable(self.block_provider):
            self._warn_once(
                "particle.world.block_unavailable",
                "Block-dependent expiration requires a block provider; standalone preview cannot evaluate it",
                category="preview_environment",
                presentation="metadata",
            )
            return
        world_position = self._point_to_world(particle, particle.position)
        block = str(self.block_provider(position=world_position.copy(), context=ctx) or "")
        if isinstance(inside, list) and block in {str(value) for value in inside}:
            self._expire_particle(particle)
        if isinstance(outside, list) and block not in {str(value) for value in outside}:
            self._expire_particle(particle)

    def get_context(self, particle=None):
        graphics_mode = str(
            self.initial_context.get("graphics_mode", "fancy")
        ).casefold()
        camera_distance = float(
            self.initial_context.get("camera_distance", 0.0) or 0.0
        )

        def graphics_mode_is_any(*modes):
            return 1.0 if any(
                str(mode).casefold() == graphics_mode for mode in modes
            ) else 0.0

        def camera_distance_range_lerp(first, second):
            start = float(first)
            end = float(second)
            if abs(end - start) <= 1.0e-12:
                return 0.0
            amount = (camera_distance - start) / (end - start)
            return max(0.0, min(1.0, amount))

        def missing_variable(name):
            self._warn_once(
                "particle.preview.variable_default",
                "Standalone preview supplied neutral value 1 for missing "
                f"variable.{name}",
                category="preview_environment",
                presentation="metadata",
            )

        def missing_query(name):
            self._warn_once(
                "particle.preview.query_default",
                "Standalone preview supplied a neutral query result for "
                f"missing query.{name}",
                category="preview_environment",
                presentation="metadata",
            )

        variable_scope = _PreviewScope(1.0, missing_variable) if self.preview_missing_context else {}
        query_scope = _PreviewScope(lambda *_args: 1.0, missing_query) if self.preview_missing_context else {}
        lifetime_context = {
            'v': variable_scope,
            'variable': variable_scope,
            'q': query_scope,
            'query': query_scope,
            't': {},
            'temp': {},
        }
        emitter_lifetime_value = self.evaluate_val(self.emitter_lifetime, lifetime_context) if not callable(self.emitter_lifetime) else 1.0
        ctx = {
            'v': variable_scope,
            'q': query_scope,
            't': {}
        }
        ctx['v'].update({
                'emitter_age': self.emitter_age,
                'emitter_lifetime': emitter_lifetime_value,
                'emitter_random_1': self.emitter_random_1,
                'emitter_random_2': self.emitter_random_2,
                'emitter_random_3': self.emitter_random_3,
                'emitter_random_4': self.emitter_random_4,
        })
        ctx['q'].update({
                'anim_time': self.anim_time,
                'emitter_age': self.emitter_age,
                'emitter_lifetime': emitter_lifetime_value,
                'emitter_random_1': self.emitter_random_1,
                'emitter_random_2': self.emitter_random_2,
                'emitter_random_3': self.emitter_random_3,
                'emitter_random_4': self.emitter_random_4,
                'graphics_mode_is_any': graphics_mode_is_any,
                'camera_distance_range_lerp': (
                    camera_distance_range_lerp
                ),
        })


        for scope, aliases in (("v", ("v", "variable")), ("q", ("q", "query")), ("t", ("t", "temp"))):
            for alias in aliases:
                values = self.initial_context.get(alias)
                if isinstance(values, dict):
                    ctx[scope].update(values)
        ctx['v'].update(self.emitter_custom_vars)

        # Alias for MoLang parser
        ctx['variable'] = ctx['v']
        ctx['query'] = ctx['q']
        ctx['temp'] = ctx['t']

        if particle:
            ctx['v'].update({
                'particle_age': particle.age,
                'particle_random_1': particle.random1,
                'particle_random_2': particle.random2,
                'particle_random_3': particle.random3,
                'particle_random_4': particle.random4,
                'particle_lifetime': particle.lifetime,
            })
            # Mirror particle-specific values into query namespace
            ctx['q'].update({
                'particle_age': particle.age,
                'particle_lifetime': particle.lifetime,
                'particle_random_1': particle.random1,
                'particle_random_2': particle.random2,
                'particle_random_3': particle.random3,
                'particle_random_4': particle.random4,
            })
            if hasattr(particle, 'custom_vars'):
                ctx['v'].update(particle.custom_vars)
        return ctx

    def evaluate_val(self, val, ctx):
        if isinstance(val, dict) and "expression" in val:
            val = val["expression"]
            
        if isinstance(val, (int, float)):
            return float(val)
        elif isinstance(val, str):
            return evaluate(val, ctx, rng=self.rng, min_engine_version=self.min_engine_version, diagnostics=self.molang_diagnostics)
        return 0.0

    def evaluate_curves(self, particle, ctx=None):
        curves = self.parser.particle_effect.get("curves", {})
        if not curves:
            return
        ctx = ctx if isinstance(ctx, dict) else self.get_context(particle)
        for curve_name, curve_data in curves.items():
            try:
                result = evaluate_curve(
                    str(curve_data.get("type", "linear")),
                    curve_data.get("nodes", []),
                    self.evaluate_val(curve_data.get("input", 0.0), ctx),
                    horizontal_range=curve_data.get("horizontal_range", 1.0),
                    resolver=lambda value: self.evaluate_val(value, ctx),
                )
            except CurveEvaluationError as exc:
                self._warn_once(
                    "particle.curve." + str(curve_name),
                    f"Curve {curve_name} could not be evaluated: {exc}",
                )
                result = 0.0
            canonical_name = str(curve_name)
            if canonical_name.startswith("variable."):
                canonical_name = canonical_name[9:]
            elif canonical_name.startswith("v."):
                canonical_name = canonical_name[2:]
            particle.custom_vars[canonical_name] = result
            # Curves are variables and may feed later curves or emitter-shape
            # expressions in the same creation/update evaluation.
            ctx.setdefault("v", {})[canonical_name] = result


    def get_shape_offset_and_dir(self, ctx):
        sample = sample_emitter_shape(
            self.shape_type,
            self.shape_data if isinstance(self.shape_data, dict) else {},
            lambda value: self.evaluate_val(value, ctx),
            self.rng,
            entity_aabb=(
                self._resolve_entity_aabb(ctx)
                if self.shape_type == "entity_aabb"
                else None
            ),
        )
        return (
            mathutils.Vector(sample.position),
            mathutils.Vector(sample.direction),
        )

    def _trigger_events(self, particle, event_names):
        if not event_names:
            return
        if isinstance(event_names, str):
            event_names = [event_names]
        elif not isinstance(event_names, (list, tuple)):
            return

        if particle is None:
            event_position = self._emitter_position()
            event_velocity = self._emitter_velocity()
            particle_id = None
        else:
            event_position = self._point_to_world(particle, particle.position)
            event_velocity = self._vector_to_world(particle, particle.velocity)
            particle_id = particle.id
        matrix = mathutils.Matrix.Translation(
            self._bedrock_to_blender_vector(event_position)
        )

        for event_name in event_names:
            ctx = self.get_context(particle)
            executor = EventExecutor(
                self.parser.events,
                _EventRandom(self.rng),
                expression_resolver=lambda value, _event_context: self.evaluate_val(value, ctx),
            )

            def execute_expression_value(value):
                execute_statements(
                    _expression_text(value),
                    ctx,
                    rng=self.rng,
                    min_engine_version=self.min_engine_version,
                    diagnostics=self.molang_diagnostics,
                )
                if particle is None:
                    self._store_custom_variables(
                        self.emitter_custom_vars, ctx
                    )
                else:
                    self._store_custom_variables(
                        particle.custom_vars, ctx
                    )

            def record_particle_effect(action, _event_context, _session):
                payload = action.payload if isinstance(action.payload, dict) else {}
                execute_expression_value(
                    payload.get("pre_effect_expression", 0.0)
                )
                child_context = dict(self.initial_context)
                for scope in ("v", "q", "t"):
                    child_context[scope] = dict(ctx.get(scope, {}))
                record = {
                    "time": self.anim_time,
                    "event_name": str(event_name),
                    "matrix": matrix.copy(),
                    "particle_id": particle_id,
                    "velocity": event_velocity.copy(),
                    "particle_effect": action.payload,
                    "molang_context": child_context,
                }
                if particle is not None:
                    particle.triggered_events.append(record)
                self.all_triggered_events.append(record)

            def execute_expression(action, _event_context, _session):
                execute_expression_value(action.payload)

            def record_host_action(action, _event_context, _session):
                if action.kind == "log":
                    self.diagnostics.append({
                        "severity": "info",
                        "code": "particle.event.log",
                        "message": str(action.payload),
                    })
                else:
                    self._warn_once(
                        "particle.event.sound_unavailable",
                        "Particle sound events are preserved but cannot be previewed in Blender",
                        category="preview_environment",
                        presentation="metadata",
                    )

            executor.register_handler("particle_effect", record_particle_effect)
            executor.register_handler("expression", execute_expression)
            executor.register_handler("log", record_host_action)
            executor.register_handler("sound_effect", record_host_action)
            result = executor.dispatch(
                str(event_name),
                EventContext(
                    effect_identifier=self.parser.get_identifier(),
                    emitter_id=0,
                    particle_id=particle_id,
                    variables=ctx,
                    source=SourceRef(uri=str(self.parser.filepath)),
                ),
            )
            for diagnostic in result.diagnostics:
                self.diagnostics.append({
                    "severity": diagnostic.severity.name.lower(),
                    "code": diagnostic.code,
                    "message": diagnostic.message,
                    "source": diagnostic.source.display(),
                })

    def spawn_particle(self, fraction_offset=0.0, frame=1):
        particle = ParticleState(self.next_particle_id, self.rng)
        self.next_particle_id += 1
        particle.spawn_frame = frame
        particle.local_space_position = self.local_space.position
        particle.local_space_rotation = self.local_space.rotation
        particle.local_space_velocity = self.local_space.velocity
        particle.manually_emitted = self.rate_mode == "manual"

        ctx = self.get_context(particle)
        if "v" in ctx:
            ctx["v"]["emitter_age"] = self.emitter_age + fraction_offset
        if "variable" in ctx:
            ctx["variable"]["emitter_age"] = self.emitter_age + fraction_offset

        particle.lifetime = self.evaluate_val(
            self.parser.get_particle_lifetime(), ctx
        )
        if str(getattr(
            self, "lifetime_override_mode", "SOURCE"
        )).upper() == "CLIP":
            remaining = max(
                self.dt,
                (self.duration_frames - frame + 1)
                / max(1.0, float(self.fps)),
            )
            particle.lifetime = max(float(particle.lifetime), remaining)
        for scope in ("v", "variable", "q", "query"):
            if isinstance(ctx.get(scope), dict):
                ctx[scope]["particle_lifetime"] = particle.lifetime
        # Bedrock curves are variable providers. Evaluate them before sampling
        # the emitter shape so offsets such as `variable.radius` do not fall
        # through the compatibility boundary as a missing reference.
        self.evaluate_curves(particle, ctx)
        if isinstance(ctx.get("v"), dict):
            ctx["v"].update(particle.custom_vars)
        if isinstance(ctx.get("variable"), dict):
            ctx["variable"].update(particle.custom_vars)
        offset, direction = self.get_shape_offset_and_dir(ctx)
        matrix = self._emitter_matrix_at(frame, fraction_offset)

        if particle.local_space_position:
            particle.position = offset
            particle.spawn_matrix = None
        else:
            if matrix is not None:
                blender_offset = matrix @ self._bedrock_to_blender_vector(offset)
                offset = self._blender_to_bedrock_vector(blender_offset)
                blender_direction = (
                    matrix.to_3x3()
                    @ self._bedrock_to_blender_vector(direction)
                )
                direction = self._blender_to_bedrock_vector(blender_direction)
                if direction.length > 1.0e-12:
                    direction.normalize()
                particle.spawn_matrix = matrix.to_3x3()
            else:
                particle.spawn_matrix = None
            particle.position = offset

        speed_expression = self.parser.get_initial_speed()
        if isinstance(speed_expression, list):
            velocity = mathutils.Vector(tuple(
                self.evaluate_val(
                    speed_expression[index]
                    if index < len(speed_expression)
                    else 0.0,
                    ctx,
                )
                for index in range(3)
            ))
            if not particle.local_space_position and matrix is not None:
                blender_velocity = (
                    matrix.to_3x3()
                    @ self._bedrock_to_blender_vector(velocity)
                )
                velocity = self._blender_to_bedrock_vector(blender_velocity)
            particle.velocity = velocity
        else:
            speed = self.evaluate_val(speed_expression, ctx)
            particle.velocity = (
                direction.normalized() * speed
                if direction.length > 1.0e-12
                else mathutils.Vector((0.0, 0.0, 0.0))
            )

        inherited_velocity = getattr(self, "inherited_velocity", None)
        if inherited_velocity is not None:
            inherited = self._vector(
                inherited_velocity, mathutils.Vector((0.0, 0.0, 0.0))
            )
            particle.velocity += self._vector_to_local(particle, inherited)
        if self.local_space.velocity:
            particle.velocity += self._vector_to_local(
                particle, self._emitter_velocity(frame)
            )

        vertical_override = self._vertical_override_values()
        if vertical_override is not None:
            world_velocity = self._vector_to_world(
                particle, particle.velocity
            )
            particle.override_vertical_velocity = vertical_override[0]
            world_velocity.y = particle.override_vertical_velocity
            particle.velocity = self._vector_to_local(
                particle, world_velocity
            )

        particle.spawn_direction = direction.copy()
        spin_component = self.parser.components.get(
            "minecraft:particle_initial_spin", {}
        )
        particle.rotation = self.evaluate_val(
            spin_component.get("rotation", 0.0), ctx
        )
        particle.rotation_rate = self.evaluate_val(
            spin_component.get("rotation_rate", 0.0), ctx
        )
        self.particles.append(particle)
        if self.lifetime_events:
            self._trigger_events(
                particle, self.lifetime_events.get("creation_event")
            )

    def simulate(self):
        ctx_init = self.get_context()
        execute_statements(self.creation_expr, ctx_init, rng=self.rng, min_engine_version=self.min_engine_version, diagnostics=self.molang_diagnostics)
        self._store_custom_variables(self.emitter_custom_vars, ctx_init)
        self._cache_lifetime_window()
        self._trigger_events(None, self.emitter_events.creation_events())
        previous_emitter_position = self._emitter_position(
            max(0, self.start_frame - 1)
        )
        
        for frame in range(self.start_frame, self.duration_frames + 1):
            self.current_frame = frame
            # A completed zero-sleep loop restarts before this tick's
            # timeline, update expressions, and emissions.
            self._roll_completed_loop()
            current_emitter_position = self._emitter_position(frame)
            travel_delta = (
                current_emitter_position - previous_emitter_position
            ).length
            self.emitter_total_distance += travel_delta
            self._trigger_events(
                None,
                self.emitter_events.travel_events(
                    self.emitter_total_distance, travel_delta
                ),
            )
            previous_emitter_position = current_emitter_position
            self._trigger_events(
                None,
                self.emitter_events.timeline_events(
                    self.emitter_age, self.emitter_age
                ),
            )
            ctx_emitter = self.get_context()
            execute_statements(self.per_update_expr, ctx_emitter, rng=self.rng, min_engine_version=self.min_engine_version, diagnostics=self.molang_diagnostics)
            self._store_custom_variables(
                self.emitter_custom_vars, ctx_emitter
            )

            # Spawn logic
            ctx_emitter = self.get_context()
            emitter_life = self.current_active_time
            max_particles = int(self.evaluate_val(self.rate_max, ctx_emitter))
            max_particles = max(0, max_particles)

            is_active = True
            if self.activation_expr is not None:
                is_active = self.evaluate_val(self.activation_expr, ctx_emitter) > 0.0
            expiration_requested = (
                not self.emitter_expired
                and self.evaluate_val(
                    self.emitter_expiration_expr, ctx_emitter
                ) != 0.0
            )
            defer_expiration = (
                expiration_requested
                and self.rate_mode == "instant"
                and not getattr(self, "has_spawned_instant", False)
            )
            if expiration_requested and not defer_expiration:
                self._expire_emitter()
            active_window = self.emitter_age + 1.0e-9 < emitter_life
            if (
                emitter_life <= 0.0
                and self.rate_mode == "instant"
                and not getattr(self, "has_spawned_instant", False)
            ):
                active_window = True
            if not self.emitter_expired and active_window and is_active:
                if self.rate_mode == "instant" and not getattr(self, "has_spawned_instant", False):
                    amt = int(self.evaluate_val(self.rate_amt, ctx_emitter))
                    active_count = sum(1 for p in self.particles if p.active)
                    for _ in range(min(amt, max_particles - active_count)):
                        self.spawn_particle(frame=frame)
                    self.has_spawned_instant = True
                elif self.rate_mode == "steady":
                    rate = self.evaluate_val(self.rate_amt, ctx_emitter)
                    spawn_amount = rate * self.dt
                    self.spawn_fraction += spawn_amount
                    
                    spawn_count = int(self.spawn_fraction)
                    self.spawn_fraction -= spawn_count
                    
                    for i in range(spawn_count):
                        active_count = sum(1 for p in self.particles if p.active)
                        if active_count < max_particles:
                            fraction_offset = (i / max(1, spawn_count)) * self.dt
                            self.spawn_particle(fraction_offset, frame=frame)
            else:
                # Loop transitions happen at the top of a frame. During a
                # positive sleep window this tick remains intentionally idle.
                pass

            if defer_expiration and not self.emitter_expired:
                self._expire_emitter()
            # Update particles
            motion_dynamic = self.parser.components.get("minecraft:particle_motion_dynamic", {})
            motion_param = self.parser.components.get("minecraft:particle_motion_parametric", {})
            
            linear_accel = motion_dynamic.get("linear_acceleration", [0,0,0])
            linear_drag = motion_dynamic.get("linear_drag_coefficient", 0.0)
            vertical_override = self._vertical_override_values()
            lifetime_through_clip = str(getattr(
                self, "lifetime_override_mode", "SOURCE"
            )).upper() == "CLIP"
            ignore_collisions = str(getattr(
                self, "collision_override_mode", "SOURCE"
            )).upper() == "IGNORE"

            size_data = self.parser.get_appearance_size()

            for p in self.particles:
                if not p.active:
                    continue

                if p.age >= p.lifetime:
                    self._expire_particle(p)
                    continue
                newly_spawned = int(getattr(p, "spawn_frame", -1)) == frame

                # Pre-calculate curves for this frame and push them to context
                self.evaluate_curves(p)

                ctx_p = self.get_context(p)
                if self.particle_update_expr:
                    execute_statements(self.particle_update_expr, ctx_p, rng=self.rng, min_engine_version=self.min_engine_version, diagnostics=self.molang_diagnostics)
                self._store_custom_variables(p.custom_vars, ctx_p)


                if (
                    not lifetime_through_clip
                    and self.evaluate_val(
                        self.particle_expiration_expr, ctx_p
                    ) != 0.0
                ):
                    self._expire_particle(p)
                    continue

                p._previous_world_position = self._point_to_world(
                    p, p.position
                )

                # Update physics
                parametric_supported = (
                    not getattr(p, "manually_emitted", False)
                    and (
                        not self.entity_based_emitter
                        or p.local_space_position
                    )
                )
                if motion_param and not parametric_supported:
                    self._warn_once(
                        "particle.motion.parametric_unsupported_host",
                        "Parametric motion is undefined for manually emitted "
                        "particles and non-local entity emitters; the unsupported "
                        "motion component was left inactive",
                    )
                if (
                    motion_param
                    and parametric_supported
                    and vertical_override is None
                ):
                    if "relative_position" in motion_param:
                        pos_expr = motion_param.get("relative_position", [0,0,0])
                        px = self.evaluate_val(pos_expr[0] if len(pos_expr)>0 else 0, ctx_p)
                        py = self.evaluate_val(pos_expr[1] if len(pos_expr)>1 else 0, ctx_p)
                        pz = self.evaluate_val(pos_expr[2] if len(pos_expr)>2 else 0, ctx_p)
                        relative_position = mathutils.Vector((px, py, pz))
                        p.position = (
                            relative_position
                            if p.local_space_position
                            else self._emitter_relative_to_world_position(
                                relative_position, frame
                            )
                        )
                    if "direction" in motion_param:
                        direction_expr = motion_param.get(
                            "direction", [0.0, 0.0, 0.0]
                        )
                        direction_value = mathutils.Vector(tuple(
                            self.evaluate_val(
                                direction_expr[index]
                                if index < len(direction_expr)
                                else 0.0,
                                ctx_p,
                            )
                            for index in range(3)
                        ))
                        if not p.local_space_position and p.spawn_matrix is not None:
                            blender_direction = (
                                p.spawn_matrix
                                @ self._bedrock_to_blender_vector(direction_value)
                            )
                            direction_value = self._blender_to_bedrock_vector(
                                blender_direction
                            )
                        p.velocity = direction_value
                    if "rotation" in motion_param:
                        p.rotation = self.evaluate_val(motion_param.get("rotation", 0.0), ctx_p)
                elif (
                    (not motion_param or vertical_override is not None)
                    and not newly_spawned
                ):
                    # Dynamic or user-authored vertical override. A particle's
                    # birth frame is sampled before its first physics step so
                    # imported effects expose their authored starting pose.
                    ax = self.evaluate_val(linear_accel[0] if len(linear_accel)>0 else 0, ctx_p)
                    ay = self.evaluate_val(linear_accel[1] if len(linear_accel)>1 else 0, ctx_p)
                    az = self.evaluate_val(linear_accel[2] if len(linear_accel)>2 else 0, ctx_p)
                    
                    if getattr(p, 'spawn_matrix', None):
                        bl_accel = mathutils.Vector((ax, az, ay))
                        bl_accel = p.spawn_matrix @ bl_accel
                        ax, ay, az = bl_accel.x, bl_accel.z, bl_accel.y
                    
                    drag = self.evaluate_val(linear_drag, ctx_p)
                    
                    p.velocity.x += ax * self.dt
                    p.velocity.y += ay * self.dt
                    p.velocity.z += az * self.dt

                    p.velocity *= max(0.0, 1.0 - drag * self.dt)
                    if vertical_override is not None:
                        current_vertical = float(getattr(
                            p,
                            "override_vertical_velocity",
                            vertical_override[0],
                        ))
                        current_vertical += (
                            vertical_override[1] * self.dt
                        )
                        p.override_vertical_velocity = current_vertical
                        world_velocity = self._vector_to_world(
                            p, p.velocity
                        )
                        world_velocity.y = current_vertical
                        p.velocity = self._vector_to_local(
                            p, world_velocity
                        )

                    p.position += p.velocity * self.dt

                    rotation_acceleration = self.evaluate_val(
                        motion_dynamic.get("rotation_acceleration", 0.0), ctx_p
                    )
                    rotation_drag = self.evaluate_val(motion_dynamic.get("rotation_drag_coefficient", 0.0), ctx_p)
                    p.rotation_rate += rotation_acceleration * self.dt
                    p.rotation_rate *= max(0.0, 1.0 - rotation_drag * self.dt)
                # --- Collision ---
                collision = (
                    None
                    if ignore_collisions
                    else self.parser.components.get(
                        "minecraft:particle_motion_collision"
                    )
                )
                if collision:
                    # Disabled collision must not skip the rest of the particle update.
                    enabled_expr = collision.get("enabled", True)
                    if self.evaluate_val(enabled_expr, ctx_p) > 0.0:
                        self._apply_collision(p, collision, ctx_p)

                # --- Kill Plane ---
                kill_plane = (
                    None
                    if ignore_collisions
                    else self.parser.components.get(
                        "minecraft:particle_kill_plane"
                    )
                )
                if kill_plane and isinstance(kill_plane, list) and len(kill_plane) >= 4:
                    a = self.evaluate_val(kill_plane[0], ctx_p)
                    b = self.evaluate_val(kill_plane[1], ctx_p)
                    c = self.evaluate_val(kill_plane[2], ctx_p)
                    d = self.evaluate_val(kill_plane[3], ctx_p)
                    world_position = self._point_to_world(p, p.position)
                    previous_world_position = getattr(
                        p, "_previous_world_position", world_position
                    )
                    relative_position = self._emitter_relative_position(
                        world_position, frame
                    )
                    previous_relative_position = (
                        self._emitter_relative_position(
                            previous_world_position, frame
                        )
                    )
                    value = (
                        a * relative_position.x
                        + b * relative_position.y
                        + c * relative_position.z
                        + d
                    )
                    previous_value = (
                        a * previous_relative_position.x
                        + b * previous_relative_position.y
                        + c * previous_relative_position.z
                        + d
                    )
                    if (
                        (previous_value <= 0.0 < value)
                        or (previous_value >= 0.0 > value)
                    ):
                        self._expire_particle(p)
                    p.kill_plane_previous = value

                if not ignore_collisions:
                    self._apply_block_expiration(p, ctx_p)
                if not p.active:
                    continue


                if self.particle_render_expr:
                    execute_statements(self.particle_render_expr, ctx_p, rng=self.rng, min_engine_version=self.min_engine_version, diagnostics=self.molang_diagnostics)
                    self._store_custom_variables(p.custom_vars, ctx_p)

                # Render expressions run immediately before appearance evaluation.
                if isinstance(size_data, list):
                    w = self.evaluate_val(size_data[0] if len(size_data)>0 else 0.1, ctx_p)
                    h = self.evaluate_val(size_data[1] if len(size_data)>1 else 0.1, ctx_p)
                else:
                    w = self.evaluate_val(size_data, ctx_p)
                    h = w
                
                # Bedrock billboard size values are radii/half-extents.
                p.size = [max(0.0, 2.0 * w), max(0.0, 2.0 * h)]

                # Update color tinting
                tinting = self.parser.components.get("minecraft:particle_appearance_tinting", {})
                color_data = tinting.get("color", [1.0, 1.0, 1.0, 1.0])

                def srgb2lin(c):
                    c = max(0.0, min(1.0, c))
                    return (c / 12.92) if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

                def parse_hex_color(hx):
                    """Parse a Bedrock hex color string. Bedrock uses #AARRGGBB format."""
                    if isinstance(hx, list):
                        ev = [self.evaluate_val(v, ctx_p) for v in hx]
                        rv, gv, bv = ev[0], ev[1], ev[2]
                        av = ev[3] if len(ev) > 3 else 1.0
                        # Bedrock color arrays are already linear; no sRGB conversion.
                        return (rv, gv, bv, av)
                    if isinstance(hx, str):
                        h = hx.lstrip('#')
                        if len(h) == 8:
                            # Bedrock: #AARRGGBB
                            av = int(h[0:2], 16) / 255.0
                            rv = int(h[2:4], 16) / 255.0
                            gv = int(h[4:6], 16) / 255.0
                            bv = int(h[6:8], 16) / 255.0
                        elif len(h) == 6:
                            # Standard: #RRGGBB
                            rv = int(h[0:2], 16) / 255.0
                            gv = int(h[2:4], 16) / 255.0
                            bv = int(h[4:6], 16) / 255.0
                            av = 1.0
                        else:
                            return (1.0, 1.0, 1.0, 1.0)
                        return (srgb2lin(rv), srgb2lin(gv), srgb2lin(bv), av)
                    return (1.0, 1.0, 1.0, 1.0)

                r, g, b, a = 1.0, 1.0, 1.0, 1.0

                if isinstance(color_data, dict):
                    # Gradient color
                    interpolant = self.evaluate_val(color_data.get("interpolant", 0.0), ctx_p)
                    # Bedrock gradients interpolate between terminal stops;
                    # out-of-range values hold the nearest terminal color.
                    interpolant = max(0.0, min(1.0, interpolant))
                    gradient = color_data.get("gradient", {})
                    if gradient:
                        if isinstance(gradient, list):
                            # Array format — equally spaced
                            n = len(gradient)
                            if n == 1:
                                r, g, b, a = parse_hex_color(gradient[0])
                            else:
                                scaled = interpolant * (n - 1)
                                gi = int(math.floor(scaled))
                                gi = max(0, min(n - 2, gi))
                                lt = scaled - gi
                                c1 = parse_hex_color(gradient[gi])
                                c2 = parse_hex_color(gradient[gi + 1])
                                r = c1[0] + (c2[0] - c1[0]) * lt
                                g = c1[1] + (c2[1] - c1[1]) * lt
                                b = c1[2] + (c2[2] - c1[2]) * lt
                                a = c1[3] + (c2[3] - c1[3]) * lt
                        else:
                            # Object format with explicit stop keys
                            stops = sorted([(float(k), v) for k, v in gradient.items()])
                            if interpolant <= stops[0][0]:
                                r, g, b, a = parse_hex_color(stops[0][1])
                            elif interpolant >= stops[-1][0]:
                                r, g, b, a = parse_hex_color(stops[-1][1])
                            else:
                                for si in range(len(stops) - 1):
                                    if stops[si][0] <= interpolant <= stops[si + 1][0]:
                                        span = stops[si + 1][0] - stops[si][0]
                                        lt = (interpolant - stops[si][0]) / span if span > 0 else 0.0
                                        c1 = parse_hex_color(stops[si][1])
                                        c2 = parse_hex_color(stops[si + 1][1])
                                        r = c1[0] + (c2[0] - c1[0]) * lt
                                        g = c1[1] + (c2[1] - c1[1]) * lt
                                        b = c1[2] + (c2[2] - c1[2]) * lt
                                        a = c1[3] + (c2[3] - c1[3]) * lt
                                        break

                elif isinstance(color_data, list):
                    rv = self.evaluate_val(color_data[0] if len(color_data) > 0 else 1.0, ctx_p)
                    gv = self.evaluate_val(color_data[1] if len(color_data) > 1 else 1.0, ctx_p)
                    bv = self.evaluate_val(color_data[2] if len(color_data) > 2 else 1.0, ctx_p)
                    av = self.evaluate_val(color_data[3] if len(color_data) > 3 else 1.0, ctx_p)
                    # Bedrock color arrays are already linear; no sRGB conversion.
                    r, g, b, a = rv, gv, bv, av

                elif isinstance(color_data, str) and color_data.startswith("#"):
                    r, g, b, a = parse_hex_color(color_data)

                else:
                    cv = self.evaluate_val(color_data, ctx_p)
                    r, g, b, a = cv, cv, cv, 1.0

                p.color = [r, g, b, a]

                # Update UV
                uv_offset = [0.0, 0.0]
                uv_scale = [1.0, 1.0]
                billboard = self.parser.components.get("minecraft:particle_appearance_billboard")
                if billboard is None:
                    billboard = self.parser.components.get("minecraft:particle_appearance_stretched_billboard", {})
                uv_data = billboard.get("uv", {})
                
                if uv_data:
                    # Bedrock's omitted axes use normalized coordinates,
                    # independently of the physical texture resolution.
                    authored_width = (
                        self.evaluate_val(uv_data["texture_width"], ctx_p)
                        if "texture_width" in uv_data
                        else 1.0
                    )
                    authored_height = (
                        self.evaluate_val(uv_data["texture_height"], ctx_p)
                        if "texture_height" in uv_data
                        else 1.0
                    )
                    tex_w = (
                        authored_width
                        if isinstance(authored_width, (int, float))
                        and not isinstance(authored_width, bool)
                        and math.isfinite(float(authored_width))
                        and float(authored_width) > 0.0
                        else self.texture_dimensions[0]
                    )
                    tex_h = (
                        authored_height
                        if isinstance(authored_height, (int, float))
                        and not isinstance(authored_height, bool)
                        and math.isfinite(float(authored_height))
                        and float(authored_height) > 0.0
                        else self.texture_dimensions[1]
                    )
                    if tex_w is None:
                        self._warn_once(
                            "particle.uv.texture_width_unavailable",
                            "Particle UV texture_width is absent or invalid and the texture image width is unavailable; deterministic preview width 1 was used",
                        )
                        tex_w = 1.0
                    if tex_h is None:
                        self._warn_once(
                            "particle.uv.texture_height_unavailable",
                            "Particle UV texture_height is absent or invalid and the texture image height is unavailable; deterministic preview height 1 was used",
                        )
                        tex_h = 1.0
                    
                    if "flipbook" in uv_data:
                        fb = uv_data["flipbook"]
                        base_u = self.evaluate_val(fb.get("base_UV", [0, 0])[0], ctx_p)
                        base_v = self.evaluate_val(fb.get("base_UV", [0, 0])[1], ctx_p)
                        size_u = self.evaluate_val(fb.get("size_UV", [1, 1])[0], ctx_p)
                        size_v = self.evaluate_val(fb.get("size_UV", [1, 1])[1], ctx_p)
                        step_u = self.evaluate_val(fb.get("step_UV", [0, 0])[0], ctx_p)
                        step_v = self.evaluate_val(fb.get("step_UV", [0, 0])[1], ctx_p)
                        fps = self.evaluate_val(fb.get("frames_per_second", 1), ctx_p)
                        max_frame = self.evaluate_val(fb.get("max_frame", 1), ctx_p)
                        stretch = fb.get("stretch_to_lifetime", False)
                        loop = fb.get("loop", False)
                        
                        if stretch:
                            progress = p.age / max(0.001, p.lifetime)
                            fb_frame = int(progress * max_frame)
                        else:
                            fb_frame = int(p.age * fps)
                            
                        if loop and max_frame > 0:
                            fb_frame = fb_frame % int(max_frame)
                        else:
                            fb_frame = min(fb_frame, int(max_frame) - 1 if max_frame > 0 else 0)
                            
                        u_px = base_u + fb_frame * step_u
                        v_px = base_v + fb_frame * step_v
                        
                        if tex_w > 0 and tex_h > 0:
                            uv_scale = [size_u / tex_w, size_v / tex_h]
                            uv_offset = [u_px / tex_w, 1.0 - ((v_px + size_v) / tex_h)]
                    elif "uv" in uv_data and "uv_size" in uv_data:
                        u_px = self.evaluate_val(uv_data["uv"][0], ctx_p)
                        v_px = self.evaluate_val(uv_data["uv"][1], ctx_p)
                        su_px = self.evaluate_val(uv_data["uv_size"][0], ctx_p)
                        sv_px = self.evaluate_val(uv_data["uv_size"][1], ctx_p)
                        
                        if tex_w > 0 and tex_h > 0:
                            uv_scale = [su_px / tex_w, sv_px / tex_h]
                            uv_offset = [u_px / tex_w, 1.0 - ((v_px + sv_px) / tex_h)]

                p.uv_offset = uv_offset
                p.uv_scale = uv_scale

                # Evaluate custom direction if lookat_direction is used
                custom_direction_vec = None
                dir_config = self.parser.get_billboard_direction()
                if (
                    dir_config
                    and dir_config.get("mode") in {"custom", "custom_direction"}
                ):
                    cd = dir_config.get("custom_direction", [0, 0, 1])
                    if isinstance(cd, list) and len(cd) >= 3:
                        cdx = self.evaluate_val(cd[0], ctx_p)
                        cdy = self.evaluate_val(cd[1], ctx_p)
                        cdz = self.evaluate_val(cd[2], ctx_p)
                        custom_direction_vec = mathutils.Vector((cdx, cdy, cdz))
                        if custom_direction_vec.length > 0.0001:
                            custom_direction_vec.normalize()

                # Save renderer-facing history in world Bedrock space.
                history_position = self._point_to_world(p, p.position)
                history_velocity = self._vector_to_world(p, p.velocity)
                history_spawn_direction = self._vector_to_world(
                    p, p.spawn_direction
                )
                history_custom_direction = (
                    self._vector_to_world(p, custom_direction_vec)
                    if custom_direction_vec is not None
                    else None
                )
                p.history[frame] = {
                    'pos': history_position * self.animation_scale,
                    'scale_x': p.size[0] * self.particle_scale,
                    'scale_y': p.size[1] * self.particle_scale,
                    'color': p.color,
                    'uv_offset': p.uv_offset,
                    'uv_scale': p.uv_scale,
                    'rot_z': p.rotation,
                    'velocity': history_velocity,
                    'spawn_direction': history_spawn_direction,
                    'custom_direction': history_custom_direction,
                }

                # Age
                p.age = round(p.age + self.dt, 12)
                if not motion_param or "rotation" not in motion_param:
                    p.rotation += p.rotation_rate * self.dt
                if p.age >= p.lifetime:
                    self._expire_particle(p)
                    
                # Evaluate lifetime events
                if self.lifetime_events:
                    # Timeline events
                    timeline = self.lifetime_events.get("timeline", {})
                    for t_str, event_names in timeline.items():
                        try:
                            t_val = float(t_str)
                            if p.age >= t_val and t_str not in p.fired_timeline_events:
                                p.fired_timeline_events.add(t_str)
                                self._trigger_events(p, event_names)
                        except ValueError:
                            pass

            previous_emitter_age = self.emitter_age
            self.emitter_age = round(self.emitter_age + self.dt, 12)
            self._trigger_events(
                None,
                self.emitter_events.timeline_events(
                    previous_emitter_age, self.emitter_age
                ),
            )
            if (
                self.has_lifetime_once
                and not self.is_looping
                and not self.emitter_expired
                and self.emitter_age >= emitter_life
            ):
                self._expire_emitter()
            self.anim_time = round(self.anim_time + self.dt, 12)

        self._finalize_timing_metadata()
        return self.particles

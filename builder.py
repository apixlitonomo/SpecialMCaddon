import bpy
from contextlib import contextmanager
import hashlib
import json
import os
import secrets
import tempfile
import uuid

from .parser import BedrockParticleParser
from .java_parser import JavaParticleParser
from .materials import create_particle_material, create_billboard_instance
from .importers.bedrock_assets import load_json_file


JSON_CACHE = {}

PARTICLE_SCHEMA_VERSION = 2
BACKEND_LEGACY_OBJECTS = "LEGACY_OBJECTS"
BACKEND_GN_CACHE_V2 = "GN_CACHE_V2"
_BACKEND_FACTORIES = {}
EMISSION_SOURCE = "SOURCE"
EMISSION_BURST = "BURST"
EMISSION_CONTINUOUS = "CONTINUOUS"
MOTION_SOURCE = "SOURCE"
MOTION_RISE = "RISE"
MOTION_FALL = "FALL"
MOTION_CUSTOM = "CUSTOM"
LIFETIME_SOURCE = "SOURCE"
LIFETIME_CLIP = "CLIP"
COLLISION_SOURCE = "SOURCE"
COLLISION_IGNORE = "IGNORE"


def particle_control_values(
    scene=None, emitter=None, overrides=None, *, new_import=False
):
    """Return one validated, format-neutral particle control contract."""
    start = int(
        scene.frame_current
        if scene is not None and new_import
        else getattr(scene, "frame_start", 1)
    )
    duration = max(
        1, int(getattr(scene, "mcbedrock_bake_frames", 400))
    )
    values = {
        "mode": EMISSION_SOURCE,
        "burst_count": 10,
        "rate": 10.0,
        "max_active": 100,
        "start_frame": start,
        "end_frame": start + duration - 1,
        "motion_mode": MOTION_SOURCE,
        "vertical_speed": 1.0,
        "vertical_acceleration": 0.0,
        "lifetime_mode": LIFETIME_SOURCE,
        "collision_mode": COLLISION_SOURCE,
        "random_seed": 0,
    }
    if emitter is not None:
        values.update({
            "mode": str(getattr(
                emitter, "mcbedrock_emission_mode", EMISSION_SOURCE
            ) or EMISSION_SOURCE),
            "burst_count": getattr(emitter, "mcbedrock_burst_count", 10),
            "rate": getattr(emitter, "mcbedrock_emission_rate", 10.0),
            "max_active": getattr(emitter, "mcbedrock_max_active", 100),
            "start_frame": getattr(emitter, "mcbedrock_start_frame", start),
            "end_frame": getattr(
                emitter, "mcbedrock_end_frame", start + duration - 1
            ),
            "motion_mode": str(getattr(
                emitter, "mcbedrock_motion_mode", MOTION_SOURCE
            ) or MOTION_SOURCE),
            "vertical_speed": getattr(
                emitter, "mcbedrock_vertical_speed", 1.0
            ),
            "vertical_acceleration": getattr(
                emitter, "mcbedrock_vertical_acceleration", 0.0
            ),
            "lifetime_mode": str(getattr(
                emitter, "mcbedrock_lifetime_mode", LIFETIME_SOURCE
            ) or LIFETIME_SOURCE),
            "collision_mode": str(getattr(
                emitter, "mcbedrock_collision_mode", COLLISION_SOURCE
            ) or COLLISION_SOURCE),
            "random_seed": getattr(
                emitter,
                "mcbedrock_random_seed",
                emitter.get("mcbedrock_seed", 0),
            ),
        })
    if isinstance(overrides, dict):
        values.update({
            key: value for key, value in overrides.items()
            if key in values
        })

    values["mode"] = (
        values["mode"]
        if values["mode"] in {
            EMISSION_SOURCE, EMISSION_BURST, EMISSION_CONTINUOUS
        }
        else EMISSION_SOURCE
    )
    values["motion_mode"] = (
        values["motion_mode"]
        if values["motion_mode"] in {
            MOTION_SOURCE, MOTION_RISE, MOTION_FALL, MOTION_CUSTOM
        }
        else MOTION_SOURCE
    )
    values["lifetime_mode"] = (
        values["lifetime_mode"]
        if values["lifetime_mode"] in {LIFETIME_SOURCE, LIFETIME_CLIP}
        else LIFETIME_SOURCE
    )
    values["collision_mode"] = (
        values["collision_mode"]
        if values["collision_mode"] in {
            COLLISION_SOURCE, COLLISION_IGNORE
        }
        else COLLISION_SOURCE
    )
    values["burst_count"] = max(0, int(values["burst_count"]))
    values["rate"] = max(0.0, float(values["rate"]))
    values["max_active"] = max(1, int(values["max_active"]))
    values["start_frame"] = int(values["start_frame"])
    values["end_frame"] = max(
        values["start_frame"], int(values["end_frame"])
    )
    values["duration_frames"] = (
        values["end_frame"] - values["start_frame"] + 1
    )
    values["vertical_speed"] = float(values["vertical_speed"])
    values["vertical_acceleration"] = float(
        values["vertical_acceleration"]
    )
    values["random_seed"] = max(
        0, min(2147483647, int(values["random_seed"]))
    )
    return values


def ensure_particle_controls(
    emitter, scene, *, new_import=False, overrides=None
):
    """Initialize or explicitly apply the shared particle controls."""
    if emitter is None:
        return None
    initialized = bool(
        emitter.get("mcbedrock_controls_initialized", False)
    )
    if not initialized or isinstance(overrides, dict):
        values = particle_control_values(
            scene,
            emitter if initialized else None,
            overrides,
            new_import=new_import,
        )
        emitter.mcbedrock_start_frame = values["start_frame"]
        emitter.mcbedrock_end_frame = values["end_frame"]
        emitter.mcbedrock_emission_mode = values["mode"]
        emitter.mcbedrock_burst_count = values["burst_count"]
        emitter.mcbedrock_emission_rate = values["rate"]
        emitter.mcbedrock_max_active = values["max_active"]
        emitter.mcbedrock_motion_mode = values["motion_mode"]
        emitter.mcbedrock_vertical_speed = values["vertical_speed"]
        emitter.mcbedrock_vertical_acceleration = values[
            "vertical_acceleration"
        ]
        emitter.mcbedrock_lifetime_mode = values["lifetime_mode"]
        emitter.mcbedrock_collision_mode = values["collision_mode"]
        emitter.mcbedrock_random_seed = values["random_seed"]
        emitter.mcbedrock_emission_dirty = False
        emitter["mcbedrock_controls_initialized"] = True
    return emitter


def particle_emission_controls(emitter):
    """Return the selected emitter's validated bake controls."""
    return particle_control_values(emitter=emitter)


def apply_java_particle_controls(emitter, scene):
    """Apply the shared controls to Blender's native Java particle backend."""
    systems = tuple(getattr(emitter, "particle_systems", ()) or ())
    if not systems:
        return False
    settings = systems[0].settings
    controls = particle_control_values(scene, emitter=emitter)
    start = controls["start_frame"]
    end = controls["end_frame"]
    fps = max(1.0, float(scene.render.fps))
    clip_frames = end - start + 1
    source_lifetime = max(
        1.0,
        float(emitter.get(
            "mcbedrock_java_source_lifetime", settings.lifetime
        )),
    )
    lifetime = (
        float(clip_frames)
        if controls["lifetime_mode"] == LIFETIME_CLIP
        else source_lifetime
    )

    if controls["mode"] == EMISSION_BURST:
        settings.count = controls["burst_count"]
        settings.frame_start = start
        settings.frame_end = start
    elif controls["mode"] == EMISSION_CONTINUOUS:
        duration_seconds = clip_frames / fps
        settings.count = max(
            0, int(round(controls["rate"] * duration_seconds))
        )
        settings.frame_start = start
        settings.frame_end = end
        if controls["rate"] > 0.0:
            lifetime = min(
                lifetime,
                controls["max_active"] / controls["rate"] * fps,
            )
    else:
        settings.count = max(
            1, int(emitter.get(
                "mcbedrock_java_source_count", settings.count
            ))
        )
        settings.frame_start = start
        settings.frame_end = end
    settings.lifetime = max(1.0, lifetime)

    source_speed = float(
        emitter.get("mcbedrock_java_source_speed", settings.normal_factor)
    )
    source_align = tuple(emitter.get(
        "mcbedrock_java_source_align", (0.0, 0.0, 0.0)
    ))
    source_gravity = float(emitter.get(
        "mcbedrock_java_source_gravity",
        getattr(settings.effector_weights, "gravity", 1.0),
    ))
    speed_scale = max(
        0.001, float(getattr(emitter, "mcbedrock_speed_scale", 1.0))
    )
    motion_mode = controls["motion_mode"]
    if motion_mode == MOTION_SOURCE:
        settings.normal_factor = source_speed * speed_scale
        if hasattr(settings, "object_align_factor"):
            settings.object_align_factor = tuple(
                float(value) * speed_scale for value in source_align
            )
        settings.effector_weights.gravity = source_gravity
    else:
        speed = controls["vertical_speed"]
        acceleration = controls["vertical_acceleration"]
        if motion_mode == MOTION_RISE:
            speed, acceleration = abs(speed), abs(acceleration)
        elif motion_mode == MOTION_FALL:
            speed, acceleration = -abs(speed), -abs(acceleration)
        settings.normal_factor = 0.0
        if hasattr(settings, "object_align_factor"):
            settings.object_align_factor = (
                0.0, 0.0, speed * speed_scale
            )
        # Scene gravity points down. A negative weight produces upward force.
        settings.effector_weights.gravity = -acceleration / 9.81

    if (
        controls["collision_mode"] == COLLISION_IGNORE
        and hasattr(settings, "use_die_on_collision")
    ):
        settings.use_die_on_collision = False
    emitter.mcbedrock_emission_dirty = False
    return True


class ParticleBackendUnavailable(RuntimeError):
    """The host cannot provide an optional particle renderer backend."""


def _ensure_builtin_backends():
    if BACKEND_GN_CACHE_V2 in _BACKEND_FACTORIES:
        return
    from .particle_backend_v3 import build_gn_cache
    register_particle_backend(BACKEND_GN_CACHE_V2, build_gn_cache)


def _append_warning(current, message):
    message = str(message or "").strip()
    if not message:
        return current
    return f"{current} | {message}" if current else message


_PREVIEW_ENVIRONMENT_DIAGNOSTIC_CODES = frozenset({
    "particle.world.collision_unavailable",
    "particle.world.block_unavailable",
    "particle.event.sound_unavailable",
})


def _diagnostic_is_preview_environment(diagnostic):
    if not isinstance(diagnostic, dict):
        return False
    if str(diagnostic.get("category", "")).casefold() == "preview_environment":
        return True
    if str(diagnostic.get("presentation", "")).casefold() in {
        "metadata",
        "stored",
        "stored_only",
    }:
        return True
    return str(diagnostic.get("code", "")) in (
        _PREVIEW_ENVIRONMENT_DIAGNOSTIC_CODES
    )


def _annotate_empty_particle_previews(effects, bake_frames):
    """Explain valid effects that produced no renderable preview samples."""
    for effect_data in effects.values():
        particles = effect_data.get("particles", ())
        if any(getattr(particle, "history", None) for particle in particles):
            continue

        parser = effect_data.get("parser")
        rate_mode = parser.get_rate()[0] if parser is not None else ""
        existing = list(effect_data.get("diagnostics", ()) or ())
        existing_codes = {
            str(item.get("code", ""))
            for item in existing
            if isinstance(item, dict)
        }
        if rate_mode == "manual":
            code = "particle.preview.manual_emitter"
            reason = "the effect uses manual emission"
        elif "molang.missing_reference" in existing_codes:
            code = "particle.preview.empty_context"
            reason = "required host context was unavailable"
        else:
            code = "particle.preview.empty_output"
            reason = "its authored conditions emitted no particles"

        if code in existing_codes:
            continue
        existing.append({
            "severity": "warning",
            "code": code,
            "message": (
                f"No renderable particles were produced during the "
                f"{int(bake_frames)}-frame preview because {reason}; "
                "an empty controllable cache was created"
            ),
        })
        effect_data["diagnostics"] = existing


def _collect_particle_diagnostics(effects):
    collected = []
    for effect_identifier, effect_data in effects.items():
        for item in effect_data.get("diagnostics", ()):
            if isinstance(item, dict):
                diagnostic = dict(item)
            else:
                diagnostic = {
                    "severity": "warning",
                    "code": "particle.diagnostic",
                    "message": str(item),
                }
            diagnostic.setdefault(
                "effect_identifier", str(effect_identifier)
            )
            if diagnostic not in collected:
                collected.append(diagnostic)
    return collected


def _diagnostics_json(diagnostics):
    return json.dumps(
        list(diagnostics),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def apply_particle_preview_frame(context, result):
    """Move new imports to a representative visible cache frame."""
    if not result or not result.get("emitter"):
        return None
    emitter = result["emitter"]
    preview_frame = int(
        result.get(
            "preview_frame",
            emitter.get("mcbedrock_preview_frame", 1),
        )
        or 1
    )
    scene = context.scene
    # The render range may exclude this emitter (for example, an import at a
    # later playhead position). Clamping would show a frame with no particles.
    scene.frame_set(preview_frame)
    return preview_frame



def register_particle_backend(name, factory, *, replace=False):
    """Register a renderer factory without changing the public build API.

    A factory is called with keyword-only build state (context, emitter,
    collection, effects, source settings, and helpers). It must return ``None``
    without side effects to decline, or a mapping containing ``render_objects``
    after it has created a real backend. Backend metadata is stamped only for a
    successful result.
    """
    backend_name = str(name)
    if not callable(factory):
        raise TypeError("Particle backend factory must be callable")
    if backend_name in _BACKEND_FACTORIES and not replace:
        raise ValueError(f"Particle backend already registered: {backend_name}")
    _BACKEND_FACTORIES[backend_name] = factory


def unregister_particle_backend(name, factory=None):
    """Remove an optional backend, optionally checking its current owner."""
    backend_name = str(name)
    current = _BACKEND_FACTORIES.get(backend_name)
    if current is None or (factory is not None and current is not factory):
        return False
    del _BACKEND_FACTORIES[backend_name]
    return True


def _stable_particle_seed(identifier, source_path):
    payload = (
        f"{identifier}|{os.path.normcase(os.path.abspath(source_path))}"
    ).encode("utf-8", errors="surrogatepass")
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "little") & 0x7FFFFFFF

def _derive_particle_seed(seed, *parts):
    payload = "|".join([str(int(seed or 0)), *(str(part) for part in parts)])
    return int.from_bytes(
        hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).digest()[:4],
        "little",
    ) & 0x7FFFFFFF


def _particle_engine_version(filepath, molang_context=None):
    """Resolve MoLang behavior from the containing pack, not format_version."""
    if isinstance(molang_context, dict):
        explicit = molang_context.get("min_engine_version")
        if explicit:
            if isinstance(explicit, (list, tuple)):
                return ".".join(str(int(part)) for part in explicit[:3]), None
            return str(explicit), None

    current = os.path.abspath(os.path.dirname(filepath))
    while True:
        manifest_path = os.path.join(current, "manifest.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8-sig") as handle:
                    manifest = json.load(handle)
                version = manifest.get("header", {}).get("min_engine_version")
                if isinstance(version, (list, tuple)) and version:
                    return ".".join(str(int(part)) for part in version[:3]), None
                if version:
                    return str(version), None
            except (OSError, ValueError, TypeError):
                pass
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return (
        "1.26.30",
        "No containing pack min_engine_version was found; using Bedrock 1.26.30",
    )


def _particle_source_metadata(filepath, effect_identifier=""):
    """Return namespace and containing pack manifest version."""
    identifier = str(effect_identifier or "")
    namespace = identifier.split(":", 1)[0] if ":" in identifier else ""
    manifest_version = ""
    current = os.path.abspath(os.path.dirname(str(filepath or "")))
    while current:
        manifest_path = os.path.join(current, "manifest.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8-sig") as handle:
                    manifest = json.load(handle)
                version = manifest.get("header", {}).get("version")
                if isinstance(version, (list, tuple)):
                    manifest_version = ".".join(str(part) for part in version)
                elif version is not None:
                    manifest_version = str(version)
            except (OSError, ValueError, TypeError):
                manifest_version = ""
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return namespace, manifest_version

def _json_safe_context(context):
    """Return a deterministic JSON snapshot without serializing callables."""
    if not isinstance(context, dict):
        return ""

    def sanitize(value):
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, dict):
            return {
                str(key): sanitized
                for key, item in value.items()
                if (sanitized := sanitize(item)) is not None
            }
        if isinstance(value, (list, tuple)):
            return [
                sanitized
                for item in value
                if (sanitized := sanitize(item)) is not None
            ]
        return None

    try:
        return json.dumps(
            sanitize(context),
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return ""


def stamp_emitter_metadata(
    emitter,
    *,
    source_path,
    texture_override="",
    force_loop=False,
    interpolation="Closest",
    effect_identifier="",
    source_kind=None,
    logical_entry="",
    source_namespace="",
    manifest_version="",
    molang_context=None,
    backend=BACKEND_LEGACY_OBJECTS,
):
    """Stamp the stable v3 emitter contract without migrating old scenes."""
    if emitter is None:
        return None
    physical_source = os.path.abspath(str(source_path or ""))
    source_kind = str(
        source_kind
        or emitter.get("mcbedrock_source_kind", "")
        or (
            "brarchive"
            if physical_source.lower().endswith(".brarchive")
            else "file"
        )
    )
    emitter["mcbedrock_is_emitter"] = True
    emitter["mcbedrock_particle_schema"] = PARTICLE_SCHEMA_VERSION
    emitter["mcbedrock_backend"] = str(backend)
    emitter["mcbedrock_source_kind"] = source_kind
    emitter["mcbedrock_source_path"] = physical_source
    emitter["mcbedrock_filepath"] = physical_source
    emitter["mcbedrock_source_entry"] = str(logical_entry or "")
    emitter["mcbedrock_source_namespace"] = str(
        source_namespace
        or emitter.get("mcbedrock_source_namespace", "")
        or (
            str(effect_identifier).split(":", 1)[0]
            if ":" in str(effect_identifier)
            else ""
        )
    )
    emitter["mcbedrock_manifest_version"] = str(
        manifest_version or emitter.get("mcbedrock_manifest_version", "")
    )
    if "mcbedrock_emitter_id" not in emitter:
        emitter["mcbedrock_emitter_id"] = uuid.uuid4().hex
    emitter["mcbedrock_effect_identifier"] = str(effect_identifier or "")
    emitter["mcbedrock_texture"] = str(texture_override or "")
    emitter["mcbedrock_force_loop"] = bool(force_loop)
    emitter["mcbedrock_interpolation"] = str(
        interpolation or "Closest"
    )
    if "mcbedrock_seed" not in emitter:
        emitter["mcbedrock_seed"] = _stable_particle_seed(
            effect_identifier,
            physical_source,
        )
    context_json = _json_safe_context(molang_context)
    if context_json:
        emitter["mcbedrock_context_json"] = context_json
    return emitter


def _remove_particle_render_object(obj):
    """Remove one backend object and cache-only orphan datablocks."""
    data = getattr(obj, "data", None)
    node_groups = [
        modifier.node_group
        for modifier in getattr(obj, "modifiers", ())
        if modifier.type == "NODES" and modifier.node_group is not None
    ]
    materials = list(getattr(data, "materials", ()) or ()) if data else []
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        if data.__class__.__name__ == "Curves":
            bpy.data.hair_curves.remove(data)
        elif data.__class__.__name__ == "Mesh":
            bpy.data.meshes.remove(data)
    for node_group in node_groups:
        if node_group.users == 0:
            bpy.data.node_groups.remove(node_group)
    for material in materials:
        if material is not None and material.users == 0:
            bpy.data.materials.remove(material)

def particle_render_objects(emitter):
    """Return render objects for legacy and cache-backed emitters."""
    if emitter is None:
        return ()
    result = []
    seen = set()
    emitter_id = str(emitter.get("mcbedrock_emitter_id", ""))
    collections = tuple(getattr(emitter, "users_collection", ()) or ())
    candidates = list(getattr(emitter, "children", ()) or ())
    collection_members = set()
    for collection in collections:
        objects = tuple(collection.objects)
        candidates.extend(objects)
        collection_members.update(obj.as_pointer() for obj in objects)
    for obj in candidates:
        pointer = obj.as_pointer()
        if pointer in seen or obj == emitter or not getattr(obj, "data", None):
            continue
        owner_id = str(obj.get("mcbedrock_particle_owner_id", ""))
        is_owned = bool(emitter_id and owner_id == emitter_id) or (
            not owner_id
            and obj.get("mcbedrock_particle_owner", "") == emitter.name
        )
        is_legacy = obj.name.startswith("Particle_") and (
            obj.parent == emitter or pointer in collection_members
        )
        is_cache = bool(obj.get("mcbedrock_particle_cache", False)) and (
            is_owned or obj.parent == emitter
        )
        if is_owned or is_legacy or is_cache:
            seen.add(pointer)
            result.append(obj)
    return tuple(result)


def _try_registered_cache_backend(
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
):
    """Invoke the optional GN cache renderer and normalize its result."""
    if "mcbedrock_emitter_id" not in emitter:
        emitter["mcbedrock_emitter_id"] = uuid.uuid4().hex
    factory = _BACKEND_FACTORIES.get(BACKEND_GN_CACHE_V2)
    if factory is None:
        return None
    result = factory(
        context=context,
        emitter=emitter,
        collection=collection,
        effects=effects,
        filepath=filepath,
        texture_override=texture_override,
        force_loop=force_loop,
        interpolation=interpolation,
        molang_context=molang_context,
        parent_target=parent_target,
        world_space=world_space,
        emitter_matrices=emitter_matrices,
        resolve_texture=resolve_texture_bedrock,
        create_material=create_particle_material,
        create_billboard=create_billboard_instance,
    )
    if result is None:
        return None
    if not isinstance(result, dict):
        raise TypeError("Particle backend factory must return a mapping or None")
    render_objects = tuple(result.get("render_objects", ()) or ())
    if not render_objects:
        raise ValueError("Particle cache backend created no render objects")
    for obj in render_objects:
        obj["mcbedrock_particle_cache"] = True
        obj["mcbedrock_particle_render"] = True
        obj["mcbedrock_particle_owner"] = emitter.name
        obj["mcbedrock_particle_owner_id"] = str(
            emitter.get("mcbedrock_emitter_id", "")
        )
    normalized = dict(result)
    normalized["render_objects"] = render_objects
    return normalized

@contextmanager
def materialized_emitter_source(emitter):
    """Yield a JSON path for file and .brarchive-backed emitters."""
    source_path = str(
        emitter.get("mcbedrock_source_path", "")
        or emitter.get("mcbedrock_filepath", "")
    )
    source_kind = str(emitter.get("mcbedrock_source_kind", "file"))
    logical_entry = str(emitter.get("mcbedrock_source_entry", ""))
    if source_kind != "brarchive" and not source_path.lower().endswith(
        ".brarchive"
    ):
        yield source_path
        return
    if not logical_entry:
        raise ValueError("Archive-backed emitter has no logical particle entry")
    from .importers.brarchive_reader import extract_brarchive

    with tempfile.TemporaryDirectory(prefix="mcbedrock_particle_") as folder:
        extracted = extract_brarchive(source_path, folder)
        particle_path = extracted.get(logical_entry)
        if not particle_path:
            raise FileNotFoundError(
                f"Archive entry not found: {logical_entry}"
            )
        yield particle_path

def find_json_by_identifier(root_dir, target_identifier):
    root_key = os.path.normcase(os.path.abspath(root_dir))
    cache = JSON_CACHE.get(root_key)
    if cache is None:
        cache = {}
        for root, directories, files in os.walk(root_key):
            directories.sort(key=str.casefold)
            for filename in sorted(files, key=str.casefold):
                if filename.lower().endswith(".json"):
                    path = os.path.join(root, filename)
                    try:
                        data = load_json_file(path)
                        if "particle_effect" in data:
                            ident = data["particle_effect"].get("description", {}).get("identifier", "")
                            if ident:
                                cache.setdefault(ident, path)
                    except Exception:
                        pass
        JSON_CACHE[root_key] = cache
    return cache.get(target_identifier)


def detect_format(filepath):
    """
    Auto-detect whether a JSON file is Bedrock or Java format.
    """
    try:
        data = load_json_file(filepath)
    except Exception as e:
        print("Error parsing format:", e)
        return "unknown"

    if not isinstance(data, dict):
        return "unknown"

    if "particle_effect" in data or "minecraft:particle_effect" in data:
        return "bedrock"
    
    # Sometimes bedrock files are unwrapped
    if "components" in data and "description" in data:
        return "bedrock"

    # Java checks, including Polytone custom-particle and modifier companions.
    if "textures" in data or "values" in data:
        return "java"
    normalized_path = os.path.abspath(filepath).replace("\\", "/").casefold()
    if (
        "/polytone/custom_particles/" in normalized_path
        or "/polytone/particle_modifiers/" in normalized_path
    ):
        return "java"
    # Deep bedrock check
    for key, value in data.items():
        if isinstance(value, dict) and "description" in value and "components" in value:
            return "bedrock"

    return "unknown"


def _texture_file_candidates(path):
    """Yield exact image candidates with Bedrock's extension priority."""
    path = os.path.normpath(os.fspath(path))
    extension = os.path.splitext(path)[1].casefold()
    if extension in {".tga", ".png", ".jpg", ".jpeg"}:
        yield path
        return
    for suffix in (".tga", ".png", ".jpg", ".jpeg"):
        yield path + suffix


def _particle_texture_resolution(
    context,
    parser,
    json_filepath,
):
    """Resolve one particle texture with pack provenance and safe fallbacks."""
    from .importers.bedrock_assets import (
        find_resource_pack_roots,
        resolve_texture_resource,
    )
    from .importers.pack_stack import canonical_logical_id

    raw_texture = str(parser.get_texture_path() or "")
    logical_id = canonical_logical_id(raw_texture)
    state = {
        "path": logical_id,
        "logical_id": logical_id,
        "pack_root": "",
        "priority": 0,
        "source_kind": "unresolved",
        "source_pointer": (
            "/particle_effect/description/"
            "basic_render_parameters/texture"
        ),
        "diagnostics": [],
    }
    if not logical_id:
        state["diagnostics"].append({
            "severity": "warning",
            "code": "particle.texture.missing_reference",
            "message": "Particle effect has no texture reference",
        })
        return state

    if os.path.isabs(logical_id):
        for candidate in _texture_file_candidates(logical_id):
            if os.path.isfile(candidate):
                state.update({
                    "path": os.path.abspath(candidate),
                    "pack_root": os.path.dirname(os.path.abspath(candidate)),
                    "priority": 1,
                    "source_kind": "absolute",
                })
                return state

    variants = [logical_id]
    if ":" in logical_id:
        unnamespaced = logical_id.split(":", 1)[1]
        if unnamespaced and unnamespaced not in variants:
            variants.append(unnamespaced)
    resolution_issues = []
    for variant in variants:
        result = resolve_texture_resource(
            context,
            variant,
            json_filepath,
            state["source_pointer"],
        )
        if result.ok:
            resource = result.resource
            state.update({
                "path": resource.path,
                "logical_id": logical_id,
                "pack_root": resource.pack_root,
                "priority": resource.priority,
                "source_kind": "resource_pack",
                "source_pointer": resource.source_pointer,
            })
            return state
        resolution_issues.extend(result.issues)

    # Loose-file compatibility is intentionally narrow: exact authored paths
    # relative to the JSON and an exact sibling basename are deterministic.
    json_dir = os.path.dirname(os.path.abspath(json_filepath))
    relative_id = variants[-1]
    for base in (
        os.path.join(json_dir, *relative_id.split("/")),
        os.path.join(json_dir, os.path.basename(relative_id)),
    ):
        for candidate in _texture_file_candidates(base):
            if os.path.isfile(candidate):
                state.update({
                    "path": os.path.abspath(candidate),
                    "pack_root": json_dir,
                    "priority": 1,
                    "source_kind": "loose_file",
                })
                return state

    basename = os.path.basename(relative_id)

    # Engine-owned vanilla particle sheets bundled with the add-on are an
    # explicit final fallback, never a resource-pack precedence override.
    addon_assets = os.path.join(os.path.dirname(__file__), "assets")
    for candidate in _texture_file_candidates(
        os.path.join(addon_assets, basename)
    ):
        if os.path.isfile(candidate):
            state.update({
                "path": os.path.abspath(candidate),
                "pack_root": addon_assets,
                "priority": 0,
                "source_kind": "bundled_fallback",
            })
            return state

    # Preserve legacy loose effects only when a basename search is unique.
    scene = getattr(context, "scene", None)
    preferred = (
        bpy.path.abspath(
            getattr(scene, "mcbedrock_resource_pack_path", "") or ""
        )
        if scene is not None
        else ""
    )
    search_roots = list(
        find_resource_pack_roots(json_filepath, preferred)
    )
    basename_matches = []
    seen_paths = set()
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        for current, directories, files in os.walk(root):
            directories[:] = sorted(
                [
                    name for name in directories
                    if name not in {
                        ".git", ".svn", "__MACOSX", "__pycache__",
                        "node_modules",
                    }
                ],
                key=str.casefold,
            )
            for filename in sorted(files, key=str.casefold):
                stem, extension = os.path.splitext(filename)
                if (
                    stem != basename
                    or extension.casefold()
                    not in {".tga", ".png", ".jpg", ".jpeg"}
                ):
                    continue
                candidate = os.path.abspath(os.path.join(current, filename))
                key = os.path.normcase(candidate)
                if key not in seen_paths:
                    seen_paths.add(key)
                    basename_matches.append(candidate)
    if len(basename_matches) == 1:
        matched_path = basename_matches[0]
        matched_root = next(
            (
                root
                for root in search_roots
                if os.path.commonpath((root, matched_path)) == root
            ),
            json_dir,
        )
        state.update({
            "path": matched_path,
            "pack_root": matched_root,
            "priority": 1,
            "source_kind": "legacy_unique_basename",
        })
        return state
    if len(basename_matches) > 1:
        state["diagnostics"].append({
            "severity": "warning",
            "code": "particle.texture.ambiguous_basename",
            "message": (
                f"Texture {logical_id!r} has multiple basename matches; "
                "an explicit resource-pack path or texture override is required"
            ),
        })

    if not state["diagnostics"]:
        messages = []
        for issue in resolution_issues:
            message = str(getattr(issue, "message", "")).strip()
            if message and message not in messages:
                messages.append(message)
        state["diagnostics"].append({
            "severity": "warning",
            "code": "particle.texture.not_found",
            "message": (
                f"Texture {logical_id!r} could not be resolved"
                + (f": {'; '.join(messages[:2])}" if messages else "")
            ),
        })
    return state


def resolve_texture_bedrock(context, parser, json_filepath):
    """Compatibility path wrapper around the source-aware resolver."""
    return _particle_texture_resolution(
        context,
        parser,
        json_filepath,
    )["path"]

def _resolved_particle_texture_state(
    context,
    parser,
    json_filepath,
    texture_override="",
):
    """Resolve the texture and its real pixel dimensions before simulation."""
    override = bpy.path.abspath(str(texture_override or ""))
    if override and os.path.isfile(override):
        resolution = {
            "path": os.path.abspath(override),
            "logical_id": str(parser.get_texture_path() or ""),
            "pack_root": os.path.dirname(os.path.abspath(override)),
            "priority": 2,
            "source_kind": "manual_override",
            "source_pointer": "manual_texture",
            "diagnostics": [],
        }
    else:
        resolution = _particle_texture_resolution(
            context,
            parser,
            json_filepath,
        )
    texture_path = resolution["path"]
    if not texture_path or not os.path.isfile(texture_path):
        return texture_path, None, resolution
    try:
        image = bpy.data.images.load(texture_path, check_existing=True)
        dimensions = int(image.size[0]), int(image.size[1])
    except (RuntimeError, ValueError, OSError):
        return texture_path, None, resolution
    if dimensions[0] <= 0 or dimensions[1] <= 0:
        return texture_path, None, resolution
    return os.path.abspath(texture_path), dimensions, resolution


def resolve_texture_java(context, tex_relative, json_filepath):
    """Resolve a namespace-aware Java particle texture to a local image."""
    from .java_parser import JavaParticleParser

    reference = str(tex_relative or "").replace("\\", "/").strip()
    try:
        default_namespace = JavaParticleParser(json_filepath).namespace
    except Exception:
        default_namespace = "minecraft"
    if ":" in reference:
        namespace, texture_name = reference.split(":", 1)
    else:
        namespace, texture_name = default_namespace, reference
    namespace = namespace or "minecraft"
    texture_name = texture_name.lstrip("/")
    if texture_name.startswith("textures/"):
        texture_name = texture_name[len("textures/"):]
    for extension in (".png", ".tga", ".jpg", ".jpeg"):
        if texture_name.casefold().endswith(extension):
            texture_name = texture_name[:-len(extension)]
            break

    extensions = (".png", ".tga", ".jpg", ".jpeg", "")
    logical_names = [
        f"assets/{namespace}/textures/{texture_name}",
        f"assets/{namespace}/textures/particle/{texture_name}",
        f"textures/{texture_name}",
        f"textures/particle/{texture_name}",
    ]
    roots = []

    def add_root(value):
        if not value:
            return
        value = os.path.abspath(value)
        if os.path.isdir(value) and value not in roots:
            roots.append(value)
        resource_packs = os.path.join(value, "resource_packs")
        if os.path.isdir(resource_packs):
            for child in sorted(os.listdir(resource_packs), key=str.casefold):
                candidate = os.path.join(resource_packs, child)
                if os.path.isdir(candidate) and candidate not in roots:
                    roots.append(candidate)

    from .importers.bedrock_assets import asset_resource_roots
    for root in asset_resource_roots(context, json_filepath):
        add_root(root)

    current = os.path.dirname(os.path.abspath(json_filepath))
    for _ in range(12):
        add_root(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    for root in roots:
        for logical_name in logical_names:
            for extension in extensions:
                candidate = os.path.normpath(os.path.join(root, logical_name + extension))
                if os.path.isfile(candidate):
                    return candidate

    addon_dir = os.path.dirname(__file__)
    basename = os.path.basename(texture_name)
    bundled_tex = os.path.join(addon_dir, "assets", f"{basename}.png")
    if os.path.isfile(bundled_tex):
        return bundled_tex
    return reference

def build_particle_system(context, filepath, texture_override="", spawn_offset=(0.0, 0.0, 0.0), force_loop=False, existing_emitter=None, interpolation='Closest', molang_context=None, parent_target=None, particle_controls=None):
    """
    Main entrypoint. Detects format and routes to the correct builder.
    """
    fmt = detect_format(filepath)

    if fmt == "bedrock":
        _ensure_builtin_backends()
        return _build_bedrock(context, filepath, texture_override, spawn_offset, force_loop, existing_emitter, interpolation, molang_context, parent_target, particle_controls)
    elif fmt == "java":
        return _build_java(context, filepath, texture_override, spawn_offset, interpolation, particle_controls)
    else:
        raise ValueError(
            "Unrecognized particle JSON format. "
            "Expected Bedrock ('particle_effect') or Java ('textures') top-level key."
        )

def simulate_recursively(filepath, context, fps, duration_frames, start_frame=1, static_emitter_matrix=None, global_particles_dict=None, root_dir=None, depth=0, force_loop=False, emitter_matrices=None, molang_context=None, seed=0, min_engine_version=None, child_emission_type=None, inherited_velocity=None, event_budget_state=None, texture_override="", emission_controls=None):
    if global_particles_dict is None:
        global_particles_dict = {}
    if event_budget_state is None:
        event_budget_state = {"spawns": 0, "max_spawns": 8192}
    if root_dir is None:
        root_dir = os.path.dirname(filepath)
    if depth > 16:
        print(f"Max sub-emitter depth reached. Aborting recursion for {filepath}")
        return global_particles_dict

    from .simulator import BedrockSimulator
    parser = BedrockParticleParser(filepath)
    ident = parser.get_identifier()
    texture_path, texture_dimensions, texture_resolution = _resolved_particle_texture_state(
        context,
        parser,
        filepath,
        texture_override,
    )
    if min_engine_version is None:
        min_engine_version, _ = _particle_engine_version(filepath, molang_context)
    simulation_context = dict(molang_context) if isinstance(molang_context, dict) else {}
    simulation_context.setdefault("_preview_missing_context", True)



    sim = BedrockSimulator(
        parser,
        fps=fps,
        duration_frames=duration_frames,
        start_frame=start_frame,
        static_emitter_matrix=static_emitter_matrix,
        force_loop=force_loop,
        emitter_matrices=emitter_matrices,
        initial_context=simulation_context,
        seed=seed,
        min_engine_version=min_engine_version,
        texture_dimensions=texture_dimensions,
    )
    if child_emission_type in {"particle", "particle_with_velocity"}:
        sim.rate_mode = "instant"
        sim.rate_amt = 1
        sim.rate_max = 1
        sim.emitter_lifetime = 0.0
    elif depth == 0:
        overridden = sim.apply_emission_override(emission_controls)
        if not overridden:
            sim.enable_standalone_manual_preview()
    sim.inherited_velocity = inherited_velocity if child_emission_type == "particle_with_velocity" else None
    particles = sim.simulate()
    
    if ident not in global_particles_dict:
        global_particles_dict[ident] = {"parser": parser, "particles": [], "diagnostics": [], "texture_path": texture_path, "texture_dimensions": texture_dimensions, "texture_resolution": texture_resolution, "timing": dict(sim.timing_metadata)}
    global_particles_dict[ident]["particles"].extend(particles)
    diagnostics = list(getattr(sim, "diagnostics", ()) or ())
    diagnostics.extend(texture_resolution.get("diagnostics", ()))
    for item in getattr(getattr(parser, "normalization", None), "diagnostics", ()):
        diagnostics.append({
            "severity": getattr(getattr(item, "severity", None), "name", "warning").lower(),
            "code": getattr(item, "code", "particle.normalization"),
            "message": getattr(item, "message", str(item)),
            "source": getattr(getattr(item, "source", None), "display", lambda: str(filepath))(),
        })

    for item in getattr(sim, "molang_diagnostics", ()) or ():
        span = getattr(item, "span", None)
        diagnostics.append({
            "severity": getattr(item, "severity", "error"),
            "code": getattr(item, "code", "molang.error"),
            "message": getattr(item, "message", str(item)),
            "line": getattr(span, "line", 1),
            "column": getattr(span, "column", 1),
        })
    existing_diagnostics = global_particles_dict[ident]["diagnostics"]
    for diagnostic in diagnostics:
        if diagnostic not in existing_diagnostics:
            existing_diagnostics.append(diagnostic)
    
    if hasattr(sim, "all_triggered_events") and sim.all_triggered_events:
        allowed_types = {"emitter", "emitter_bound", "particle", "particle_with_velocity"}
        for ev in sim.all_triggered_events:
            particle_effect = ev.get("particle_effect")
            if not isinstance(particle_effect, dict):
                continue
            effect_type = str(particle_effect.get("type", "emitter"))
            effect_id = particle_effect.get("effect")
            if effect_type not in allowed_types:
                existing_diagnostics.append({
                    "severity": "warning",
                    "code": "particle.event.child_type",
                    "message": f"Unsupported child particle emission type: {effect_type}",
                })
                continue
            if not effect_id:
                continue
            sub_json_path = find_json_by_identifier(root_dir, effect_id)
            if not sub_json_path:
                existing_diagnostics.append({
                    "severity": "warning",
                    "code": "particle.event.child_missing",
                    "message": f"Child particle effect could not be resolved: {effect_id}",
                })
                continue
            if event_budget_state["spawns"] >= event_budget_state["max_spawns"]:
                existing_diagnostics.append({
                    "severity": "error",
                    "code": "particle.event.spawn_budget",
                    "message": "Nested particle spawn budget exceeded",
                })
                break
            event_budget_state["spawns"] += 1
            ev_start_frame = int(ev["time"] * fps) + 1
            child_static_matrix = ev["matrix"]
            child_matrices = None
            if effect_type == "emitter_bound":
                import mathutils
                parent_state = next(
                    (particle for particle in particles if particle.id == ev.get("particle_id")),
                    None,
                )
                if parent_state is not None:
                    child_matrices = [child_static_matrix.copy() for _ in range(duration_frames + 1)]
                    current_matrix = child_static_matrix.copy()
                    for child_frame in range(ev_start_frame, duration_frames + 1):
                        history = parent_state.history.get(child_frame)
                        if history is not None:
                            position = history["pos"]
                            current_matrix = mathutils.Matrix.Translation(
                                mathutils.Vector((position.x, position.z, position.y))
                            )
                        child_matrices[child_frame] = current_matrix.copy()
                    child_static_matrix = None
            simulate_recursively(
                sub_json_path, context, fps, duration_frames,
                start_frame=ev_start_frame,
                static_emitter_matrix=child_static_matrix,
                global_particles_dict=global_particles_dict,
                root_dir=root_dir,
                depth=depth + 1,
                force_loop=force_loop if effect_type in {"emitter", "emitter_bound"} else False,
                emitter_matrices=child_matrices,
                molang_context=ev.get("molang_context", molang_context),
                seed=_derive_particle_seed(seed, effect_id, ev_start_frame, depth + 1),
                min_engine_version=min_engine_version,
                child_emission_type=effect_type,
                inherited_velocity=ev.get("velocity"),
                event_budget_state=event_budget_state,
                texture_override=texture_override,
            )
    return global_particles_dict


def _finalize_particle_build(
    *,
    emitter,
    collection,
    filepath,
    texture_override,
    force_loop,
    interpolation,
    effect_identifier,
    molang_context,
    existing_emitter,
    backend,
    warning=None,
    render_objects=(),
    extra=None,
):
    previous_source = (
        str(existing_emitter.get("mcbedrock_source_path", ""))
        if existing_emitter is not None
        else ""
    )
    source_path = previous_source or filepath
    source_kind = (
        str(existing_emitter.get("mcbedrock_source_kind", ""))
        if existing_emitter is not None
        else ""
    ) or None
    logical_entry = (
        str(existing_emitter.get("mcbedrock_source_entry", ""))
        if existing_emitter is not None
        else ""
    )
    discovered_namespace, discovered_manifest = _particle_source_metadata(
        filepath,
        effect_identifier,
    )
    source_namespace = (
        str(existing_emitter.get("mcbedrock_source_namespace", ""))
        if existing_emitter is not None
        else ""
    ) or discovered_namespace
    manifest_version = (
        str(existing_emitter.get("mcbedrock_manifest_version", ""))
        if existing_emitter is not None
        else ""
    ) or discovered_manifest
    stamp_emitter_metadata(
        emitter,
        source_path=source_path,
        texture_override=texture_override,
        force_loop=force_loop,
        interpolation=interpolation,
        effect_identifier=effect_identifier,
        source_kind=source_kind,
        logical_entry=logical_entry,
        source_namespace=source_namespace,
        manifest_version=manifest_version,
        molang_context=molang_context,
        backend=backend,
    )
    collection["mcbedrock_particle_collection_schema"] = (
        PARTICLE_SCHEMA_VERSION
    )
    collection["mcbedrock_particle_owner"] = emitter.name
    collection["mcbedrock_particle_owner_id"] = str(
        emitter.get("mcbedrock_emitter_id", "")
    )
    result = {
        "warning": warning,
        "emitter": emitter,
        "collection": collection,
        "backend": backend,
        "render_objects": tuple(render_objects or ()),
    }
    if extra:
        result.update(extra)
    return result

def _build_bedrock(context, filepath, texture_override="", spawn_offset=(0.0, 0.0, 0.0), force_loop=False, existing_emitter=None, interpolation='Closest', molang_context=None, parent_target=None, particle_controls=None):
    """Build a baked particle system from a Bedrock particle JSON using the Python Simulator."""
    from .simulator import BedrockSimulator
    
    parser = BedrockParticleParser(filepath)
    ident = parser.get_identifier()
    short_name = ident.split(":")[-1] if ":" in ident else ident

    # Determine bake duration (default 250 if not set)
    bake_frames = getattr(context.scene, "mcbedrock_bake_frames", 250)
    emission_controls = (
        particle_emission_controls(existing_emitter)
        if existing_emitter is not None
        else particle_control_values(
            context.scene,
            overrides=particle_controls,
            new_import=True,
        )
    )
    bake_frames = max(1, int(emission_controls["duration_frames"]))

    min_engine_version, engine_warning = _particle_engine_version(
        filepath,
        molang_context,
    )
    requested_seed = int(emission_controls.get("random_seed", 0) or 0)
    if requested_seed > 0:
        seed = requested_seed
    elif existing_emitter is not None and "mcbedrock_seed" in existing_emitter:
        seed = int(existing_emitter.get("mcbedrock_seed", 0))
    else:
        # Blockbench/Snowstorm rerolls emitter_random_* for a fresh preview.
        # Store the generated seed on the emitter so every rebuild stays exact.
        seed = secrets.randbelow(2147483647) + 1
    fps = context.scene.render.fps

    emitter_matrices = None
    matrix_source = existing_emitter or parent_target
    if matrix_source is not None:
        import mathutils
        emitter_matrices = []
        original_frame = context.scene.frame_current
        local_offset = (
            mathutils.Matrix.Translation(spawn_offset)
            if existing_emitter is None
            else mathutils.Matrix.Identity(4)
        )
        for frame in range(bake_frames + 1):
            context.scene.frame_set(frame)
            context.view_layer.update()
            emitter_matrices.append(
                matrix_source.matrix_world.copy() @ local_offset
            )
        context.scene.frame_set(original_frame)
        context.view_layer.update()

    global_particles_dict = simulate_recursively(
        filepath, context, fps, bake_frames,
        static_emitter_matrix=None,
        force_loop=force_loop,
        emitter_matrices=emitter_matrices,
        molang_context=molang_context,
        seed=seed,
        min_engine_version=min_engine_version,
        texture_override=texture_override,
        emission_controls=emission_controls,
    )

    _annotate_empty_particle_previews(
        global_particles_dict,
        bake_frames,
    )
    particle_diagnostics = _collect_particle_diagnostics(
        global_particles_dict
    )
    simulation_messages = []
    for diagnostic in particle_diagnostics:
        if _diagnostic_is_preview_environment(diagnostic):
            continue
        message = str(diagnostic.get("message", "")).strip()
        if message and message not in simulation_messages:
            simulation_messages.append(message)
    simulation_warning = "; ".join(simulation_messages[:6])
    if len(simulation_messages) > 6:
        simulation_warning += f"; {len(simulation_messages) - 6} more diagnostics"

    if existing_emitter:

        emitter = existing_emitter
        group_col = emitter.users_collection[0] if emitter.users_collection else context.collection

        # Rebuild only objects owned by this emitter. This keeps unrelated
        # particle systems safe even if users manually merged collections.
        for child in particle_render_objects(emitter):
            _remove_particle_render_object(child)
    else:
        # Create Dedicated Collection
        col_name = f"MC_{short_name}"
        # Ensure unique name
        if col_name in bpy.data.collections:
            i = 1
            while f"{col_name}.{i:03d}" in bpy.data.collections:
                i += 1
            col_name = f"{col_name}.{i:03d}"
            
        group_col = bpy.data.collections.new(col_name)
        context.collection.children.link(group_col)

        # Create Parent Emitter Empty
        emitter = bpy.data.objects.new(f"Emitter_{short_name}", None)
        emitter.empty_display_type = 'SPHERE'
        emitter.empty_display_size = 0.5
        emitter.location = spawn_offset
        context.collection.objects.link(emitter)
        
        # Move emitter to new collection
        group_col.objects.link(emitter)
        for col in emitter.users_collection:
            if col != group_col:
                col.objects.unlink(emitter)
        if parent_target is not None:
            emitter.parent = parent_target

    ensure_particle_controls(
        emitter,
        context.scene,
        new_import=existing_emitter is None,
        overrides=particle_controls if existing_emitter is None else None,
    )
    emitter["mcbedrock_baked_duration_frames"] = int(bake_frames)
    emitter["mcbedrock_seed"] = int(seed)
    emitter.mcbedrock_random_seed = int(seed)
    emitter["mcbedrock_min_engine_version"] = str(min_engine_version)
    emitter["mcbedrock_engine_notice"] = str(engine_warning or "")
    emitter["mcbedrock_diagnostics_json"] = _diagnostics_json(
        particle_diagnostics
    )
    emitter["mcbedrock_diagnostic_count"] = len(particle_diagnostics)
    emitter["mcbedrock_preview_limitation_count"] = sum(
        _diagnostic_is_preview_environment(item)
        for item in particle_diagnostics
    )

    warning = simulation_warning or None

    # Particle caches are authored in seconds. Let Blender drop viewport
    # frames when drawing cannot keep up so a dense effect does not appear to
    # run slower than its Bedrock timing. This must happen before the GN
    # backend's early return as well as for the legacy object backend.
    context.scene.sync_mode = 'FRAME_DROP'


    try:
        cache_result = _try_registered_cache_backend(
            context=context,
            emitter=emitter,
            collection=group_col,
            effects=global_particles_dict,
            filepath=filepath,
            texture_override=texture_override,
            force_loop=force_loop,
            interpolation=interpolation,
            molang_context=molang_context,
            parent_target=parent_target,
            world_space=emitter_matrices is not None,
            emitter_matrices=emitter_matrices,
        )
    except ParticleBackendUnavailable as exc:
        cache_result = None
        warning = _append_warning(
            warning,
            f"GN cache unavailable ({exc}); using legacy objects",
        )
    if cache_result is not None:
        emitter.mcbedrock_emission_dirty = False
        for obj in context.selected_objects:
            obj.select_set(False)
        emitter.select_set(True)
        context.view_layer.objects.active = emitter
        return _finalize_particle_build(
            emitter=emitter,
            collection=group_col,
            filepath=filepath,
            texture_override=texture_override,
            force_loop=force_loop,
            interpolation=interpolation,
            effect_identifier=ident,
            molang_context=molang_context,
            existing_emitter=existing_emitter,
            backend=BACKEND_GN_CACHE_V2,
            warning=_append_warning(warning, cache_result.get("warning")),
            render_objects=cache_result["render_objects"],
            extra={
                key: value
                for key, value in cache_result.items()
                if key not in {"warning", "render_objects"}
            },
        )

    for effect_id, effect_data in global_particles_dict.items():
        sub_parser = effect_data["parser"]
        particles = effect_data["particles"]
        if not particles:
            continue
        short_name = effect_id.split(":")[-1] if ":" in effect_id else effect_id
        parser = sub_parser
        # Billboard instance base

        # DO NOT pre-evaluate size at frame 0, as variables like variable.psize evaluate to 0.0 and break the mesh scale.
        # Instead, we create a normalized 1x1 plane and scale it per frame!
        facing_mode = parser.get_billboard_mode()
        base_instance = create_billboard_instance(context, ident, 1.0, 1.0, facing_mode=facing_mode)
        base_instance["mcbedrock_particle_render"] = True
        base_instance["mcbedrock_particle_owner"] = emitter.name
        base_instance["mcbedrock_effect_identifier"] = effect_id

        # Move base_instance to new collection
        group_col.objects.link(base_instance)
        for col in base_instance.users_collection:
            if col != group_col:
                col.objects.unlink(base_instance)

        # Texture
        if texture_override and os.path.isfile(texture_override):
            abs_tex = texture_override
        else:
            abs_tex = resolve_texture_bedrock(context, parser, filepath)

        if not os.path.isfile(abs_tex):
            warning = _append_warning(
                warning,
                f"Texture not found: '{sub_parser.get_texture_path()}'. "
                "Set your Resource Pack path or use manual texture.",
            )

        base_instance["mcbedrock_material_type"] = parser.get_material_type()
        base_instance["mcbedrock_is_lit"] = parser.has_lighting()
        base_instance["mcbedrock_resolved_texture"] = abs_tex
        create_particle_material(
            base_instance,
            abs_tex,
            parser.get_material_type(),
            is_lit=parser.has_lighting(),
            interpolation=interpolation,
        )

        base_instance.hide_set(False)
        base_instance.hide_render = False
        
        # We will hide the base instance by scaling it to 0 or moving it out of view.
        # Actually, we can just let it exist but not render it directly, or hide it statically.
        base_instance.hide_viewport = True
        base_instance.hide_render = True

        kf_options = {'options': {'FAST'}} if bpy.app.version >= (2, 92, 0) else {}

        # Bake Particles
        for p in particles:
            if not p.history:
                continue
            p_obj = base_instance.copy()
            # Do NOT copy the mesh data; share it across all particles for performance!
            group_col.objects.link(p_obj)
            
            if not existing_emitter:
                p_obj.parent = emitter
                
            p_obj.name = f"Particle_{short_name}_{p.id}"
            p_obj["mcbedrock_particle_render"] = True
            p_obj["mcbedrock_particle_owner"] = emitter.name
            p_obj["mcbedrock_particle_id"] = int(p.id)
            p_obj["mcbedrock_effect_identifier"] = effect_id

            # Keep them statically visible in the outliner so the depsgraph doesn't constantly rebuild
            p_obj.hide_viewport = False
            p_obj.hide_render = False
            
            frames = sorted(p.history.keys())
            first_frame = frames[0]
            last_frame = frames[-1]
            
            if not p_obj.animation_data:
                p_obj.animation_data_create()
            # Setup UVs on object so fcurves can bind
            p_obj["uv_offset_x"] = 0.0
            p_obj["uv_offset_y"] = 0.0
            p_obj["uv_scale_x"] = 1.0
            p_obj["uv_scale_y"] = 1.0
            p_obj["mc_alpha"] = 1.0
            p_obj["mc_r"] = 1.0
            p_obj["mc_g"] = 1.0
            p_obj["mc_b"] = 1.0

            action = bpy.data.actions.new(name=f"Anim_{p_obj.name}")
            p_obj.animation_data.action = action
            
            # Determine total frames: frame 0, frame first_frame-1 (if >1), all history frames, frame last_frame+1
            times = []
            _OFFSCREEN = (99999.0, 99999.0, 99999.0)
            
            locs_x, locs_y, locs_z = [], [], []
            scales_x, scales_y, scales_z = [], [], []
            colors_r, colors_g, colors_b, colors_a = [], [], [], []
            uv_ox, uv_oy, uv_sx, uv_sy = [], [], [], []
            rot_type = 'XYZ'
            rot_w, rot_x, rot_y, rot_z = [], [], [], []
            
            def add_frame(f, loc, sc, col, uvo, uvs, rtype, rval, hide):
                # We do NOT use hide arrays anymore, we rely entirely on scale (0,0,0) to hide particles.
                # Teleporting objects causes Eevee TAA ghosting and massive 3 FPS lag, so we rely on scale!
                times.append(f)
                locs_x.append(loc[0]); locs_y.append(loc[1]); locs_z.append(loc[2])
                scales_x.append(sc[0]); scales_y.append(sc[1]); scales_z.append(sc[2])
                colors_r.append(col[0]); colors_g.append(col[1]); colors_b.append(col[2]); colors_a.append(col[3])
                uv_ox.append(uvo[0]); uv_oy.append(uvo[1])
                uv_sx.append(uvs[0]); uv_sy.append(uvs[1])
                if rtype == 'QUATERNION':
                    rot_w.append(rval.w); rot_x.append(rval.x); rot_y.append(rval.y); rot_z.append(rval.z)
                else:
                    rot_w.append(0.0); rot_x.append(0.0); rot_y.append(0.0); rot_z.append(rval)
                    
            # Keyframe before birth (hidden via scale 0)
            import mathutils
            import math
            history_frames = p.history
            # When emitter_matrices is supplied, the simulator already baked particle
            # positions in world space per frame, so the builder must not transform again.
            emitter_matrix = mathutils.Matrix() if emitter_matrices else emitter.matrix_world
            
            if first_frame > 1:
                first_state = history_frames[first_frame]
                first_loc = emitter_matrix @ mathutils.Vector((first_state['pos'].x, -first_state['pos'].z, first_state['pos'].y))
                add_frame(first_frame - 1, first_loc, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0, 1.0), (0.0, 0.0), (1.0, 1.0), 'XYZ', 0.0, True)
                # Ensure frame 0 is also keyed so it stays hidden
                add_frame(0, first_loc, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0, 1.0), (0.0, 0.0), (1.0, 1.0), 'XYZ', 0.0, True)

            # Insert history
            for frame in frames:
                state = history_frames[frame]
                loc = emitter_matrix @ mathutils.Vector((state['pos'].x, -state['pos'].z, state['pos'].y))
                sc = (state['scale_x'], state['scale_y'], 1.0)
                c = state.get('color', [1.0, 1.0, 1.0, 1.0])
                uvo = state.get('uv_offset', [0.0, 0.0])
                uvs = state.get('uv_scale', [1.0, 1.0])
                
                rot_z_deg = state.get('rot_z', 0.0)
                velocity = state.get('velocity', None)
                
                rt = 'XYZ'
                rv = math.radians(rot_z_deg)
                
                if facing_mode.startswith('direction_') or facing_mode == 'lookat_direction':
                    vel = state.get('custom_direction')
                    if vel is None or vel.length < 0.0001:
                        vel = velocity if velocity is not None and velocity.length > 0.0001 else state.get('spawn_direction')
                    if vel is not None and vel.length > 0.0001:
                        vel_norm = vel.normalized()
                        import mathutils
                        if facing_mode == 'lookat_direction':
                            cd = state.get('custom_direction')
                            if not cd: cd = mathutils.Vector((0, 0, -1))
                            bl_cd = mathutils.Vector((cd.x, -cd.z, cd.y)).normalized()
                            default_forward = mathutils.Vector((1, 0, 0))
                            rot_quat = default_forward.rotation_difference(bl_cd)
                            spin_quat = mathutils.Quaternion((0, 0, 1), math.radians(rot_z_deg))
                            final_quat = rot_quat @ spin_quat
                        else:
                            if facing_mode == 'direction_x':
                                default_forward = mathutils.Vector((1, 0, 0))
                            elif facing_mode == 'direction_z':
                                default_forward = mathutils.Vector((0, 0, 1))
                            else:
                                default_forward = mathutils.Vector((0, 1, 0))
                            forward = mathutils.Vector((vel_norm.x, -vel_norm.z, vel_norm.y))
                            rot_quat = default_forward.rotation_difference(forward)
                            spin_quat = mathutils.Quaternion((0, 0, 1), math.radians(rot_z_deg))
                            final_quat = rot_quat @ spin_quat
                        rt = 'QUATERNION'
                        rv = final_quat
                
                rot_type = rt # Remember last mode
                p_obj.rotation_mode = rt
                add_frame(frame, loc, sc, c, uvo, uvs, rt, rv, False)
                
            # Post-death state
            last_state = history_frames[last_frame]
            last_loc = emitter_matrix @ mathutils.Vector((last_state['pos'].x, -last_state['pos'].z, last_state['pos'].y))
            add_frame(last_frame + 1, last_loc, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0, 1.0), (0.0, 0.0), (1.0, 1.0), 'XYZ', 0.0, True)
            
            # Build fcurves fast
            def apply_fcurve(data_path, idx, vals):
                if hasattr(action, 'fcurves'):
                    fc = action.fcurves.new(data_path=data_path, index=idx)
                else:
                    fc = action.fcurve_ensure_for_datablock(p_obj, data_path, index=idx)
                
                fc.keyframe_points.add(len(times))
                flat = [0.0] * (len(times) * 2)
                flat[0::2] = times
                flat[1::2] = vals
                fc.keyframe_points.foreach_set('co', flat)
                # Default linear, color/uv use constant
                if data_path.startswith('["uv_'):
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'CONSTANT'
                else:
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'LINEAR'
                        
            apply_fcurve("location", 0, locs_x)
            apply_fcurve("location", 1, locs_y)
            apply_fcurve("location", 2, locs_z)
            apply_fcurve("scale", 0, scales_x)
            apply_fcurve("scale", 1, scales_y)
            apply_fcurve("scale", 2, scales_z)
            apply_fcurve("color", 0, colors_r)
            apply_fcurve("color", 1, colors_g)
            apply_fcurve("color", 2, colors_b)
            apply_fcurve("color", 3, colors_a)
            apply_fcurve('["mc_r"]', 0, colors_r)
            apply_fcurve('["mc_g"]', 0, colors_g)
            apply_fcurve('["mc_b"]', 0, colors_b)
            apply_fcurve('["mc_alpha"]', 0, colors_a)
            apply_fcurve('["uv_offset_x"]', 0, uv_ox)
            apply_fcurve('["uv_offset_y"]', 0, uv_oy)
            apply_fcurve('["uv_scale_x"]', 0, uv_sx)
            apply_fcurve('["uv_scale_y"]', 0, uv_sy)
            if rot_type == 'QUATERNION':
                apply_fcurve("rotation_quaternion", 0, rot_w)
                apply_fcurve("rotation_quaternion", 1, rot_x)
                apply_fcurve("rotation_quaternion", 2, rot_y)
                apply_fcurve("rotation_quaternion", 3, rot_z)
            else:
                apply_fcurve("rotation_euler", 2, rot_z)
                
            # Interpolation is already applied during apply_fcurve
                                    
            # Push into NLA track so we can scale speed live
            track = p_obj.animation_data.nla_tracks.new()
            nla_strip = track.strips.new(action.name, int(action.frame_range[0]), action)
            p_obj.animation_data.action = None

        for obj in context.selected_objects:
            obj.select_set(False)
        emitter.select_set(True)
        context.view_layer.objects.active = emitter

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.overlay.show_relationship_lines = False

        if base_instance:
            bpy.data.objects.remove(base_instance, do_unlink=True)

    return _finalize_particle_build(
        emitter=emitter,
        collection=group_col,
        filepath=filepath,
        texture_override=texture_override,
        force_loop=force_loop,
        interpolation=interpolation,
        effect_identifier=ident,
        molang_context=molang_context,
        existing_emitter=existing_emitter,
        backend=BACKEND_LEGACY_OBJECTS,
        warning=warning,
        render_objects=particle_render_objects(emitter),
    )

def _build_java(context, filepath, texture_override="", spawn_offset=(0.0, 0.0, 0.0), interpolation='Closest', particle_controls=None):
    """Build a Java particle, applying safe Polytone/MVEL initializer values."""
    from .java_parser import JavaParticleParser
    from .java_particle_compat import approximate_polytone_particle

    parser = JavaParticleParser(filepath)
    ident = parser.get_identifier()
    short_name = parser.name
    fps = context.scene.render.fps
    approximation = approximate_polytone_particle(
        parser.polytone_data,
        parser.modifier_data,
        seed=0,
    )

    col_name = f"MC_{short_name}"
    if col_name in bpy.data.collections:
        i = 1
        while f"{col_name}.{i:03d}" in bpy.data.collections:
            i += 1
        col_name = f"{col_name}.{i:03d}"

    group_col = bpy.data.collections.new(col_name)
    context.collection.children.link(group_col)

    import bmesh
    mesh = bpy.data.meshes.new(f"Emitter_{short_name}_Mesh")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.02)
    bm.to_mesh(mesh)
    bm.free()

    emitter = bpy.data.objects.new(f"Emitter_{short_name}", mesh)
    emitter.location = spawn_offset
    context.collection.objects.link(emitter)
    emitter.display_type = 'WIRE'
    ensure_particle_controls(
        emitter, context.scene, new_import=True, overrides=particle_controls
    )
    group_col.objects.link(emitter)
    for col in tuple(emitter.users_collection):
        if col != group_col:
            col.objects.unlink(emitter)

    mod = emitter.modifiers.new(name="JavaParticles", type='PARTICLE_SYSTEM')
    psys = emitter.particle_systems[mod.name]
    settings = psys.settings
    settings.count = max(1, int(parser.polytone_data.get("limit", 20) or 20))
    settings.frame_start = int(emitter.mcbedrock_start_frame)
    settings.frame_end = int(emitter.mcbedrock_end_frame)
    emitter["mcbedrock_java_source_count"] = int(settings.count)
    lifetime_ticks = approximation.values.get(
        "lifetime", approximation.values.get("life", 20.0)
    )
    settings.lifetime = max(1, int(float(lifetime_ticks) * fps / 20.0))
    emitter["mcbedrock_java_source_lifetime"] = float(settings.lifetime)
    settings.normal_factor = float(approximation.values.get("speed", 1.0))
    settings.emit_from = 'FACE'

    dx = float(approximation.values.get("dx", 0.0))
    dy = float(approximation.values.get("dy", 0.0))
    dz = float(approximation.values.get("dz", 0.0))
    if any(abs(value) > 1.0e-9 for value in (dx, dy, dz)):
        settings.normal_factor = 0.0
        if hasattr(settings, "object_align_factor"):
            settings.object_align_factor = (dx, -dz, dy)
    if "friction" in approximation.values and hasattr(settings, "damping"):
        settings.damping = max(
            0.0, min(1.0, 1.0 - float(approximation.values["friction"]))
        )
    if not approximation.has_physics:
        try:
            settings.effector_weights.gravity = 0.0
        except (AttributeError, TypeError):
            pass
    emitter["mcbedrock_java_source_align"] = tuple(
        getattr(settings, "object_align_factor", (0.0, 0.0, 0.0))
    )
    emitter["mcbedrock_java_source_gravity"] = float(
        settings.effector_weights.gravity
    )

    rotation_mode = approximation.rotation_mode
    facing_modes = {
        "look_at_xyz": "rotate_xyz",
        "look_at_player": "rotate_xyz",
        "look_at_y": "rotate_y",
        "look_at_player_y": "rotate_y",
        "movement_aligned": "lookat_direction",
        "none": "emitter_transform_xy",
    }
    facing_mode = facing_modes.get(str(rotation_mode), "rotate_xyz")
    instance_obj = create_billboard_instance(
        context, ident, 0.1, 0.1, facing_mode=facing_mode
    )
    group_col.objects.link(instance_obj)
    for col in tuple(instance_obj.users_collection):
        if col != group_col:
            col.objects.unlink(instance_obj)

    if texture_override and os.path.isfile(texture_override):
        abs_tex = texture_override
    else:
        first_tex = parser.get_first_texture_path()
        abs_tex = resolve_texture_java(context, first_tex, filepath) if first_tex else ""

    warnings = list(approximation.warnings)
    if not os.path.isfile(abs_tex):
        raw_paths = parser.get_texture_paths()
        warnings.insert(
            0,
            f"Texture not found for Java particle '{short_name}'. Looked for: "
            f"{raw_paths[0] if raw_paths else '(none)'}. Set your Resource Pack "
            "path or use manual texture.",
        )
    if parser.get_frame_count() > 1:
        warnings.append(
            "Java multi-sprite animation currently previews its first sprite"
        )
    if parser.polytone_data.get("copy_from"):
        warnings.append(
            "Polytone copy_from keeps the selected sprite; vanilla class dynamics "
            "are approximated by the Blender emitter"
        )

    material_type = (
        "particles_add"
        if approximation.render_type == "additive_translucent"
        else "particles_alpha"
    )
    create_particle_material(
        instance_obj,
        abs_tex,
        material_type,
        interpolation=interpolation,
    )
    instance_obj.color = (
        max(0.0, min(1.0, float(approximation.values.get("red", 1.0)))),
        max(0.0, min(1.0, float(approximation.values.get("green", 1.0)))),
        max(0.0, min(1.0, float(approximation.values.get("blue", 1.0)))),
        max(0.0, min(1.0, float(approximation.values.get("alpha", 1.0)))),
    )

    settings.render_type = 'OBJECT'
    settings.instance_object = instance_obj
    settings.particle_size = max(
        0.001, float(approximation.values.get("size", 1.0))
    )
    emitter["mcbedrock_java_source_size"] = float(settings.particle_size)
    emitter["mcbedrock_java_source_speed"] = float(settings.normal_factor)

    emitter["mcbedrock_is_emitter"] = True
    emitter["mcbedrock_backend"] = "JAVA_NATIVE"
    emitter["mcbedrock_source_path"] = os.path.abspath(filepath)
    emitter["mcbedrock_filepath"] = os.path.abspath(filepath)
    emitter["mcbedrock_particle_schema"] = PARTICLE_SCHEMA_VERSION
    emitter["mcbedrock_asset_type"] = "java_particle"
    emitter["mcbedrock_java_identifier"] = ident
    emitter["mcbedrock_java_namespace"] = parser.namespace
    emitter["mcbedrock_java_sprite_count"] = parser.get_frame_count()
    emitter["mcbedrock_polytone"] = parser.is_polytone()
    emitter["mcbedrock_polytone_definition"] = parser.polytone_path
    emitter["mcbedrock_polytone_modifier"] = parser.modifier_path
    emitter["mcbedrock_polytone_render_type"] = approximation.render_type
    emitter["mcbedrock_polytone_initializer"] = json.dumps(
        approximation.values, sort_keys=True
    )
    emitter["mcbedrock_java_compatibility_warnings"] = json.dumps(warnings)

    instance_obj.location = (0, 0, -100)
    instance_obj.hide_render = False

    for obj in context.selected_objects:
        obj.select_set(False)
    emitter.select_set(True)
    context.view_layer.objects.active = emitter
    apply_java_particle_controls(emitter, context.scene)

    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.overlay.show_relationship_lines = False

    return {
        "warning": "; ".join(dict.fromkeys(warnings)) if warnings else None,
        "emitter": emitter,
        "format": "java_polytone" if parser.is_polytone() else "java",
        "identifier": ident,
    }

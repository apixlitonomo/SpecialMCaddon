"""Public Bedrock importer entrypoints routed from the legacy operators."""

import json
import math
import os
import re
from copy import deepcopy
from fnmatch import fnmatchcase

import bpy
from mathutils import Matrix, Vector


# Blocks between the preview camera and the entity.
PREVIEW_CAMERA_DISTANCE = 16.0

DEFAULT_ENTITY_PREVIEW_CONTEXT = {
    "variant": 0,
    "mark_variant": 0,
    "skin_id": 0,
    "armor_texture_slot": 0,
    "is_baby": False,
    "is_saddled": False,
    "has_rider": False,
    "is_chested": False,
    "is_invisible": False,
    "is_sheared": False,
    "is_tamed": False,
    "is_sitting": False,
    "wing_flap_position": 0.0,
    "frame_alpha": 0.0,
    "life_time": 0.0,
    "is_sleeping": False,
    "is_sneaking": False,
    "is_sprinting": False,
    "is_attacking": False,
    "is_carrying": False,
    "is_gliding": False,
    "is_swimming": False,
    "health": 100.0,
    "max_health": 100.0,
    "death_ticks": 0.0,
    "is_on_ground": True,
    "is_moving": False,
    "is_riding": False,
    "vertical_speed": 0.0,
    "modified_move_speed": 0.0,
    "modified_distance_moved": 0.0,
    # The camera a few blocks away, as a player sees a mob. Read as 0, it sat
    # inside the model, which packs answer by fading it out or swapping it
    # for a close-up version (Superman's ghast turned into a pink shell).
    "distance_from_camera": PREVIEW_CAMERA_DISTANCE,
}

POSE_REST_BIND = "REST_BIND"
POSE_DEFAULT_IDLE = "DEFAULT_IDLE"
POSE_PRESETS = {POSE_REST_BIND, POSE_DEFAULT_IDLE}

_LEGACY_REST_ALIASES = {
    "setup", "general", "base_pose", "arms_legs", "default_pose",
    "default_leg_pose", "neutral", "leg_default", "tail_default",
}

_SOURCE_ASSEMBLY_ALIASES = {
    "setup", "general", "base_pose", "arms_legs", "default_pose",
    "default_leg_pose", "neutral",
}
from ...importers.bedrock_assets import (
    BedrockClientEntityParser,
    controller_animation_aliases,
    builtin_boat_texture_variant,
    builtin_geometry_root,
    builtin_geometry_variant,
    find_animation_controller_file,
    find_animation_file,
    find_geometry_file,
    geometry_keys_match,
    geometry_match_key,
    load_animation_controller,
    load_json_file,
    load_json_shared,
    resolve_texture_path,
    shared_json_reads,
    texture_dedupe_key,
)
from ...importers.parse_bedrock_geometry import BedrockModelParser
from ..selection import deselect_all_objects
from ...importers.bedrock_compatibility import (
    VisualCompatibilityError,
    inspect_geometry_visuals,
    raise_for_unsupported_visuals,
    relaxed_import_warnings,
    renderer_payload_issue,
)
from ...importers.bedrock_materials import load_material_catalog
from ..bedrock_uv import blender_winding, build_cube_faces
from .animation import (
    apply_bedrock_animations,
    apply_evaluated_animation_tracks,
    apply_static_bedrock_pose,
)
from ..model_origin import place_transform_origin_below_model
from .bedrock_animation import (
    animation_has_meaningful_structural_transform,
    neutral_animation_phase,
)
from .geometry import (
    add_bedrock_model_layer,
    finalize_bedrock_rig,
    build_bedrock_model,
    cube_signature,
    cube_signature_key,
    ensure_unique_collection,
    bedrock_to_blender,
    bedrock_rotation_to_euler,
    recenter_bedrock_composite,
)
from .materials import make_model_material
from .render_controller import (
    find_render_controller,
    infer_preview_context,
    initialized_variables,
    iter_render_controller_refs,
    resolve_controller_entries,
    resolve_import_profile,
    script_animation_entries,
    part_is_hidden,
)

_CENTERED_PRESENTATION_ENTITIES = {
    "minecraft:arrow",
    "minecraft:breeze_wind_charge_projectile",
    "minecraft:dragon_fireball",
    "minecraft:egg",
    "minecraft:ender_pearl",
    "minecraft:eye_of_ender_signal",
    "minecraft:fireball",
    "minecraft:fireworks_rocket",
    "minecraft:fishing_hook",
    "minecraft:lingering_potion",
    "minecraft:llama_spit",
    "minecraft:shulker_bullet",
    "minecraft:small_fireball",
    "minecraft:snowball",
    "minecraft:splash_potion",
    "minecraft:thrown_trident",
    "minecraft:wind_charge_projectile",
    "minecraft:wither_skull",
    "minecraft:wither_skull_dangerous",
    "minecraft:xp_bottle",
}


def _presentation_origin_policy(entity_identifier):
    entity = str(entity_identifier or "").lower()
    return "CENTERED" if entity in _CENTERED_PRESENTATION_ENTITIES else "GROUNDED"


def _is_zero_rotation(value):
    if value is None:
        return True
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return False
    try:
        return all(abs(float(component)) <= 1.0e-6 for component in value[:3])
    except (TypeError, ValueError):
        return False


def _cube_bounds(cubes):
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    found = False
    for cube in cubes if isinstance(cubes, list) else ():
        if not isinstance(cube, dict):
            continue
        origin = cube.get("origin")
        size = cube.get("size")
        if (
            not isinstance(origin, (list, tuple))
            or not isinstance(size, (list, tuple))
            or len(origin) < 3
            or len(size) < 3
        ):
            continue
        try:
            start = [float(value) for value in origin[:3]]
            end = [start[axis] + float(size[axis]) for axis in range(3)]
        except (TypeError, ValueError):
            continue
        for axis in range(3):
            minimum[axis] = min(minimum[axis], start[axis], end[axis])
            maximum[axis] = max(maximum[axis], start[axis], end[axis])
        found = True
    if not found:
        return None
    center = tuple((minimum[axis] + maximum[axis]) * 0.5 for axis in range(3))
    span = tuple(maximum[axis] - minimum[axis] for axis in range(3))
    return {
        "minimum": tuple(minimum),
        "maximum": tuple(maximum),
        "center": center,
        "span": span,
    }


def infer_structural_quadruped_torso(geometry):
    """Infer an unrotated legacy quadruped assembly from geometry alone."""
    if not isinstance(geometry, dict):
        return None
    bones = [
        bone for bone in geometry.get("bones", [])
        if isinstance(bone, dict) and bone.get("name")
    ]
    if not bones:
        return None
    by_name = {str(bone["name"]): bone for bone in bones}
    children = {name: [] for name in by_name}
    for bone in bones:
        parent = bone.get("parent")
        if parent in children:
            children[parent].append(bone)

    def descendants(name):
        result = []
        pending = list(children.get(name, ()))
        seen = set()
        while pending:
            child = pending.pop(0)
            child_name = str(child.get("name", ""))
            if not child_name or child_name in seen:
                continue
            seen.add(child_name)
            result.append(child)
            pending.extend(children.get(child_name, ()))
        return result

    all_bounds = _cube_bounds([
        cube
        for bone in bones
        for cube in bone.get("cubes", [])
        if isinstance(cube, dict)
    ])
    if all_bounds is None:
        return None
    model_floor = all_bounds["minimum"][1]
    candidates = []

    for bone in bones:
        name = str(bone["name"])
        cubes = bone.get("cubes", [])
        bounds = _cube_bounds(cubes)
        if bounds is None:
            continue
        parent = by_name.get(bone.get("parent"))
        if parent is not None and _cube_bounds(parent.get("cubes", [])) is not None:
            continue
        if not _is_zero_rotation(bone.get("rotation")):
            continue
        authored_bind = bone.get("bind_pose_rotation")
        authored_torso_rotation = not _is_zero_rotation(authored_bind)
        if authored_torso_rotation:
            values = list(authored_bind or ()) + [0.0, 0.0, 0.0]
            if (
                abs(abs(float(values[0])) - 90.0) > 1.0e-4
                or abs(float(values[1])) > 1.0e-4
                or abs(float(values[2])) > 1.0e-4
            ):
                continue
        if any(
            not _is_zero_rotation(cube.get("rotation"))
            for cube in cubes if isinstance(cube, dict)
        ):
            continue

        span_x, span_y, span_z = bounds["span"]
        if (
            span_y < 6.0
            or span_y < span_z * 1.45
            or span_y < span_x * 1.45
            or span_z < 2.0
            or span_x < 3.5
        ):
            continue

        structural_descendants = descendants(name)
        if not structural_descendants:
            # A nested leaf cannot use unrelated global limbs as evidence that
            # it is a torso. Custom rigs commonly put small face/accessory
            # cubes under an empty control; borrowing every other bone here
            # falsely rotates that leaf as a quadruped body.
            if bone.get("parent"):
                continue
            structural_descendants = [other for other in bones if other is not bone]
        limbs = []
        for child in structural_descendants:
            child_bounds = _cube_bounds(child.get("cubes", []))
            if child_bounds is None:
                continue
            child_span = child_bounds["span"]
            if child_span[1] < max(child_span[0], child_span[2]) * 1.2:
                continue
            if child_bounds["minimum"][1] > model_floor + 2.5:
                continue
            limbs.append({
                "name": str(child["name"]),
                "center": child_bounds["center"],
                "bounds": child_bounds,
            })

        pairs = []
        used = set()
        center_x = bounds["center"][0]
        x_tolerance = max(1.25, span_x * 0.35)
        z_tolerance = max(1.25, span_z * 0.25)
        for left_index, left in enumerate(limbs):
            if left_index in used:
                continue
            left_offset = left["center"][0] - center_x
            if abs(left_offset) < 0.5:
                continue
            best = None
            for right_index in range(left_index + 1, len(limbs)):
                if right_index in used:
                    continue
                right = limbs[right_index]
                right_offset = right["center"][0] - center_x
                if left_offset * right_offset >= 0.0:
                    continue
                mirror_error = abs(left_offset + right_offset)
                z_error = abs(left["center"][2] - right["center"][2])
                if mirror_error <= x_tolerance and z_error <= z_tolerance:
                    score = mirror_error + z_error
                    if best is None or score < best[0]:
                        best = (score, right_index, right)
            if best is not None:
                used.update((left_index, best[1]))
                pairs.append({
                    "bones": (left["name"], best[2]["name"]),
                    "station": (left["center"][2] + best[2]["center"][2]) * 0.5,
                })

        if len(pairs) < 2:
            continue
        stations = sorted(pair["station"] for pair in pairs)
        station_separation = stations[-1] - stations[0]
        if station_separation < max(3.0, span_z * 0.35):
            continue

        limb_names = {item for pair in pairs for item in pair["bones"]}
        longitudinal_margin = max(0.75, span_z * 0.08)
        cranial_candidates = []
        # Legacy quadruped heads are often independent root bones while the
        # legs alone are parented to the rotated torso. Search the complete
        # geometry after limb exclusion so those authored heads participate.
        for child in bones:
            child_name = str(child.get("name", ""))
            if child_name in limb_names:
                continue
            child_bounds = _cube_bounds(child.get("cubes", []))
            if child_bounds is None:
                continue
            if child_bounds["minimum"][1] <= model_floor + 2.5:
                continue
            center_z = child_bounds["center"][2]
            beyond_low = center_z < bounds["minimum"][2] - longitudinal_margin
            beyond_high = center_z > bounds["maximum"][2] + longitudinal_margin
            if beyond_low or beyond_high:
                cranial_candidates.append((child, child_bounds))
        if not cranial_candidates:
            continue
        cranial, cranial_bounds = max(
            cranial_candidates,
            key=lambda item: max(item[1]["span"]),
        )
        cranial_side = -1.0 if cranial_bounds["center"][2] < bounds["center"][2] else 1.0

        # Legacy renderer quadrupeds frequently author rear appendage meshes
        # vertically while spacing their pivots along the torso's longitudinal
        # axis. Align each such segment around its own authored pivot. This
        # preserves all pivots/children and makes the chain physically meet.
        companions = []
        rear_side = -cranial_side
        torso_rotation = (
            tuple(float(value) for value in authored_bind[:3])
            if authored_torso_rotation
            else (-90.0 * cranial_side, 0.0, 0.0)
        )
        for appendage in bones:
            appendage_name = str(appendage.get("name", ""))
            if (
                appendage is bone
                or appendage is cranial
                or appendage_name in limb_names
                or not _is_zero_rotation(appendage.get("rotation"))
                or not _is_zero_rotation(appendage.get("bind_pose_rotation"))
            ):
                continue
            appendage_bounds = _cube_bounds(appendage.get("cubes", []))
            if appendage_bounds is None:
                continue
            appendage_span = appendage_bounds["span"]
            pivot = appendage.get("pivot", appendage_bounds["center"])
            if not isinstance(pivot, (list, tuple)) or len(pivot) < 3:
                continue
            rear_offset = (float(pivot[2]) - bounds["center"][2]) * rear_side
            if rear_offset < span_z * 0.35:
                continue
            if abs(float(pivot[0]) - bounds["center"][0]) > max(2.5, span_x * 0.6):
                continue
            if appendage_span[1] < max(3.0, appendage_span[2] * 1.2):
                continue
            if appendage_span[0] > max(span_x * 1.5, appendage_span[1] * 1.5):
                continue
            pivot_at_y_end = min(
                abs(float(pivot[1]) - appendage_bounds["minimum"][1]),
                abs(float(pivot[1]) - appendage_bounds["maximum"][1]),
            ) <= max(1.0, appendage_span[1] * 0.15)
            if not pivot_at_y_end:
                continue
            companions.append({
                "bone": appendage_name,
                "rotation": torso_rotation,
                "role": "rear_appendage_chain",
            })
        candidates.append({
            "family": "structural_legacy_quadruped",
            "bone": name,
            "rotation": None if authored_torso_rotation else torso_rotation,
            "companions": tuple(companions),
            "cranial_bone": str(cranial.get("name", "")),
            "limb_pairs": tuple(pair["bones"] for pair in pairs),
            "station_separation": station_separation,
            "source_floor": model_floor,
        })

    return candidates[0] if len(candidates) == 1 else None


def _geometry_with_neutral_pose(geometry):
    inferred = infer_structural_quadruped_torso(geometry)
    if inferred is None:
        return geometry, None
    prepared = deepcopy(geometry)
    rotations = {}
    if inferred.get("rotation") is not None:
        rotations[inferred["bone"]] = inferred["rotation"]
    rotations.update(
        (companion["bone"], companion["rotation"])
        for companion in inferred.get("companions", ())
    )
    for bone in prepared.get("bones", []):
        bone_name = str(bone.get("name", "")) if isinstance(bone, dict) else ""
        if bone_name in rotations:
            bone["bind_pose_rotation"] = list(rotations[bone_name])
            bone["_mcbedrock_structural_bind_rotation"] = True
    description = prepared.setdefault("description", {})
    description["mcbedrock_renderer_family"] = inferred["family"]
    return prepared, inferred


def _record_structural_metadata(inferred, *objects):
    if inferred is None:
        return
    for obj in objects:
        if obj is None:
            continue
        obj["mcbedrock_structural_family"] = inferred["family"]
        obj["mcbedrock_structural_torso_bone"] = inferred["bone"]
        obj["mcbedrock_structural_cranial_bone"] = inferred.get(
            "cranial_bone", ""
        )


def _record_neutral_pose_metadata(inferred, *objects):
    if inferred is None:
        return
    companion_records = list(inferred.get("companions", ()))
    for obj in objects:
        if obj is None:
            continue
        obj["mcbedrock_native_assembly"] = inferred["family"]
        obj["mcbedrock_inferred_family"] = inferred["family"]
        obj["mcbedrock_inferred_bone"] = inferred["bone"]
        obj["mcbedrock_inferred_rotation"] = inferred["rotation"]
        obj["mcbedrock_inferred_source_floor"] = inferred["source_floor"]
        obj["mcbedrock_inferred_limb_pairs"] = json.dumps(inferred["limb_pairs"])
        obj["mcbedrock_inferred_cranial_bone"] = inferred.get("cranial_bone", "")
        obj["mcbedrock_inferred_companion_rotations"] = json.dumps(companion_records)


def _declared_animation_bone_targets(parser, search_roots):
    """Load source-declared animation targets without evaluating runtime motion."""
    refs = _resolve_entity_animation_refs(parser, search_roots)
    result = {}
    for reference in refs:
        animation_path = next((
            path for root in search_roots
            for path in [find_animation_file(root, reference)] if path
        ), "")
        if not animation_path:
            continue
        data = load_json_shared(animation_path)
        animations = data.get("animations", {}) if isinstance(data, dict) else {}
        animation = animations.get(reference) if isinstance(animations, dict) else None
        bones = animation.get("bones", {}) if isinstance(animation, dict) else {}
        if isinstance(bones, dict):
            result[reference] = set(map(str, bones))
    return result


def _resolve_exclusive_limb_geometry(
    geometry, query_context=None, animation_filter="__NONE__"
):
    """Choose one authored composite-or-independent limb representation.

    Some rigs contain a composite folded-limb bone alongside independent
    left/right controls used for attacks. The native renderer toggles those
    alternatives. Infer the same choice from structure and preview state while
    retaining every control in the armature.
    """
    if not isinstance(geometry, dict):
        return geometry
    bones = [
        bone for bone in geometry.get("bones", ())
        if isinstance(bone, dict) and bone.get("name")
    ]
    by_lower = {str(bone["name"]).lower(): bone for bone in bones}
    composite = by_lower.get("arms")
    left = by_lower.get("leftarm")
    right = by_lower.get("rightarm")
    if not all(
        bone is not None and bone.get("cubes")
        for bone in (composite, left, right)
    ):
        return geometry

    query = query_context if isinstance(query_context, dict) else {}
    selected = str(animation_filter or "").lower()
    independent_active = (
        animation_filter not in ("", "__NONE__", None)
        and any(token in selected for token in ("attack", "celebrat", "raise"))
    ) or any(bool(query.get(key, False)) for key in (
        "is_angry", "is_attacking", "has_target", "is_celebrating",
    ))
    hidden = {"arms"} if independent_active else {"leftarm", "rightarm"}

    prepared = deepcopy(geometry)
    for bone in prepared.get("bones", ()):
        if (
            isinstance(bone, dict)
            and str(bone.get("name", "")).lower() in hidden
        ):
            bone["neverRender"] = True
            bone["_mcbedrock_exclusive_limb_alternative"] = True
    description = prepared.setdefault("description", {})
    description["_mcbedrock_limb_representation"] = (
        "independent" if independent_active else "composite"
    )
    return prepared


def _reflect_duplicate_bilateral_geometry(geometry):
    """Reflect the second of two identical one-sided sibling parts.

    Some renderer-owned geometries declare both controls but reuse one side's
    cubes verbatim. Detect that structural shorthand from matching numeric
    siblings and reflect around their shared pivot plane before rig creation.
    """
    if not isinstance(geometry, dict):
        return geometry
    bones = [
        bone for bone in geometry.get("bones", ())
        if isinstance(bone, dict) and bone.get("name") and bone.get("cubes")
    ]
    groups = {}
    for bone in bones:
        match = re.fullmatch(r"(.+?)([12])", str(bone.get("name", "")))
        if not match:
            continue
        key = (
            str(bone.get("parent", "")).lower(),
            match.group(1).lower(),
            json.dumps(bone.get("pivot", [0, 0, 0]), sort_keys=True),
            json.dumps(bone.get("cubes", ()), sort_keys=True),
        )
        groups.setdefault(key, []).append((int(match.group(2)), bone))

    targets = []
    for siblings in groups.values():
        if len(siblings) != 2:
            continue
        siblings.sort(key=lambda item: item[0])
        first, second = siblings
        if (first[0], second[0]) != (1, 2):
            continue
        pivot = list(first[1].get("pivot", [0, 0, 0])) + [0, 0, 0]
        pivot_x = float(pivot[0])
        bounds = _cube_bounds(first[1].get("cubes", ()))
        if bounds is None:
            continue
        entirely_positive = bounds["minimum"][0] >= pivot_x - 1.0e-6
        entirely_negative = bounds["maximum"][0] <= pivot_x + 1.0e-6
        if not (entirely_positive or entirely_negative):
            continue
        targets.append(str(second[1]["name"]))

    if not targets:
        return geometry
    prepared = deepcopy(geometry)
    by_name = {
        str(bone.get("name", "")): bone
        for bone in prepared.get("bones", ())
        if isinstance(bone, dict)
    }
    for name in targets:
        bone = by_name[name]
        pivot = list(bone.get("pivot", [0, 0, 0])) + [0, 0, 0]
        pivot_x = float(pivot[0])
        for cube in bone.get("cubes", ()):
            origin = list(cube.get("origin", [0, 0, 0])) + [0, 0, 0]
            size = list(cube.get("size", [0, 0, 0])) + [0, 0, 0]
            origin[0] = 2.0 * pivot_x - (
                float(origin[0]) + float(size[0])
            )
            cube["origin"] = origin[:3]
            if isinstance(cube.get("pivot"), list) and cube["pivot"]:
                cube["pivot"][0] = (
                    2.0 * pivot_x - float(cube["pivot"][0])
                )
            cube["mirror"] = not bool(cube.get("mirror", False))
        bone["_mcbedrock_native_reflected"] = "duplicate_bilateral_sibling"
    return prepared


def _renderer_local_segment_layout(geometry, animation_targets):
    """Detect geometry whose native renderer owns repeated and mirrored parts."""
    if not isinstance(geometry, dict):
        return None
    bones = [bone for bone in geometry.get("bones", ()) if isinstance(bone, dict)]
    by_name = {str(bone.get("name", "")): bone for bone in bones if bone.get("name")}
    declared_targets = set().union(*animation_targets.values()) if animation_targets else set()
    missing_indexed = sorted(
        name for name in declared_targets
        if name not in by_name and re.fullmatch(r"[A-Za-z_]+[0-9]+", name)
    )
    base_templates = {
        match.group(1): by_name[match.group(1)]
        for name in missing_indexed
        for match in [re.fullmatch(r"([A-Za-z_]+)[0-9]+", name)]
        if match and match.group(1) in by_name and by_name[match.group(1)].get("cubes")
    }
    mirrored_pairs = []
    identical_pair_count = 0
    for name, target in by_name.items():
        match = re.fullmatch(r"(.+?)1", name)
        if not match:
            continue
        source = by_name.get(match.group(1))
        if not source or not source.get("cubes") or not target.get("cubes"):
            continue
        if len(source.get("cubes", ())) != len(target.get("cubes", ())):
            continue
        mirrored_pairs.append((name, match.group(1)))
        if source.get("cubes") == target.get("cubes"):
            identical_pair_count += 1
    if len(missing_indexed) < 3 or not base_templates or identical_pair_count < 3:
        return None
    return {
        "missing_indexed": missing_indexed,
        "base_templates": base_templates,
        "mirrored_pairs": mirrored_pairs,
        "assembly_refs": [
            reference for reference, targets in animation_targets.items()
            if (
                any(target not in by_name for target in targets)
                or sum(
                    1 for target in targets
                    if target in by_name and by_name[target].get("cubes")
                ) >= 2
            )
        ],
    }


def _expand_renderer_local_geometry(geometry, animation_targets):
    """Materialize source-declared renderer-local segments and bilateral parts."""
    layout = _renderer_local_segment_layout(geometry, animation_targets)
    if layout is None:
        return geometry
    expanded = deepcopy(geometry)
    bones = [bone for bone in expanded.get("bones", ()) if isinstance(bone, dict)]
    by_name = {str(bone.get("name", "")): bone for bone in bones if bone.get("name")}

    def reflect_bone_x(target_name, source_name):
        target = by_name[target_name]
        source = by_name[source_name]
        target["pivot"] = deepcopy(source.get("pivot", [0, 0, 0]))
        target["pivot"][0] = -float(target["pivot"][0])
        target["cubes"] = deepcopy(source.get("cubes", ()))
        for cube in target["cubes"]:
            origin = list(cube.get("origin", [0, 0, 0]))
            size = list(cube.get("size", [0, 0, 0]))
            origin[0] = -(float(origin[0]) + float(size[0]))
            cube["origin"] = origin
            if isinstance(cube.get("pivot"), list) and cube["pivot"]:
                cube["pivot"][0] = -float(cube["pivot"][0])
            cube["mirror"] = not bool(cube.get("mirror", False))
        target["_mcbedrock_native_reflected"] = source_name

    for target_name, source_name in layout["mirrored_pairs"]:
        reflect_bone_x(target_name, source_name)

    root_name = next(
        (bone["name"] for bone in bones if not bone.get("parent") and not bone.get("cubes")),
        bones[0].get("name", "root") if bones else "root",
    )
    template_values = list(layout["base_templates"].values())
    fallback_template = template_values[0] if len(template_values) == 1 else None
    used_templates = set()
    for name in layout["missing_indexed"]:
        match = re.fullmatch(r"([A-Za-z_]+)([0-9]+)", name)
        template = layout["base_templates"].get(match.group(1)) if match else None
        template = template or fallback_template
        if not template or name in by_name:
            continue
        clone = {
            "name": name,
            "parent": root_name,
            "pivot": deepcopy(template.get("pivot", [0, 24, 0])),
            "cubes": deepcopy(template.get("cubes", ())),
            "_mcbedrock_native_template": str(template.get("name", "")),
        }
        bones.append(clone)
        by_name[name] = clone
        used_templates.add(str(template.get("name", "")))
    for template_name in used_templates:
        template = by_name.get(template_name)
        if template:
            template["cubes"] = []
            template["neverRender"] = True

    def paired_name(base_name, suffix):
        return base_name + suffix if base_name + suffix in by_name else base_name

    parent_map = {}
    for name in list(by_name):
        suffix = "1" if name.endswith("1") and name[:-1] in by_name else ""
        base = name[:-1] if suffix else name
        parent_base = ""
        if base.endswith("tip") and base[:-3] in by_name:
            parent_base = base[:-3]
        elif base.endswith("foot"):
            candidate = base[:-4] + "legtip"
            if candidate in by_name:
                parent_base = candidate
        if parent_base:
            parent_map[name] = paired_name(parent_base, suffix)

    def materialize_local_child(child_name, parent_name):
        child = by_name[child_name]
        parent = by_name[parent_name]
        parent_pivot = list(parent.get("pivot", [0, 0, 0])) + [0, 0, 0]
        child_pivot = list(child.get("pivot", [0, 0, 0])) + [0, 0, 0]
        child["pivot"] = [float(child_pivot[i]) + float(parent_pivot[i]) for i in range(3)]
        for cube in child.get("cubes", ()):
            origin = list(cube.get("origin", [0, 0, 0])) + [0, 0, 0]
            cube["origin"] = [float(origin[i]) + float(parent_pivot[i]) for i in range(3)]
        child["_mcbedrock_native_parent_offset"] = parent_name

    ordered_children = sorted(
        (item for item in parent_map.items() if "legtip" in item[0] or "foot" in item[0]),
        key=lambda item: ("foot" in item[0], item[0]),
    )
    for child_name, parent_name in ordered_children:
        materialize_local_child(child_name, parent_name)

    for name, bone in by_name.items():
        if name in parent_map:
            bone["parent"] = parent_map[name]
        elif name != root_name and not bone.get("parent"):
            bone["parent"] = root_name

    expanded["bones"] = bones
    description = expanded.setdefault("description", {})
    description["_mcbedrock_renderer_family"] = "renderer_local_segmented"
    description["_mcbedrock_renderer_assembly_refs"] = list(layout["assembly_refs"])
    return expanded

def _search_roots_for_asset(context, filepath):
    from ...importers.bedrock_assets import asset_resource_roots
    roots = asset_resource_roots(context, filepath)
    current = os.path.dirname(os.path.abspath(filepath))
    for _ in range(8):
        if current not in roots:
            roots.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    builtin_root = builtin_geometry_root()
    if os.path.isdir(builtin_root) and builtin_root not in roots:
        # Built-ins are deliberately last: exact pack geometry overrides them.
        roots.append(builtin_root)
    return roots


def _behavior_preview_context(search_roots, entity_identifier):
    roots = []
    for root in search_roots:
        for candidate in (
            os.path.join(root, "behavior_packs"),
            os.path.join(root, "entities"),
        ):
            if os.path.isdir(candidate) and candidate not in roots:
                roots.append(candidate)
    for root in roots:
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in {"__pycache__", ".git"}]
            if "entities" not in current.replace(os.sep, "/").lower() and current != root:
                continue
            for filename in files:
                if not filename.lower().endswith(".json"):
                    continue
                try:
                    data = load_json_file(os.path.join(current, filename))
                except Exception:
                    continue
                entity = data.get("minecraft:entity", {}) if isinstance(data, dict) else {}
                description = entity.get("description", {}) if isinstance(entity, dict) else {}
                if description.get("identifier") != entity_identifier:
                    continue
                result = {"properties": {}}
                properties = description.get("properties", {})
                for name, definition in properties.items() if isinstance(properties, dict) else []:
                    if isinstance(definition, dict) and "default" in definition:
                        result["properties"][str(name)] = definition["default"]
                components = entity.get("components", {}) if isinstance(entity, dict) else {}
                component_queries = {
                    "minecraft:variant": "variant",
                    "minecraft:mark_variant": "mark_variant",
                    "minecraft:skin_id": "skin_id",
                }
                for component_name, query_name in component_queries.items():
                    value = components.get(component_name) if isinstance(components, dict) else None
                    if isinstance(value, dict):
                        value = value.get("value", value.get("default", 0))
                    if isinstance(value, (int, float)):
                        result[query_name] = value
                if isinstance(components, dict) and "minecraft:is_baby" in components:
                    result["is_baby"] = True
                return result
    return {}


_METADATA_DIRS = {"entity", "blocks", "resource_packs", "behavior_packs"}
_SKIPPED_DIRS = {"__MACOSX", ".git", "node_modules", "__pycache__"}


def _list_metadata_dir(current):
    """One directory's subfolders and JSON files (with size and mtime), or None."""
    subdirs = []
    json_files = []
    try:
        with os.scandir(current) as listing:
            for entry in listing:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    try:
                        linked = entry.is_symlink()
                    except OSError:
                        linked = False
                    subdirs.append((entry.name, entry.path, linked))
                elif entry.name.lower().endswith(".json"):
                    try:
                        stat = entry.stat()
                    except OSError:
                        continue
                    json_files.append((entry.path, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return None
    return subdirs, json_files


def _scan_metadata_json(root, listings, current=None, found=None):
    """Metadata JSON below ``root`` with sizes and mtimes, in ``os.walk`` order.

    Folders outside entity/blocks/pack trees are only entered for those
    names. ``listings`` shares directory reads between overlapping roots
    (``games`` contains ``com.mojang`` contains ``resource_packs``), and the
    listing returns size and mtime for free on Windows, so no file is opened.
    """
    current = root if current is None else current
    found = [] if found is None else found
    listing = listings.get(current, False)
    if listing is False:
        listing = listings[current] = _list_metadata_dir(current)
    if listing is None:
        return found
    subdirs, json_files = listing
    lowered = current.replace("\\", "/").lower()
    pruned = current != root and not any(
        part in lowered for part in ("/entity", "/blocks", "/resource_packs", "/behavior_packs")
    )
    found.extend(json_files)
    for name, path, linked in subdirs:
        if (pruned and name not in _METADATA_DIRS) or name in _SKIPPED_DIRS or linked:
            continue
        _scan_metadata_json(root, listings, path, found)
    return found


# root -> (file listing with sizes and mtimes, [(geometry match key, texture ref, source file)]).
# Rebuilt only when a metadata JSON below the root is added, removed or edited.
_GEOMETRY_TEXTURE_REFS = {}
# Directory listings trusted for a whole batch (thumbnail workers); None
# means every import checks the packs again.
_BATCH_LISTINGS = None


def trust_pack_listings_for_batch():
    """Read pack folders once for the rest of this process.

    Only for one-shot batch processes, such as the thumbnail worker, which
    render a snapshot of the packs. Interactive imports always re-check.
    """
    global _BATCH_LISTINGS
    if _BATCH_LISTINGS is None:
        _BATCH_LISTINGS = {}


def _geometry_texture_refs(root, listings):
    """Client entity, attachable and block geometry->texture references.

    Parsing every metadata JSON under a pack folder (all installed packs when
    a model sits in ``resource_packs``) cost seconds per import, so the refs
    are cached per root and checked against a stat-only listing.
    """
    files = tuple(_scan_metadata_json(root, listings))
    key = os.path.normcase(os.path.abspath(root))
    cached = _GEOMETRY_TEXTURE_REFS.get(key)
    if cached is not None and cached[0] == files:
        return cached[1]
    refs = []
    for filepath, _size, _mtime in files:
        try:
            data = load_json_file(filepath)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        entity = data.get("minecraft:client_entity", {})
        if not entity:
            entity = data.get("minecraft:attachable", {})
        desc = entity.get("description") if isinstance(entity, dict) else None
        geometries = desc.get("geometry") if isinstance(desc, dict) else None
        if isinstance(geometries, dict):
            textures = desc.get("textures", {})
            for geo_key, geo_ref in geometries.items():
                refs.append((
                    geometry_match_key(geo_ref),
                    _texture_ref_for_geometry_key(textures, geo_key),
                    filepath,
                ))
        block = data.get("minecraft:block", {})
        components = block.get("components") if isinstance(block, dict) else None
        if not isinstance(components, dict):
            continue
        block_geo = components.get("minecraft:geometry")
        block_geo_id = block_geo.get("identifier") if isinstance(block_geo, dict) else block_geo
        if isinstance(block_geo_id, str):
            refs.append((
                geometry_match_key(block_geo_id),
                _material_texture_ref(components.get("minecraft:material_instances", {})),
                filepath,
            ))
    _GEOMETRY_TEXTURE_REFS[key] = (files, refs)
    return refs


def _first_texture_ref(textures):
    if isinstance(textures, dict):
        for key in ("default", "*", "all"):
            if isinstance(textures.get(key), str):
                return textures[key]
        for value in textures.values():
            if isinstance(value, str):
                return value
    return ""



def _texture_ref_for_geometry_key(textures, geometry_key):
    """Resolve direct and variant-prefixed textures for a geometry key."""
    if not isinstance(textures, dict):
        return ""
    key = str(geometry_key or "")
    for candidate in (key, f"{key}_default"):
        value = textures.get(candidate)
        if isinstance(value, str) and value:
            return value
    prefix = f"{key}_"
    if key:
        for texture_key, value in textures.items():
            if str(texture_key).startswith(prefix) and isinstance(value, str) and value:
                return value
    return _first_texture_ref(textures)


def _material_texture_ref(material_instances):
    if not isinstance(material_instances, dict):
        return ""
    for key in ("*", "default", "north", "south", "east", "west", "up", "down"):
        value = material_instances.get(key)
        if isinstance(value, dict) and isinstance(value.get("texture"), str):
            return value["texture"]
    for value in material_instances.values():
        if isinstance(value, dict) and isinstance(value.get("texture"), str):
            return value["texture"]
    return ""


def _find_texture_for_geometry(context, geometry_identifier, model_filepath):
    target = geometry_match_key(geometry_identifier)
    if not target:
        return ""
    seen = set()
    listings = _BATCH_LISTINGS if _BATCH_LISTINGS is not None else {}
    for root in _search_roots_for_asset(context, model_filepath):
        for geometry_key, texture_ref, filepath in _geometry_texture_refs(root, listings):
            if (filepath, geometry_key, texture_ref) in seen:
                continue
            seen.add((filepath, geometry_key, texture_ref))
            if texture_ref and geometry_keys_match(geometry_key, target):
                resolved = resolve_texture_path(context, texture_ref, filepath)
                if os.path.isfile(resolved):
                    return resolved
    return ""


def _merge_canonical_bones(canonical_bones, geometry):
    """Merge skeleton metadata with first/base geometry taking precedence."""
    bones = geometry.get("bones", []) if isinstance(geometry, dict) else []
    lowered = {str(name).lower(): name for name in canonical_bones}
    for bone in bones if isinstance(bones, list) else []:
        if not isinstance(bone, dict):
            continue
        incoming_name = str(bone.get("name", "bone"))
        canonical_name = lowered.get(incoming_name.lower())
        if canonical_name is None:
            metadata = dict(bone)
            metadata.pop("cubes", None)
            canonical_bones[incoming_name] = metadata
            lowered[incoming_name.lower()] = incoming_name
            continue
        existing = canonical_bones[canonical_name]
        for key in ("parent", "pivot", "rotation", "binding", "mirror", "locators"):
            if key not in existing and key in bone:
                existing[key] = bone[key]
    return canonical_bones


def _geometry_cube_signatures(geometry):
    return set(_geometry_cube_records(geometry).values())


def _geometry_cube_records(geometry):
    """Map transform-safe controller-overlay keys to builder skip signatures."""
    records = {}
    bones = geometry.get("bones", []) if isinstance(geometry, dict) else []
    bones = bones if isinstance(bones, list) else []
    bones_by_name = {
        str(bone.get("name", "bone")): bone
        for bone in bones if isinstance(bone, dict)
    }
    for bone in bones:
        if not isinstance(bone, dict):
            continue
        bone_name = bone.get("name", "bone")
        cubes = bone.get("cubes", [])
        for cube in cubes if isinstance(cubes, list) else []:
            if isinstance(cube, dict):
                records[cube_signature_key(
                    bone_name, cube, bones_by_name
                )] = cube_signature(bone_name, cube)
    return records


def _geometry_has_non_cube_mesh(geometry):
    bones = geometry.get("bones", ()) if isinstance(geometry, dict) else ()
    return any(
        isinstance(bone, dict)
        and (bone.get("poly_mesh") is not None or bone.get("texture_meshes") is not None)
        for bone in bones if isinstance(bones, list)
    )


def _entry_list_from_geometry_items(geometry_items, textures):
    entries = []
    textures = textures if isinstance(textures, dict) else {}
    for geometry_key, geometry_identifier in geometry_items:
        texture_ref = _texture_ref_for_geometry_key(textures, geometry_key)
        entries.append({
            "controller": "",
            "geometry_key": geometry_key,
            "geometry_identifier": geometry_identifier,
            "texture_key": geometry_key,
            "texture_ref": texture_ref,
            "hidden_parts": set(),
        })
    return entries


def _resolve_texture_for_entry(context, parser, filepath, entry, texture_override):
    if texture_override:
        return texture_override
    texture_ref = entry.get("texture_ref") or ""
    if texture_ref:
        resolved = resolve_texture_path(context, texture_ref, filepath)
        if os.path.isfile(resolved):
            return resolved
    textures = parser.description.get("textures", {})
    texture_ref = ""
    if isinstance(textures, dict):
        geometry_key = entry.get("geometry_key")
        texture_ref = _texture_ref_for_geometry_key(textures, geometry_key)
    if texture_ref:
        resolved = resolve_texture_path(context, texture_ref, filepath)
        if os.path.isfile(resolved):
            return resolved
    return ""


def _resolve_texture_paths_for_entry(context, parser, filepath, entry, texture_override):
    """Resolve every ordered render-controller texture layer for one geometry."""
    if texture_override:
        return [texture_override] if os.path.isfile(texture_override) else []
    paths = []
    seen = set()
    refs = entry.get("texture_refs", [])
    if not isinstance(refs, list):
        refs = []
    for texture_ref in refs:
        resolved = resolve_texture_path(context, texture_ref, filepath)
        key = os.path.normcase(os.path.abspath(resolved)) if resolved else ""
        if resolved and os.path.isfile(resolved) and key not in seen:
            seen.add(key)
            paths.append(resolved)
    fallback = _resolve_texture_for_entry(context, parser, filepath, entry, "")
    fallback_key = os.path.normcase(os.path.abspath(fallback)) if fallback else ""
    if fallback and os.path.isfile(fallback) and fallback_key not in seen:
        paths.insert(0, fallback)
    return paths


def _add_chest_minecart_payload(armature, collection, search_roots, interpolation):
    """Add the vanilla renderer-provided chest missing from entity geometry."""
    # The native chest renderer uses the 64x64 entity atlas and three
    # independently textured cuboids: lower shell, lid, and protruding latch.
    bottom_units = 0.34 * 16.0
    box_specs = (
        ((-7.0, bottom_units, -7.0), (14.0, 10.0, 14.0), (0.0, 19.0)),
        ((-7.0, bottom_units + 10.0, -7.0), (14.0, 5.0, 14.0), (0.0, 0.0)),
        ((-1.0, bottom_units + 9.0, -8.0), (2.0, 4.0, 1.0), (0.0, 0.0)),
    )
    vertices = []
    faces = []
    loop_uvs = []
    for origin, size, uv in box_specs:
        ox, oy, oz = origin
        sx, sy, sz = size
        cube_result = build_cube_faces(
            {"uv": list(uv)},
            ox, oy, oz, sx, sy, sz,
            64, 64,
        )
        base = len(vertices)
        vertices.extend(tuple(bedrock_to_blender(point)) for point in cube_result.vertices)
        for emitted in cube_result.faces:
            local_indices = list(emitted.indices)
            wound_indices = blender_winding(local_indices)
            uv_by_local = dict(zip(local_indices, emitted.uv_corners))
            faces.append(tuple(base + index for index in wound_indices))
            loop_uvs.append(tuple(uv_by_local[index] for index in wound_indices))

    mesh = bpy.data.meshes.new("Chest Minecart Payload Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for polygon, face_uv in zip(mesh.polygons, loop_uvs):
        for loop_index, uv in zip(polygon.loop_indices, face_uv):
            uv_layer.data[loop_index].uv = uv
    obj = bpy.data.objects.new("Chest Minecart Payload", mesh)
    collection.objects.link(obj)

    texture_path = ""
    for root in search_roots:
        for extension in (".png", ".tga"):
            candidate = os.path.join(
                root, "textures", "entity", "chest", "normal" + extension
            )
            if os.path.isfile(candidate):
                texture_path = candidate
                break
        if texture_path:
            break
    if not texture_path:
        bpy.data.objects.remove(obj, do_unlink=True)
        raise ValueError("Missing native chest entity texture: textures/entity/chest/normal")
    obj.data.materials.append(make_model_material(
        "Chest Minecart Native Chest",
        texture_path,
        interpolation=interpolation,
        render_method="alpha_test",
    ))
    obj["mcbedrock_payload_texture"] = texture_path
    obj["mcbedrock_payload_model"] = "native_chest"

    target = next(
        (name for name in ("bottom", "body", "entity_root") if name in armature.data.bones),
        next(iter(armature.data.bones)).name,
    )
    group = obj.vertex_groups.new(name=target)
    group.add(range(len(vertices)), 1.0, "REPLACE")
    modifier = obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = armature
    obj.parent = armature
    obj["mcbedrock_payload"] = "minecraft:chest"
    obj["mcbedrock_anchor_bone"] = target
    obj["mcbedrock_coordinate_space"] = "presentation"
    return obj


def _add_native_minecart_payload(identifier, armature, collection, search_roots, interpolation):
    """Build renderer-native minecart block payloads and bind them to the cart rig."""
    payload = str(identifier or "").lower()
    if payload == "minecraft:chest_minecart":
        return _add_chest_minecart_payload(
            armature, collection, search_roots, interpolation
        )
    specs = {
        "minecraft:tnt_minecart": {
            "tag": "minecraft:tnt",
            "boxes": [((-0.5, -0.5, 0.34), (0.5, 0.5, 1.34), (0, 1, 2, 1, 1, 1))],
            "textures": ("tnt_side", "tnt_top", "tnt_bottom"),
        },
        "minecraft:command_block_minecart": {
            "tag": "minecraft:command_block",
            "boxes": [((-0.5, -0.5, 0.34), (0.5, 0.5, 1.34), (0, 1, 0, 0, 2, 0))],
            "textures": ("command_block_side", "command_block_front", "command_block_back"),
        },
        "minecraft:hopper_minecart": {
            "tag": "minecraft:hopper",
            # Four rim bars, tapered-looking stepped funnel, and centered spout.
            "boxes": [
                ((-0.56, -0.56, 0.82), (0.56, -0.38, 1.05), (0, 2, 0, 0, 0, 0)),
                ((-0.56, 0.38, 0.82), (0.56, 0.56, 1.05), (0, 2, 0, 0, 0, 0)),
                ((-0.56, -0.38, 0.82), (-0.38, 0.38, 1.05), (0, 2, 0, 0, 0, 0)),
                ((0.38, -0.38, 0.82), (0.56, 0.38, 1.05), (0, 2, 0, 0, 0, 0)),
                ((-0.40, -0.40, 0.52), (0.40, 0.40, 0.84), (0, 1, 0, 0, 0, 0)),
                ((-0.18, -0.18, 0.30), (0.18, 0.18, 0.54), (0, 1, 0, 0, 0, 0)),
            ],
            "textures": ("hopper_outside", "hopper_inside", "hopper_top"),
        },
    }
    spec = specs.get(payload)
    if spec is None:
        return None

    vertices, faces, material_indices = [], [], []
    face_template = (
        (0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
        (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
    )
    for minimum, maximum, slots in spec["boxes"]:
        x0, y0, z0 = minimum
        x1, y1, z1 = maximum
        base = len(vertices)
        vertices.extend((
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ))
        faces.extend(tuple(base + value for value in face) for face in face_template)
        material_indices.extend(slots)

    mesh = bpy.data.meshes.new(f"{spec['tag']} Minecart Payload Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap")
    # Bedrock command-block textures are four identical 16x16 animation
    # frames packed vertically in a 16x64 image. Sample one frame instead
    # of stretching the strip over the payload cube.
    frame_v0, frame_v1 = ((0.75, 1.0) if payload == "minecraft:command_block_minecart" else (0.0, 1.0))
    quad_uv = ((0.0, frame_v0), (1.0, frame_v0), (1.0, frame_v1), (0.0, frame_v1))
    for polygon in mesh.polygons:
        for loop_index, uv in zip(polygon.loop_indices, quad_uv):
            uv_layer.data[loop_index].uv = uv
    obj = bpy.data.objects.new(f"{spec['tag']} Minecart Payload", mesh)
    collection.objects.link(obj)

    def block_texture(name):
        for root in search_roots:
            for extension in (".png", ".tga"):
                candidate = os.path.join(root, "textures", "blocks", name + extension)
                if os.path.isfile(candidate):
                    return candidate
        return ""

    resolved = []
    for name in spec["textures"]:
        path = block_texture(name)
        if not path:
            bpy.data.objects.remove(obj, do_unlink=True)
            raise ValueError(f"Missing native minecart payload texture: {name}")
        resolved.append(path)
        obj.data.materials.append(make_model_material(
            f"{spec['tag']} {name}", path,
            interpolation=interpolation, render_method="opaque",
        ))
    for polygon, slot in zip(obj.data.polygons, material_indices):
        polygon.material_index = min(int(slot), len(obj.data.materials) - 1)

    target = next(
        (name for name in ("bottom", "body", "entity_root") if name in armature.data.bones),
        next(iter(armature.data.bones)).name,
    )
    group = obj.vertex_groups.new(name=target)
    group.add(range(len(vertices)), 1.0, "REPLACE")
    modifier = obj.modifiers.new(name="Armature", type="ARMATURE")
    modifier.object = armature
    obj.parent = armature
    obj["mcbedrock_payload"] = spec["tag"]
    obj["mcbedrock_anchor_bone"] = target
    obj["mcbedrock_coordinate_space"] = "presentation"
    obj["mcbedrock_payload_textures"] = " | ".join(resolved)
    return obj


def build_bedrock_model_from_file(
    context,
    filepath,
    texture_path="",
    geometry_identifier="",
    geometry_index=0,
    interpolation="Closest",
    display_name="",
    origin_offset=None,
    collection=None,
    hidden_bones=None,
    object_suffix="",
    render_method="alpha_test",
    texture_paths=None,
    solid_backface_culling=True,
    entity_preview=False,
    entity_identifier="",
):
    parser = BedrockModelParser(filepath=filepath)
    geometry = parser.get_geometry(identifier=geometry_identifier or None, index=geometry_index)
    report = raise_for_unsupported_visuals(geometry)
    description = geometry.get("description", {}) if isinstance(geometry, dict) else {}
    resolved_identifier = str(
        description.get("identifier", geometry_identifier)
        if isinstance(description, dict) else geometry_identifier
    ).lower()
    # The legacy mobs.json bed mesh is authored in renderer-local upright
    # coordinates. Minecraft's bed renderer lays that shared model onto the
    # ground; retain the source rig and apply the missing presentation frame
    # at the armature object level.
    presentation_rotation = (
        (math.radians(90.0), 0.0, 0.0)
        if resolved_identifier == "geometry.bed"
        else None
    )
    if not texture_path:
        desc = description
        geo_identifier = desc.get("identifier", geometry_identifier) if isinstance(desc, dict) else geometry_identifier
        texture_path = _find_texture_for_geometry(context, geo_identifier, filepath)
    result = build_bedrock_model(
        context,
        geometry,
        texture_path=texture_path,
        source_path=filepath,
        interpolation=interpolation,
        display_name=display_name,
        origin_offset=origin_offset,
        collection=collection,
        hidden_bones=hidden_bones,
        object_suffix=object_suffix,
        render_method=render_method,
        texture_paths=texture_paths,
        solid_backface_culling=solid_backface_culling,
        presentation_rotation=presentation_rotation,
    )
    # Data the game still renders imports with a warning instead of failing.
    result["warnings"] = relaxed_import_warnings(
        list(report.warnings)
        + json.loads(result["mesh"].get("mcbedrock_uv_issues", "[]"))
    )
    return result


def direct_clip_key(source_path, animation_id):
    """Stable identity for one authored clip, independent of aliases."""
    return (
        os.path.normcase(os.path.abspath(str(source_path or ""))),
        str(animation_id or "").casefold(),
    )


def _resolve_entity_animation_refs(parser, search_roots):
    """Return direct animation ids, expanding animation-controller references.

    Raw controllers stay hidden from the result; their states are resolved to
    the real ``animation.*`` ids via the entity's animation alias table.
    """
    alias_table = parser.description.get("animations", {})
    alias_table = alias_table if isinstance(alias_table, dict) else {}

    refs = []
    for ref in parser.get_animation_references():
        if not isinstance(ref, str):
            continue
        if ref.startswith("animation.") and ref not in refs:
            refs.append(ref)
            continue
        if not ref.startswith("controller.animation."):
            continue
        controller_path = ""
        for root in search_roots:
            controller_path = find_animation_controller_file(root, ref)
            if controller_path:
                break
        if not controller_path:
            continue
        controller = load_animation_controller(controller_path, ref)
        for alias in controller_animation_aliases(controller):
            resolved = alias_table.get(alias, alias)
            if isinstance(resolved, str) and resolved.startswith("animation.") and resolved not in refs:
                refs.append(resolved)
    return refs


def _is_unconditional(condition):
    return condition is True or condition == 1 or (
        isinstance(condition, str) and condition.strip().lower() in {"true", "1"}
    )


def _script_statements(description, key):
    scripts = description.get("scripts", {}) if isinstance(description, dict) else {}
    values = scripts.get(key, []) if isinstance(scripts, dict) else []
    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
    yield from walk(values)

def _material_slot_key(render_method, behavior):
    behavior = behavior if isinstance(behavior, dict) else {}
    return (
        str(render_method or "alpha_test"),
        str(behavior.get("name", "")),
        str(behavior.get("surface_mode", "")),
        float(behavior.get("cutout_threshold", 0.5)),
        str(behavior.get("legacy_emission_mode", "none")),
        str(behavior.get("culling", "none")),
        str(behavior.get("alpha_role", "coverage")),
        str(behavior.get("lighting", "lit")),
        str(behavior.get("emission", "none")),
        tuple(behavior.get("provenance", ())),
        bool(behavior.get("cull_backfaces", False)),
        bool(behavior.get("ignore_lighting", False)),
        float(behavior.get("light_color_multiplier", 1.0)),
        tuple(behavior.get("uv_offset", (0.0, 0.0))),
        tuple(behavior.get("uv_scale", (1.0, 1.0))),
    )


def _apply_entry_material(
    mesh_obj,
    texture_paths,
    interpolation,
    render_method,
    material_rules=None,
    material_behavior=None,
    material_layers=None,
    backface_culling=None,
    surface_override=None,
):
    """Assign per-bone ordered controller-pass stacks to one geometry mesh.

    ``backface_culling=False`` keeps every stack double-sided and
    ``surface_override="cutout"`` keeps each material a cutout under its
    blended passes (Java Edition entities); the defaults follow Bedrock.
    """
    if mesh_obj is None:
        return
    if material_layers is None:
        material_layers = [{
            "texture_paths": list(texture_paths or ()),
            "render_method": render_method,
            "material": dict(material_behavior or {}),
            "material_rules": list(material_rules or ()),
            "hidden_parts": set(),
            "controller": "",
        }]
    else:
        # Mcblend evaluates the last declared render controller first for each
        # bone, so preserve that stack order before resolving scoped rules.
        material_layers = [dict(layer) for layer in reversed(material_layers)]

    bones_by_name = {}
    parent = getattr(mesh_obj, "parent", None)
    armature_data = getattr(parent, "data", None)
    for bone in getattr(armature_data, "bones", ()):
        bones_by_name[bone.name] = {
            "parent": bone.parent.name if bone.parent is not None else ""
        }

    group_names = {group.index: group.name for group in mesh_obj.vertex_groups}
    # Clear slots before retaining attribute handles. Blender 4.5 can invalidate
    # Boolean attribute data pointers when the material collection changes.
    mesh_obj.data.materials.clear()
    two_sided_attribute = mesh_obj.data.attributes.get(
        "mcbedrock_two_sided_face"
    )
    pair_mode_attribute = mesh_obj.data.attributes.get(
        "mcbedrock_uv_pair_mode"
    )
    try:
        face_cube_signatures = json.loads(
            mesh_obj.get("mcbedrock_face_cube_signatures", "[]")
        )
    except (TypeError, ValueError):
        face_cube_signatures = []
    material_slots = {}
    pass_metadata = []
    all_texture_paths = []

    for pass_index, layer in enumerate(material_layers):
        paths = list(layer.get("texture_paths", ()))
        all_texture_paths.extend(path for path in paths if path)
        pass_metadata.append({
            "index": pass_index,
            "controller": layer.get("controller", ""),
            "texture_paths": paths,
            "hidden_parts": sorted(layer.get("hidden_parts", ())),
            "render_method": layer.get("render_method", "alpha_test"),
            "material": layer.get("material", {}),
            "material_rules": layer.get("material_rules", []),
        })

    def resolved_layer(layer, bone_names, face_cube_signature):
        included = layer.get("included_cube_signatures")
        if included is not None and face_cube_signature not in included:
            return None
        if bone_names and all(
            part_is_hidden(name, bones_by_name, layer.get("hidden_parts", ()))
            for name in bone_names
        ):
            return None
        method = layer.get("render_method", "alpha_test")
        behavior = dict(layer.get("material", {}) or {})
        rules = list(layer.get("material_rules", ()))
        matched_rule = False
        for rule in rules:
            pattern = str(rule.get("pattern", "*")).lower()
            if pattern == "*" or any(
                fnmatchcase(name.lower(), pattern)
                for name in bone_names
            ):
                matched_rule = True
                method = rule.get("render_method", method)
                behavior = dict(rule.get("material", {}) or {})
        if rules and not matched_rule:
            return None
        behavior["render_method"] = method
        return {
            "texture_paths": list(layer.get("texture_paths", ())),
            "behavior": behavior,
        }

    def stack_key(stack, is_two_sided, pair_mode):
        return (
            bool(is_two_sided),
            str(pair_mode),
            tuple(
                (
                    tuple(texture_dedupe_key(path) for path in layer["texture_paths"]),
                    _material_slot_key(
                        layer["behavior"].get("render_method", "alpha_test"),
                        layer["behavior"],
                    ),
                )
                for layer in stack
            ),
        )

    for polygon in mesh_obj.data.polygons:
        bone_names = {
            group_names.get(membership.group, "")
            for vertex_index in polygon.vertices
            for membership in mesh_obj.data.vertices[vertex_index].groups
            if group_names.get(membership.group, "")
        }
        face_cube_signature = (
            face_cube_signatures[polygon.index]
            if polygon.index < len(face_cube_signatures) else ""
        )
        stack = []
        for layer in material_layers:
            resolved = resolved_layer(
                layer, bone_names, face_cube_signature
            )
            if resolved is not None and resolved["texture_paths"]:
                stack.append(resolved)
        is_two_sided = bool(
            two_sided_attribute is not None
            and polygon.index < len(two_sided_attribute.data)
            and two_sided_attribute.data[polygon.index].value
        )
        pair_mode_code = (
            int(pair_mode_attribute.data[polygon.index].value)
            if pair_mode_attribute is not None
            and polygon.index < len(pair_mode_attribute.data) else 0
        )
        pair_mode = {1: "directional", 2: "composite"}.get(pair_mode_code, "none")
        key = stack_key(stack, is_two_sided, pair_mode)
        slot = material_slots.get(key)
        if slot is None:
            first_layer = stack[0] if stack else {
                "texture_paths": [],
                "behavior": {"render_method": "blend"},
            }
            material = make_model_material(
                f"{mesh_obj.name}_Stack_{len(material_slots)}",
                first_layer["texture_paths"][0]
                if first_layer["texture_paths"] else "",
                interpolation=interpolation,
                render_method=first_layer["behavior"].get(
                    "render_method", "alpha_test"
                ),
                texture_paths=first_layer["texture_paths"],
                material_layers=stack,
                two_sided_uv=(
                    mesh_obj.data.uv_layers.get("UVMap_Back") is not None
                ),
                force_two_sided=is_two_sided,
                uv_pair_mode=pair_mode,
                backface_culling=backface_culling,
                surface_override=surface_override,
            )
            slot = len(mesh_obj.data.materials)
            mesh_obj.data.materials.append(material)
            material_slots[key] = slot
        polygon.material_index = slot

    mesh_obj["mcbedrock_texture_layers"] = " | ".join(all_texture_paths)
    mesh_obj["mcbedrock_material_passes"] = json.dumps(pass_metadata)
    issues = []
    for material in mesh_obj.data.materials:
        try:
            issues.extend(json.loads(material.get("mcbedrock_material_issues", "[]")))
        except (TypeError, ValueError):
            pass
    mesh_obj["mcbedrock_material_issues"] = json.dumps(issues)


def _entity_evaluation_context(description, preview_context):
    from ...molang_parser import MOLANG_UNKNOWN, execute_statements
    query = dict(preview_context or {})
    properties = query.pop("properties", {})
    query.pop("_mcbedrock_default_keys", None)
    if isinstance(properties, dict):
        query["property"] = lambda name="": properties.get(
            str(name), MOLANG_UNKNOWN
        )
    # Bedrock's idle controller state uses a negative attack timer.  Leaving
    # this variable implicit makes ``variable.attack_time >= 0`` transition
    # humanoid controllers into their attack state during a rest import.
    evaluation_context = {"q": query, "v": {"attack_time": -1.0}}
    for script_key in ("initialize", "pre_animation"):
        for statement in _script_statements(description, script_key):
            execute_statements(statement, evaluation_context)
    return evaluation_context


def _condition_active(condition, description, preview_context):
    if _is_unconditional(condition):
        return True
    from ...molang_parser import evaluate
    return evaluate(
        condition,
        _entity_evaluation_context(description, preview_context),
        0.0,
    ) != 0.0


def _controller_static_refs(controller, alias_table, preview_context, description=None):
    from ...animation_controller import (
        AnimationControllerEvaluator,
        resolve_animation_weights,
    )

    from ...molang_parser import evaluate

    evaluation_context = _entity_evaluation_context(description, preview_context)

    # A controller may explicitly provide a semantic unposed/neutral state
    # alongside gameplay presentation poses. For a deterministic neutral
    # import that state is a stronger declaration than an incidental numeric
    # initial pose index.
    states = controller.get("states", {}) if isinstance(controller, dict) else {}
    if isinstance(states, dict):
        for state_name in (
            "none", "no_pose", "neutral", "unposed",
            "default", "idle", "setup", "standing", "normal", "swimming",
        ):
            state = states.get(state_name)
            animations = state.get("animations", []) if isinstance(state, dict) else []
            authored_weights = {}
            for entry in animations if isinstance(animations, list) else []:
                if isinstance(entry, str):
                    authored_weights[entry] = 1.0
                elif isinstance(entry, dict):
                    for alias, weight in entry.items():
                        try:
                            authored_weights[alias] = float(weight)
                        except (TypeError, ValueError):
                            authored_weights[alias] = evaluate(
                                weight, evaluation_context, 0.0
                            )
            weights = resolve_animation_weights(authored_weights, alias_table)
            declared = [ref for ref, weight in weights.items() if weight > 1e-8]
            if declared:
                return declared
        # Gameplay-only state sets (sitting, attacking, flopping, etc.)
        # are not deterministic neutral poses without runtime query state.
        return []

    evaluator = AnimationControllerEvaluator(controller, context=evaluation_context)
    frame = evaluator.step(0.0)
    weights = resolve_animation_weights(frame.animations, alias_table)
    # The authored initial state is still authoritative at time zero when a
    # runtime transition query is unavailable. Keeping its declared animation
    # matches Bedrock controller semantics and preserves renderer setup tracks.
    return [ref for ref, weight in weights.items() if weight > 1e-8]


def _controller_effect_refs(controller, alias_table, preview_context, description=None):
    """Resolve time-zero effect tracks from evaluated controller state."""
    from ...animation_controller import AnimationControllerEvaluator, resolve_animation_weights

    evaluation_context = _entity_evaluation_context(description, preview_context)
    frame = AnimationControllerEvaluator(controller, context=evaluation_context).step(0.0)
    if not frame.transitions_determined:
        return []
    weights = resolve_animation_weights(frame.animations, alias_table)
    return [ref for ref, weight in weights.items() if weight > 1.0e-8]
def _preview_variables(description, preview_context):
    """Evaluate initialization and pre-animation variables for rendering."""
    variables = initialized_variables(description)
    scripts = description.get("scripts", {}) if isinstance(description, dict) else {}
    pre_animation = scripts.get("pre_animation", []) if isinstance(scripts, dict) else []
    if isinstance(pre_animation, str):
        pre_animation = [pre_animation]
    context = _entity_evaluation_context(description, preview_context)
    context.setdefault("v", {}).update(variables)
    if isinstance(pre_animation, list):
        from ...molang_parser import execute_statements
        for statement in pre_animation:
            if isinstance(statement, str):
                execute_statements(statement, context)
    variables.update(context.get("v", {}))
    return variables


def _preview_entity_scale(description, preview_context):
    """Evaluate a static client-entity ``scripts.scale`` for preview imports."""
    if not isinstance(description, dict):
        return 1.0
    scripts = description.get("scripts", {})
    expression = scripts.get("scale") if isinstance(scripts, dict) else None
    if expression is None:
        return 1.0
    if isinstance(expression, str) and "animation." in expression.lower():
        return 1.0
    try:
        value = float(expression)
    except (TypeError, ValueError):
        from ...molang_parser import evaluate
        value = evaluate(
            expression,
            _entity_evaluation_context(description, preview_context),
            1.0,
        )
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    return value if value > 1.0e-6 else 1.0

def _baby_pose_refs(parser, preview_context):
    """Resolve renderer-authored transforms required by dedicated baby rigs."""
    if not bool((preview_context or {}).get("is_baby", False)):
        return []
    aliases = parser.description.get("animations", {})
    aliases = aliases if isinstance(aliases, dict) else {}
    refs = []
    for alias, reference in aliases.items():
        normalized = str(alias).lower()
        if (
            isinstance(reference, str)
            and reference.startswith("animation.")
            and "baby" in normalized
            and any(token in normalized for token in ("transform", "scal", "setup"))
            and reference not in refs
        ):
            refs.append(reference)
    # Vanilla Goat ships this transform without exposing an entity alias.
    if str(parser.get_identifier() or "").lower() == "minecraft:goat":
        refs.append("animation.goat.baby_scaling")
    return refs


def _description_animation_controller_entries(description):
    """Yield legacy ``description.animation_controllers`` bindings.

    Bedrock 1.8 client entities commonly store controller bindings in this
    field instead of ``scripts.animate``.  They are semantically equivalent
    for a deterministic rest preview: evaluate each controller's initial
    state and apply only the animations active at time zero.
    """
    raw = description.get("animation_controllers", []) if isinstance(description, dict) else []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return
    for entry in raw:
        if isinstance(entry, str):
            yield "", entry
        elif isinstance(entry, dict):
            for alias, controller_ref in entry.items():
                yield str(alias), controller_ref


def _resolve_static_animation_refs(
    reference, alias_table, search_roots, preview_context, description,
    visiting=None,
):
    """Resolve nested animation-controller references at time zero."""
    resolved = alias_table.get(reference, reference)
    if not isinstance(resolved, str):
        return []
    if resolved.startswith("animation."):
        return [resolved]
    if not resolved.startswith("controller.animation."):
        return []

    key = resolved.casefold()
    visiting = set(visiting or ())
    if key in visiting:
        return []
    visiting.add(key)
    controller_path = next((
        path for root in search_roots
        for path in [find_animation_controller_file(root, resolved)] if path
    ), "")
    controller = (
        load_animation_controller(controller_path, resolved)
        if controller_path else {}
    )
    result = []
    for nested in _controller_static_refs(
        controller, alias_table, preview_context, description
    ):
        for animation_ref in _resolve_static_animation_refs(
            nested, alias_table, search_roots, preview_context, description,
            visiting,
        ):
            if animation_ref not in result:
                result.append(animation_ref)
    return result


def _static_pose_refs(parser, search_roots, preview_context):
    description = parser.description
    alias_table = description.get("animations", {}) if isinstance(description, dict) else {}
    alias_table = alias_table if isinstance(alias_table, dict) else {}
    refs = []

    def append_reference(reference):
        for resolved in _resolve_static_animation_refs(
            reference, alias_table, search_roots, preview_context, description
        ):
            if resolved not in refs:
                refs.append(resolved)

    animate_entries = list(script_animation_entries(description))
    for alias, condition in animate_entries:
        if _condition_active(condition, description, preview_context):
            append_reference(alias)

    # Legacy 1.8 entities keep controller bindings directly on description.
    if not animate_entries:
        for _alias, controller_ref in _description_animation_controller_entries(description):
            append_reference(controller_ref)

    format_version = str(parser.data.get("format_version", ""))
    if not animate_entries and format_version.startswith("1.8"):
        for alias, animation_ref in alias_table.items():
            normalized = str(alias).lower().replace("animation.", "").split(".")[-1]
            if not any(
                normalized == token or normalized.endswith("_" + token)
                for token in _LEGACY_REST_ALIASES
            ):
                continue
            if isinstance(animation_ref, str) and animation_ref.startswith("animation."):
                if animation_ref not in refs:
                    refs.append(animation_ref)
    explicit_unposed = [
        ref for ref in refs
        if any(token in str(ref).lower() for token in (".no_pose", ".unposed"))
    ]
    return explicit_unposed or refs

def _animation_semantic_tail(animation_ref):
    """Return a version-insensitive final semantic animation component."""
    lowered = str(animation_ref or "").lower()
    version = re.search(r"\.v\d+(?:\.\d+)*$", lowered)
    if version:
        lowered = lowered[:version.start()]
    return lowered.rsplit(".", 1)[-1]


def _assembly_pose_refs(parser, search_roots, preview_context, declared_refs=()):
    """Select active, source-declared renderer assembly tracks."""
    refs = _static_pose_refs(parser, search_roots, preview_context)
    result = []
    for ref in refs:
        tail = _animation_semantic_tail(ref)
        if any(
            tail == token or tail.endswith("_" + token)
            for token in _SOURCE_ASSEMBLY_ALIASES
        ):
            result.append(ref)
    for ref in declared_refs or ():
        if (
            isinstance(ref, str)
            and ref.startswith("animation.")
            and ref not in result
        ):
            result.append(ref)
    return result

def _idle_pose_refs(parser, search_roots, preview_context, assembly_refs):
    """Safe deterministic idle tracks layered after the assembled bind state."""
    blocked = (
        "attack", "sneak", "sit", "lie", "sleep", "sprint", "flop",
        "carry", "walk", "run", "jump", "swim", "glide", "fly", "look",
    )
    assembly = set(assembly_refs or ())
    return [
        ref for ref in _static_pose_refs(parser, search_roots, preview_context)
        if ref not in assembly
        and "idle" in str(ref).lower()
        and not any(token in str(ref).lower() for token in blocked)
    ]


def _animation_is_static_assembly(search_roots, animation_ref):
    """Classify a legacy t=0 clip by authored assembly semantics.

    Older clients often use one looping controller both to lay out renderer
    parts and to animate them. Position channels are required to assemble
    those rigs at all, while semantic setup/base/default clips can provide
    required bind rotations. Rotation-only locomotion clips (for example a
    fish flop/swim cycle) are intentionally excluded from static imports.
    """
    normalized = str(animation_ref or "").lower().replace("animation.", "")
    tail = normalized.split(".")[-1]
    if any(
        tail == token or tail.endswith("_" + token)
        for token in _SOURCE_ASSEMBLY_ALIASES
    ):
        return True
    animation_path = next((
        path for root in search_roots
        for path in [find_animation_file(root, animation_ref)] if path
    ), "")
    if not animation_path:
        return False
    data = load_json_shared(animation_path)
    animations = data.get("animations", {}) if isinstance(data, dict) else {}
    animation = animations.get(animation_ref) if isinstance(animations, dict) else None
    bones = animation.get("bones", {}) if isinstance(animation, dict) else {}
    return any(
        isinstance(channels, dict) and "position" in channels
        for channels in bones.values()
    ) if isinstance(bones, dict) else False


def _preview_pose_refs(parser, search_roots, preview_context, renderer_assembly_refs):
    """Select deterministic t=0 pose tracks without model-name exceptions."""
    renderer_refs = list(dict.fromkeys(renderer_assembly_refs or ()))
    if renderer_refs:
        return list(dict.fromkeys(
            renderer_refs
            + _idle_pose_refs(parser, search_roots, preview_context, renderer_refs)
        ))

    active_refs = _static_pose_refs(parser, search_roots, preview_context)
    if list(script_animation_entries(parser.description)):
        # Modern scripts declare the controller graph that exists at t=0.
        # Controller weights are evaluated against the zero-motion preview
        # context, so adult/baby and moving/stopped branches remain exclusive.
        return active_refs

    # Legacy clients lack scripts.animate. Keep only renderer assembly clips;
    # do not turn gameplay-only rotation cycles into a supposed static pose.
    return [
        ref for ref in active_refs
        if _animation_is_static_assembly(search_roots, ref)
    ]


def _meaningful_assembly_refs(search_roots, references):
    """Return declared setup clips with a meaningful structural transform."""
    result = []
    for animation_ref in dict.fromkeys(references or ()):
        animation_path = next((
            path for root in search_roots
            for path in [find_animation_file(root, animation_ref)] if path
        ), "")
        if not animation_path:
            continue
        data = load_json_shared(animation_path)
        animations = data.get("animations", {}) if isinstance(data, dict) else {}
        animation = animations.get(animation_ref) if isinstance(animations, dict) else None
        if animation_has_meaningful_structural_transform(animation):
            result.append(animation_ref)
    return result


def _separate_structural_cranial_overlap(context, armature):
    """Move an inferred quadruped head to the exterior torso boundary.

    Legacy renderer geometries can rotate the torso through a bind-pose field
    while leaving an independently rooted head in the source frame. Applying
    the authored setup translation fixes height but can still leave half the
    head inside the rotated torso. Resolve that collision from evaluated
    bounds and structural role metadata, without entity-name rules.
    """
    if armature is None:
        return False
    family = str(
        armature.get("mcbedrock_structural_family", "")
        or armature.get("mcbedrock_inferred_family", "")
    )
    if family != "structural_legacy_quadruped":
        return False
    torso_name = str(
        armature.get("mcbedrock_structural_torso_bone", "")
        or armature.get("mcbedrock_inferred_bone", "")
    )
    cranial_name = str(
        armature.get("mcbedrock_structural_cranial_bone", "")
        or armature.get("mcbedrock_inferred_cranial_bone", "")
    )
    pose_bone = armature.pose.bones.get(cranial_name)
    if not torso_name or not cranial_name or pose_bone is None:
        return False

    points = {torso_name: [], cranial_name: []}
    depsgraph = context.evaluated_depsgraph_get()
    collection = (
        armature.users_collection[0]
        if armature.users_collection else None
    )
    for obj in collection.all_objects if collection is not None else ():
        if obj.type != "MESH":
            continue
        groups = {
            name: obj.vertex_groups.get(name)
            for name in points
        }
        if not any(groups.values()):
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            for name, group in groups.items():
                if group is None:
                    continue
                indices = {
                    vertex.index
                    for vertex in obj.data.vertices
                    if any(
                        membership.group == group.index
                        and membership.weight > 1.0e-6
                        for membership in vertex.groups
                    )
                }
                points[name].extend(
                    evaluated.matrix_world @ mesh.vertices[index].co
                    for index in indices
                    if index < len(mesh.vertices)
                )
        finally:
            evaluated.to_mesh_clear()

    torso_points = points[torso_name]
    cranial_points = points[cranial_name]
    if not torso_points or not cranial_points:
        return False
    torso_center = sum(torso_points, Vector()) / len(torso_points)
    cranial_center = sum(cranial_points, Vector()) / len(cranial_points)
    direction = cranial_center - torso_center
    direction.z = 0.0
    if direction.length <= 1.0e-6:
        return False
    direction.normalize()
    torso_edge = max(point.dot(direction) for point in torso_points)
    cranial_edge = min(point.dot(direction) for point in cranial_points)
    overlap = torso_edge - cranial_edge
    model_span = max(
        max(point[axis] for point in torso_points + cranial_points)
        - min(point[axis] for point in torso_points + cranial_points)
        for axis in range(3)
    )
    tolerance = max(1.0e-4, model_span * 0.01)
    if overlap <= tolerance:
        return False

    world_delta = direction * (overlap + tolerance)
    local_delta = (
        armature.matrix_world.inverted().to_3x3() @ world_delta
    )
    pose_bone.matrix = (
        Matrix.Translation(local_delta) @ pose_bone.matrix
    )
    context.view_layer.update()
    armature["mcbedrock_cranial_separation"] = tuple(world_delta)
    armature["mcbedrock_cranial_overlap_resolved"] = float(overlap)
    return True


def _bake_pose_as_rest(context, armature):
    """Convert current assembly pose and its evaluated mesh to rest state."""
    if not armature or armature.type != "ARMATURE":
        return
    depsgraph = context.evaluated_depsgraph_get()
    evaluated_coordinates = {}
    for obj in armature.users_collection[0].all_objects:
        if obj.type != "MESH":
            continue
        modifiers = [
            modifier for modifier in obj.modifiers
            if modifier.type == "ARMATURE" and modifier.object == armature
        ]
        if not modifiers:
            continue
        evaluated_obj = obj.evaluated_get(depsgraph)
        evaluated_mesh = evaluated_obj.to_mesh()
        if len(evaluated_mesh.vertices) == len(obj.data.vertices):
            evaluated_coordinates[obj.name] = [
                vertex.co.copy() for vertex in evaluated_mesh.vertices
            ]
        evaluated_obj.to_mesh_clear()
    previous_active = context.view_layer.objects.active
    previous_selection = list(context.selected_objects)
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    deselect_all_objects(context)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.select_all(action="SELECT")
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    for object_name, coordinates in evaluated_coordinates.items():
        obj = bpy.data.objects.get(object_name)
        if obj is None or len(obj.data.vertices) != len(coordinates):
            continue
        for vertex, coordinate in zip(obj.data.vertices, coordinates):
            vertex.co = coordinate
        obj.data.update()
    armature.animation_data_clear()
    for pose_bone in armature.pose.bones:
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_mode = 'QUATERNION'
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)
        for channel in ("position", "rotation", "scale"):
            key = f"mcbedrock_static_{channel}"
            baked_key = f"mcbedrock_baked_source_{channel}"
            if key in pose_bone:
                pose_bone[baked_key] = list(pose_bone[key])
                del pose_bone[key]
    deselect_all_objects(context)
    for obj in previous_selection:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if previous_active and previous_active.name in bpy.data.objects:
        context.view_layer.objects.active = previous_active


def _link_dragon_segment_chain(context, armature):
    """Parent native dragon segments while preserving their baked world rest."""
    if not armature or armature.type != "ARMATURE":
        return
    previous_active = context.view_layer.objects.active
    previous_selection = list(context.selected_objects)
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    deselect_all_objects(context)
    armature.select_set(True)
    context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    root = armature.data.edit_bones.get("root")
    neck_anchor = armature.data.edit_bones.get("neck") or root
    if neck_anchor is not None and neck_anchor.name != "root":
        neck_anchor.parent = root
        neck_anchor.use_connect = False
    previous = neck_anchor
    for index in range(1, 6):
        bone = armature.data.edit_bones.get(f"neck{index}")
        if bone is None:
            continue
        matrix = bone.matrix.copy()
        bone.parent = previous
        bone.use_connect = False
        bone.matrix = matrix
        bone["mcbedrock_parent"] = previous.name if previous else ""
        previous = bone
    previous = armature.data.edit_bones.get("body") or root
    for index in range(1, 13):
        bone = armature.data.edit_bones.get(f"tail{index}")
        if bone is None:
            continue
        matrix = bone.matrix.copy()
        bone.parent = previous
        bone.use_connect = False
        bone.matrix = matrix
        bone["mcbedrock_parent"] = previous.name if previous else ""
        previous = bone
    bpy.ops.object.mode_set(mode="OBJECT")
    armature["mcbedrock_dragon_segment_chains"] = "neck->neck1..neck5;body->tail1..tail12"
    deselect_all_objects(context)
    for obj in previous_selection:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if previous_active and previous_active.name in bpy.data.objects:
        context.view_layer.objects.active = previous_active


def _load_animation_definitions(parser, search_roots):
    definitions = {}
    for animation_ref in _resolve_entity_animation_refs(parser, search_roots):
        animation_path = next((
            path for root in search_roots
            for path in [find_animation_file(root, animation_ref)] if path
        ), "")
        data = load_json_shared(animation_path) if animation_path else {}
        values = data.get("animations", {}) if isinstance(data, dict) else {}
        if isinstance(values, dict):
            definitions.update(values)
    return definitions


def _entity_effect_events(parser, search_roots, preview_context):
    alias_table = parser.description.get("animations", {})
    alias_table = alias_table if isinstance(alias_table, dict) else {}
    definitions = _load_animation_definitions(parser, search_roots)
    active_animations = []
    for alias, condition in script_animation_entries(parser.description):
        if not _condition_active(condition, parser.description, preview_context):
            continue
        reference = alias_table.get(alias, alias)
        if isinstance(reference, str) and reference.startswith("animation."):
            active_animations.append(reference)
            continue
        if not isinstance(reference, str) or not reference.startswith("controller.animation."):
            continue
        controller_path = next((
            path for root in search_roots
            for path in [find_animation_controller_file(root, reference)] if path
        ), "")
        controller = load_animation_controller(controller_path, reference) if controller_path else {}
        active_animations.extend(_controller_effect_refs(controller, alias_table, preview_context, parser.description))

    events = []
    for animation_ref in dict.fromkeys(active_animations):
        animation = definitions.get(animation_ref, {})
        timeline = animation.get("particle_effects", {}) if isinstance(animation, dict) else {}
        if not isinstance(timeline, dict):
            continue
        for time_key, entries in timeline.items():
            try:
                event_time = float(time_key)
            except (TypeError, ValueError):
                continue
            if event_time > 1.0e-6:
                continue
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and entry.get("effect"):
                    event = dict(entry)
                    event["animation"] = animation_ref
                    events.append(event)
    return events


def _find_particle_effect(search_roots, effect_id):
    for root in search_roots:
        particle_root = root if os.path.basename(root).lower() == "particles" else os.path.join(root, "particles")
        if not os.path.isdir(particle_root):
            continue
        for current, dirs, files in os.walk(particle_root):
            dirs[:] = [name for name in dirs if name not in {"__pycache__", ".git"}]
            for filename in files:
                if not filename.lower().endswith(".json"):
                    continue
                filepath = os.path.join(current, filename)
                try:
                    data = load_json_file(filepath)
                except Exception:
                    # Current vanilla packs ship compiled (binary) particle
                    # files; one unreadable file must not fail the import.
                    continue
                particle = data.get("particle_effect", {}) if isinstance(data, dict) else {}
                description = particle.get("description", {}) if isinstance(particle, dict) else {}
                if description.get("identifier") == effect_id:
                    return filepath
    return ""


def _build_entity_effects(context, parser, search_roots, preview_context, armature, locators, mode):
    if str(mode or "OFF").upper() == "OFF":
        return [], []
    aliases = parser.description.get("particle_effects", {})
    aliases = aliases if isinstance(aliases, dict) else {}
    effects = []
    warnings = []
    from ...builder import build_particle_system
    from ...molang_parser import execute_statements

    for event in _entity_effect_events(parser, search_roots, preview_context):
        effect_id = aliases.get(event.get("effect"), event.get("effect"))
        effect_path = _find_particle_effect(search_roots, effect_id)
        if not effect_path:
            warnings.append(f"Unresolved entity particle: {effect_id}")
            continue
        initial = {"q": dict(preview_context), "v": {}, "t": {}}
        initial["q"].pop("properties", None)
        initial["q"].pop("_mcbedrock_default_keys", None)
        execute_statements(event.get("pre_effect_script", ""), initial)
        # Particle variables and textures must come from authored entity/event
        # data plus normal resource resolution. Never fabricate model-specific
        # defaults or substitute a texture based on an effect identifier.
        built = build_particle_system(
            context,
            effect_path,
            texture_override="",
            force_loop=False,
            molang_context=initial,
            parent_target=locators.get(str(event.get("locator", "")), armature),
        )
        if built.get("warning"):
            warnings.append(built["warning"])
        effects.append(built)
    return effects, warnings


def _create_locator_objects(collection, armature, canonical_bones, origin_offset):
    locators = {}
    offset = Vector(origin_offset or (0.0, 0.0, 0.0))
    pose_names = {bone.name.lower(): bone.name for bone in armature.pose.bones}
    for bone_name, bone_def in canonical_bones.items():
        authored = bone_def.get("locators", {}) if isinstance(bone_def, dict) else {}
        if not isinstance(authored, dict):
            continue
        resolved_bone = pose_names.get(str(bone_name).lower(), "")
        for locator_name, locator_def in authored.items():
            position = locator_def.get("offset", (0.0, 0.0, 0.0)) if isinstance(locator_def, dict) else locator_def
            rotation = (
                locator_def.get("rotation", (0.0, 0.0, 0.0))
                if isinstance(locator_def, dict) else (0.0, 0.0, 0.0)
            )
            ignore_scale = bool(
                locator_def.get("ignore_inherited_scale", False)
            ) if isinstance(locator_def, dict) else False
            empty = bpy.data.objects.new(f"Locator_{locator_name}", None)
            empty.empty_display_type = "PLAIN_AXES"
            empty.empty_display_size = 0.125
            collection.objects.link(empty)
            empty.parent = armature
            empty.location = bedrock_to_blender(position) - offset
            empty["mcbedrock_locator"] = str(locator_name)
            empty.rotation_mode = "XZY"
            empty.rotation_euler = bedrock_rotation_to_euler(rotation)
            empty["mcbedrock_locator_bone"] = resolved_bone
            empty["mcbedrock_locator_rotation"] = tuple(float(value) for value in rotation)
            empty["mcbedrock_ignore_inherited_scale"] = ignore_scale
            if resolved_bone:
                constraint = empty.constraints.new("CHILD_OF")
                constraint.target = armature
                constraint.subtarget = resolved_bone
                if ignore_scale:
                    constraint.use_scale_x = False
                    constraint.use_scale_y = False
                    constraint.use_scale_z = False
                target_world = armature.matrix_world @ armature.pose.bones[resolved_bone].matrix
                constraint.inverse_matrix = target_world.inverted_safe() @ empty.matrix_world
            locators[str(locator_name)] = empty
    return locators


def _unsupported_features(parser, canonical_bones):
    supported_description = {
        "identifier", "min_engine_version", "materials", "textures", "geometry",
        "animations", "scripts", "animation_controllers", "render_controllers", "particle_effects",
        "sound_effects", "spawn_egg", "enable_attachables", "hide_armor",
        "held_item_scale", "held_item_ignores_lighting",
    }
    unsupported = [
        f"client_entity.description.{key}"
        for key in parser.description
        if key not in supported_description and not str(key).startswith("_mcbedrock_")
    ]
    supported_bone = {
        "name", "parent", "pivot", "rotation", "binding", "mirror", "inflate",
        "bind_pose_rotation", "neverRender", "never_render", "locators", "cubes", "debug", "reset",
        "_mcbedrock_native_reflected", "_mcbedrock_native_parent_offset",
        "_mcbedrock_native_template",
    }
    supported_cube = {"origin", "size", "uv", "pivot", "rotation", "inflate", "mirror"}
    for bone_name, bone in canonical_bones.items():
        for key in bone if isinstance(bone, dict) else {}:
            if key not in supported_bone and not str(key).startswith("_mcbedrock_"):
                unsupported.append(f"bone.{bone_name}.{key}")
        for cube in bone.get("cubes", []) if isinstance(bone, dict) else []:
            if isinstance(cube, dict):
                for key in cube:
                    if key not in supported_cube and not str(key).startswith("_mcbedrock_"):
                        unsupported.append(f"cube.{bone_name}.{key}")
    return sorted(set(unsupported))


def import_client_entity(
    context,
    filepath,
    texture_override="",
    interpolation="Closest",
    display_name="",
    animation_filter="__NONE__",
    import_profile="rest",
    controller_context=None,
    entity_context=None,
    entity_effects="PREVIEW",
    controller_duration=10.0,
    pose_preset=POSE_REST_BIND,
):
    # One import reads the same animation files many times: parse each once.
    with shared_json_reads():
        return _import_client_entity(
            context, filepath, texture_override, interpolation, display_name,
            animation_filter, import_profile, controller_context, entity_context,
            entity_effects, controller_duration, pose_preset,
        )


def _import_client_entity(
    context,
    filepath,
    texture_override,
    interpolation,
    display_name,
    animation_filter,
    import_profile,
    controller_context,
    entity_context,
    entity_effects,
    controller_duration,
    pose_preset,
):
    pose_preset = str(pose_preset or POSE_REST_BIND).upper()
    if pose_preset not in POSE_PRESETS:
        raise ValueError(f"Unknown Bedrock pose preset: {pose_preset}")
    strict_rest = animation_filter == "__NONE__" and pose_preset == POSE_REST_BIND
    idle_preview = animation_filter == "__NONE__" and pose_preset == POSE_DEFAULT_IDLE
    parser = BedrockClientEntityParser(filepath)
    inline_geometry_ids = set()
    # A client-entity JSON may embed its geometry (common in small fixture
    # packs and custom assets). Keep that source authoritative before searching
    # the active pack or the built-in fallback geometry directory.
    try:
        source_data = load_json_file(filepath)
        inline_geometries = source_data.get("minecraft:geometry", []) if isinstance(source_data, dict) else []
        if isinstance(inline_geometries, dict):
            inline_geometries = [inline_geometries]
        if isinstance(inline_geometries, list):
            for inline_geometry in inline_geometries:
                if not isinstance(inline_geometry, dict):
                    continue
                inline_description = inline_geometry.get("description", {})
                inline_identifier = inline_description.get("identifier") if isinstance(inline_description, dict) else ""
                if inline_identifier:
                    inline_geometry_ids.add(str(inline_identifier))
        if isinstance(source_data, dict):
            inline_geometry_ids.update(
                str(key) for key in source_data
                if str(key).lower().startswith("geometry.")
            )
    except Exception:
        inline_geometry_ids = set()
    search_roots = _search_roots_for_asset(context, filepath)
    animation_bone_targets = _declared_animation_bone_targets(parser, search_roots)
    renderer_assembly_refs = []
    material_catalog = load_material_catalog(search_roots)
    geometry_items = parser.get_geometry_reference_items()
    if not geometry_items:
        geometry_items = [("default", ref) for ref in parser.get_geometry_references()]

    selected_animation = animation_filter not in ("__NONE__", "__ALL__", "")
    animation_context = (
        infer_preview_context(parser.description, animation_filter)
        if selected_animation
        else None
    )
    preview_context = dict(DEFAULT_ENTITY_PREVIEW_CONTEXT)
    default_query_keys = set(DEFAULT_ENTITY_PREVIEW_CONTEXT)
    preview_context["properties"] = {
        "minecraft:oxidation_level": "unoxidized",
        "minecraft:chest_interaction": "none",
    }
    behavior_context = _behavior_preview_context(search_roots, parser.get_identifier())
    behavior_properties = behavior_context.pop("properties", {}) if behavior_context else {}
    preview_context.update(behavior_context)
    if isinstance(behavior_properties, dict):
        preview_context["properties"].update(behavior_properties)
    if isinstance(entity_context, dict):
        supplied_properties = entity_context.get("properties", {})
        preview_context.update({
            key: value for key, value in entity_context.items() if key != "properties"
        })
        if isinstance(supplied_properties, dict):
            preview_context["properties"].update(supplied_properties)
        default_query_keys.difference_update(entity_context)
    if animation_context is not None:
        preview_context.update(animation_context)
        default_query_keys.difference_update(animation_context)
    preview_context["_mcbedrock_default_keys"] = tuple(sorted(default_query_keys))
    source_assembly_refs = (
        _assembly_pose_refs(parser, search_roots, preview_context)
        if not strict_rest else []
    )
    profile = resolve_import_profile(
        animation_filter,
        import_profile,
        preview_context=preview_context,
    )
    variables = _preview_variables(parser.description, preview_context)
    controller_ids = list(
        dict.fromkeys(
            iter_render_controller_refs(
                parser.description, variables, import_profile=profile, query_context=preview_context
            )
            or []
        )
    )

    controller_cache = {}

    def controller_lookup(controller_id):
        if controller_id not in controller_cache:
            controller_path, controller = find_render_controller(search_roots, controller_id)
            controller_cache[controller_id] = (controller_path, controller)
        return controller_cache[controller_id][1]

    render_entries = resolve_controller_entries(
        parser.description,
        controller_ids,
        controller_lookup,
        variables,
        import_profile=profile,
        query_context=preview_context,
        material_catalog=material_catalog,
    )
    if not render_entries:
        fallback_items = geometry_items
        if profile != "all" and len(fallback_items) > 1:
            default_item = next(((k, v) for k, v in fallback_items if k == "default"), None)
            fallback_items = [default_item] if default_item else fallback_items[:1]
        render_entries = _entry_list_from_geometry_items(fallback_items, parser.description.get("textures", {}))

    imported_results = []
    skipped_geometry = []
    shared_origin_offset = None
    asset_name = display_name or parser.get_identifier()
    master_collection = ensure_unique_collection(context, f"MC_Model_{asset_name}")
    seen_entries = set()

    # Preserve declared render-controller pass order and ownership boundaries.
    unique_entries = []
    for entry in render_entries:
        if profile != "all" and not entry.get("active", True):
            continue
        dedupe_key = (
            geometry_match_key(entry.get("geometry_identifier", "")),
            tuple(entry.get("texture_refs", [])) or (entry.get("texture_ref", ""),),
            tuple(sorted(entry.get("hidden_parts", set()))),
        )
        seen_entries.add(dedupe_key)
        unique_entries.append(entry)

    # Controller passes targeting the same geometry are material layers, not
    # duplicate coincident meshes. Distinct geometry identifiers remain
    # separate ownership boundaries.
    coalesced_entries = []
    coalesced_by_geometry = {}
    for entry in unique_entries:
        layer_key = geometry_match_key(entry.get("geometry_identifier", ""))
        existing = coalesced_by_geometry.get(layer_key)
        if existing is None:
            existing = dict(entry)
            existing["pass_entries"] = [dict(entry)]
            existing["source_geometry_identifiers"] = [
                entry.get("geometry_identifier", "")
            ]
            coalesced_by_geometry[layer_key] = existing
            coalesced_entries.append(existing)
            continue
        existing["pass_entries"].append(dict(entry))
        existing["source_geometry_identifiers"].append(
            entry.get("geometry_identifier", "")
        )
        existing["controller"] = ", ".join(
            value for value in (existing.get("controller", ""), entry.get("controller", ""))
            if value
        )
        existing["hidden_parts"] = set(existing.get("hidden_parts", ())).intersection(
            set(entry.get("hidden_parts", ()))
        )
    unique_entries = coalesced_entries

    # Attachable layers share the primary armature so a single animation
    # drives body and props together (chair/briefcase/newspaper stay attached).
    layered = len(unique_entries) > 1
    multi_entry = len(unique_entries) > 1
    primary_armature = None
    canonical_bones = {}
    seen_cube_signatures_by_texture = {}
    layer_meshes = []
    cube_owner_by_key = {}
    mesh_material_states = {}

    def remember_mesh_materials(
        mesh_obj, geometry, layers, paths, method,
    ):
        if mesh_obj is None:
            return
        mesh_material_states[mesh_obj.name] = {
            "mesh": mesh_obj,
            "layers": [dict(layer) for layer in layers],
            "texture_paths": list(paths),
            "render_method": method,
        }
        try:
            visible_keys = set(json.loads(
                mesh_obj.get("mcbedrock_face_cube_signatures", "[]")
            ))
        except (TypeError, ValueError):
            visible_keys = set()
        records = _geometry_cube_records(geometry)
        for key in visible_keys:
            if key and key in records:
                cube_owner_by_key.setdefault(key, mesh_obj)

    def append_overlay_passes(target, layers, included_keys):
        state = mesh_material_states.get(target.name)
        if state is None:
            return
        for layer in layers:
            overlay = dict(layer)
            overlay["included_cube_signatures"] = set(included_keys)
            state["layers"].append(overlay)
        _apply_entry_material(
            target,
            state["texture_paths"],
            interpolation,
            state["render_method"],
            material_layers=state["layers"],
        )

    visual_compatibility_issues = []

    def record_visual_compatibility(geometry):
        description = (
            geometry.get("description", {}) if isinstance(geometry, dict) else {}
        )
        identifier = (
            description.get("identifier", "") if isinstance(description, dict) else ""
        )
        for issue in inspect_geometry_visuals(geometry):
            payload = issue.as_dict()
            payload["geometry"] = identifier
            visual_compatibility_issues.append(payload)
    for index, entry in enumerate(unique_entries):
        geometry_key = entry.get("geometry_key", "default")
        geometry_identifier = entry.get("geometry_identifier", "")

        resolved_material_layers = []
        texture_paths = []
        seen_texture_paths = set()
        for pass_entry in entry.get("pass_entries", [entry]):
            pass_paths = _resolve_texture_paths_for_entry(
                context, parser, filepath, pass_entry, texture_override
            )
            declared_textures = (
                pass_entry.get("texture_refs", [])
                or ([pass_entry.get("texture_ref", "")] if pass_entry.get("texture_ref") else [])
            )
            if any(declared_textures) and not pass_paths:
                skipped_geometry.append(
                    f"{geometry_key}: missing pass texture(s) {', '.join(map(str, declared_textures))}"
                )
                continue
            resolved_material_layers.append({
                "texture_paths": pass_paths,
                "render_method": pass_entry.get("render_method", "alpha_test"),
                "material": pass_entry.get("material", {}),
                "material_rules": pass_entry.get("material_rules", []),
                "hidden_parts": set(pass_entry.get("hidden_parts", ())),
                "controller": pass_entry.get("controller", ""),
            })
            for path in pass_paths:
                key = texture_dedupe_key(path)
                if key not in seen_texture_paths:
                    seen_texture_paths.add(key)
                    texture_paths.append(path)
        if not resolved_material_layers or not texture_paths:
            continue
        texture_path = texture_paths[0]
        geometry_path = ""
        effective_geometry_identifier = geometry_identifier
        inline_identifier = next(
            (
                candidate for candidate in inline_geometry_ids
                if geometry_keys_match(
                    geometry_match_key(candidate),
                    geometry_match_key(geometry_identifier),
                )
            ),
            "",
        )
        if inline_identifier:
            geometry_path = filepath
            effective_geometry_identifier = inline_identifier
        geometry_search_roots = () if inline_identifier else search_roots
        for root in geometry_search_roots:
            geometry_path = find_geometry_file(root, geometry_identifier)
            if geometry_path:
                effective_geometry_identifier = geometry_identifier
                break
        if not geometry_path:
            skipped_geometry.append(f"{geometry_key}: unresolved {geometry_identifier}")
            continue

        texture_key = tuple(texture_dedupe_key(path) for path in texture_paths)
        same_texture_signatures = seen_cube_signatures_by_texture.setdefault(
            texture_key, set()
        )
        suffix = geometry_key if multi_entry else ""
        if suffix in {"default", ""} and multi_entry:
            suffix = f"geo_{index + 1}"

        try:
            source_geometry = BedrockModelParser(
                filepath=geometry_path
            ).get_geometry(identifier=effective_geometry_identifier)
            source_geometry = _resolve_exclusive_limb_geometry(
                source_geometry,
                preview_context,
                animation_filter=animation_filter,
            )
            source_geometry = _reflect_duplicate_bilateral_geometry(
                source_geometry
            )
            source_geometry = _expand_renderer_local_geometry(
                source_geometry, animation_bone_targets
            )
            renderer_assembly_refs.extend(
                source_geometry.get("description", {}).get(
                    "_mcbedrock_renderer_assembly_refs", ()
                )
            )
            raise_for_unsupported_visuals(source_geometry)
        except VisualCompatibilityError:
            raise
        except (ValueError, IndexError) as exc:
            skipped_geometry.append(f"{geometry_key}: {exc}")
            continue
        meaningful_assembly_refs = _meaningful_assembly_refs(
            search_roots,
            list(source_assembly_refs) + list(renderer_assembly_refs),
        )
        authored_assembly = bool(meaningful_assembly_refs)
        if idle_preview and not authored_assembly:
            prepared_geometry, neutral_inference = (
                _geometry_with_neutral_pose(source_geometry)
            )
        else:
            prepared_geometry = source_geometry
            neutral_inference = (
                None if authored_assembly
                else infer_structural_quadruped_torso(source_geometry)
            )

        if layered and primary_armature is not None:
            layer_geometry = prepared_geometry
            _merge_canonical_bones(canonical_bones, layer_geometry)
            record_visual_compatibility(source_geometry)
            layer_records = _geometry_cube_records(layer_geometry)
            overlap_by_mesh = {}
            for key in layer_records:
                owner = cube_owner_by_key.get(key)
                if owner is not None:
                    overlap_by_mesh.setdefault(owner.name, {
                        "mesh": owner, "keys": set()
                    })["keys"].add(key)
            for overlap in overlap_by_mesh.values():
                append_overlay_passes(
                    overlap["mesh"], resolved_material_layers,
                    overlap["keys"],
                )
            overlapping_keys = {
                key for overlap in overlap_by_mesh.values()
                for key in overlap["keys"]
            }
            skip_signatures = {
                layer_records[key] for key in overlapping_keys
            }
            if (
                layer_records
                and len(overlapping_keys) == len(layer_records)
                and not _geometry_has_non_cube_mesh(layer_geometry)
            ):
                # Every polygon is already owned by an earlier geometry pass.
                # Its texture/material remains as an ordered pass on those
                # polygons, avoiding exact coincident controller meshes.
                same_texture_signatures.update(
                    _geometry_cube_signatures(layer_geometry)
                )
                continue
            try:
                mesh_obj = add_bedrock_model_layer(
                    context,
                    primary_armature,
                    master_collection,
                    layer_geometry,
                    texture_path=texture_path,
                    texture_paths=texture_paths,
                    interpolation=interpolation,
                    origin_offset=shared_origin_offset,
                    hidden_bones=entry.get("hidden_parts", set()),
                    object_suffix=suffix,
                    render_method=entry.get("render_method", "alpha_test"),
                    # Exact coincident cuboids have become material passes on
                    # their existing polygons; distinct cubes remain a layer.
                    skip_cube_signatures=skip_signatures,
                    canonical_bones=canonical_bones,
                )
            except VisualCompatibilityError:
                raise
            except ValueError as exc:
                skipped_geometry.append(f"{geometry_key}: {exc}")
                continue
            mesh_obj["mcbedrock_geometry_key"] = geometry_key
            mesh_obj["mcbedrock_render_controller"] = entry.get("controller", "")
            _apply_entry_material(
                mesh_obj,
                texture_paths,
                interpolation,
                entry.get("render_method", "alpha_test"),
                entry.get("material_rules", []),
                entry.get("material", {}),
                material_layers=resolved_material_layers,
            )
            remember_mesh_materials(
                mesh_obj, layer_geometry, resolved_material_layers,
                texture_paths, entry.get("render_method", "alpha_test"),
            )
            if mesh_obj and not entry.get("active", True):
                mesh_obj.hide_viewport = True
                mesh_obj.hide_render = True
            _record_structural_metadata(
                neutral_inference, primary_armature, mesh_obj
            )
            if idle_preview:
                _record_neutral_pose_metadata(
                    neutral_inference, primary_armature, mesh_obj
                )
            layer_meshes.append(mesh_obj)
            same_texture_signatures.update(_geometry_cube_signatures(layer_geometry))
            continue

        try:
            result = build_bedrock_model(
                context,
                prepared_geometry,
                texture_path=texture_path,
                texture_paths=texture_paths,
                source_path=geometry_path,
                interpolation=interpolation,
                display_name=asset_name,
                origin_offset=shared_origin_offset,
                collection=master_collection,
                hidden_bones=entry.get("hidden_parts", set()),
                object_suffix=suffix,
                render_method=entry.get("render_method", "alpha_test"),
            )
            mesh_obj = result.get("mesh")
            _apply_entry_material(
                mesh_obj,
                texture_paths,
                interpolation,
                entry.get("render_method", "alpha_test"),
                entry.get("material_rules", []),
                entry.get("material", {}),
                material_layers=resolved_material_layers,
            )
            remember_mesh_materials(
                mesh_obj, prepared_geometry, resolved_material_layers,
                texture_paths, entry.get("render_method", "alpha_test"),
            )
            if mesh_obj and not entry.get("active", True):
                mesh_obj.hide_viewport = True
                mesh_obj.hide_render = True
        except VisualCompatibilityError:
            raise
        except ValueError as exc:
            skipped_geometry.append(f"{geometry_key}: {exc}")
            continue

        if shared_origin_offset is None:
            shared_origin_offset = result.get("origin_offset")
        result["geometry_path"] = geometry_path
        result["texture_path"] = texture_path
        result["geometry_key"] = geometry_key
        result["render_controller"] = entry.get("controller", "")
        arm = result["armature"]
        arm["mcbedrock_asset_type"] = "client_entity"
        arm["mcbedrock_entity_identifier"] = parser.get_identifier()
        arm["mcbedrock_entity_source_path"] = filepath
        arm["mcbedrock_geometry_key"] = geometry_key
        arm["mcbedrock_render_controller"] = entry.get("controller", "")
        arm["mcbedrock_import_profile"] = profile
        arm["mcbedrock_controllers_selected"] = len(controller_ids)
        arm["mcbedrock_render_method"] = entry.get("render_method", "alpha_test")
        arm["mcbedrock_available_geometry_refs"] = ", ".join(str(key) for key, _ref in geometry_items)
        _record_structural_metadata(neutral_inference, arm, mesh_obj)
        if idle_preview:
            _record_neutral_pose_metadata(neutral_inference, arm, mesh_obj)
        if preview_context and "variant" in preview_context:
            arm["mcbedrock_preview_variant"] = preview_context["variant"]
        imported_results.append(result)
        if primary_armature is None:
            primary_armature = arm
            try:
                primary_geometry = prepared_geometry
                _merge_canonical_bones(canonical_bones, primary_geometry)
                record_visual_compatibility(source_geometry)
                same_texture_signatures.update(_geometry_cube_signatures(primary_geometry))
            except (ValueError, IndexError):
                canonical_bones = {}
                seen_cube_signatures_by_texture = {}

    if not imported_results:
        if master_collection and not master_collection.objects:
            bpy.data.collections.remove(master_collection)
        detail = "; ".join(skipped_geometry[:4]) if skipped_geometry else "no geometry references found"
        raise ValueError(f"Could not import any visible entity geometry ({detail})")

    result = imported_results[0]
    armature = result["armature"]
    armature["mcbedrock_canonical_bone_count"] = len(armature.data.bones)
    if layer_meshes:
        armature["mcbedrock_geometry_layers"] = ", ".join(obj.name for obj in layer_meshes)
        result["layers"] = layer_meshes
    # Renderer-owned payloads are absent from resource-pack declarations.
    # Preserve the declared cart and diagnose the unavailable engine payload
    # instead of inventing cuboids, UVs, textures, or anchor bones.
    entity_identifier = str(parser.get_identifier() or "").lower()
    namespace, separator, entity_name = entity_identifier.partition(":")
    if entity_name.endswith("_minecart") and entity_name != "minecart":
        payload_name = entity_name[:-len("_minecart")]
        visual_compatibility_issues.append(renderer_payload_issue(
            f"{namespace + separator if separator else ''}{payload_name}",
            entity=entity_identifier,
            source_path=filepath,
        ).as_dict())

    applied = []
    resolved_animation_refs = _resolve_entity_animation_refs(parser, search_roots)
    animation_refs = list(resolved_animation_refs)
    if animation_filter and animation_filter not in ("__NONE__", "__ALL__"):
        animation_refs = [animation_filter]
    elif animation_filter == "__NONE__":
        animation_refs = []

    assembly_refs = (
        _assembly_pose_refs(
            parser, search_roots, preview_context, renderer_assembly_refs
        )
        if not strict_rest else []
    )
    if animation_filter not in ("", "__NONE__", "__ALL__"):
        assembly_refs = [
            ref for ref in assembly_refs
            if ref != animation_filter
        ]
    assembly_refs = _meaningful_assembly_refs(search_roots, assembly_refs)
    applied_assembly_refs = []
    armature.animation_data_clear()
    for pose_bone in armature.pose.bones:
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_mode = 'QUATERNION'
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)
    for animation_ref in assembly_refs:
        animation_path = next((
            path for root in search_roots
            for path in [find_animation_file(root, animation_ref)] if path
        ), "")
        if animation_path and apply_static_bedrock_pose(
            armature,
            animation_path,
            animation_ref,
            client_description=parser.description,
            query_context=preview_context,
        ):
            applied_assembly_refs.append(animation_ref)
    if applied_assembly_refs:
        _bake_pose_as_rest(context, armature)
    armature["mcbedrock_assembly_refs"] = ", ".join(applied_assembly_refs)

    # Presentation is derived only after renderer assembly has become rest.
    presentation_offset = recenter_bedrock_composite(
        context, armature, policy="GROUNDED"
    )
    if idle_preview and "mcbedrock_inferred_source_floor" in armature:
        desired_floor_offset = (
            float(armature["mcbedrock_inferred_source_floor"]) / 16.0
        )
        floor_delta = presentation_offset.z - desired_floor_offset
        if abs(floor_delta) > 1.0e-6:
            armature.matrix_world = (
                Matrix.Translation((0.0, 0.0, floor_delta))
                @ armature.matrix_world
            )
            presentation_offset.z = desired_floor_offset
            armature["mcbedrock_origin_offset"] = tuple(presentation_offset)
            armature["mcbedrock_presentation_offset"] = tuple(
                presentation_offset
            )
    if presentation_offset.length > 1.0e-6:
        centered_origin = tuple(
            armature.get("mcbedrock_origin_offset", (0.0, 0.0, 0.0))
        )
        result["origin_offset"] = centered_origin
        for imported in imported_results:
            imported["origin_offset"] = centered_origin

    preview_pose_refs = _preview_pose_refs(
        parser, search_roots, preview_context, renderer_assembly_refs
    ) if idle_preview else []
    idle_refs = [
        ref for ref in preview_pose_refs
        if ref not in applied_assembly_refs
    ]
    idle_phases = {}
    for animation_ref in idle_refs:
        animation_path = next((
            path for root in search_roots
            for path in [find_animation_file(root, animation_ref)] if path
        ), "")
        if not animation_path:
            continue
        animation_data = load_json_shared(animation_path)
        definitions = (
            animation_data.get("animations", {})
            if isinstance(animation_data, dict) else {}
        )
        animation = (
            definitions.get(animation_ref, {})
            if isinstance(definitions, dict) else {}
        )
        loop_value = animation.get("loop", False) if isinstance(animation, dict) else False
        is_loop = loop_value is True or str(loop_value).strip().lower() == "true"
        sample_phase = neutral_animation_phase(animation) if is_loop else 0.0
        idle_phases[animation_ref] = sample_phase
        apply_static_bedrock_pose(
            armature,
            animation_path,
            animation_ref,
            client_description=parser.description,
            query_context=preview_context,
            static_neutral=False,
            sample_time=sample_phase,
        )
    _separate_structural_cranial_overlap(context, armature)
    armature["mcbedrock_static_pose_refs"] = ", ".join(idle_refs)
    armature["mcbedrock_idle_phases"] = json.dumps(idle_phases, sort_keys=True)
    skipped_pose_refs = [
        ref for ref in _static_pose_refs(parser, search_roots, preview_context)
        if ref not in applied_assembly_refs and ref not in idle_refs
    ]

    imported_clip_keys = set()
    for animation_ref in animation_refs:
        animation_path = next((
            path for root in search_roots
            for path in [find_animation_file(root, animation_ref)] if path
        ), "")
        if not animation_path:
            continue
        clip_key = direct_clip_key(animation_path, animation_ref)
        if clip_key in imported_clip_keys:
            continue
        imported_clip_keys.add(clip_key)
        actions = apply_bedrock_animations(
            context,
            armature,
            animation_path,
            animation_filter=animation_ref,
            append_nla=animation_filter == "__ALL__",
            client_description=parser.description,
            query_context=preview_context,
        )
        applied.extend(actions)

    if animation_filter == "__ALL__" and armature.animation_data is not None:
        from .animation import arrange_nla_tracks_for_preview
        arrange_nla_tracks_for_preview(context, armature)

    if controller_context is not None:
        animation_definitions = {}
        for animation_ref in resolved_animation_refs:
            animation_path = ""
            for root in search_roots:
                animation_path = find_animation_file(root, animation_ref)
                if animation_path:
                    break
            if not animation_path:
                continue
            animation_data = load_json_shared(animation_path)
            definitions = animation_data.get("animations", {}) if isinstance(animation_data, dict) else {}
            if isinstance(definitions, dict):
                animation_definitions.update(definitions)

        alias_table = parser.description.get("animations", {})
        alias_table = alias_table if isinstance(alias_table, dict) else {}
        controller_refs = [
            ref for ref in parser.get_animation_references()
            if isinstance(ref, str) and ref.startswith("controller.animation.")
        ]
        for controller_ref in controller_refs:
            controller_path = ""
            for root in search_roots:
                controller_path = find_animation_controller_file(root, controller_ref)
                if controller_path:
                    break
            controller = load_animation_controller(controller_path, controller_ref) if controller_path else {}
            if not controller:
                continue
            action = apply_evaluated_animation_tracks(
                context,
                armature,
                controller,
                animation_definitions,
                alias_table=alias_table,
                duration=controller_duration,
                query_context=controller_context,
                action_name=controller_ref,
            )
            applied.append(action)

    entity_scale = _preview_entity_scale(parser.description, preview_context)
    if abs(entity_scale - 1.0) > 1.0e-6:
        armature.scale = (entity_scale, entity_scale, entity_scale)
        armature["mcbedrock_script_scale"] = entity_scale

    finalize_bedrock_rig(context, armature)
    place_transform_origin_below_model(context, armature)

    locators = _create_locator_objects(
        master_collection,
        armature,
        canonical_bones,
        result.get("origin_offset", (0.0, 0.0, 0.0)),
    )
    effective_entity_effects = "OFF" if strict_rest else entity_effects
    effects, effect_warnings = _build_entity_effects(
        context,
        parser,
        search_roots,
        preview_context,
        armature,
        locators,
        effective_entity_effects,
    )
    mesh_objects = [result.get("mesh")] + list(layer_meshes)
    unweighted = 0
    for mesh_obj in mesh_objects:
        if mesh_obj is None or getattr(mesh_obj, "type", "") != "MESH":
            continue
        unweighted += sum(1 for vertex in mesh_obj.data.vertices if not vertex.groups)
    roots = [bone.name for bone in armature.data.bones if bone.parent is None]
    warnings = list(skipped_geometry) + effect_warnings
    warnings.extend(
        f"{issue.get('geometry', 'geometry')} {issue['path']}: {issue['detail']}"
        for issue in visual_compatibility_issues
    )
    if len(roots) != 1:
        warnings.append(f"Rig has {len(roots)} root bones: {', '.join(roots)}")
    if unweighted:
        warnings.append(f"{unweighted} mesh vertices have no bone weight")
    face_report = {}
    uv_report = {}
    rig_distance_failures = []
    for mesh_obj in mesh_objects:
        if mesh_obj is None or getattr(mesh_obj, "type", "") != "MESH":
            continue
        group_names = {group.index: group.name for group in mesh_obj.vertex_groups}
        by_bone = {}
        for polygon in mesh_obj.data.polygons:
            owners = {
                group_names.get(membership.group, "")
                for vertex_index in polygon.vertices
                for membership in mesh_obj.data.vertices[vertex_index].groups
                if membership.weight > 0.0
            }
            for owner in owners:
                if owner:
                    by_bone[owner] = by_bone.get(owner, 0) + 1
        face_report[mesh_obj.name] = {
            "imported_faces": len(mesh_obj.data.polygons),
            "by_bone": by_bone,
        }
        uv_layer = mesh_obj.data.uv_layers.active
        if uv_layer and uv_layer.data:
            coords = [tuple(loop.uv) for loop in uv_layer.data]
            minimum = [min(value[axis] for value in coords) for axis in range(2)]
            maximum = [max(value[axis] for value in coords) for axis in range(2)]
            uv_report[mesh_obj.name] = {
                "minimum": minimum,
                "maximum": maximum,
                "in_unit_bounds": all(
                    -1.0e-6 <= value <= 1.0 + 1.0e-6
                    for uv in coords for value in uv
                ),
            }
        for group in mesh_obj.vertex_groups:
            indices = [
                vertex.index for vertex in mesh_obj.data.vertices
                if any(
                    member.group == group.index and member.weight > 0.0
                    for member in vertex.groups
                )
            ]
            bone = armature.pose.bones.get(group.name)
            if not indices or bone is None:
                continue
            center = sum(
                (mesh_obj.data.vertices[index].co for index in indices),
                Vector((0.0, 0.0, 0.0)),
            ) / len(indices)
            distance = (center - bone.head).length
            if distance > 4.0:
                rig_distance_failures.append({
                    "mesh": mesh_obj.name,
                    "bone": group.name,
                    "distance": distance,
                })

    if rig_distance_failures:
        warnings.append(
            f"{len(rig_distance_failures)} bone controls are far from owned geometry"
        )

    resolved_texture_paths = sorted({
        path for entry in unique_entries
        for path in _resolve_texture_paths_for_entry(
            context, parser, filepath, entry, texture_override
        )
    })
    texture_validation = {}
    for path in resolved_texture_paths:
        try:
            image = bpy.data.images.load(path, check_existing=True)
            texture_validation[path] = {
                "exists": True,
                "dimensions": [int(image.size[0]), int(image.size[1])],
                "has_pixels": bool(image.size[0] and image.size[1]),
            }
        except Exception as exc:
            texture_validation[path] = {
                "exists": False,
                "error": str(exc),
            }
    rest_matrices = {
        bone.name: [
            float(bone.matrix_local[row][column])
            for row in range(4) for column in range(4)
        ]
        for bone in armature.data.bones
    }
    unresolved_dependencies = sorted({
        f"part_visibility:{pattern}"
        for entry in unique_entries
        for pattern in entry.get("unresolved_visibility", [])
    })

    held_item_scale = float(parser.description.get("held_item_scale", 1.0) or 1.0)
    held_item_ignores_lighting = bool(
        parser.description.get("held_item_ignores_lighting", False)
    )
    armature["mcbedrock_held_item_scale"] = held_item_scale
    armature["mcbedrock_held_item_ignores_lighting"] = held_item_ignores_lighting
    nonvisual_omissions = (
        ["sound_effects"] if parser.description.get("sound_effects") else []
    )
    compatibility = {
        "entity": parser.get_identifier(),
        "pose_mode": pose_preset,
        "geometry_format_version": armature.get("mcbedrock_format_version", ""),
        "geometry_schema_family": armature.get("mcbedrock_schema_family", ""),
        "presentation_offset": list(
            armature.get("mcbedrock_presentation_offset", (0.0, 0.0, 0.0))
        ),
        "assembly_family": armature.get(
            "mcbedrock_renderer_family", "authored_geometry"
        ),
        "assembly_animations": list(applied_assembly_refs),
        "held_item_presentation": {
            "scale": held_item_scale,
            "ignores_lighting": held_item_ignores_lighting,
            "mesh": "runtime inventory dependent",
        },
        "nonvisual_omissions": nonvisual_omissions,
        "rest_matrices": rest_matrices,
        "applied_animations": [getattr(action, "name", str(action)) for action in applied],
        "skipped_animations": skipped_pose_refs,
        "preview_context": {
            key: value for key, value in preview_context.items()
            if key != "_mcbedrock_default_keys"
        },
        "geometry_layers": [
            source_id
            for entry in unique_entries
            for source_id in entry.get(
                "source_geometry_identifiers", [entry.get("geometry_identifier", "")]
            )
        ],
        "texture_layers": [entry.get("texture_refs", []) for entry in unique_entries],
        "resolved_textures": resolved_texture_paths,
        "texture_validation": texture_validation,
        "face_validation": face_report,
        "uv_validation": uv_report,
        "known_molang_dependencies": sorted(variables),
        "unresolved_molang_dependencies": unresolved_dependencies,
        "unweighted_geometry": unweighted,
        "rig_distance_failures": rig_distance_failures,
        "preview_fallbacks": list(skipped_geometry),
        "native_payloads": [
            obj.get("mcbedrock_payload")
            for obj in result.get("layers", [])
            if obj and obj.get("mcbedrock_payload")
        ],
        "controllers": controller_ids,
        "static_pose_animations": armature.get("mcbedrock_static_pose_refs", "").split(", ") if armature.get("mcbedrock_static_pose_refs") else [],
        "effects": len(effects),
        "warnings": warnings,
        "visual_issues": list(visual_compatibility_issues),
        "unsupported": sorted(set(
            _unsupported_features(parser, canonical_bones)
            + [
                f"{issue['code']}:{issue['path']}"
                for issue in visual_compatibility_issues
            ]
        )),
    }
    armature["mcbedrock_compatibility_report"] = json.dumps(compatibility, default=str)

    return {
        "armature": armature,
        "mesh": result["mesh"],
        "animations": applied,
        "geometry_path": result.get("geometry_path", ""),
        "texture_path": result.get("texture_path", ""),
        "geometries": imported_results,
        "layers": result.get("layers", layer_meshes),
        "effects": effects,
        "locators": locators,
        "compatibility": compatibility,
    }

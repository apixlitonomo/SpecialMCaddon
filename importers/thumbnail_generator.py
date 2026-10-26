"""Offscreen thumbnail rendering for asset-browser items.

Renders a still of an asset into a throwaway scene using Eevee, saves it to the
thumbnail cache, and cleans up. Reuses the existing builders rather than
duplicating import logic. Everything is wrapped defensively: on any failure the
caller keeps the placeholder icon.
"""

import math
import os

try:
    import bpy
    from mathutils import Vector
except ImportError:  # pragma: no cover - non-Blender context
    bpy = None
    Vector = None


QUALITY_RES = {"LOW": 128, "MEDIUM": 256, "HIGH": 512}


def _resolve_res(context):
    quality = getattr(context.scene, "mcbedrock_thumb_quality", "MEDIUM")
    return QUALITY_RES.get(quality, 256)


def _set_eevee(render):
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            render.engine = engine
            return
        except Exception:
            continue


def _import_asset(asset, context):
    """Import the asset into the current (temporary) scene via existing builders."""
    atype = (asset.asset_type or "").lower()
    path = asset.source_path

    if "particle" in atype:
        from .. import builder
        builder.build_particle_system(context, path)
        return

    from .bedrock_assets import detect_json_asset
    from ..builders import bedrock_builder

    try:
        kind = detect_json_asset(path)
    except Exception:
        kind = ""

    if kind == "entity" or "entity" in atype:
        bedrock_builder.import_client_entity(
            context, path, display_name=asset.name, pose_preset="DEFAULT_IDLE"
        )
    elif kind == "java_model" or "java" in atype:
        from ..builders import java_builder
        java_builder.build_java_model_from_file(context, path, display_name=asset.name)
    else:
        bedrock_builder.build_bedrock_model_from_file(context, path, display_name=asset.name)


def _bounds(objs):
    """World-space center and largest dimension of the given objects."""
    mins = Vector((math.inf, math.inf, math.inf))
    maxs = Vector((-math.inf, -math.inf, -math.inf))
    found = False
    for obj in objs:
        corners = None
        if obj.type == 'MESH' and getattr(obj.data, "vertices", None):
            corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        else:
            corners = [obj.matrix_world.translation]
        for world in corners:
            found = True
            for i in range(3):
                mins[i] = min(mins[i], world[i])
                maxs[i] = max(maxs[i], world[i])
    if not found:
        return Vector((0.0, 0.0, 0.0)), 2.0
    center = (mins + maxs) / 2.0
    size = max((maxs - mins))
    return center, max(size, 0.1)


def _look_at(obj, target):
    direction = target - obj.location
    if direction.length < 1e-6:
        return
    obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()


def _setup_camera(scene, objs):
    center, size = _bounds(objs)
    cam_data = bpy.data.cameras.new("_mcthumb_cam")
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = max(size * 1.25, 0.1)
    cam = bpy.data.objects.new("_mcthumb_cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    offset = Vector((1.0, -1.0, 0.8)).normalized() * max(size * 2.5, 2.0)
    cam.location = center + offset
    _look_at(cam, center)


def _add_light(scene):
    light = bpy.data.lights.new("_mcthumb_sun", 'SUN')
    light.energy = 3.0
    obj = bpy.data.objects.new("_mcthumb_sun", light)
    scene.collection.objects.link(obj)
    obj.rotation_euler = (math.radians(55), math.radians(10), math.radians(60))


def _add_world(scene):
    world = bpy.data.worlds.new("_mcthumb_world")
    world.use_nodes = True
    try:
        bg = world.node_tree.nodes.get("Background")
        if bg:
            bg.inputs["Color"].default_value = (0.05, 0.05, 0.05, 1.0)
            bg.inputs["Strength"].default_value = 1.0
    except Exception:
        pass
    scene.world = world


def _render(context, scene):
    try:
        with context.temp_override(scene=scene):
            bpy.ops.render.render(write_still=True)
    except Exception:
        bpy.ops.render.render(write_still=True)


def generate_thumbnail(asset, context):
    """Render a still of the asset and return the cached PNG path (or None)."""
    if not bpy:
        return None

    from . import thumbnail_cache

    src = getattr(asset, "source_path", "")
    if not src or not os.path.isfile(src):
        return None

    out_path = thumbnail_cache.cache_path_for(asset)
    original_scene = context.window.scene
    tmp = bpy.data.scenes.new("_mcthumb_tmp")

    try:
        res = _resolve_res(context)
        _set_eevee(tmp.render)
        tmp.render.resolution_x = res
        tmp.render.resolution_y = res
        tmp.render.film_transparent = True
        tmp.render.image_settings.file_format = 'PNG'
        tmp.render.image_settings.color_mode = 'RGBA'
        _add_world(tmp)

        context.window.scene = tmp

        before = set(bpy.data.objects)
        _import_asset(asset, context)
        new_objs = [o for o in bpy.data.objects if o not in before]
        if not new_objs:
            return None

        # Make sure everything imported is visible in the temp scene.
        for obj in new_objs:
            if obj.name not in tmp.collection.all_objects:
                try:
                    tmp.collection.objects.link(obj)
                except Exception:
                    pass

        frame_objs = [o for o in new_objs if o.type in {'MESH', 'ARMATURE', 'EMPTY'}]
        _setup_camera(tmp, frame_objs or new_objs)
        _add_light(tmp)

        if "particle" in (asset.asset_type or "").lower():
            span = tmp.frame_end - tmp.frame_start
            tmp.frame_set(int(tmp.frame_start + span * 0.3))

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        tmp.render.filepath = out_path
        _render(context, tmp)

        return out_path if os.path.isfile(out_path) else None
    except Exception as exc:
        print(f"[MCBedrock] thumbnail generation error: {exc}")
        return None
    finally:
        try:
            context.window.scene = original_scene
        except Exception:
            pass
        try:
            bpy.data.scenes.remove(tmp, do_unlink=True)
        except Exception:
            pass
        try:
            bpy.data.orphans_purge(do_recursive=True)
        except Exception:
            pass

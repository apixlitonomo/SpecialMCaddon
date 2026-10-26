"""Ground bounce in EEVEE: the sunlit ground's color in the sky's lower half.

Cycles bounces light off the ground for real (one real bounce before Fast
GI, sky.setup_cycles). EEVEE's bounce is screen-space and misses most of it:
canopy undersides, overhangs and the shaded sides of trunks only see the
sky's lower hemisphere, a dim fixed tone, and read far darker than in
Cycles. Unreal's Sky Light captures the real ground for its lower
hemisphere; this does the same cheaply. A few rays measure the ground in
front of the camera (texture average times biome tint), and the dome's
ground color becomes that ground lit by the current sun and sky. It is
measured once per shot and follows time of day, sun angle and biome.
"""
import math

import bpy
import numpy as np
from mathutils import Vector

ALBEDO = 'mc_ground_albedo'        # sky settings: measured linear albedo
SHOT = 'mc_ground_albedo_shot'     # ... and the shot it was measured for
BASE = (.34, .3, .24)              # the dome's dim warm ground (Cycles, fallback)
SUNLIT = .75                       # share of the ground below in sunlight
LIMIT = 2.                         # bright snow and sand: a strong, capped bounce
GRID, STEP, AHEAD = 3, 5., 12.     # 7 x 7 rays, 5 blocks apart, 12 ahead
_textures = {}


def _linear(values):
    return np.where(values <= .04045, values / 12.92, ((values + .055) / 1.055) ** 2.4)


def _texture_albedo(material):
    """Average color of a material's base texture, weighted by coverage."""
    tree = material.node_tree if material is not None else None
    image = next((n.image for n in tree.nodes if n.type == 'TEX_IMAGE' and n.image and n.image.size[0])
                 if tree else (), None)
    if image is None:
        return None
    key = (image.name, image.size[0], image.size[1])
    if key not in _textures:
        pixels = np.array(image.pixels[:], dtype=np.float32).reshape(-1, 4)
        color, cover = pixels[:, :3], pixels[:, 3:]
        if image.colorspace_settings.name not in ('Non-Color', 'Linear Rec.709'):
            color = _linear(color)
        _textures[key] = tuple((color * cover).sum(0) / max(float(cover.sum()), 1e-6))
    return np.array(_textures[key])


def _tint(obj, polygon, material):
    """MiEx's biome tint (the Cd color) where the material uses it."""
    colors = obj.data.color_attributes.get('Cd')
    if colors is None or not any(n.type == 'VERTEX_COLOR' and n.layer_name == 'Cd' for n in material.node_tree.nodes):
        return None
    index = polygon.loop_start if colors.domain == 'CORNER' else polygon.vertices[0]
    return np.array(colors.data[index].color[:3])


def measure(scene, camera):
    """Linear albedo of the ground in front of the camera, or None."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    forward = camera.matrix_world.to_3x3() @ Vector((0, 0, -1))
    forward.z = 0
    forward = forward.normalized() if forward.length > 1e-6 else Vector((0, 1, 0))
    side = Vector((forward.y, -forward.x, 0))
    centre = camera.matrix_world.translation + forward * AHEAD
    height = camera.matrix_world.translation.z
    down = Vector((0, 0, -1))
    total, found = np.zeros(3), 0
    for i in range(-GRID, GRID + 1):
        for j in range(-GRID, GRID + 1):
            point = centre + forward * (i * STEP) + side * (j * STEP)
            hit, location, normal, face, obj, _matrix = scene.ray_cast(depsgraph, point + Vector((0, 0, 64)), down, distance=256)
            if hit and location.z > height + 1:
                # A canopy or roof over the camera: the ground is below it.
                below = scene.ray_cast(depsgraph, Vector((point.x, point.y, height + .5)), down, distance=64)
                if below[0] and below[2].z >= .5:
                    hit, location, normal, face, obj, _matrix = below
            if not hit or obj.type != 'MESH' or normal.z < .5 or not obj.material_slots:
                continue
            mesh = obj.data
            if face >= len(mesh.polygons):
                continue
            polygon = mesh.polygons[face]
            material = obj.material_slots[min(polygon.material_index, len(obj.material_slots) - 1)].material
            albedo = _texture_albedo(material)
            if albedo is None:
                continue
            tint = _tint(obj, polygon, material)
            total += albedo * tint if tint is not None else albedo
            found += 1
    return tuple(float(v) for v in total / found) if found else None


def albedo(scene):
    """The shot's ground albedo, measured again when the camera changes or moves."""
    s = scene.mcbedrock_sky
    camera = scene.camera
    if camera is None:
        return None
    place = camera.matrix_world.translation
    shot = f'{camera.name}|{round(place.x / 16)}|{round(place.y / 16)}|{round(place.z / 16)}'
    if s.get(SHOT) != shot:
        try:
            measured = measure(scene, camera)
        except (RuntimeError, ReferenceError, ValueError):
            measured = None
        s[SHOT] = shot
        if measured is None:
            if ALBEDO in s:
                del s[ALBEDO]
        else:
            s[ALBEDO] = measured
    value = s.get(ALBEDO)
    return tuple(value) if value is not None else None


def tone(scene, ambient):
    """The dome's ground color in EEVEE: the measured ground, lit like the
    land (sun lamps plus the sky), relative to the dome's ambient radiance.
    Never below the former dim tone, so shade and nights only gain light."""
    ground = albedo(scene)
    if ground is None:
        return BASE
    sun = np.zeros(3)
    # Only the rig's role-tagged suns count; they live in its collection, and
    # this runs on every sky update, so skip a walk over the whole world.
    rig = getattr(scene.mcbedrock_sky, 'collection', None)
    for obj in rig.all_objects if rig is not None else scene.objects:
        data = obj.data if obj.type == 'LIGHT' else None
        if data is None or data.type != 'SUN' or obj.hide_render or not obj.get('role'):
            continue
        lift = -(obj.matrix_world.to_3x3() @ Vector((0, 0, -1))).z
        if lift > 0:
            sun += np.array(data.color[:]) * data.energy * getattr(data, 'diffuse_factor', 1.) * lift
    ambient = np.maximum(np.array(ambient), 1e-4)
    bounce = np.array(ground) * (SUNLIT * sun / (math.pi * ambient) + 1)
    return tuple(float(min(LIMIT, max(b, a))) for a, b in zip(BASE, bounce))


def update(scene):
    """Set the dome's ground color for the current engine."""
    s = scene.mcbedrock_sky
    world = s.world
    nodes = world.node_tree.nodes if world and world.node_tree else None
    if nodes is None or nodes.get('Ambient dome ground color') is None or nodes.get('Ambient light color') is None:
        return
    from .lighting_pipeline import set_value
    value = BASE
    if scene.render.engine == 'BLENDER_EEVEE':
        # As the sky last wrote them: a moving cycle drives these (sky_drive).
        from . import sky_drive
        color = sky_drive.value(nodes['Ambient light color'].outputs[0])
        strength = sky_drive.value(nodes['Ambient'].inputs['Strength'])
        value = tone(scene, [c * strength for c in color[:3]])
    set_value(nodes['Ambient dome ground color'].outputs[0], 'default_value', (*value, 1))

"""Owned, reversible Minecraft sky rig. No context-dependent mesh operators."""
import json
import colorsys
import math
import re
import struct
import uuid
from pathlib import Path

import bpy
from .compatibility import SKY_REQUIREMENT, SkyOperator, supports_sky
from bpy.app.handlers import persistent
from mathutils import Quaternion, Vector

PRESETS = json.loads((Path(__file__).resolve().parents[1] / 'data/sky_presets.json').read_text())
BUSY = set()
TAG = 'mc_sky_owner'
SKY_SHADER_VERSION = 28
CLOUD_LAYOUT_VERSION = 22
EEVEE_SETUP_VERSION = 5
SUN_RADIANCE = 4.
SUN_REFLECTION_STRENGTH = .15
MOON_REFLECTION_STRENGTH = .3
# Degrees. Reflections use a tight source; broad shadow softness spread the
# solar and lunar glints over whole lakes.
REFLECTION_ANGLE = 2.
from .sky_textures import PHASES, discover, load_images, upgrade_builtin_images
from . import sky_materials, lighting_pipeline, atmosphere, preview_quality, native_color, outlines, light_blocks, sky_drive
from . import clouds as cloud_pattern

# Blender interpretations of the referenced game lighting, using editable rig
# controls. These are not renderer ports. Optional volume/probe/edge passes stay off.
# Trailer lighting by day: how much the sky dome brightens per unit of Fill,
# per engine. It stands in for the fill suns it replaced. At 1.95 the dome
# alone outshone the sun on a real world and every shadow washed out.
DOME_BOOST = {'BLENDER_EEVEE': .3, 'CYCLES': .7}
# The distance fog a new sky adds (fog_planes, one card in front of the
# camera): the far ground only, never the sky, so the near ground and the
# character stay clear and a hazed treeline meets the sky with a clean edge.
# From 96 blocks with crisp 6-block edges: from 48 (with Flat fog stacked on
# it) a raised camera saw the whole frame under a milky veil, not distance.
FOG_PLANES = dict(near=96., far=300., density=.35, count=6, top=16., fade=24., soft=6.,
                  glow=.4, glow_spread=35., color_mode='AUTO', relative_top=True, edge=True)
FOG_PLANES_TAG = 'mc_sky_fog_planes'
# On the sky's compositor: the scene's own compositor it was copied from.
COMPOSITOR_SOURCE = 'mc_sky_compositor_source'
ADD_FOG_PLANES = True
# Trailer nights, matched to Mojang's night key art: navy overhead, a teal
# glow along the treeline, a cool-white moon strong enough to light faces,
# and cool skylight that leaves warm ground warm. Scene-linear values.
NIGHT_HORIZON = (.012, .05, .11)
NIGHT_AMBIENT = (.085, .12, .25)
NIGHT_MOON = 2.1                    # moon strength at full night, at Moon strength 4
NIGHT_MOON_COLOR = (.72, .85, 1.)
PRESET_OVERRIDES = {
    # Daylight's W1 (2026-10-19): a slightly stronger key from the camera's right.
    'DAY': dict(sunlight=1.3),
    # Element Animation's night shots: a readable world under pale moonlight,
    # so torches and lanterns (which light their surroundings) read as warm
    # pockets. Ordinary artist controls; the day/night light model is unchanged.
    'NIGHT': dict(moonlight=4., moon_color=(1.,1.,1.), ambient_light=.55, fill=.35),
    'DUNGEONS': dict(sunlight=1.1, ambient_light=.95, fill=.85, softness=2.,
                     penumbra=.45, sun_light_warmth=.12, shadow_color=(.85,.93,1.),
                     horizon_haze=.3, fog=.12, sun_world_haze=.15,
                     atmosphere_match_lighting=True),
    'VIBRANT': dict(sunlight=1.2, ambient_light=.85, fill=.8, softness=1.25,
                    penumbra=.3, daylight_balance=.65, sun_light_warmth=.045,
                    shadow_color=(.88,.95,1.),
                    exposure=-.05, contrast=0., saturation=1., temperature=0.,
                    horizon_haze=.3, fog=.16, sun_world_haze=.2,
                    atmosphere_match_lighting=True, surface_sheen=.08),
}


def mix(a, b, t):
    return tuple(x * (1-t) + y*t for x, y in zip(a, b))


def sky_color_mix(a, b, t):
    """Keep temporal color blends from passing through a washed-out gray."""
    color = mix(a,b,t)
    hue,saturation,value = colorsys.rgb_to_hsv(*color)
    minimum = min(colorsys.rgb_to_hsv(*a)[1],colorsys.rgb_to_hsv(*b)[1])
    # Partial recovery avoids a fast hue swing when warm and cool colors meet.
    saturation += .5*max(0,minimum-saturation)
    return colorsys.hsv_to_rgb(hue,saturation,value)


def smoothstep(low, high, value):
    t=max(0.,min(1.,(value-low)/(high-low)))
    return t*t*t*(t*(t*6-15)+10)


def time_state(hour, azimuth=0):
    """Continuous lighting state across midnight, dawn and dusk."""
    angle = (hour - 6) * math.tau / 24
    az = math.radians(azimuth)
    direction = Vector((math.cos(angle)*math.cos(az), math.cos(angle)*math.sin(az), math.sin(angle)))
    day = smoothstep(-.35,.45,direction.z)
    twilight = math.exp(-((direction.z/.32)**2))
    return direction, day, twilight


def golden_light(direction):
    """Broader than the sky's twilight band; light warms before the sky reddens."""
    return 1-smoothstep(.05,.65,abs(direction.z))


def surface_sun_direction(direction, trailer, azimuth=0):
    """Original trailer key: raised at dusk, crossed heading, lateral noon light.

    This is the former world-relative recipe, independent of the camera. The
    decorative sun and day/night energy still use the unmodified orbit.
    """
    result = direction.copy()
    if trailer:
        altitude = max(0., direction.z)
        result.z = altitude + .32 * (1-smoothstep(0, .65, altitude))
        angle = math.radians(35)
        result.x, result.y = (result.x*math.cos(angle)-result.y*math.sin(angle),
                              result.x*math.sin(angle)+result.y*math.cos(angle))
        heading = math.radians(azimuth+35)
        lateral = .65*smoothstep(.55, .95, direction.z)
        result.x += math.sin(heading)*lateral
        result.y -= math.cos(heading)*lateral
        result.normalize()
    return result


def default_light_direction(preset):
    # Night included: the moon is the key, aimed from the shot so faces read.
    return 'TRAILER' if preset in {'DAY', 'GOLDEN', 'SUNSET', 'NIGHT', 'DUNGEONS'} else 'SUN'


# Trailer lighting aims its key from the shot: from behind the scene camera,
# KEY_SIDE degrees to its right, and low. Faces and fronts are lit, and trees
# throw long shadows diagonally across the frame, onto the ground and up any
# slope or wall facing the camera. A new camera re-aims it; moving the same
# camera does not, so shadows never swim during a camera move.
KEY_SIDE = 45.
# Daylight's key comes from the camera's right side, 32 degrees up: block tops
# and right faces lit, shadows falling to the side like the trailers' (the
# user's pick of four test renders, "W1", 2026-10-19). From 45 degrees behind
# the camera the key front-lit everything flat.
DAY_KEY_SIDE = 90.
DAY_KEY_ELEVATION = 32.


def key_side(preset):
    return DAY_KEY_SIDE if preset == 'DAY' else KEY_SIDE


def key_elevation(altitude, preset=None):
    """Degrees above the horizon for the aimed key, from the sun's (or by
    night the moon's) real height: low enough for long shadows at any hour,
    lowest at golden hour. Daylight's key stays at DAY_KEY_ELEVATION while
    the sun is higher."""
    elevation = max(14., min(36., 15. + .35 * altitude))
    return min(elevation, DAY_KEY_ELEVATION) if preset == 'DAY' else elevation


# Character fill: trailers light characters apart from the set (Unreal's
# lighting channels), so a hero standing in a tree's shade still reads. A
# soft, shadowless fill from the camera side, linked to characters and mobs
# only; the world's light and shadows are untouched. A share of the key.
CHARACTER_FILL_ELEVATION = 25.      # degrees above the camera's heading
CHARACTER_FILL_ANGLE = 40.          # degrees: a broad, soft source
CHARACTER_FILL_COLOR = (.86, .92, 1.)
# Cycles occludes the sky fully under a canopy; EEVEE's screen-space sky
# already reaches a character there, which needs less fill to read the same.
CHARACTER_FILL_ENGINE = {'BLENDER_EEVEE': .35, 'CYCLES': 1.}
CHARACTER_FILL_RECEIVERS = 'MC Character fill receivers'


_character_names = {}  # scene pointer -> (object count, names of character meshes)


def characters(scene):
    """Meshes of imported characters and mobs: Bedrock entities and models,
    and Blockbench models.

    Every sky update asks; the walk over a large world is kept until objects
    are added or removed, or a remembered mesh is renamed or deleted.
    """
    count = len(bpy.data.objects)
    cached = _character_names.get(scene.as_pointer())
    if cached is None or cached[0] != count:
        found = []
        for obj in scene.objects:
            if obj.type != 'MESH':
                continue
            parent = obj.parent
            if (obj.get('mcbedrock_asset_type') == 'model_mesh' or
                    (parent is not None and parent.get('mcbedrock_asset_type') == 'bbmodel')):
                found.append(obj)
        _character_names[scene.as_pointer()] = (count, [obj.name for obj in found])
        return found
    found = [bpy.data.objects.get(name) for name in cached[1]]
    if None in found:
        _character_names.pop(scene.as_pointer(), None)
        return characters(scene)
    return found


def aim_character_fill(scene):
    """Point the character fill from behind the camera, a little above it."""
    fill = owned(scene, 'Character fill')
    camera = scene.camera
    if fill is None or camera is None:
        return
    basis = camera.matrix_world.to_3x3()
    heading = basis @ Vector((0, 0, -1))
    heading.z = 0
    if heading.length < 1e-6:
        heading = basis @ Vector((0, 1, 0))
        heading.z = 0
    if heading.length < 1e-6:
        return
    heading.normalize()
    lift = math.radians(CHARACTER_FILL_ELEVATION)
    travel = Vector((heading.x * math.cos(lift), heading.y * math.cos(lift), -math.sin(lift)))
    set_sky_value(fill, 'rotation_euler', travel.to_track_quat('-Z', 'Y').to_euler())


def character_fill(scene, energy):
    """Create or update the character fill and its receivers."""
    s = scene.mcbedrock_sky
    fill = owned(scene, 'Character fill')
    wanted = characters(scene)
    if fill is None:
        if not wanted or energy <= 0:
            return
        fill = light(scene, 'Character fill')
    data = fill.data
    for name, value in (('use_shadow', False), ('angle', math.radians(CHARACTER_FILL_ANGLE)),
                        ('specular_factor', 0.), ('volume_factor', 0.), ('color', CHARACTER_FILL_COLOR)):
        set_sky_value(data, name, value)
    set_sky_value(fill, 'visible_glossy', False)
    set_sky_value(fill, 'visible_transmission', False)
    receivers = fill.light_linking.receiver_collection
    if receivers is None:
        receivers = bpy.data.collections.new(CHARACTER_FILL_RECEIVERS)
        receivers[TAG] = s.owner
        fill.light_linking.receiver_collection = receivers
    keep = set(wanted)
    for obj in [o for o in receivers.objects if o not in keep]:
        receivers.objects.unlink(obj)
    for obj in wanted:
        if obj.name not in receivers.objects:
            receivers.objects.link(obj)
    # Never an unlinked light: with no characters it stays dark.
    set_sky_value(data, 'energy', float(energy) if wanted else 0.)
    set_sky_value(fill, 'hide_render', not wanted or energy <= 0)
    aim_character_fill(scene)


def camera_key_heading(camera, side=KEY_SIDE):
    """World heading (degrees) of a key behind the camera, `side` degrees to its right."""
    if camera is None:
        return None
    forward = camera.matrix_world.to_3x3() @ Vector((0, 0, -1))
    if forward.x * forward.x + forward.y * forward.y < 1e-8:
        forward = camera.matrix_world.to_3x3() @ Vector((0, 1, 0))
    return forward_key_heading(forward, side)


def forward_key_heading(forward, side=KEY_SIDE):
    """World heading (degrees) of a key behind a view looking along forward."""
    return (math.degrees(math.atan2(-forward.y, -forward.x)) + side) % 360


def view_forward(context):
    """Where the artist is looking: the 3D viewport's view, or None when it
    looks through the scene camera (or there is no viewport)."""
    space = getattr(context, 'space_data', None)
    view = space.region_3d if space is not None and space.type == 'VIEW_3D' else None
    if view is None and context.window is not None:
        areas = [a for a in context.window.screen.areas if a.type == 'VIEW_3D']
        if areas:
            view = max(areas, key=lambda a: a.width * a.height).spaces.active.region_3d
    if view is None or view.view_perspective == 'CAMERA':
        return None
    forward = view.view_rotation @ Vector((0, 0, -1))
    if forward.x * forward.x + forward.y * forward.y < 1e-8:
        forward = view.view_rotation @ Vector((0, 1, 0))
    return forward


def aimed_direction(settings, altitude):
    """Unit vector toward the aimed key, with the artist offsets applied."""
    heading = math.radians(settings.key_heading + settings.sun_heading_offset)
    elevation = math.radians(max(5., min(85., key_elevation(altitude, settings.preset) + settings.key_height_offset)))
    return Vector((math.cos(elevation) * math.cos(heading), math.cos(elevation) * math.sin(heading), math.sin(elevation)))


def aim_key(scene, forward=None):
    """Aim the trailer key from a view looking along forward, else from the
    scene camera (or the sun without one)."""
    s = scene.mcbedrock_sky
    heading = (forward_key_heading(forward, key_side(s.preset)) if forward is not None
               else camera_key_heading(scene.camera, key_side(s.preset)))
    if heading is None:
        # No camera yet: from the sun by day, the moon by night.
        sun = time_state(current_time(scene), s.azimuth)[0]
        body = sun if sun.z > -.05 else -sun
        heading = math.degrees(math.atan2(body.y, body.x)) % 360
    s['key_heading'] = heading
    s['key_aimed'] = True
    if s.collection is not None:
        s.collection['mc_key_camera'] = scene.camera.name if scene.camera else ''


def aim_sun_toward(scene, direction):
    """Point the rig's sun light along direction (toward the light) for the
    shot: through the trailer key's artist offsets, so it keeps following
    the camera when the shot changes. Elevation stays within the key's range."""
    _aim_toward(scene, direction)
    update(scene)


def _aim_toward(scene, direction):
    """aim_sun_toward without the update: settings are written as ID
    properties, so their update callbacks do not run the rig mid-update."""
    s = scene.mcbedrock_sky
    direction = Vector(direction).normalized()
    if s.light_direction_mode != 'TRAILER':
        s['light_direction_mode'] = 0
    # Offsets are relative to the key aimed from the current camera: aim it
    # first, or the next update re-aims it under them.
    if not s.key_aimed or s.collection is None or (
            scene.camera is not None and s.collection.get('mc_key_camera') != scene.camera.name):
        aim_key(scene)
    altitude = math.degrees(math.asin(max(-1., min(1., time_state(current_time(scene), s.azimuth)[0].z))))
    heading = math.degrees(math.atan2(direction.y, direction.x))
    elevation = math.degrees(math.asin(max(-1., min(1., direction.z))))
    s['sun_heading_offset'] = (heading - s.key_heading + 180.) % 360. - 180.
    s['key_height_offset'] = max(-45., min(45., elevation - key_elevation(altitude, s.preset)))


def hand_rotated(sun):
    """True when someone rotated the rig's sun light since the rig last aimed it."""
    written = sun.get('mc_rig_rotation')
    return written is not None and any(abs(a - b) > 1e-4 for a, b in zip(sun.rotation_euler, written))


def adopt_hand_aim(scene, sun):
    """A sun light rotated by hand (R in the viewport, or the Properties
    editor) keeps its direction: it becomes the key's offsets, so the rig
    stops putting it back and it still follows later camera cuts."""
    travel = sun.rotation_euler.to_matrix() @ Vector((0, 0, -1))
    _aim_toward(scene, -travel)


def star_orientation(hour,azimuth,offset):
    """Sample a fixed star field rotating on the sun/moon's celestial axis."""
    az=math.radians(azimuth)
    axis=Vector((math.sin(az),-math.cos(az),0))
    # Inverse motion is applied to lookup coordinates; midnight is the origin.
    daily=Quaternion(axis,-(hour%24)*math.tau/24)
    manual=Quaternion((0,0,1),math.radians(offset))
    return (manual @ daily).to_euler()


def golden_hour_band(direction):
    """Honey-colored low daylight, before the orange sunset takes over."""
    return smoothstep(.04,.20,direction.z)*(1-smoothstep(.30,.60,direction.z))


def cloud_light_tint(direction, day):
    """Warm clouds around sunrise/set, fading smoothly into cool night light."""
    warmth=golden_light(direction)*smoothstep(-.28,-.02,direction.z)
    return mix(mix((.55,.64,.9),(1,1,1),day),(1,.60,.38),.8*warmth)


def setup_eevee(scene):
    """One consistent, reversible quality setup, also migrated on existing skies."""
    s=scene.mcbedrock_sky
    version = s.collection.get('eevee_setup_version')
    if version == EEVEE_SETUP_VERSION:
        return
    settings={
        'use_raytracing':True, 'ray_tracing_method':'SCREEN',
        'use_fast_gi':True, 'fast_gi_method':'GLOBAL_ILLUMINATION',
        'fast_gi_quality':.5, 'fast_gi_ray_count':4, 'fast_gi_step_count':16,
        'fast_gi_distance':8., 'fast_gi_thickness_near':.5,
        'use_shadows':True, 'use_shadow_jitter_viewport':False,
        'taa_samples':min(32,scene.eevee.taa_samples) if scene.eevee.taa_samples else 32, 'use_taa_reprojection':True,
        'shadow_ray_count':4, 'shadow_step_count':8, 'use_volumetric_shadows':True,
        # Large imported worlds exhaust the default 512 MB pool with the
        # layered sun, dropping shadow tiles. Preserve any larger user budget.
        'shadow_pool_size':str(max(1024,int(scene.eevee.shadow_pool_size))),
        'direct_light_intensity':1., 'indirect_light_intensity':1.,
        'ray_tracing_options.screen_trace_quality':.5,
        # Rough terrain uses GI approximation; forcing screen traces for all
        # diffuse surfaces loses off-screen bounce and darkens sunset/night.
        'ray_tracing_options.trace_max_roughness':.5,
        'ray_tracing_options.use_backface_hit':True,
        'ray_tracing_options.use_denoise':True,
    }
    saved=json.loads(s.previous_eevee or '{}')
    if version == 4:
        # Change viewport cost only; keep existing render tuning and backups.
        settings = {key: value for key, value in settings.items()
                    if key in {'use_shadow_jitter_viewport', 'taa_samples'}}
    for path,value in settings.items():
        target=scene.eevee
        parts=path.split('.')
        for part in parts[:-1]:
            target=getattr(target,part)
        key=parts[-1]
        if hasattr(target,key):
            saved.setdefault(path,getattr(target,key))
            setattr(target,key,value)
    s.previous_eevee=json.dumps(saved)
    s.collection['eevee_setup_version']=EEVEE_SETUP_VERSION


CYCLES_BOUNCES = 2   # one real bounce of light, then Fast GI's sky approximation
# Fast GI's sky reach in Cycles, in blocks. At Blender's 10 a canopy blocks
# the whole sky above it and shade under trees goes near black; EEVEE's
# screen-space sky reaches there. 2 blocks (with the Cycles sky fill at .7)
# matched EEVEE's shadow levels on the stage and the user's riverside scene
# while shadow edges stay crisp. EEVEE ignores this setting.
CYCLES_AO_DISTANCE = 2.


def setup_cycles(scene):
    """Set reversible Cycles defaults once; preserve later user adjustments."""
    s = scene.mcbedrock_sky
    world = s.world
    if scene.render.engine == 'CYCLES' and world is not None and world.get('mc_cycles_ao') != 1:
        # The sky owns this world, so restoring the sky restores the user's own.
        if abs(world.light_settings.distance - 10.) < 1e-4:
            world.light_settings.distance = CYCLES_AO_DISTANCE
        world['mc_cycles_ao'] = 1
    version = s.get('cycles_setup_version', 0)
    if scene.render.engine != 'CYCLES' or version >= 3:
        return
    saved = json.loads(s.previous_cycles or '{}')
    if version < 1:
        depth = scene.cycles.transparent_max_bounces
        saved.setdefault('transparent_max_bounces', depth)
        # This is an upper bound, not a mandatory bounce count. Opaque surfaces
        # still stop rays immediately; general diffuse/glossy bounce limits stay put.
        scene.cycles.transparent_max_bounces = max(32, depth)
    if version < 2:
        saved.setdefault('use_fast_gi', scene.cycles.use_fast_gi)
        scene.cycles.use_fast_gi = True
    # At Blender's default of 1 AO bounce, the first bounce is already Fast
    # GI's sky approximation: no light bounces off sunlit ground into shade,
    # so shade is flat sky color. One real bounce gives the colored bounce
    # (green under canopies, warm under dirt overhangs) without the
    # multi-bounce gradients that wash out the sun's shadows. Only Blender's
    # default is raised; other tuning stays.
    for key in ('ao_bounces', 'ao_bounces_render'):
        if getattr(scene.cycles, key) == 1:
            saved.setdefault(key, 1)
            setattr(scene.cycles, key, CYCLES_BOUNCES)
    s.previous_cycles = json.dumps(saved)
    s['cycles_setup_version'] = 3


def current_time(scene):
    s = scene.mcbedrock_sky
    if not s.cycle_enabled:
        return s.time
    frames = max(1, round(s.cycle_seconds * scene.render.fps / scene.render.fps_base))
    return (s.time + 24 * (scene.frame_current_final - s.cycle_start) / frames) % 24


def toggle_cycle(scene):
    s = scene.mcbedrock_sky
    BUSY.add(scene.as_pointer())
    try:
        if s.cycle_enabled:
            hour = current_time(scene)
            s.cycle_enabled = False
            s.time = hour
            if scene.frame_end == s.cycle_end:
                scene.frame_end = s.previous_frame_end
            if scene.frame_start == int(s.cycle_start):
                scene.frame_start = s.previous_frame_start
        else:
            s.cycle_start = scene.frame_current_final
            s.previous_frame_end = scene.frame_end
            s.previous_frame_start = scene.frame_start
            s.cycle_end = int(s.cycle_start) + max(1, round(s.cycle_seconds * scene.render.fps / scene.render.fps_base)) - 1
            scene.frame_start = int(s.cycle_start)
            scene.frame_end = s.cycle_end
            s.cycle_enabled = True
    finally:
        BUSY.discard(scene.as_pointer())
    update(scene)


DAY_SOFTNESS = 3.     # degrees: one sun, crisp where a shadow starts and softer toward its end


def trailer_shadow_defaults(preset):
    # Keep dusk shade readable in either key-direction mode. Sky/fill energy
    # is independent of the key's direction and shadow-edge softness.
    if preset == 'NIGHT':
        return dict(ambient_light=.85, fill=.8, penumbra=.8, softness=5)
    if preset in {'GOLDEN', 'SUNSET'}:
        return dict(ambient_light=.85, fill=.8, penumbra=.6, softness=.6)
    if preset == 'DAY':
        # One sun of a few degrees, no soft copies: EEVEE and Cycles both give
        # it a sharp contact that softens with distance. The trailer recipe's
        # .6 degree key read as hard-edged shadows. Less sky fill than before
        # (.45, was .6), so shade has texture instead of the frame reading
        # bright and flat everywhere (W1).
        return dict(ambient_light=.45, fill=.45, penumbra=0., softness=DAY_SOFTNESS)
    return dict(ambient_light=.6, fill=.45, penumbra=.6, softness=.6)


def lighting_look(scene, look, *, set_exposure=True, overrides=None):
    """Lighting recipes leave the chosen time, artwork and atmosphere intact."""
    recipes = {
        'NATURAL': dict(trailer_lighting=False, light_direction_mode='SUN', sunlight=1, moonlight=1, ambient_light=.75, fill=.45,
                        penumbra=0, exposure=0, contrast=0, saturation=1, temperature=0, tint=0,shadow_lift=0,midtone_light=0,grade_amount=0),
        'TRAILER': dict(trailer_lighting=True, light_direction_mode=default_light_direction(scene.mcbedrock_sky.preset),
                        sunlight=1.1, moonlight=1, **trailer_shadow_defaults(scene.mcbedrock_sky.preset),
                        exposure=-.05, contrast=0, saturation=1.03, temperature=.03, tint=0,shadow_lift=0,midtone_light=0,
                        grade_amount=1,look_strength=.75,texture_vibrance=0,texture_brightness=0,foliage_color=0,surface_sheen=0),
        'GRADE_RESET': dict(exposure=0, contrast=0, saturation=1, temperature=0, tint=0,shadow_lift=0,midtone_light=0,grade_amount=0),
    }
    values = dict(recipes[look])
    values.update(overrides or {})
    if look=='GRADE_RESET':
        values.update({f'grade_{i}_{key}':(1 if key=='saturation' else 0) for i in range(5) for key in ('exposure','contrast','saturation','temperature','tint')})
    BUSY.add(scene.as_pointer())
    try:
        for key, value in values.items():
            setattr(scene.mcbedrock_sky, key, value)
    finally:
        BUSY.discard(scene.as_pointer())
    if set_exposure:
        scene.view_settings.exposure=values['exposure']
    if look == 'TRAILER' or (look == 'NATURAL' and atmosphere.enabled(scene)):
        scene.mcbedrock_sky.auto_materials=True
        sky_materials.prepare(scene)
    elif look == 'NATURAL':
        scene.mcbedrock_sky.auto_materials=False
        sky_materials.restore(scene)
    update(scene)


def node(tree, kind, name, **attrs):
    n = tree.nodes.new(kind)
    n.name = name
    for key, value in attrs.items():
        setattr(n, key, value)
    return n


def math_node(tree, op, a, b=0):
    n = node(tree, 'ShaderNodeMath', op, operation=op)
    for idx, value in enumerate((a, b)):
        if isinstance(value, (int, float)):
            n.inputs[idx].default_value = value
        else:
            tree.links.new(value, n.inputs[idx])
    return n.outputs[0]


def dot(tree, vector, name):
    n = node(tree, 'ShaderNodeVectorMath', name, operation='DOT_PRODUCT')
    tree.links.new(vector, n.inputs[0])
    return n


def gradient_coordinates(tree, height):
    """Spherical V: horizon=.5, zenith=1, independent of camera position."""
    bounded=math_node(tree,'MINIMUM',math_node(tree,'MAXIMUM',height,-1),1)
    latitude=math_node(tree,'ARCSINE',bounded)
    v=math_node(tree,'ADD',math_node(tree,'MULTIPLY',latitude,1/math.pi),.5)
    v.node.name='Sky gradient V'
    remap=node(tree,'ShaderNodeMapRange','Sky gradient range',clamp=True)
    tree.links.new(v,remap.inputs['Value'])
    remap.inputs['To Min'].default_value=0
    remap.inputs['To Max'].default_value=1
    power=node(tree,'ShaderNodeMath','Sky gradient power',operation='POWER')
    tree.links.new(remap.outputs[0],power.inputs[0])
    coordinates=node(tree,'ShaderNodeCombineXYZ','Sky gradient coordinates')
    tree.links.new(power.outputs[0],coordinates.inputs['X'])
    gradient=node(tree,'ShaderNodeTexGradient','Sky gradient texture',gradient_type='QUADRATIC')
    tree.links.new(coordinates.outputs[0],gradient.inputs['Vector'])
    return gradient.outputs['Fac']


def celestial(tree, vector, name, image, strength):
    x, y, front = [dot(tree, vector, name+s) for s in (' X', ' Y', ' Front')]
    size = node(tree,'ShaderNodeValue',name+' Size')
    size.outputs[0].default_value = .3
    uv = node(tree,'ShaderNodeCombineXYZ',name+' coordinates')
    # Project onto a plane so enlarged square sprites retain straight edges.
    depth=math_node(tree,'MAXIMUM',front.outputs['Value'],.0001)
    projected=[math_node(tree,'DIVIDE',axis.outputs['Value'],depth) for axis in (x,y)]
    for axis, component in zip(('X','Y'),projected):
        coordinate = math_node(tree,'ADD',math_node(tree,'MULTIPLY',math_node(tree,'DIVIDE',component,size.outputs[0]),.5),.5)
        tree.links.new(coordinate,uv.inputs[axis])
    texture = node(tree,'ShaderNodeTexImage',name+' artwork',interpolation='Closest',extension='CLIP')
    texture.image = image
    coords = uv.outputs[0]
    if name == 'Moon':
        scale = node(tree,'ShaderNodeVectorMath','Moon atlas scale',operation='MULTIPLY')
        tree.links.new(coords,scale.inputs[0])
        scale.inputs[1].default_value = (.25,.5,1)
        offset = node(tree,'ShaderNodeVectorMath','Moon phase',operation='ADD')
        tree.links.new(scale.outputs[0],offset.inputs[0])
        offset.inputs[1].default_value = (0,.5,0)
        coords = offset.outputs[0]
    tree.links.new(coords,texture.inputs['Vector'])
    extent = math_node(tree,'MAXIMUM',*(math_node(tree,'ABSOLUTE',axis) for axis in projected))
    edge = math_node(tree,'LESS_THAN',extent,size.outputs[0])
    mask = math_node(tree,'MULTIPLY',edge,math_node(tree,'GREATER_THAN',front.outputs['Value'],0))
    mask = math_node(tree,'MULTIPLY',mask,texture.outputs['Alpha'])
    # Some packs use opaque black around both sprites. That border must not
    # punch a star-free rectangle into the sky around the visible artwork.
    luminance = node(tree,'ShaderNodeRGBToBW',name+' artwork coverage')
    tree.links.new(texture.outputs['Color'],luminance.inputs[0])
    coverage = math_node(tree,'MINIMUM',math_node(tree,'MULTIPLY',luminance.outputs[0],8),1)
    mask = math_node(tree,'MULTIPLY',mask,coverage)
    visibility = node(tree,'ShaderNodeValue',name+' Visibility')
    visibility.outputs[0].default_value = 1
    mask = math_node(tree,'MULTIPLY',mask,visibility.outputs[0])
    strength_node = node(tree,'ShaderNodeMixRGB',name+' Brightness',blend_type='MULTIPLY')
    strength_node.inputs[0].default_value = 1
    strength_node.inputs[2].default_value = (strength,strength,strength,1)
    tree.links.new(texture.outputs['Color'],strength_node.inputs[1])
    if name == 'Sun':
        tint = node(tree,'ShaderNodeMixRGB','Sun soft ivory',blend_type='MIX')
        tint.inputs[0].default_value = .1
        tint.inputs[2].default_value = (1,1,.85,1)
        tree.links.new(texture.outputs['Color'],tint.inputs[1])
        tree.links.new(tint.outputs[0],strength_node.inputs[1])
        # Separable soft-box filtering follows the actual square emitter.
        exponent = node(tree,'ShaderNodeValue','Sun halo corners')
        spread = node(tree,'ShaderNodeValue','Sun halo spread')
        axes = [math_node(tree,'DIVIDE',math_node(tree,'ABSOLUTE',axis),size.outputs[0]) for axis in projected]
        width = math_node(tree,'MULTIPLY',spread.outputs[0],exponent.outputs[0])
        profiles = []
        for axis in axes:
            edges = []
            for edge in (-.25,.25):
                slope = math_node(tree,'DIVIDE',math_node(tree,'SUBTRACT',axis,edge),width)
                edges.append(math_node(tree,'DIVIDE',1,math_node(tree,'ADD',1,math_node(tree,'EXPONENT',math_node(tree,'MULTIPLY',slope,-1)))))
            profiles.append(math_node(tree,'SUBTRACT',edges[0],edges[1]))
        falloff = math_node(tree,'MULTIPLY',profiles[0],profiles[1])
        halo_strength = node(tree,'ShaderNodeValue','Sun halo strength')
        halo = math_node(tree,'MULTIPLY',falloff,halo_strength.outputs[0])
        halo = math_node(tree,'MULTIPLY',halo,math_node(tree,'GREATER_THAN',front.outputs['Value'],0))
        halo_color = node(tree,'ShaderNodeMixRGB','Sun halo',blend_type='MULTIPLY')
        halo_color.inputs[0].default_value = 1
        halo_color.inputs[1].default_value = (1,.78,.3,1)
        tree.links.new(halo,halo_color.inputs[2])
        wide = node(tree,'ShaderNodeMapRange','Sun wide falloff',clamp=True,interpolation_type='SMOOTHERSTEP')
        tree.links.new(front.outputs['Value'],wide.inputs['Value'])
        wide.inputs['From Max'].default_value = 1
        width = math_node(tree,'SUBTRACT',1,wide.inputs['From Min'].default_value)
        width.node.name = 'Sun wide width'
        separation = math_node(tree,'SUBTRACT',1,front.outputs['Value'])
        falloff = math_node(tree,'EXPONENT',math_node(tree,'MULTIPLY',math_node(tree,'DIVIDE',separation,width),-1.2))
        falloff = math_node(tree,'MULTIPLY',falloff,wide.outputs['Result'])
        amount = node(tree,'ShaderNodeValue','Sun wide strength')
        wide_color = node(tree,'ShaderNodeMixRGB','Sun wide glow',blend_type='MULTIPLY')
        wide_color.inputs[0].default_value = 1
        tree.links.new(math_node(tree,'MULTIPLY',falloff,amount.outputs[0]),wide_color.inputs[2])
        combined = node(tree,'ShaderNodeMixRGB','Sun combined glow',blend_type='ADD')
        combined.inputs[0].default_value = 1
        tree.links.new(halo_color.outputs[0],combined.inputs[1])
        tree.links.new(wide_color.outputs[0],combined.inputs[2])
    return mask,strength_node.outputs[0]


def star_mask(tree, vector, altitude):
    """Random star positions with square silhouettes on local tangent planes."""
    mapping=node(tree,'ShaderNodeMapping','Star position',vector_type='VECTOR')
    tree.links.new(vector,mapping.inputs['Vector'])
    cells=node(tree,'ShaderNodeTexVoronoi','Star cells')
    cells.voronoi_dimensions='3D'
    cells.feature='F1'
    cells.distance='EUCLIDEAN'
    cells.inputs['Scale'].default_value=16
    tree.links.new(mapping.outputs['Vector'],cells.inputs['Vector'])
    def vector_math(name,operation,a,b=None):
        n=node(tree,'ShaderNodeVectorMath',name,operation=operation)
        tree.links.new(a,n.inputs[0])
        if b is not None:
            if isinstance(b,tuple):n.inputs[1].default_value=b
            else:tree.links.new(b,n.inputs[1])
        return n.outputs['Value' if operation=='DOT_PRODUCT' else 'Vector']
    center=vector_math('Star direction','NORMALIZE',cells.outputs['Position'])
    horizontal=vector_math('Star tangent','CROSS_PRODUCT',center,(0,0,1))
    horizontal=vector_math('Star tangent unit','NORMALIZE',horizontal)
    vertical=vector_math('Star up','CROSS_PRODUCT',center,horizontal)
    delta=vector_math('Star local offset','SUBTRACT',mapping.outputs['Vector'],center)
    x=math_node(tree,'ABSOLUTE',vector_math('Star horizontal distance','DOT_PRODUCT',delta,horizontal))
    y=math_node(tree,'ABSOLUTE',vector_math('Star vertical distance','DOT_PRODUCT',delta,vertical))
    square=math_node(tree,'MAXIMUM',x,y)
    square.node.name='Star square distance'
    edge=node(tree,'ShaderNodeMapRange','Star square edge',clamp=True)
    for key,value in [('From Min',.0015),('From Max',.0018),('To Min',1),('To Max',0)]:
        edge.inputs[key].default_value=value
    tree.links.new(square,edge.inputs['Value'])
    random=node(tree,'ShaderNodeSeparateColor','Star selection')
    tree.links.new(cells.outputs['Color'],random.inputs[0])
    amount=node(tree,'ShaderNodeMath','Star amount',operation='LESS_THAN')
    tree.links.new(random.outputs[0],amount.inputs[0])
    horizon=node(tree,'ShaderNodeMapRange','Star horizon fade',clamp=True,interpolation_type='SMOOTHERSTEP')
    horizon.inputs['From Min'].default_value=0
    horizon.inputs['From Max'].default_value=.12
    tree.links.new(altitude,horizon.inputs['Value'])
    strength=node(tree,'ShaderNodeValue','Star strength')
    mask=math_node(tree,'MULTIPLY',edge.outputs[0],amount.outputs[0])
    mask=math_node(tree,'MULTIPLY',mask,horizon.outputs[0])
    return math_node(tree,'MULTIPLY',mask,strength.outputs[0])


def build_world(scene, images):
    s = scene.mcbedrock_sky
    upgrade_builtin_images(images,s.owner)
    world = bpy.data.worlds.new('Sparkle Sky · '+scene.name)
    world[TAG] = s.owner
    world['mc_sky_shader_version'] = SKY_SHADER_VERSION
    world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()
    coords = node(tree, 'ShaderNodeTexCoord', 'Sky direction')
    outward = node(tree, 'ShaderNodeVectorMath', 'Outward sky', operation='SCALE')
    tree.links.new(coords.outputs['Normal'], outward.inputs[0])
    outward.inputs['Scale'].default_value = -1
    vector = outward.outputs[0]
    sep = node(tree, 'ShaderNodeSeparateXYZ', 'Altitude')
    tree.links.new(vector, sep.inputs[0])
    shifted = node(tree,'ShaderNodeMath','Horizon position',operation='SUBTRACT')
    tree.links.new(sep.outputs['Z'],shifted.inputs[0])
    gradient_coordinates(tree,shifted.outputs[0])
    elevation = math_node(tree,'MAXIMUM',shifted.outputs[0],0)
    height_curve = node(tree,'ShaderNodeMath','Sky horizon curve')
    height_curve.operation = 'POWER'
    tree.links.new(elevation,height_curve.inputs[0])
    height_curve.inputs[1].default_value = 1
    ramp = node(tree, 'ShaderNodeValToRGB', 'Sky colors')
    ramp.color_ramp.elements[0].position = 0
    ramp.color_ramp.elements[1].position = 1
    for position in (.08,.22,.7):
        ramp.color_ramp.elements.new(position)
    # Matching endpoint anchors let the spline reach the actual palette colors
    # without averaging orange out of the horizon or washing out the upper blue.
    ramp.color_ramp.interpolation = 'B_SPLINE'
    tree.links.new(height_curve.outputs[0], ramp.inputs[0])
    background = node(tree, 'ShaderNodeBackground', 'Ambient')
    tree.links.new(ramp.outputs['Color'], background.inputs['Color'])
    # Skylight comes from a dome, not a flat color: brighter and bluer
    # overhead, pale at the horizon and a dim warm bounce from the ground.
    # Tops, sides and undersides of blocks each read differently, and in
    # shadow the cool sky is what lights them, as in the trailers.
    up = node(tree, 'ShaderNodeMapRange', 'Ambient dome sky', clamp=True, interpolation_type='SMOOTHSTEP')
    tree.links.new(sep.outputs['Z'], up.inputs['Value'])
    up.inputs['From Min'].default_value, up.inputs['From Max'].default_value = 0., .8
    below = node(tree, 'ShaderNodeMapRange', 'Ambient dome ground', clamp=True, interpolation_type='SMOOTHSTEP')
    tree.links.new(sep.outputs['Z'], below.inputs['Value'])
    below.inputs['From Min'].default_value, below.inputs['From Max'].default_value = -.25, .02
    horizon_tone = node(tree, 'ShaderNodeRGB', 'Ambient dome horizon')
    zenith_tone = node(tree, 'ShaderNodeRGB', 'Ambient dome zenith')
    ground_tone = node(tree, 'ShaderNodeRGB', 'Ambient dome ground color')
    sky_tone = node(tree, 'ShaderNodeMixRGB', 'Ambient dome upper')
    tree.links.new(up.outputs[0], sky_tone.inputs[0])
    tree.links.new(horizon_tone.outputs[0], sky_tone.inputs[1])
    tree.links.new(zenith_tone.outputs[0], sky_tone.inputs[2])
    dome = node(tree, 'ShaderNodeMixRGB', 'Ambient dome shape')
    tree.links.new(below.outputs[0], dome.inputs[0])
    tree.links.new(ground_tone.outputs[0], dome.inputs[1])
    tree.links.new(sky_tone.outputs[0], dome.inputs[2])
    lit = node(tree, 'ShaderNodeMixRGB', 'Ambient dome', blend_type='MULTIPLY')
    lit.inputs[0].default_value = 1
    tree.links.new(dome.outputs[0], lit.inputs[2])
    tree.links.new(lit.outputs[0], background.inputs['Color'])
    # The palette is authored for display, like the preset artwork. Compensate
    # daylight before LOOCS so its blue does not become a dark, muted blue.
    palette=node(tree,'ShaderNodeGroup','Sky colour compensation')
    palette.node_tree=lighting_pipeline.compensation(scene)
    tree.links.new(ramp.outputs['Color'],palette.inputs['Color'])
    visible = palette.outputs['Color']
    stars=star_mask(tree,vector,sep.outputs['Z'])
    rays = node(tree, 'ShaderNodeLightPath', 'Sky visibility')
    for name, image, strength in [('Sun',images[0],SUN_RADIANCE),('Moon',images[1],2)]:
        before_disk = visible
        mask, disk = celestial(tree, vector, name, image, strength)
        stars=math_node(tree,'MULTIPLY',stars,math_node(tree,'SUBTRACT',1,mask))
        if name == 'Sun':
            halo = node(tree,'ShaderNodeMixRGB','Sun atmosphere',blend_type='ADD')
            halo.inputs[0].default_value = 1
            tree.links.new(visible,halo.inputs[1])
            tree.links.new(tree.nodes['Sun combined glow'].outputs[0],halo.inputs[2])
            visible = halo.outputs[0]
        layer = node(tree, 'ShaderNodeMixRGB', name+' Layer',blend_type='MIX' if name=='Sun' else 'ADD')
        tree.links.new(mask, layer.inputs[0])
        tree.links.new(visible, layer.inputs[1])
        tree.links.new(disk, layer.inputs[2])
        visible = layer.outputs[0]
        if name == 'Sun':
            # The aligned reflection light supplies the white solar highlight.
            # Decorative texture bands and their halo belong only in the sky.
            artwork = node(tree, 'ShaderNodeMixRGB', 'Sun camera artwork')
            tree.links.new(rays.outputs['Is Camera Ray'], artwork.inputs[0])
            tree.links.new(before_disk, artwork.inputs[1])
            tree.links.new(visible, artwork.inputs[2])
            visible = artwork.outputs[0]
    star_layer=node(tree,'ShaderNodeMixRGB','Night stars',blend_type='ADD')
    star_layer.inputs[0].default_value=1
    tree.links.new(visible,star_layer.inputs[1])
    tree.links.new(stars,star_layer.inputs[2])
    visible=star_layer.outputs[0]
    # A plain backdrop, like the pink or blue of character key art. Only the
    # camera sees it: reflections and lighting keep following the sky and the
    # time of day.
    plain_color = node(tree, 'ShaderNodeRGB', 'Plain background color')
    plain_amount = node(tree, 'ShaderNodeValue', 'Plain background')
    plain_amount.outputs[0].default_value = 0
    plain = node(tree, 'ShaderNodeMixRGB', 'Plain background mix')
    tree.links.new(math_node(tree, 'MULTIPLY', plain_amount.outputs[0], rays.outputs['Is Camera Ray']), plain.inputs[0])
    tree.links.new(visible, plain.inputs[1])
    tree.links.new(plain_color.outputs[0], plain.inputs[2])
    visible=plain.outputs[0]
    display = node(tree, 'ShaderNodeBackground', 'Visible sky')
    # Standard has no highlight shoulder. Limit only the decorative sky in
    # that view, preserving RGB ratios rather than clipping channels to yellow.
    # Native ChromaLuma/AgX continue to receive the original HDR background.
    radiance=node(tree,'ShaderNodeValue','Sky display radiance')
    radiance.outputs[0].default_value=1
    g=lighting_pipeline.Graph(tree)
    channels=g.split(visible)
    peak=g.op('MULTIPLY',g.op('MAXIMUM',g.op('MAXIMUM',channels[0],channels[1]),channels[2]),radiance.outputs[0])
    excess=g.op('MAXIMUM',g.op('SUBTRACT',peak,.5),0)
    shoulder=g.op('ADD',.5,g.op('DIVIDE',g.op('MULTIPLY',.4,excess),g.op('ADD',.4,excess)))
    scale=g.op('MINIMUM',1,g.op('DIVIDE',shoulder,g.op('MAXIMUM',peak,1e-8)))
    bounded=g.combine([g.op('MULTIPLY',c,scale) for c in channels])
    fallback=node(tree,'ShaderNodeMixRGB','Standard sky highlights')
    fallback.inputs[0].default_value=0
    tree.links.new(visible,fallback.inputs[1])
    tree.links.new(bounded,fallback.inputs[2])
    tree.links.new(fallback.outputs[0], display.inputs['Color'])
    # Keep the bright artwork out of environment sampling; real lights cast shadows.
    seen = math_node(tree, 'MAXIMUM', rays.outputs['Is Camera Ray'], rays.outputs['Is Glossy Ray'])
    shader = node(tree, 'ShaderNodeMixShader', 'Background and ambient')
    tree.links.new(seen, shader.inputs[0])
    tree.links.new(background.outputs[0], shader.inputs[1])
    tree.links.new(display.outputs[0], shader.inputs[2])
    out = node(tree, 'ShaderNodeOutputWorld', 'World')
    tree.links.new(shader.outputs[0], out.inputs['Surface'])
    scene.world = world
    s.world = world


def owned(scene, role):
    s = scene.mcbedrock_sky
    return next((o for o in s.collection.objects if o.get('role') == role), None) if s.collection else None


def light(scene, role):
    s = scene.mcbedrock_sky
    data = bpy.data.lights.new('MC '+role, 'SUN')
    if hasattr(data,'use_shadow_jitter'):
        data.use_shadow_jitter = True
    data[TAG] = s.owner
    obj = bpy.data.objects.new('MC '+role, data)
    obj[TAG], obj['role'] = s.owner, role
    s.collection.objects.link(obj)
    return obj


def cloud_mesh(amount, tile_origin=(0,0), *, preset='CLASSIC', density='SPARSE', size=1.):
    if preset=='DUNGEONS':
        heights=cloud_pattern.dungeons_cells(tile_origin,density)
        verts,faces,fade_heights=[],[],[]
        cell=cloud_pattern.DUNGEONS_UNIT*size
        def face(points,height):
            start=len(verts)
            verts.extend((x*cell,y*cell,z) for x,y,z in points)
            fade_heights.extend(z/height for _,_,z in points)
            faces.append(tuple(range(start,start+len(points))))
        for (x,y),h in sorted(heights.items()):
            face(((x,y,0),(x,y+1,0),(x+1,y+1,0),(x+1,y,0)),h)
            face(((x,y,h),(x+1,y,h),(x+1,y+1,h),(x,y+1,h)),h)
            for neighbor,edge in (((x,y-1),((x,y),(x+1,y))),
                                  ((x+1,y),((x+1,y),(x+1,y+1))),
                                  ((x,y+1),((x+1,y+1),(x,y+1))),
                                  ((x-1,y),((x,y+1),(x,y)))):
                low=heights.get(neighbor,0)
                if low<h:
                    a,b=edge
                    face(((*a,low),(*b,low),(*b,h),(*a,h)),h)
        mesh=bpy.data.meshes.new('MC Dungeons II cloud shapes')
        mesh.from_pydata(verts,[],faces)
        mesh.update()
        attribute=mesh.attributes.new('mc_dungeons_cloud_height','FLOAT','POINT')
        attribute.data.foreach_set('value',fade_heights)
        return mesh
    occupied=cloud_pattern.window_cells(amount,tile_origin,preset)
    verts, faces = [], []
    for x, y in sorted(occupied):
        i = len(verts)
        verts.extend([(px*cloud_pattern.CLASSIC_CELL,py*cloud_pattern.CLASSIC_CELL,z)
                      for z in (0,1) for px,py in ((x,y),(x+1,y),(x+1,y+1),(x,y+1))])
        faces.extend([tuple(i+j for j in f) for f in [(3,2,1,0),(4,5,6,7)]])
        for neighbor, f in [((x,y-1),(0,1,5,4)),((x+1,y),(1,2,6,5)),((x,y+1),(2,3,7,6)),((x-1,y),(3,0,4,7))]:
            if neighbor not in occupied:
                faces.append(tuple(i+j for j in f))
    mesh = bpy.data.meshes.new('MC Cloud shapes')
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return mesh


def cloud_shading(material, dimensional=False):
    """Share broad-face lighting across the silhouette without flattening geometry."""
    tree = material.node_tree
    shader = tree.nodes.get('Principled BSDF')
    set_sky_value(shader.inputs['Specular IOR Level'], 'default_value', 0)
    if 'Cloud uniform normal' not in tree.nodes:
        geometry = node(tree,'ShaderNodeNewGeometry','Cloud viewing direction')
        separate = node(tree,'ShaderNodeSeparateXYZ','Cloud viewing height')
        tree.links.new(geometry.outputs['Incoming'],separate.inputs[0])
        upward = math_node(tree,'GREATER_THAN',separate.outputs['Z'],0)
        sign = math_node(tree,'SUBTRACT',math_node(tree,'MULTIPLY',upward,2),1)
        normal = node(tree,'ShaderNodeCombineXYZ','Cloud uniform normal')
        tree.links.new(sign,normal.inputs['Z'])
    if not dimensional and not shader.inputs['Normal'].is_linked:
        tree.links.new(tree.nodes['Cloud uniform normal'].outputs[0],shader.inputs['Normal'])
    if material.get('mc_cloud_dimensional') != dimensional:
        for name in ('Normal', 'Base Color', 'Emission Color'):
            for link in list(shader.inputs[name].links):
                tree.links.remove(link)
        if dimensional:
            if 'Cloud shape tint' not in tree.nodes:
                geometry = node(tree,'ShaderNodeNewGeometry','Cloud shape normal')
                separate = node(tree,'ShaderNodeSeparateXYZ','Cloud face height')
                tree.links.new(geometry.outputs['Normal'], separate.inputs[0])
                height = math_node(tree,'ADD',math_node(tree,'MULTIPLY',separate.outputs['Z'],.5),.5)
                tint = node(tree,'ShaderNodeMixRGB','Cloud shape tint',blend_type='MIX')
                tint.inputs[1].default_value = (.48,.63,.78,1)
                tint.inputs[2].default_value = (1,1,1,1)
                tree.links.new(height,tint.inputs[0])
                color = node(tree,'ShaderNodeMixRGB','Cloud face color',blend_type='MULTIPLY')
                color.inputs[0].default_value = 1
                tree.links.new(tint.outputs[0],color.inputs[2])
            color = tree.nodes['Cloud face color']
            tree.links.new(color.outputs[0],shader.inputs['Base Color'])
            tree.links.new(color.outputs[0],shader.inputs['Emission Color'])
        else:
            tree.links.new(tree.nodes['Cloud uniform normal'].outputs[0],shader.inputs['Normal'])
        material['mc_cloud_dimensional'] = dimensional
    if 'Cloud hidden faces' not in tree.nodes:
        # Material backface culling alone does not remove Cycles back faces.
        # During distance fading, seeing both sides of the shell doubles alpha
        # and exposes a second silhouette through the camera-facing surface.
        geometry=node(tree,'ShaderNodeNewGeometry','Cloud face visibility')
        hidden=node(tree,'ShaderNodeMath','Cloud hidden faces',operation='MAXIMUM')
        tree.links.new(geometry.outputs['Backfacing'],hidden.inputs[0])
        tree.links.new(tree.nodes['Cloud shadow visibility'].outputs['Is Shadow Ray'],hidden.inputs[1])
        tree.links.new(hidden.outputs[0],tree.nodes['Cloud surface'].inputs[0])


def cloud_material(scene):
    m = bpy.data.materials.new('MC Cloud material')
    m[TAG] = scene.mcbedrock_sky.owner
    m.use_nodes = True
    m.use_backface_culling = True
    if hasattr(m, 'surface_render_method'):
        m.surface_render_method = 'DITHERED'
    t = m.node_tree
    shader = t.nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (.92,.95,1,1)
    shader.inputs['Roughness'].default_value = 1
    shader.inputs['Emission Color'].default_value = (.25,.3,.4,1)
    shader.inputs['Emission Strength'].default_value = .08
    camera = node(t, 'ShaderNodeCameraData', 'Distance')
    fade = node(t, 'ShaderNodeMapRange', 'Distant cloud fade', clamp=True)
    fade.inputs['From Min'].default_value = 800
    fade.inputs['From Max'].default_value = 1450
    fade.inputs['To Min'].default_value = 1
    fade.inputs['To Max'].default_value = 0
    t.links.new(camera.outputs['View Distance'], fade.inputs['Value'])
    t.links.new(fade.outputs[0], shader.inputs['Alpha'])
    # Trailer clouds decorate the sky without randomly putting the subject in shade.
    rays = node(t,'ShaderNodeLightPath','Cloud shadow visibility')
    transparent = node(t,'ShaderNodeBsdfTransparent','No cloud shadows')
    mix_shader = node(t,'ShaderNodeMixShader','Cloud surface')
    t.links.new(rays.outputs['Is Shadow Ray'],mix_shader.inputs[0])
    t.links.new(shader.outputs[0],mix_shader.inputs[1])
    t.links.new(transparent.outputs[0],mix_shader.inputs[2])
    t.links.new(mix_shader.outputs[0],t.nodes.get('Material Output').inputs['Surface'])
    cloud_shading(m)
    return m


# Nodes the rig writes on every update and camera move. A renamed or deleted
# one gets the clouds a fresh material instead of failing each time.
CLOUD_NODES = ('Principled BSDF', 'Material Output', 'Distant cloud fade', 'Cloud shadow visibility', 'Cloud surface')
DIMENSIONAL_CLOUD_NODES = ('Cloud face color', 'Cloud shape tint')


def cloud_material_intact(material):
    tree = material.node_tree if material is not None else None
    if tree is None:
        return False
    names = CLOUD_NODES + (DIMENSIONAL_CLOUD_NODES if material.get('mc_cloud_dimensional') else ())
    return all(tree.nodes.get(name) is not None for name in names)


def cloud_object_material(scene, cloud):
    """The clouds' material, replaced by a fresh one when it was removed or a
    node the rig writes was renamed or deleted."""
    slots = cloud.data.materials
    material = slots[0] if len(slots) else None
    if cloud_material_intact(material):
        return material
    fresh = cloud_material(scene)
    if len(slots):
        slots[0] = fresh
    else:
        slots.append(fresh)
    return fresh


def apply_cloud_preset(scene, preset):
    """Replace only cloud settings; the owned rig can still be restored normally."""
    if preset not in cloud_pattern.PRESETS:
        raise ValueError('Unknown cloud preset')
    s = scene.mcbedrock_sky
    pointer = scene.as_pointer()
    was_busy = pointer in BUSY
    BUSY.add(pointer)
    try:
        s.cloud_preset = preset
        for key, value in cloud_pattern.PRESETS[preset].items():
            if key != 'label':
                setattr(s, key, value)
    finally:
        if not was_busy:
            BUSY.discard(pointer)
    if not was_busy:
        update(scene)


def cloud_anchor(scene):
    s = scene.mcbedrock_sky
    origin = scene.camera.matrix_world.translation if scene.camera else Vector((0,0,0))
    seconds = (scene.frame_current + scene.frame_subframe - scene.frame_start) / (scene.render.fps/scene.render.fps_base)
    travel = seconds * s.cloud_speed if s.moving else 0
    angle = math.radians(s.cloud_direction)
    offsets = [travel*math.cos(angle), travel*math.sin(angle)]
    tile_size=cloud_pattern.TILE_SIZE*s.dungeons_cloud_scale if s.cloud_preset=='DUNGEONS' else cloud_pattern.CLASSIC_TILE
    tile_origin = tuple(math.floor((origin[i]-offsets[i])/tile_size) for i in (0,1))
    return tile_origin, Vector((tile_origin[0]*tile_size+offsets[0],tile_origin[1]*tile_size+offsets[1],0))


def rebuild_clouds(scene):
    s = scene.mcbedrock_sky
    old = [o for o in s.collection.objects if o.get('role') == 'Cloud']
    material = cloud_object_material(scene, old[0]) if old else cloud_material(scene)
    meshes = {o.data for o in old}
    # Keep one stable object while sliders or camera tile crossings replace its mesh.
    cloud = old[0] if old else None
    previous_layout=s.collection.get('cloud_layout_version')
    if previous_layout is not None and previous_layout<19 and s.cloud_preset=='DUNGEONS':
        # The former image layer left these controls unused. Upgrade its stock
        # values, while retaining actual artist edits and the existing opacity.
        if s.cloud_thickness in (3.,5.):s['cloud_thickness']=32.
        if all(abs(a-b)<1e-5 for a,b in zip(s.cloud_color,(.62,.82,1.))):
            s['cloud_color']=(.92,.95,1.)
    if previous_layout is not None and previous_layout<20 and s.cloud_preset=='DUNGEONS':
        # Layout 20 places the game's own flat cloud slabs at 8 blocks per
        # game block, so the untouched 32-block cluster thickness becomes 8.
        if abs(s.cloud_thickness-32.)<1e-5:
            s['cloud_thickness']=8.
    if previous_layout in (6,8,9,10,11,12,13):
        # Undo only the replacement system's stock dimensions and drift.
        # A manually moved layer or edited setting keeps the user's value.
        if abs(s.cloud_height-192.33)<.001 and (cloud is None or abs(cloud.location.z-s.cloud_height)<.001):
            s['cloud_height']=256.
        if previous_layout in (12,13):
            if s.clouds==1:
                s['clouds']=PRESETS.get(s.preset,PRESETS['DAY'])['clouds']
            if s.cloud_direction==180 and abs(s.cloud_speed-.6)<.0001:
                s['cloud_direction']=0.
                s['moving']=False
        for key,previous,value in (('cloud_thickness',4,5.),('cloud_speed',.6,1.5)):
            if abs(getattr(s,key)-previous)<.0001:s[key]=value
    for obj in old[1:]:
        bpy.data.objects.remove(obj, do_unlink=True)
    tile_origin, _ = cloud_anchor(scene)
    image=s.cloud_image
    s.cloud_image=None
    if image and image.get(TAG)==s.owner and not image.users:
        bpy.data.images.remove(image)
    mesh = cloud_mesh(s.clouds,tile_origin,preset=s.cloud_preset,
                      density=s.dungeons_cloud_density,size=s.dungeons_cloud_scale)
    mesh[TAG] = s.owner
    mesh.materials.append(material)
    if not old:
        cloud = bpy.data.objects.new('MC Clouds', mesh)
        s.collection.objects.link(cloud)
        cloud.location.z = s.cloud_height
        cloud['cloud_position_version'] = 2
        cloud['cloud_applied_height'] = s.cloud_height
    else:
        cloud.data = mesh
    cloud[TAG], cloud['role'] = s.owner, 'Cloud'
    for key in ('tile_x', 'tile_y'):
        if key in cloud:
            del cloud[key]
    for name in ('MC Cloud tiles X','MC Cloud tiles Y'):
        modifier = cloud.modifiers.get(name)
        if modifier:
            cloud.modifiers.remove(modifier)
    cloud['cloud_tile_origin'] = tile_origin
    if hasattr(cloud, 'visible_shadow'):
        cloud.visible_shadow = False
    for mesh in meshes:
        if not mesh.users:
            bpy.data.meshes.remove(mesh)
    s.collection['cloud_amount'] = s.clouds
    if previous_layout==21 and s.cloud_preset=='CLASSIC':
        # Layout 22 returns Classic clouds to Sparkle's scale (32-block cells,
        # at 256, 5 thick) after layout 21's Minecraft-sized ones. Only the
        # untouched layout 21 values move back.
        if abs(s.cloud_height-192.)<1e-5:
            s['cloud_height']=256.
        if abs(s.cloud_thickness-4.)<1e-5:
            s['cloud_thickness']=5.
    s.collection['cloud_preset'] = s.cloud_preset
    s.collection['cloud_layout_version'] = CLOUD_LAYOUT_VERSION
    s.collection['cloud_density'] = s.dungeons_cloud_density
    s.collection['cloud_size'] = s.dungeons_cloud_scale


def position_clouds(scene):
    s = scene.mcbedrock_sky
    if not s.active or not s.collection:
        return
    camera = scene.camera
    origin = camera.matrix_world.translation if camera else Vector((0,0,0))
    fog = owned(scene,'Fog')
    if fog:
        if (fog.location-origin).length_squared > 1e-8:
            fog.location = origin
        extent = max(10,min(s.fog_distance*4,camera.data.clip_end if camera else 1000))
        if abs(fog.scale.x-extent)>1e-5:
            fog.scale = (extent,extent,extent)
    tile_origin, location = cloud_anchor(scene)
    cloud = owned(scene,'Cloud')
    if cloud:
        if tuple(cloud.get('cloud_tile_origin',())) != tile_origin:
            rebuild_clouds(scene)
        if cloud.get('cloud_position_version') != 2:
            recenter=cloud.matrix_basis.to_3x3() @ Vector((3*512,3*512,0))
            cloud.location += recenter-location
            cloud['cloud_position_version'] = 2
            cloud['cloud_applied_height'] = s.cloud_height
        # Automatic drift/camera tiling uses delta transforms; ordinary Move
        # transforms remain under the user's control across all sky updates.
        if (cloud.delta_location-location).length_squared > 1e-8:
            cloud.delta_location = location
        if abs(cloud.get('cloud_applied_height',s.cloud_height)-s.cloud_height)>1e-5:
            cloud.location.z = s.cloud_height
        elif abs(cloud.location.z-s.cloud_height)>1e-5:
            pointer=scene.as_pointer()
            was_busy=pointer in BUSY
            BUSY.add(pointer)
            try:
                s.cloud_height=cloud.location.z
            finally:
                if not was_busy:
                    BUSY.discard(pointer)
        if cloud.get('cloud_applied_height') != s.cloud_height:
            cloud['cloud_applied_height'] = s.cloud_height
        if abs(cloud.scale.z-s.cloud_thickness) > 1e-5:
            cloud.scale.z = s.cloud_thickness
        fade = cloud_object_material(scene, cloud).node_tree.nodes['Distant cloud fade']
        distance = camera.data.clip_end if camera else 1000
        window_scale = min(1., s.dungeons_cloud_scale) if s.cloud_preset == 'DUNGEONS' else 1.
        for key, value in [('From Min',min(800*window_scale,distance*.65)),('From Max',min(1450*window_scale,distance*.9))]:
            if abs(fade.inputs[key].default_value-value)>1e-4:
                fade.inputs[key].default_value=value


def update_fog(scene):
    s = scene.mcbedrock_sky
    fog = owned(scene,'Fog')
    if s.terrain_fog:
        if fog:fog.hide_render=fog.hide_viewport=True
        return
    if fog is None and s.fog > 0:
        mesh = bpy.data.meshes.new('MC Atmosphere')
        mesh[TAG] = s.owner
        mesh.from_pydata([(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)],[],[(3,2,1,0),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)])
        material = bpy.data.materials.new('MC Fog')
        material[TAG] = s.owner
        material.use_nodes = True
        tree = material.node_tree
        tree.nodes.clear()
        scatter = node(tree,'ShaderNodeVolumeScatter','Fog')
        scatter.inputs['Color'].default_value = (.8,.87,1,1)
        scatter.inputs['Anisotropy'].default_value = .2
        output = node(tree,'ShaderNodeOutputMaterial','Output')
        tree.links.new(scatter.outputs[0],output.inputs['Volume'])
        mesh.materials.append(material)
        fog = bpy.data.objects.new('MC Atmosphere',mesh)
        fog[TAG],fog['role'] = s.owner,'Fog'
        fog.display_type = 'WIRE'
        s.collection.objects.link(fog)
    if fog:
        fog.hide_render = fog.hide_viewport = s.fog == 0
        tree = fog.data.materials[0].node_tree
        scatter = tree.nodes['Fog']
        lighting_pipeline.set_value(scatter.inputs['Anisotropy'], 'default_value', s.volume_anisotropy)
        lighting_pipeline.set_value(scatter.inputs['Color'], 'default_value', (*s.fog_color,1))
        if s.volume_height_fog:
            if not tree.nodes.get('Height density'):
                g = lighting_pipeline.Graph(tree)
                position = tree.nodes.new('ShaderNodeNewGeometry')
                separate = tree.nodes.new('ShaderNodeSeparateXYZ')
                tree.links.new(position.outputs['Position'], separate.inputs[0])
                for name in ('Ground height', 'Height falloff', 'Volume density'):
                    value = tree.nodes.new('ShaderNodeValue'); value.name = name
                altitude = g.op('DIVIDE', g.op('SUBTRACT', separate.outputs['Z'], tree.nodes['Ground height'].outputs[0]), tree.nodes['Height falloff'].outputs[0])
                density = g.op('DIVIDE', tree.nodes['Volume density'].outputs[0], g.op('ADD', 1, g.op('EXPONENT', g.op('MINIMUM', 20, g.op('MAXIMUM', -20, altitude)))))
                density.node.name = 'Height density'
            for name, value in (('Ground height', s.sun_world_haze_height), ('Height falloff', s.sun_world_haze_height_falloff), ('Volume density', s.fog/s.fog_distance)):
                lighting_pipeline.set_value(tree.nodes[name].outputs[0], 'default_value', value)
            source = tree.nodes['Height density'].outputs[0]
            if not scatter.inputs['Density'].links or scatter.inputs['Density'].links[0].from_socket != source:
                tree.links.new(source, scatter.inputs['Density'])
        else:
            for link in list(scatter.inputs['Density'].links): tree.links.remove(link)
            lighting_pipeline.set_value(scatter.inputs['Density'], 'default_value', s.fog/s.fog_distance)
        tree['mc_volume_version'] = 2


def strip_sky(tree):
    """Take every Sky & Lighting node, of any scene's sky, out of ``tree``,
    joining the image that fed them to what they fed."""
    removed = {node.name for node in tree.nodes if node.get(TAG)}
    if not removed:
        return
    # Where the user's image enters the sky: its glow, or one entry all share.
    entries = {}
    for link in tree.links:
        if link.to_node.name in removed and link.from_node.name not in removed and link.to_socket.name == 'Image':
            entries.setdefault(link.to_node.name, link.from_socket)
    entry = entries.get('MC Sky glow')
    if entry is None and entries and all(socket == next(iter(entries.values())) for socket in entries.values()):
        entry = next(iter(entries.values()))

    def upstream(node):
        seen = set()
        while node.name in removed:
            if node.name in seen:
                return None
            seen.add(node.name)
            port = node.inputs.get('Image')
            if port is None or not port.is_linked:
                port = next((p for p in node.inputs if p.type == 'RGBA' and p.is_linked), None)
            if port is None or not port.links:
                return None
            node, socket = port.links[0].from_node, port.links[0].from_socket
        return socket

    for from_node, to_socket in [(link.from_node, link.to_socket) for link in tree.links
                                 if link.from_node.name in removed and link.to_node.name not in removed]:
        if to_socket.type != 'RGBA':
            continue  # a pass the sky provided: nothing of the user's to put there
        source = entry if entry is not None else upstream(from_node)
        if source is not None:
            tree.links.new(source, to_socket)
    if tree.animation_data:
        prefixes = tuple(tree.nodes[name].path_from_id() for name in removed)
        for curve in [c for c in tree.animation_data.drivers if c.data_path.startswith(prefixes)]:
            tree.animation_data.drivers.remove(curve)
    for name in removed:
        tree.nodes.remove(tree.nodes[name])


def user_compositor(tree):
    """The compositor a scene had before any sky, from the scene's current one.

    A sky's compositor names the tree it was copied from. One that does not
    (an older Sparkle made it, or it was another scene's) is never built on:
    its drivers and render layers read that other sky's scene, and a second
    chain on top of the first grades the image twice (the purple render).
    Its sky is taken out of a copy instead."""
    seen = set()
    while tree is not None and tree.get(TAG) and tree.as_pointer() not in seen:
        seen.add(tree.as_pointer())
        source = tree.get(COMPOSITOR_SOURCE)
        if not isinstance(source, bpy.types.NodeTree):
            break
        tree = source
    if tree is not None and (tree.get(TAG) or any(node.get(TAG) for node in tree.nodes)):
        clean = tree.copy()
        for key in (TAG, COMPOSITOR_SOURCE):
            if key in clean:
                del clean[key]
        strip_sky(clean)
        tree = clean
    return tree


def setup_compositor(scene):
    s = scene.mcbedrock_sky
    s.previous_use_nodes = scene.use_nodes
    if hasattr(scene, 'compositing_node_group'):
        base = user_compositor(scene.compositing_node_group)
        s.previous_compositor = base
        t = base.copy() if base else bpy.data.node_groups.new('MC Sky glow', 'CompositorNodeTree')
        t[TAG] = s.owner
        if base is not None:
            t[COMPOSITOR_SOURCE] = base
        s.compositor = t
        scene.compositing_node_group = t
        outputs = [n for n in t.nodes if n.type == 'GROUP_OUTPUT']
        if not outputs:
            t.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
            outputs = [t.nodes.new('NodeGroupOutput')]
        output = outputs[0]
        target = output.inputs.get('Image') or next((p for p in output.inputs if p.type == 'RGBA'), None)
    else:
        scene.use_nodes = True
        t = scene.node_tree
        s.compositor = t
        outputs = [n for n in t.nodes if n.type == 'COMPOSITE']
        output = outputs[0] if outputs else t.nodes.new('CompositorNodeComposite')
        target = output.inputs['Image']
    if target is None:
        return
    source = target.links[0].from_socket if target.links else None
    if source is None:
        render = t.nodes.new('CompositorNodeRLayers')
        render[TAG] = s.owner
        source = render.outputs['Image']
    glare = t.nodes.new('CompositorNodeGlare')
    glare[TAG] = s.owner
    glare.name = 'MC Sky glow'
    if hasattr(glare, 'glare_type'):
        glare.glare_type = 'FOG_GLOW'
    type_socket = glare.inputs.get('Type') or glare.inputs.get('Glare Type')
    if type_socket:
        type_socket.default_value = 'Fog Glow'
    if glare.inputs.get('Size'):
        glare.inputs['Size'].default_value = .65
    elif hasattr(glare,'size'):
        glare.size = 9
    if glare.inputs.get('Quality'):
        glare.inputs['Quality'].default_value = 'High'
    if glare.inputs.get('Threshold'):
        glare.inputs['Threshold'].default_value = 1
    elif hasattr(glare, 'threshold'):
        glare.threshold = 1
    t.links.new(source, glare.inputs['Image'])
    t.links.new(glare.outputs['Image'], target)


def setup_grade(scene):
    s=scene.mcbedrock_sky
    tree=s.compositor
    glare=next((n for n in tree.nodes if n.type=='GLARE' and n.get(TAG)==s.owner),None)
    if not glare:
        return
    contrast=tree.nodes.get('MC Color contrast')
    if contrast is None:
        targets=[link.to_socket for link in glare.outputs['Image'].links]
        contrast=node(tree,'CompositorNodeBrightContrast','MC Color contrast')
        saturation=node(tree,'CompositorNodeHueSat','MC Color saturation')
        balance=node(tree,'CompositorNodeColorBalance','MC Color balance')
        for n in (contrast,saturation,balance):
            n[TAG]=s.owner
        tree.links.new(glare.outputs['Image'],contrast.inputs['Image'])
        tree.links.new(contrast.outputs['Image'],saturation.inputs['Image'])
        tree.links.new(saturation.outputs['Image'],balance.inputs['Image'])
        for target in targets:
            tree.links.new(balance.outputs['Image'],target)
    if tree.nodes.get('MC Grade contrast power') is None:
        # Contrast as a power curve around mid-grey, in scene-linear light, the
        # way a colourist's contrast behaves. Bright/Contrast pivots at 0.5,
        # above nearly every pixel of a render, so even +6 crushed the shadows
        # to black and pumped the colour.
        g=lighting_pipeline.Graph(tree)
        source=contrast.outputs['Image']
        targets=[link.to_socket for link in source.links]
        before=set(tree.nodes)
        power=node(tree,'ShaderNodeValue','MC Grade contrast power')
        pivot=node(tree,'ShaderNodeValue','MC Grade contrast pivot')
        power.outputs[0].default_value=pivot.outputs[0].default_value=1.
        split=node(tree,'CompositorNodeSeparateColor','MC Grade contrast split')
        tree.links.new(source,split.inputs[0])
        join=node(tree,'CompositorNodeCombineColor','MC Grade contrast join')
        for index in range(3):
            curved=g.op('MULTIPLY',g.op('POWER',g.op('MAXIMUM',split.outputs[index],0),power.outputs[0]),pivot.outputs[0])
            tree.links.new(curved,join.inputs[index])
        tree.links.new(split.outputs[3],join.inputs[3])
        for target in targets:
            tree.links.new(join.outputs[0],target)
        for n in set(tree.nodes)-before:
            n[TAG]=s.owner
    if not tree.nodes.get('MC Film response'):
        tone=node(tree,'CompositorNodeCurveRGB','MC Film response')
        tone[TAG]=s.owner
        source=contrast.inputs['Image'].links[0].from_socket
        tree.links.new(source,tone.inputs['Image'])
        tree.links.new(tone.outputs['Image'],contrast.inputs['Image'])
    tone=tree.nodes['MC Film response']
    if tone.get('mc_grade_version',0)<2:
        source=tone.inputs['Image'].links[0].from_socket
        tone.mapping.initialize()
        for curve in tone.mapping.curves:
            while len(curve.points)>2:
                curve.points.remove(curve.points[1])
            curve.points[0].location=(0,0)
            curve.points[-1].location=(1,1)
        for x,y in ((.018,.014),(.07,.06),(.18,.18),(.5,.52)):
            tone.mapping.curves[3].points.new(x,y)
        tone.mapping.update()
        # Shape luminance, then scale all RGB channels equally. Applying a
        # lifted curve per channel turns saturated dark colors gray/pastel.
        luma=node(tree,'CompositorNodeRGBToBW','MC Grade luminance')
        floor=node(tree,'ShaderNodeMath','MC Grade safe luminance',operation='MAXIMUM')
        floor.inputs[1].default_value=1e-6
        ratio=node(tree,'ShaderNodeMath','MC Grade luminance ratio',operation='DIVIDE')
        color=node(tree,'ShaderNodeMix','MC Grade preserve color')
        color.data_type='RGBA'
        color.blend_type='MULTIPLY'
        color.inputs[0].default_value=1
        color.clamp_result=False
        for n in (luma,floor,ratio,color):
            n[TAG]=s.owner
        tree.links.new(source,luma.inputs[0])
        tree.links.new(luma.outputs[0],floor.inputs[0])
        tree.links.new(floor.outputs[0],tone.inputs['Image'])
        tree.links.new(tone.outputs['Image'],ratio.inputs[0])
        tree.links.new(floor.outputs[0],ratio.inputs[1])
        tree.links.new(source,color.inputs[6])
        tree.links.new(ratio.outputs[0],color.inputs[7])
        tree.links.new(color.outputs[2],contrast.inputs['Image'])
        tone['mc_grade_version']=2


def ensure_lighting_collection(scene):
    """Recover an active sky whose light collection was removed or unlinked.

    The world and compositor can survive deleting scene objects. Keep their
    settings and restoration backups; rebuilding the whole sky loses them.
    """
    s = scene.mcbedrock_sky
    if not s.collection:
        s.collection = next((c for c in scene.collection.children_recursive
                             if c.get(TAG) == s.owner), None)
        if not s.collection:
            s.collection = bpy.data.collections.new('Sparkle Sky & Lighting · '+scene.name)
            s.collection[TAG] = s.owner
            # Deleting a collection may leave its lights as orphaned objects.
            # Reuse only this rig's objects, never another scene's objects.
            for obj in bpy.data.objects:
                if obj.get(TAG) == s.owner and not obj.users_scene:
                    s.collection.objects.link(obj)
    if s.collection not in scene.collection.children_recursive:
        scene.collection.children.link(s.collection)
    if s.collection.name == 'MC Sky & Lighting · '+scene.name:
        s.collection.name = 'Sparkle Sky & Lighting · '+scene.name


def set_sky_value(target, name, value):
    """Skip identical writes at RNA precision, preserving exact cycle endpoints.

    Node sockets are driven (sky_drive): a moving day/night cycle then costs
    a property write per value instead of a node-tree update. The sky's color
    ramp is set directly, since fog samples it in Python every frame, and its
    stops are published for the clouds' and fog's copies to follow."""
    if name == 'default_value' and isinstance(target, bpy.types.NodeSocket) and sky_drive.drivable(target):
        sky_drive.write(target, name, value)
        return
    if isinstance(target, bpy.types.ColorRampElement):
        sky_drive.publish(target, name, value)
        return
    old = getattr(target, name)
    if hasattr(old, '__len__') and not isinstance(old, str):
        old = tuple(old)
        value = tuple(Vector(value))
    elif isinstance(old, float):
        value = struct.unpack('f', struct.pack('f', value))[0]
    if old != value:
        setattr(target, name, value)


_MISSING_KEY = re.compile(r'key "(.*)" not found')


def follow_compositor(scene):
    """Keep driving the scene's compositor when it was replaced by a duplicate
    of the sky's own (Blender's duplicate button in the compositor header)."""
    s = scene.mcbedrock_sky
    current = getattr(scene, 'compositing_node_group', None)
    if s.active and current is not None and current != s.compositor and s.owner and current.get(TAG) == s.owner:
        s.compositor = current


def update(scene, geometry=False):
    follow_compositor(scene)
    try:
        _update(scene, geometry)
    except KeyError as error:
        # A node of the sky's world was renamed or deleted. Rebuild the world
        # once (every setting is kept) instead of failing on each update.
        s = scene.mcbedrock_sky
        missing = _MISSING_KEY.search(str(error))
        tree = s.world.node_tree if s.world else None
        if missing is None or tree is None or missing.group(1) in tree.nodes:
            raise
        s.world['mc_sky_shader_version'] = 0
        _update(scene, geometry)


def _world_artwork(scene, tree):
    """The sun and moon artwork for a rebuilt world: the old world's, or fresh
    copies when its artwork nodes (or the world itself) are gone."""
    nodes = [tree.nodes.get(name+' artwork') if tree is not None else None for name in ('Sun','Moon')]
    images = [node.image if node is not None else None for node in nodes]
    if all(images):
        return images
    owner = scene.mcbedrock_sky.owner
    try:
        return load_images(discover(scene), owner)
    except Exception:
        # A sky pack that moved or broke: the built-in artwork stands in.
        return load_images(None, owner)


def world_status(scene):
    """'' while the sky's world is shown, 'MISSING' once it was deleted, and
    'REPLACED' while the scene shows another world. Only a real sky (one with
    an owner id) counts."""
    s = getattr(scene, 'mcbedrock_sky', None)
    if s is None or not s.active or not s.owner:
        return ''
    if s.world is None:
        return 'MISSING'
    return 'REPLACED' if scene.world != s.world else ''


def repair_world(scene):
    """Bring the sky's world back after it was deleted, or show it again after
    another world replaced it. Every sky setting is kept."""
    s = scene.mcbedrock_sky
    if not s.active:
        return False
    if s.world is None:
        build_world(scene, _world_artwork(scene, None))
    elif scene.world != s.world:
        scene.world = s.world
    update(scene)
    return True


# The rig's suns. Trailer lighting leaves most of them dark at any one time:
# the fill sun by day, the side skies always, the soft copies at Soft light
# layers 0 (Daylight), the moon at noon and the sun at night.
RIG_LIGHTS = ('Sun','Sun reflection','Moon','Fill','Sun soft 1','Sun soft 2','Sun soft 3','Sky left','Sky right')
IDLE = 'mc_idle_hidden'     # object: hidden by the rig while it gives no light


def _keyframed(scene):
    """Sky settings keyframed on the scene: light strengths change on frames."""
    action = scene.animation_data.action if scene.animation_data else None
    if action is None:
        return False
    curves = list(getattr(action, 'fcurves', None) or ())
    for layer in getattr(action, 'layers', None) or ():
        for strip in getattr(layer, 'strips', None) or ():
            for bag in getattr(strip, 'channelbags', None) or ():
                curves += list(bag.fcurves)
    return any(curve.data_path.startswith('mcbedrock_sky') for curve in curves)


def rest_idle_lights(scene):
    """Hide the rig's suns that give no light, and show them when they do.

    EEVEE shades every pixel with every visible sun, dark or not: hidden,
    Daylight renders with two suns instead of nine, the same picture faster.
    Lights the user hid keep their state. While the sky moves (the day/night
    cycle, keyframed sky settings) every light stays shown, since a render
    would not see a light come back on in time; a running render leaves them
    as the last update set them."""
    if bpy.app.is_job_running('RENDER') or sky_drive.in_per_frame():
        return
    s = scene.mcbedrock_sky
    moving = s.cycle_enabled or _keyframed(scene)
    for role in RIG_LIGHTS:
        obj = owned(scene, role)
        if obj is None or obj.type != 'LIGHT':
            continue
        idle = not moving and obj.data.energy <= 0
        if obj.get(IDLE):
            if not idle:
                obj.hide_render = obj.hide_viewport = False
                del obj[IDLE]
        elif idle and not obj.hide_render and not obj.hide_viewport:
            obj.hide_render = obj.hide_viewport = True
            obj[IDLE] = True


def _update(scene, geometry=False):
    if not supports_sky():
        return
    s = scene.mcbedrock_sky
    if not s.active or not s.world:
        return
    native_color.sync(scene)
    ensure_lighting_collection(scene)
    if not s.collection.get('mc_neutral_texture_defaults'):
        # Migrate known generated recipes only; keep separately tuned controls.
        values=(s.texture_vibrance,s.texture_brightness,s.foliage_color)
        if any(all(abs(a-b)<1e-5 for a,b in zip(values,recipe))
               for recipe in ((.65,.25,.65),(.45,.2,.4),(.15,.1,.15))):
            for key in ('texture_vibrance','texture_brightness','foliage_color'):s[key]=0.
        if abs(s.horizon_haze-1)<1e-5 and abs(s.horizon_height-.22)<1e-5 and s.sky_color_mix==0:
            s['horizon_haze']=.4
        s.collection['mc_neutral_texture_defaults']=True
        sky_materials.update(scene)
    setup_eevee(scene)
    setup_cycles(scene)
    if s.collection.get('contact_shading_engine')!=scene.render.engine:
        sky_materials.update(scene)
    if s.collection.get('contact_shading_engine')!=scene.render.engine:
        s.collection['contact_shading_engine']=scene.render.engine
    for role in ('Sun','Sun reflection','Moon','Fill','Sun soft 1','Sun soft 2','Sun soft 3','Sky left','Sky right'):
        if owned(scene,role) is None:
            light(scene,role)
    hour=current_time(scene)
    direction, day, twilight = time_state(hour, s.azimuth)
    golden=golden_light(direction) if s.trailer_lighting else 0
    # Trailer nights retain broad skylight so foliage and terrain remain
    # readable. Fade smoothly after sunset without changing daytime lighting.
    night=1-smoothstep(-.35,-.04,direction.z)
    night_fill=1-(.5 if s.trailer_lighting else .72)*night
    t = s.world.node_tree
    if s.world.get('mc_sky_shader_version',0) != SKY_SHADER_VERSION or 'Sun halo corners' not in t.nodes:
        # Upgrade saved rigs without losing their original lighting backup.
        old_world = s.world
        old_version=old_world.get('mc_sky_shader_version',0)
        if (old_version < 13 and abs(s.sun_haze_radius-55) < .001) or (old_version < 18 and abs(s.sun_haze_radius-85) < .001):
            s['sun_haze_radius'] = 25.0
        build_world(scene,_world_artwork(scene,t))
        t = s.world.node_tree
        if not old_world.users:
            bpy.data.worlds.remove(old_world)
    # Only the rig's explicit Sun supplies directional illumination in Eevee.
    set_sky_value(s.world,'sun_threshold',0)
    star_strength=s.star_brightness*(1-smoothstep(-.25,.05,direction.z)) if s.stars else 0
    set_sky_value(t.nodes['Star amount'].inputs[1],'default_value',s.star_amount)
    set_sky_value(t.nodes['Plain background'].outputs[0],'default_value',float(s.plain_background))
    set_sky_value(t.nodes['Plain background color'].outputs[0],'default_value',(*s.plain_background_color,1))
    set_sky_value(t.nodes['Star position'].inputs['Rotation'],'default_value',star_orientation(hour,s.azimuth,s.star_rotation))
    horizon = mix(NIGHT_HORIZON if s.trailer_lighting else (.015,.025,.065), (.48,.68,.77), day)
    horizon = mix(horizon, (1,.35,.065), twilight*(.65+.3*s.warmth))
    zenith = mix((.002,.004,.02), (.005,.28,.6), day)
    daylight = smoothstep(.15,.55,direction.z)
    light_brightness=s.brightness*(1+.5*daylight)
    set_sky_value(t.nodes['Sky colour compensation'].inputs['Amount'],'default_value',.25*daylight if native_color.active(scene) and native_color.correction(scene) else 0)
    # Keep the vivid day palette until the sunset takes over. The previous
    # altitude window faded blue early into a pale intermediate palette.
    color_daylight = smoothstep(0,.45,direction.z)
    color_twilight = twilight*(1-color_daylight)
    # A pale sky blue, not white: a white horizon rendered brighter than the
    # clouds above it and read as a glow instead of haze.
    horizon = sky_color_mix(horizon,(.66,.8,1),color_daylight)
    zenith = mix(zenith,(.003,.12,.9),color_daylight)
    middle = mix(horizon,zenith,.65)
    middle = mix(middle,(.025,.42,1),color_daylight)
    middle = sky_color_mix(middle,(.402,.445,.235),color_twilight)
    # The old daylight blend washed 17:00 back toward pale blue. Warm only
    # the low-sun horizon; noon, night and the sunset endpoint remain intact.
    honey=golden_hour_band(direction)
    horizon=mix(horizon,(1,.56,.16),.78*honey)
    # The upper sky remains blue while warm color rises from the horizon.
    # Less green deepens twilight luminance; stronger blue keeps it saturated.
    zenith = mix(mix((.002,.004,.02),(.003,.18,.9),day),
                 (.0001,.065,.42),twilight**3)
    # Keep the sunset warm until the sun has gone below the horizon. The
    # cooler twilight palette takes over afterwards, smoothly into night.
    sunset_warm=twilight*smoothstep(-.22,-.02,direction.z)
    zenith=mix(zenith,(.003,.22,.55),sunset_warm)
    if s.preset=='VIBRANT':
        # Mojang's daylight forest/plains images use a muted mid-blue sky,
        # pale warm clouds and a warm key over cool shade. Author that sky
        # directly, leaving material saturation and the night cycle intact.
        horizon=mix(horizon,(.62,.75,.86),daylight)
        middle=mix(middle,(.18,.38,.62),daylight)
        zenith=mix(zenith,(.07,.24,.48),daylight)
    horizon=mix(horizon,s.horizon_color,s.sky_color_mix)
    zenith=mix(zenith,s.zenith_color,s.sky_color_mix)
    middle=mix(middle,mix(s.horizon_color,s.zenith_color,.65),s.sky_color_mix)
    # Daylight keeps a pale horizon beneath a saturated overhead blue, like
    # the preset card. The former linear .4 mix collapsed both into mid-blue.
    # Trailer nights keep their horizon glow the same way: a navy zenith over
    # a teal treeline, not a sky that is one dark color all the way down.
    glow_amount=max(color_daylight,night if s.trailer_lighting else 0)
    haze_amount=s.horizon_haze+(1-(1-s.horizon_haze)**5-s.horizon_haze)*glow_amount*(1-s.sky_color_mix)
    horizon=mix(zenith,horizon,haze_amount)
    middle=mix(zenith,middle,haze_amount)
    warm_amount=sunset_warm*min(1,s.horizon_haze/.4)*(1-s.sky_color_mix)
    horizon=mix(horizon,(3,.45,.06),warm_amount)
    # A luminous cream-gold transition avoids a dull grey stripe where the
    # complementary orange horizon and blue upper sky blend together.
    middle=mix(middle,(1.2,.8,.4),warm_amount)
    if s.preset=='VIBRANT':
        # The supplied sunset reference and native atmosphere's pink/lilac
        # horizon keys need a separate dusk gradient. These are Blender-view
        # calibrated colors, not raw Bedrock scattering coefficients.
        vibrant_twilight=(golden_light(direction)*smoothstep(-.25,0,direction.z)
                          *min(1,s.horizon_haze/.3)*(1-s.sky_color_mix))
        horizon=mix(horizon,(1.1,.33,.21),vibrant_twilight)
        middle=mix(middle,(.78,.35,.42),vibrant_twilight)
        zenith=mix(zenith,(.29,.29,.46),vibrant_twilight)
    # ColorRamp sockets clamp RGB to one. Store chromaticity there and carry
    # radiance separately, rather than clipping warm HDR colours to yellow-green.
    sky_radiance=1+2*warm_amount
    # Palette normalization must not brighten the sun, moon or stars at dawn
    # and dusk. Their radiance is independent of the gradient's HDR storage.
    set_sky_value(t.nodes['Star strength'].outputs[0],'default_value',star_strength/sky_radiance)
    ramp = t.nodes['Sky colors'].color_ramp
    # Equal spacing gives the spline a continuous slope through every stop.
    # A broad twilight blend reaches the original blue well before the zenith.
    # Shape the coordinate smoothly to retain the Horizon height control;
    # moving just the middle stop would reintroduce abrupt slope changes.
    # Keep the pale horizon's transition into blue below the upper daytime sky.
    # Dawn/dusk need room to blend: compressing the HDR cream/orange stops
    # into nine degrees produced a bright, hard-looking stripe at the horizon.
    upper_height = .22+.38*daylight+.14*sunset_warm
    positions = tuple(upper_height*fraction for fraction in (0,.25,.5,.75,1))
    set_sky_value(t.nodes['Sky horizon curve'].inputs[1],'default_value',.8+.2*min(.95,s.horizon_height)/.22)
    set_sky_value(t.nodes['Horizon position'].inputs[1],'default_value',math.sin(math.radians(s.horizon_position)))
    # Capture references before moving stops: Blender reorders the collection.
    for stop,position,color in zip(tuple(ramp.elements),positions,
                                   (horizon,horizon,middle,zenith,zenith)):
        set_sky_value(stop,'position',position)
        set_sky_value(stop,'color',(*(channel/sky_radiance for channel in color),1))
    remap=t.nodes['Sky gradient range']
    start,end=s.gradient_start,s.gradient_end
    # A coincident range is a narrow step, never a division by zero. Reversed
    # ranges remain valid for intentionally inverting the vertical palette.
    if abs(end-start)<.0001:
        end=start+(.0001 if start<1 else -.0001)
    set_sky_value(remap.inputs['From Min'],'default_value',start)
    set_sky_value(remap.inputs['From Max'],'default_value',end)
    set_sky_value(t.nodes['Sky gradient power'].inputs[1],'default_value',s.gradient_power)
    source=t.nodes['Sky gradient texture'].outputs['Fac'] if s.gradient_mapping else t.nodes['Sky horizon curve'].outputs[0]
    destination=t.nodes['Sky colors'].inputs[0]
    if not destination.links or destination.links[0].from_socket!=source:
        t.links.new(source,destination)
    # The decorative sky has its own palette. Environment illumination uses
    # a restrained, near-neutral palette so sky saturation cannot wash colors.
    ambient_color=t.nodes.get('Ambient light color')
    if ambient_color is None:
        ambient_color=node(t,'ShaderNodeRGB','Ambient light color')
        t.links.new(ambient_color.outputs[0],t.nodes['Ambient dome'].inputs[1])
    # Dome shape, relative to the ambient color. Its average over the upper
    # hemisphere stays near one, so overall exposure holds while the light
    # gains direction: overhead sky bluer and brighter, ground warm and dim.
    set_sky_value(t.nodes['Ambient dome zenith'].outputs[0],'default_value',(.92,1.08,1.42,1))
    set_sky_value(t.nodes['Ambient dome horizon'].outputs[0],'default_value',(1.,.98,.95,1))
    # The ground color (lower hemisphere) is set last, from the lit ground: ground_bounce.
    # The supplied artist World uses a substantial ambient base at every time
    # of day. Keep it separate from the decorative sky and direct key lights.
    ambient=mix(NIGHT_AMBIENT if s.trailer_lighting else (.070,.095,.253),(.198,.203,.285),day)
    # Cool skylight balances the golden key; keep twilight luminance comparable
    # to the old warm ambient so this changes color rather than shadow depth.
    ambient=mix(ambient,(.115,.170,.300),twilight)
    set_sky_value(ambient_color.outputs[0],'default_value',(*(a*b for a,b in zip(ambient,s.shadow_color)),1))
    # The complete rig adds sun/moon/fill to this cool environment base.
    ambient_strength=light_brightness*(2.45+.35*day)*s.ambient_light*(1-.15*golden)
    # Keep a cool ambient base under trailer canopies, even when moonlight is
    # occluded. The natural look retains its darker night environment.
    if not s.trailer_lighting:
        ambient_strength *= night_fill*(1-.65*night)
    if scene.render.engine == 'BLENDER_EEVEE':
        # EEVEE's distant environment is only screen-space occluded. A lower
        # base lets shadow-casting sky lights provide the missing directionality.
        ambient_strength *= .85-.05*day
    if s.trailer_lighting:
        # Keep the visible background untouched; reduce daytime shadow wash
        # where the key can provide contrast, retaining readable night fill.
        ambient_strength *= 1-.08*day*s.look_strength
    if s.trailer_lighting:
        # The dome carries the light the Fill sun gave, so shadow brightness
        # follows Fill as before, only from the open sky instead of one side.
        # Measured on real worlds: trailer shadows sit about 3-4 times below
        # the sunlit ground. Cycles occludes the dome fully; EEVEE's horizon
        # scan less so, which needs less of it for the same depth.
        ambient_strength *= 1 + DOME_BOOST.get(scene.render.engine, DOME_BOOST['CYCLES'])*s.fill*(1-night)
    set_sky_value(t.nodes['Ambient'].inputs['Strength'],'default_value',ambient_strength)
    set_sky_value(t.nodes['Visible sky'].inputs['Strength'],'default_value',s.brightness*s.sky_brightness*(1+.12*daylight)*sky_radiance)
    set_sky_value(t.nodes['Sky display radiance'].outputs[0],'default_value',sky_drive.value(t.nodes['Visible sky'].inputs['Strength']))
    set_sky_value(t.nodes['Standard sky highlights'].inputs[0],'default_value',float(scene.view_settings.view_transform=='Standard'))
    # The ramp's HDR normalization must not multiply the separately added
    # solar glow. A broad orange wash otherwise neutralizes the blue sky.
    low_sun_glow=1-.5*twilight
    set_sky_value(t.nodes['Sun halo strength'].outputs[0],'default_value',s.glow*1.2*low_sun_glow*smoothstep(-.12,.04,direction.z)/sky_radiance)
    set_sky_value(t.nodes['Sun halo spread'].outputs[0],'default_value',s.glow_spread)
    set_sky_value(t.nodes['Sun halo corners'].outputs[0],'default_value',.12+.25*s.glow_softness)
    set_sky_value(t.nodes['Sun wide strength'].outputs[0],'default_value',.2*s.sun_haze*(.6+.8*twilight)*low_sun_glow*smoothstep(-.12,.04,direction.z)/sky_radiance)
    set_sky_value(t.nodes['Sun wide falloff'].inputs['From Min'],'default_value',math.cos(math.radians(s.sun_haze_radius)))
    set_sky_value(t.nodes['Sun wide width'].inputs[1],'default_value',math.cos(math.radians(s.sun_haze_radius)))
    set_sky_value(t.nodes['Sun wide glow'].inputs[1],'default_value',(*mix((1,.9,.7),(1,.48,.16),twilight),1))
    # Before the artwork pass below writes the visible sun's direction into it.
    hand_sun = owned(scene,'Sun')
    if hand_sun is not None and hand_rotated(hand_sun):
        adopt_hand_aim(scene, hand_sun)
    for name, vector, size in [('Sun',direction,s.sun_size),('Moon',-direction,s.moon_size)]:
        tint=s.sun_color if name=='Sun' else s.moon_color
        # Fade coverage, not radiance: a dim opaque MIX layer becomes a black sun.
        # Retain emissive headroom without bleaching the source texture's
        # yellow border. This changes artwork, never the terrain key light.
        strength=(SUN_RADIANCE if name=='Sun' else 2)/sky_radiance
        set_sky_value(t.nodes[name+' Visibility'].outputs[0],'default_value',smoothstep(-.12,.04,vector.z))
        set_sky_value(t.nodes[name+' Brightness'].inputs[2],'default_value',(*(channel*strength for channel in tint),1))
        azimuth = math.radians(s.azimuth)
        tangent = Vector((math.sin(azimuth),-math.cos(azimuth),0))
        up = tangent.cross(vector).normalized()
        for suffix, basis in [(' X',tangent),(' Y',up),(' Front',vector)]:
            set_sky_value(t.nodes[name+suffix].inputs[1],'default_value',basis)
        radius = math.tan(math.radians(size/2))
        set_sky_value(t.nodes[name+' Size'].outputs[0],'default_value',radius*4)
        if name == 'Moon':
            index = next(index for key,_,index in PHASES if key==s.phase)
            set_sky_value(t.nodes['Moon phase'].inputs[1],'default_value',(index%4/4,(1-index//4)/2,0))
        obj = owned(scene,name)
        set_sky_value(obj,'rotation_euler',(-vector).to_track_quat('-Z','Y').to_euler())
        set_sky_value(obj.data,'angle',math.radians(s.softness))
    sun = owned(scene,'Sun')
    for obj in s.collection.objects:
        if obj.get('mc_camera_follow') and scene.camera:
            track=next((c for c in obj.constraints if c.type=='LOCKED_TRACK'),None)
            if track is None:
                track=obj.constraints.new('LOCKED_TRACK');track.track_axis='TRACK_Y';track.lock_axis='LOCK_Z'
            track.target=scene.camera
        if obj.type == 'LIGHT':
            # The default ten-percent jitter overblur washes small voxel shadows.
            set_sky_value(obj.data,'shadow_jitter_overblur',1. if scene.render.engine == 'BLENDER_EEVEE' else 10.)
    set_sky_value(sun.data,'use_shadow',True)
    set_sky_value(sun.data,'shadow_maximum_resolution',.02)
    # A new scene camera is a new shot: aim the trailer key from it.
    aimed = s.light_direction_mode=='TRAILER' and s.key_aimed
    if s.key_aimed and scene.camera is not None and s.collection.get('mc_key_camera') != scene.camera.name:
        aim_key(scene)
    if aimed:
        key_direction = aimed_direction(s, math.degrees(math.asin(max(-1.,min(1.,direction.z)))))
    else:
        # The former crossed trailer key or the visible sun's direction.
        # Artist offsets remain additive in either mode.
        key_direction = surface_sun_direction(direction, s.light_direction_mode=='TRAILER', s.azimuth)
        if s.sun_heading_offset:
            angle=math.radians(s.sun_heading_offset)
            key_direction.x,key_direction.y=(key_direction.x*math.cos(angle)-key_direction.y*math.sin(angle),key_direction.x*math.sin(angle)+key_direction.y*math.cos(angle))
        if s.key_height_offset:
            altitude=math.asin(max(-1,min(1,key_direction.z)))+math.radians(s.key_height_offset)
            altitude=max(math.radians(5),min(math.radians(85),altitude))
            heading=math.atan2(key_direction.y,key_direction.x)
            key_direction=Vector((math.cos(altitude)*math.cos(heading),math.cos(altitude)*math.sin(heading),math.sin(altitude)))
    set_sky_value(sun,'rotation_euler',(-key_direction).to_track_quat('-Z','Y').to_euler())
    sun['mc_rig_rotation'] = tuple(sun.rotation_euler)
    fade = smoothstep(-.3,.1,direction.z) if s.trailer_lighting else smoothstep(-.08,.12,direction.z)
    sun.data.energy = light_brightness*s.sunlight*fade*3.5
    if s.trailer_lighting:
        # Hold a bright key through golden hour; a fading horizontal sun alone
        # produces an overcast-looking world beneath a colorful sunset sky.
        sun.data.energy *= 1 + .65*twilight*(1-smoothstep(.35,.65,direction.z))
    # Warm the actual light rather than grading every material orange. Keep
    # enough green in the key for foliage; shaded faces retain their sky fill.
    warm_key = (1,.58,.26) if s.trailer_lighting else (1,.48,.18)
    warmth=golden*(.65+.35*s.warmth) if s.trailer_lighting else max(twilight*.7,s.warmth*.65)
    # Preserve the existing midday light color when away from golden hour.
    neutral=mix((1,.96,.86),(1,.82,.6),s.warmth*.65) if s.trailer_lighting else (1,.96,.86)
    # Explicit preset control: saved skies default to the former warm key.
    # Cleaner daylight whites retain the existing luminance and sunset warmth.
    balanced = mix((1,.985,.95),(1,.88,.72),s.warmth*.65)
    luminance = lambda color: sum(a*b for a,b in zip(color,(.2126,.7152,.0722)))
    scale = luminance(neutral)/luminance(balanced)
    neutral = mix(neutral, tuple(c*scale for c in balanced), s.daylight_balance*smoothstep(.2,.65,direction.z))
    set_sky_value(sun.data,'color',mix(neutral,warm_key,warmth))
    if s.trailer_lighting:
        set_sky_value(sun.data,'color',mix(tuple(sun.data.color),(1,.65,.27),.35*honey))
        sun.data.energy*=1+.12*honey
    set_sky_value(sun.data,'color',tuple(a*b for a,b in zip(sun.data.color,s.sun_color)))
    set_sky_value(sun.data,'color',tuple(a*b for a,b in zip(mix(tuple(sun.data.color),(1,.48,.16),s.sun_light_warmth),s.sun_light_color)))
    total_energy=sun.data.energy
    sun.data.energy*=1-.26*s.penumbra
    # The art-directed key and broad fill lobes illuminate terrain. They must
    # not create displaced or repeated solar reflections. Cycles' transmissive
    # glass closure is excluded only when BOTH glossy and transmission are off;
    # EEVEE instead uses the light's contribution factors.
    for role in ('Sun','Sun soft 1','Sun soft 2','Sun soft 3','Fill','Sky left','Sky right'):
        diffuse=owned(scene,role)
        set_sky_value(diffuse.data,'specular_factor',0.)
        set_sky_value(diffuse,'visible_glossy',False)
        set_sky_value(diffuse,'visible_transmission',False)
    moon = owned(scene,'Moon')
    moon.data.energy = s.brightness*s.moonlight*(1-day)*.22
    # Keep moonlit surfaces readable as the unoccluded ambient base recedes.
    moon.data.energy *= 1+.4*night
    if s.trailer_lighting:
        # A trailer moon is a key light: faces and fronts read clearly.
        moon.data.energy = s.brightness*s.moonlight/4*(1-day)*NIGHT_MOON
    if aimed:
        # By night the moon is the key: aimed from the shot like the sun.
        moon_key = aimed_direction(s, math.degrees(math.asin(max(-1.,min(1.,-direction.z)))))
        set_sky_value(moon,'rotation_euler',(-moon_key).to_track_quat('-Z','Y').to_euler())
    set_sky_value(moon.data,'angle',math.radians(max(32,s.softness)))
    set_sky_value(moon.data,'color',NIGHT_MOON_COLOR if s.trailer_lighting else (.48,.64,1))
    set_sky_value(moon.data,'color',tuple(a*b for a,b in zip(moon.data.color,s.moon_color)))
    # The 32-degree moon light softens night shadows. As a specular source it
    # spread one bright sheet across whole lakes, so it is diffuse-only and
    # the narrow reflection light follows the moon once the sun has set.
    set_sky_value(moon.data,'specular_factor',0.)
    set_sky_value(moon,'visible_glossy',False)
    set_sky_value(moon,'visible_transmission',False)
    character_fill(scene, s.character_fill*CHARACTER_FILL_ENGINE.get(scene.render.engine, 1.)*(sun.data.energy+moon.data.energy))
    reflection=owned(scene,'Sun reflection')
    sun_up=smoothstep(-.12,.04,direction.z)
    moon_up=smoothstep(-.12,.04,-direction.z)
    lit_by_moon=moon_up>sun_up
    source=-direction if lit_by_moon else direction
    set_sky_value(reflection,'rotation_euler',(-source).to_track_quat('-Z','Y').to_euler())
    if lit_by_moon:
        set_sky_value(reflection.data,'energy',moon.data.energy*MOON_REFLECTION_STRENGTH*moon_up)
        set_sky_value(reflection.data,'color',moon.data.color)
        set_sky_value(reflection.data,'angle',math.radians(REFLECTION_ANGLE))
    else:
        set_sky_value(reflection.data,'energy',sun.data.energy*SUN_REFLECTION_STRENGTH*sun_up)
        set_sky_value(reflection.data,'color',sun.data.color)
        set_sky_value(reflection.data,'angle',min(sun.data.angle,math.radians(REFLECTION_ANGLE)))
    for field,value in (('diffuse_factor',0.),('volume_factor',0.),
                        ('transmission_factor',0.),('specular_factor',1.)):
        set_sky_value(reflection.data,field,value)
    for field,value in (('visible_camera',False),('visible_diffuse',False),('visible_volume_scatter',False),
                        ('visible_transmission',False),('visible_glossy',True)):
        set_sky_value(reflection,field,value)
    # Keep most energy in the shaped key. Narrow supporting lobes soften the
    # cast edge without turning one-block overhangs into broad lighting halos.
    if s.trailer_lighting:
        # Element builds the sun from a main light and softer copies: a sharp
        # line where the shadow starts, then a short soft falloff. Copies
        # stay a few times the key's width, warmer and richer in the penumbra.
        # Weights sum to the .26 the key gives up, so total sun energy holds.
        lobes=((.17,max(3*s.softness,1.5)),(.09,max(7*s.softness,3.5)),(0.,0.))
    else:
        lobes=((.14,max(s.softness,12)),(.08,max(s.softness,25)),(.04,max(s.softness,45)))
    for index,(weight,angle) in enumerate(lobes,1):
        soft=owned(scene,'Sun soft '+str(index))
        set_sky_value(soft,'rotation_euler',sun.rotation_euler)
        soft.data.energy=total_energy*weight*s.penumbra
        set_sky_value(soft.data,'angle',math.radians(angle) if angle else math.radians(s.softness))
        if s.trailer_lighting:
            set_sky_value(soft.data,'color',mix(tuple(sun.data.color),(1,.62,.32),.25))
        else:
            set_sky_value(soft.data,'color',mix(tuple(sun.data.color),(.8,.88,1),.15*s.penumbra))
        set_sky_value(soft.data,'use_shadow',weight>0)
        set_sky_value(soft.data,'shadow_maximum_resolution',.04 if s.trailer_lighting else .08)
    fill = owned(scene,'Fill')
    fill_direction = Vector((-direction.x,-direction.y,max(.65,abs(direction.z)))).normalized()
    set_sky_value(fill,'rotation_euler',(-fill_direction).to_track_quat('-Z','Y').to_euler())
    fill.data.energy = light_brightness*s.fill*(.8-.18*day+.25*twilight)
    fill.data.energy *= night_fill
    fill_energy = fill.data.energy
    if s.trailer_lighting:
        # Retain readable backlit foliage; this broad shadow-casting fill is
        # weaker than the key and does not illuminate through solid canopies.
        fill.data.energy *= .9
    set_sky_value(fill.data,'color',mix(mix((.62,.72,1),(.82,.9,1),day),(1,.86,.7),twilight*.35))
    set_sky_value(fill.data,'color',tuple(a*b for a,b in zip(fill.data.color,s.shadow_color)))
    # Broad skylight fill lifts shaded faces without lighting straight through terrain.
    set_sky_value(fill.data,'angle',math.radians(80))
    set_sky_value(fill.data,'use_shadow',True)
    set_sky_value(fill.data,'shadow_maximum_resolution',.12)
    sideways=Vector((math.sin(math.radians(s.azimuth)),-math.cos(math.radians(s.azimuth)),0))
    for role,sign in (('Sky left',1),('Sky right',-1)):
        bounce=owned(scene,role)
        vector=Vector((sideways.x*sign,sideways.y*sign,.8)).normalized()
        set_sky_value(bounce,'rotation_euler',(-vector).to_track_quat('-Z','Y').to_euler())
        bounce.data.energy=light_brightness*s.ambient_light*s.penumbra*(.22+.225*day)
        bounce.data.energy *= night_fill
        if s.trailer_lighting:
            bounce.data.energy *= .65
        set_sky_value(bounce.data,'color',mix((.48,.6,1),(.87,.94,1),day))
        set_sky_value(bounce.data,'color',tuple(a*b for a,b in zip(bounce.data.color,s.shadow_color)))
        set_sky_value(bounce.data,'angle',math.radians(100))
        set_sky_value(bounce.data,'use_shadow',True)
        set_sky_value(bounce.data,'shadow_maximum_resolution',.2)
        if s.trailer_lighting:
            # The sky dome lights shadows from every open direction; a
            # 100-degree sun only smeared one more shadow across them.
            bounce.data.energy = 0.
    if s.trailer_lighting:
        # By day the dome is the fill. At night Element keeps one directional
        # light with a very high source angle for fill (bramstout.nl, Minecraft
        # Live 2024), so the fill sun fades in as the dome's boost fades out.
        fill.data.energy *= night
    # Fog light (bramstout.nl, Monster in the Woods): the sky's lights shade
    # surfaces only, and one separate light colors fog and smoke, so fog can
    # be lit apart from the land.
    fog_light=owned(scene,'Fog light')
    if s.fog_light and fog_light is None:
        fog_light=light(scene,'Fog light')
    for role in ('Sun','Moon','Fill','Sun soft 1','Sun soft 2','Sun soft 3','Sky left','Sky right'):
        other=owned(scene,role)
        if other is not None:
            set_sky_value(other.data,'volume_factor',0. if s.fog_light else 1.)
    if fog_light is not None:
        set_sky_value(fog_light,'hide_render',not s.fog_light)
        set_sky_value(fog_light,'hide_viewport',not s.fog_light)
        source=direction if direction.z>-.05 else -direction
        set_sky_value(fog_light,'rotation_euler',(-source).to_track_quat('-Z','Y').to_euler())
        set_sky_value(fog_light.data,'energy',s.fog_light_strength)
        set_sky_value(fog_light.data,'color',tuple(s.fog_light_color))
        set_sky_value(fog_light.data,'angle',math.radians(2))
        for field,value in (('diffuse_factor',0.),('specular_factor',0.),('transmission_factor',0.),('volume_factor',1.)):
            set_sky_value(fog_light.data,field,value)
        set_sky_value(fog_light.data,'use_shadow',True)
    rest_idle_lights(scene)
    missing_clouds = s.clouds > 0 and owned(scene,'Cloud') is None
    if (geometry or missing_clouds or (s.cloud_preset!='DUNGEONS' and s.collection.get('cloud_amount') != s.clouds)
            or (s.cloud_preset=='DUNGEONS' and (s.collection.get('cloud_density')!=s.dungeons_cloud_density
                                               or s.collection.get('cloud_size')!=s.dungeons_cloud_scale))
            or s.collection.get('cloud_layout_version') != CLOUD_LAYOUT_VERSION
            or s.collection.get('cloud_preset','CLASSIC') != s.cloud_preset):
        rebuild_clouds(scene)
    cloud = owned(scene,'Cloud')
    if cloud:
        # Clouds would float in front of a plain backdrop.
        set_sky_value(cloud,'visible_camera',not s.plain_background)
        material=cloud_object_material(scene,cloud)
        set_sky_value(material,'use_backface_culling',True)
        dimensional = s.cloud_preset != 'CLASSIC'
        cloud_shading(material,dimensional)
        shader = material.node_tree.nodes.get('Principled BSDF')
        # Carry the broad time-of-day tint into diffuse and fill, preserving
        # the user's cloud color as a multiplier.
        cloud_tint=cloud_light_tint(direction,day)
        color = (*(a*b for a,b in zip(s.cloud_color,cloud_tint)),1)
        set_sky_value(shader.inputs['Base Color'],'default_value',color)
        set_sky_value(shader.inputs['Emission Color'],'default_value',(*(a*b for a,b in zip(mix((.8,.82,.85),(1,.62,.32),twilight),cloud_tint)),1))
        if dimensional:
            set_sky_value(material.node_tree.nodes['Cloud face color'].inputs[1],'default_value',color)
            set_sky_value(material.node_tree.nodes['Cloud shape tint'].inputs[1],'default_value',(.8,.85,.93,1))
        strength = .08+.28*day+.65*daylight+.2*twilight
        set_sky_value(shader.inputs['Emission Strength'],'default_value',strength*night_fill)
        if atmosphere.enabled(scene):atmosphere.prepare_material(scene,material)
    update_fog(scene)
    position_clouds(scene)
    from . import sky_clouds, cloud_streak
    sky_clouds.sync(scene)
    cloud_streak.sync(scene)
    if s.compositor:
        setup_grade(scene)
        set_sky_value(s.compositor.nodes['MC Film response'].inputs['Fac'],'default_value',0)
        # Contrast is the filmic power curve (camera_grade.apply); this node stays neutral.
        set_sky_value(s.compositor.nodes['MC Color contrast'].inputs['Contrast'],'default_value',0)
        set_sky_value(s.compositor.nodes['MC Color saturation'].inputs['Saturation'],'default_value',s.saturation)
        balance=s.compositor.nodes['MC Color balance']
        gain=(1+.2*s.temperature+.08*s.tint,1-.12*s.tint,1-.2*s.temperature+.08*s.tint)
        if hasattr(balance,'gain'):
            set_sky_value(balance,'gain',gain)
        else:
            set_sky_value(next(socket for socket in balance.inputs if socket.name=='Gain' and socket.type=='RGBA'),'default_value',(*gain,1))
            set_sky_value(next(socket for socket in balance.inputs if socket.name=='Lift' and socket.type=='VALUE'),'default_value',.025*s.shadow_lift)
            set_sky_value(next(socket for socket in balance.inputs if socket.name=='Gamma' and socket.type=='VALUE'),'default_value',1+.3*s.midtone_light)
        # The shot camera's own grade rides on top of the look.
        from . import camera_grade
        camera_grade.apply(scene)
        glare = next((n for n in s.compositor.nodes if n.get(TAG)==s.owner and n.type=='GLARE'),None)
        if glare:
            # Saved compositors may retain an older Streaks setting.
            if hasattr(glare,'glare_type'):
                set_sky_value(glare,'glare_type','FOG_GLOW')
            type_socket=glare.inputs.get('Type') or glare.inputs.get('Glare Type')
            if type_socket:
                set_sky_value(type_socket,'default_value','Fog Glow')
            set_sky_value(glare,'mute',s.bloom == 0)
            if glare.inputs.get('Strength'):
                set_sky_value(glare.inputs['Strength'],'default_value',s.bloom)
            elif hasattr(glare,'mix'):
                set_sky_value(glare,'mix',s.bloom-1)
            if glare.inputs.get('Size'):
                set_sky_value(glare.inputs['Size'],'default_value',s.bloom_size)
            elif hasattr(glare,'size'):
                set_sky_value(glare,'size',round(6+3*s.bloom_size))
    from . import sun_rays
    sun_rays.update(scene)
    from . import ground_bounce
    ground_bounce.update(scene)
    # Plain water dims toward night like the land it reflects.
    from . import water
    water.set_daylight(scene, day)

    lighting_pipeline.update(scene)


def apply(scene, preset):
    if not supports_sky():
        raise RuntimeError(SKY_REQUIREMENT)
    s = scene.mcbedrock_sky
    if preset not in PRESETS:
        raise ValueError('Unknown sky preset')
    # Linked scene copies can share the rig pointers. Detach before rebuilding.
    shared = s.active and any(sc != scene and sc.mcbedrock_sky.active and sc.mcbedrock_sky.owner==s.owner for sc in bpy.data.scenes)
    if shared:
        restore(scene)
    if s.active and (not s.world or not s.collection or not s.compositor):
        restore(scene)
    if s.cycle_enabled:
        toggle_cycle(scene)
    BUSY.add(scene.as_pointer())
    try:
        if not s.active:
            source = discover(scene)
            s.owner = uuid.uuid4().hex
            images = load_images(source,s.owner)
            s.source_pack = source[0] if source else ''
            s.previous_world = scene.world
            s.previous_exposure = scene.view_settings.exposure
            s.previous_view = scene.view_settings.view_transform
            s.previous_look = scene.view_settings.look
            s.previous_transparent = scene.render.film_transparent
            s.previous_lock_interface = scene.render.use_lock_interface
            s.previous_use_compositing = scene.render.use_compositing
            s.native_color_version = 1
            native_color.select(scene, True)
            s.previous_eevee = json.dumps({key:getattr(scene.eevee,key) for key in ('use_raytracing','use_fast_gi','use_shadow_jitter_viewport') if hasattr(scene.eevee,key)})
            s.previous_cycles = ''
            s['cycles_setup_version'] = 0
            s.lights.clear()
            for obj in scene.objects:
                if obj.type == 'LIGHT':
                    # Shared light objects must not change another scene's render.
                    if obj.library or sum(obj.name in sc.objects for sc in bpy.data.scenes) > 1:
                        continue
                    entry = s.lights.add()
                    entry.object, entry.render = obj, obj.hide_render
                    entry.viewport = obj.hide_viewport
                    obj.hide_render = obj.hide_viewport = True
            s.collection = bpy.data.collections.new('Sparkle Sky & Lighting · '+scene.name)
            s.collection[TAG] = s.owner
            scene.collection.children.link(s.collection)
            build_world(scene, images)
            for role in ('Sun','Moon','Fill'):
                light(scene,role)
            s.active = True
            setup_compositor(scene)
        s.preset = preset
        for key, value in PRESETS[preset].items():
            if key != 'label':
                setattr(s,key,value)
        for key, value in dict(azimuth=270,sun_size=9,moon_size=8,phase='FULL',glow=.5,sun_haze=.2,sun_haze_radius=25,softness=5,fill=.45,cloud_preset='CLASSIC',cloud_height=256,cloud_thickness=5,moving=False,cloud_speed=1.5,cloud_direction=0).items():
            setattr(s,key,value)
        for key,value in dict(sky_glow=0,glow_spread=.25,glow_softness=.5,bloom=0,bloom_size=.5,edge_highlights=0,fog=0,fog_distance=100,exposure=0,contrast=0,saturation=1,temperature=0,tint=0,sky_brightness=1,horizon_height=.22,sky_color_mix=0,gradient_mapping=False,gradient_start=.5,gradient_end=1,gradient_power=3).items():
            setattr(s,key,value)
        s.sunlight=s.moonlight=s.ambient_light=1
        s.horizon_haze=.4
        s.horizon_position=0
        s.ambient_light=.75
        s.key_height_offset=0
        s.sun_light_warmth=s.sun_heading_offset=s.sun_world_haze=0
        s.daylight_balance=0
        for key in ('horizon_color','zenith_color','sun_color','moon_color','cloud_color','fog_color','shadow_color','sun_light_color','sun_world_haze_color'):
            setattr(s,key,s.bl_rna.properties[key].default_array)
        atmosphere.defaults(scene)
        if preset in {'VIBRANT','DUNGEONS'}:
            apply_cloud_preset(scene,preset)
        s['loocs_enabled']=native_color.wants_loocs(scene)
        s['preview_quality']='AUTO'
        scene.world = s.world
        scene.render.film_transparent = False
        scene.render.use_lock_interface = True
        # Element renders Minecraft at a 90-degree shutter; 180 smears blocks.
        if abs(scene.render.motion_blur_shutter - .5) < 1e-6:
            scene.render.motion_blur_shutter = .25
        for key in ('use_raytracing','use_fast_gi'):
            if hasattr(scene.eevee,key):
                setattr(scene.eevee,key,True)
    finally:
        BUSY.discard(scene.as_pointer())
    aim_key(scene)
    lighting_look(scene,'TRAILER',set_exposure=False,overrides=PRESET_OVERRIDES.get(preset))
    lighting_pipeline.setup(scene)
    lighting_pipeline.update(scene)
    s.preset_settings=preset_signature(s)
    # Distance reads through painted fog layers in front of the camera
    # instead of a haze over the whole world (atmosphere.defaults). Without a
    # camera yet, the fog is added as soon as the scene has one.
    planes = getattr(scene, 'mcbedrock_fog_planes', None)
    if ADD_FOG_PLANES and planes is not None:
        from . import fog_planes
        if not planes.active:
            for key, value in FOG_PLANES.items():
                setattr(planes, key, value)
            scene[FOG_PLANES_TAG] = True
            if scene.camera is not None:
                fog_planes.create(scene)
            else:
                scene[fog_planes.PENDING] = True


def preset_signature(settings):
    """Capture editable look values, excluding rig pointers and runtime caches."""
    values={}
    for name in PRESET_FIELDS:
        value=getattr(settings,name)
        values[name]=tuple(value) if getattr(settings.bl_rna.properties[name],'is_array',False) else value
    return json.dumps(values,sort_keys=True)


def is_preset_current(settings,state=None):
    if not settings.active or not settings.preset_settings:
        return False
    # Cloud drags are deferred until release; reflect their preview immediately.
    for key,value in (state or {}).items():
        if key.startswith('sky.pending.') and value!=getattr(settings,key.removeprefix('sky.pending.')):
            return False
    return settings.preset_settings==preset_signature(settings)


def reset_section(scene, section):
    """Reset only the requested section, with one update and one undo step."""
    s=scene.mcbedrock_sky
    fields={
        'LIGHTING': ('time','brightness','warmth','light_direction_mode','sunlight','moonlight','fill','ambient_light','softness','penumbra','azimuth','sun_light_warmth','daylight_balance','sun_light_color','sun_heading_offset','key_height_offset','shadow_color','contact_shadows','surface_matte','surface_sheen','grass_vibrance','leaf_scattering','vegetation_translucency','texture_vibrance','texture_brightness','foliage_color','surface_tint','texture_compensation','blue_noise_opacity','look_strength'),
        'SKY': ('cloud_preset','dungeons_cloud_density','dungeons_cloud_scale','clouds','cloud_height','cloud_thickness','cloud_speed','cloud_direction','moving','cloud_streak','cloud_streak_length','cloud_color','sky_brightness','horizon_height','horizon_haze','horizon_position','sky_color_mix','gradient_mapping','gradient_start','gradient_end','gradient_power','horizon_color','zenith_color','sun_size','moon_size','sun_color','moon_color','stars','star_amount','star_brightness','star_rotation','glow','glow_spread','glow_softness','sun_haze','sun_haze_radius','plain_background','plain_background_color'),
        'COLOR': ('edge_highlights','exposure','contrast','saturation','temperature','tint','shadow_lift','midtone_light','bloom','bloom_size'),
        'GLOW': GLOW_FIELDS,
        'ATMOSPHERE': ('flat_fog','flat_fog_start','flat_fog_distance','flat_fog_color','atmosphere_match_lighting','volume_height_fog','volume_anisotropy','fog','fog_start','fog_distance','fog_color','terrain_fog','sun_world_haze','sun_world_haze_color','sun_world_haze_radiance','sun_world_haze_height','sun_world_haze_height_falloff','sun_world_haze_start','sun_world_haze_distance','sun_world_haze_spread'),
    }
    BUSY.add(scene.as_pointer())
    try:
        for name in fields[section]:
            meta=s.bl_rna.properties[name]
            setattr(s,name,tuple(meta.default_array) if getattr(meta,'is_array',False) else meta.default)
        if section=='LIGHTING':
            for name,value in dict(time=PRESETS[s.preset]['time'],light_direction_mode=default_light_direction(s.preset),sunlight=1.1,**trailer_shadow_defaults(s.preset),daylight_balance=0,brightness=PRESETS[s.preset]['brightness'],warmth=PRESETS[s.preset]['warmth']).items():setattr(s,name,value)
        elif section=='ATMOSPHERE' and atmosphere.enabled(scene):atmosphere.defaults(scene)
        elif section=='SKY':
            s.clouds=PRESETS[s.preset]['clouds']
            if s.preset in {'VIBRANT','DUNGEONS'}:
                apply_cloud_preset(scene,s.preset)
            s.horizon_haze=.4
            s.phase='FULL'
        elif section=='COLOR':
            s.exposure=-.05;s.saturation=1.03;s.temperature=.03
            for i in range(5):
                for name in ('exposure','contrast','saturation','temperature','tint'):setattr(s,f'grade_{i}_{name}',1 if name=='saturation' else 0)
        for name, value in PRESET_OVERRIDES.get(s.preset, {}).items():
            if name in fields[section]:
                setattr(s, name, value)
    finally:BUSY.discard(scene.as_pointer())
    if section=='COLOR':scene.view_settings.exposure=s.exposure
    sky_materials.update(scene)
    if section=='COLOR' and s.compositor and s.compositor.nodes.get('MC Edge highlights'):
        for material in {entry.prepared for entry in s.materials if entry.prepared}:
            lighting_pipeline.edge_details.prepare_material(scene, material)
        lighting_pipeline.setup(scene)
    update(scene,geometry=section=='SKY')


def preview_lighting(scene, screen, *, activate=False):
    """Preview lighting on request, with live compositing off for performance.

    Passive updates must leave the user's shading and compositor choices alone.
    """
    s = scene.mcbedrock_sky
    if not activate or not s.active or screen is None:
        return
    saved = json.loads(s.preview_state or '[]')
    original_state = len(saved)
    values = dict(type='RENDERED', use_scene_world=True, use_scene_lights=True,
                  use_scene_world_render=True, use_scene_lights_render=True,
                  use_compositor='DISABLED')
    for index, area in enumerate(screen.areas):
        if area.type != 'VIEW_3D':
            continue
        shade = area.spaces.active.shading
        desired = {key: value for key, value in values.items() if hasattr(shade, key)}
        if all(getattr(shade, key) == value for key, value in desired.items()):
            continue
        if not any(e['screen'] == screen.name and e['area'] == index for e in saved):
            saved.append({'screen': screen.name, 'area': index,
                          'values': {key: getattr(shade, key) for key in desired}})
        for key, value in desired.items():
            if getattr(shade, key) != value:
                setattr(shade, key, value)
    if len(saved) != original_state:
        s.preview_state = json.dumps(saved)


def restore(scene):
    s = scene.mcbedrock_sky
    if not s.active:
        return
    if scene.get(FOG_PLANES_TAG):
        from . import fog_planes
        fog_planes.remove(scene)
        del scene[FOG_PLANES_TAG]
        scene.pop(fog_planes.PENDING, None)
    owner = s.owner
    shared = any(sc != scene and sc.mcbedrock_sky.active and sc.mcbedrock_sky.owner==owner for sc in bpy.data.scenes)
    collection = s.collection
    sky_materials.restore(scene)
    # The originals are back in their slots: outlined meshes need the lines
    # on them again.
    outlines.prepare(scene)
    lighting_pipeline.restore(scene)
    from .preview_quality import restore as restore_preview_quality
    restore_preview_quality(scene)
    if s.cycle_enabled:
        toggle_cycle(scene)
    s.active = False
    from . import water
    water.set_daylight(scene, 1.)
    scene.world = s.previous_world
    scene.view_settings.view_transform = s.previous_view
    scene.view_settings.look = s.previous_look
    scene.view_settings.exposure = s.previous_exposure
    scene.render.film_transparent = s.previous_transparent
    scene.render.use_lock_interface = s.previous_lock_interface
    scene.render.use_compositing = s.previous_use_compositing
    # Cycles is an add-on that can be turned off; its settings go with it.
    cycles = getattr(scene, 'cycles', None)
    for key, value in json.loads(s.previous_cycles or '{}').items():
        if cycles is not None:
            setattr(cycles, key, value)
    s.previous_cycles = ''
    s['cycles_setup_version'] = 0
    for key,value in json.loads(s.previous_eevee or '{}').items():
        target=scene.eevee
        parts=key.split('.')
        for part in parts[:-1]:
            target=getattr(target,part)
        if hasattr(target,parts[-1]):
            setattr(target,parts[-1],value)
    for entry in json.loads(s.preview_state or '[]'):
        screen = bpy.data.screens.get(entry['screen'])
        if screen and entry['area'] < len(screen.areas):
            area = screen.areas[entry['area']]
            if area.type == 'VIEW_3D':
                shading = area.spaces.active.shading
                for key, value in entry['values'].items():
                    if hasattr(shading,key):
                        setattr(shading,key,value)
    s.preview_state = ''
    if s.preview_previous_engine:
        if scene.render.engine=='BLENDER_EEVEE':
            scene.render.engine=s.preview_previous_engine
        s.preview_previous_engine=''
    for entry in s.lights:
        if entry.object:
            still_managed = any(sc != scene and sc.mcbedrock_sky.active and any(item.object==entry.object for item in sc.mcbedrock_sky.lights) for sc in bpy.data.scenes)
            if not still_managed:
                entry.object.hide_render, entry.object.hide_viewport = entry.render, entry.viewport
    s.lights.clear()
    if hasattr(scene,'compositing_node_group'):
        current = scene.compositing_node_group
        # A duplicate of the sky's compositor is the sky's too: Remove puts the
        # scene's own compositor back either way.
        if current is not None and (current == s.compositor or current.get(TAG)):
            scene.compositing_node_group = user_compositor(s.previous_compositor)
    elif s.compositor:
        t = s.compositor
        for n in list(t.nodes):
            if n.get(TAG)!=owner:
                continue
            for output in n.outputs:
                targets=[link.to_socket for link in output.links if link.to_node.get(TAG)!=owner]
                source=n.inputs[6] if n.name=='MC Terrain fog' else n.inputs.get('Image')
                source=source.links[0].from_socket if source and source.links else None
                visited=set()
                while source and source.node.get(TAG)==owner and source.node.name not in visited:
                    visited.add(source.node.name)
                    upstream=source.node.inputs[6] if source.node.name=='MC Terrain fog' else source.node.inputs.get('Image')
                    source=upstream.links[0].from_socket if upstream and upstream.links else None
                if source:
                    for target in targets:
                        t.links.new(source,target)
        for n in list(t.nodes):
            if n.get(TAG) == owner and n.type == 'GLARE':
                source = n.inputs['Image'].links[0].from_socket if n.inputs['Image'].links else None
                targets = [link.to_socket for link in n.outputs['Image'].links]
                if source and source.node.get(TAG) != owner:
                    for target in targets:
                        t.links.new(source,target)
            if n.get(TAG) == owner:
                t.nodes.remove(n)
    scene.use_nodes = s.previous_use_nodes
    s.world = None
    s.compositor = None
    s.collection = None
    s.cloud_image = None
    retained_elsewhere = collection and any(sc != scene and collection in sc.collection.children_recursive for sc in bpy.data.scenes)
    if shared or retained_elsewhere:
        if collection and collection.name in scene.collection.children:
            scene.collection.children.unlink(collection)
        s.owner = ''
        return
    # Remove only this scene's owned objects; retain data linked elsewhere.
    for obj in list(bpy.data.objects):
        if obj.get(TAG) == owner:
            if not any(sc != scene and obj.name in sc.objects for sc in bpy.data.scenes):
                bpy.data.objects.remove(obj,do_unlink=True)
    for coll in list(bpy.data.collections):
        if coll.get(TAG) == owner:
            bpy.data.collections.remove(coll)
    for datablocks in (bpy.data.worlds,bpy.data.meshes,bpy.data.lights,bpy.data.lightprobes,bpy.data.materials,bpy.data.node_groups,bpy.data.images):
        for data in list(datablocks):
            if data.get(TAG)==owner and not data.users:
                datablocks.remove(data)
    s.owner = ''


def haze_changed(self, context):
    if not supports_sky():
        return
    scene=self.id_data
    if scene.as_pointer() in BUSY or not self.active:return
    tree=self.compositor
    if tree is None:return
    if not self.terrain_fog:
        update_fog(scene)
    if atmosphere.enabled(scene):
        atmosphere.update(scene)
        return
    if not tree.nodes.get('MC Native mist strength'):
        lighting_pipeline.setup(scene)
    if self.sun_haze_mode=='SUN':lighting_pipeline.update_sun_haze(scene)
    lighting_pipeline.native_mist.update(scene)
    haze=tree.nodes.get('MC Sun on terrain')
    if haze:lighting_pipeline.set_value(haze.inputs[7],'default_value',(*(c*self.sun_world_haze_radiance for c in self.sun_world_haze_color),1))


def flat_fog_changed(self, context):
    if not supports_sky() or not self.active or self.id_data.as_pointer() in BUSY:
        return
    scene = self.id_data
    from . import flat_fog
    if self.compositor and any(self.compositor.nodes.get(name) is None for name in flat_fog.REQUIRED):
        lighting_pipeline.setup(scene)
    flat_fog.update(scene)


def edge_changed(self, context):
    if not supports_sky() or not self.active or self.id_data.as_pointer() in BUSY:
        return
    scene = self.id_data
    node = self.compositor.nodes.get('MC Edge highlights') if self.compositor else None
    enabled = self.edge_highlights > 0
    if enabled != bool(node and not node.mute):
        if enabled:
            sky_materials.prepare(scene)
        else:
            for material in {entry.prepared for entry in self.materials if entry.prepared}:
                lighting_pipeline.edge_details.prepare_material(scene, material)
        lighting_pipeline.setup(scene)
    else:
        lighting_pipeline.edge_details.update(scene)


def light_blocks_changed(self, context):
    if supports_sky() and self.active and self.id_data.as_pointer() not in BUSY:
        light_blocks.update(self.id_data)
        light_blocks.place_lights(self.id_data)


def exposure_changed(self, context):
    if not supports_sky():
        return
    if self.active and self.id_data.as_pointer() not in BUSY:
        self.id_data.view_settings.exposure=self.exposure
        update(self.id_data)


def changed(self, context):
    if not supports_sky():
        return
    scene = self.id_data
    if scene.as_pointer() not in BUSY:
        if self.cycle_enabled and scene.frame_end == self.cycle_end:
            self.cycle_end = int(self.cycle_start) + max(1, round(self.cycle_seconds * scene.render.fps / scene.render.fps_base)) - 1
            scene.frame_end = self.cycle_end
        update(scene)


def cloud_preset_changed(self, context):
    if supports_sky() and self.id_data.as_pointer() not in BUSY:
        apply_cloud_preset(self.id_data, self.cloud_preset)


@persistent
def clear_update_cache(*args):
    # Undo/load can restore RNA without property callbacks. Never carry a
    # successful-update signature across those datablock replacements.
    lighting_pipeline._update_signatures.clear()
    lighting_pipeline._signatures.clear()
    lighting_pipeline._material_counts.clear()
    lighting_pipeline._material_dirty.clear()
    _character_names.clear()
    sky_drive.clear()


def surface_changed(self, context):
    if not supports_sky():
        return
    if self.id_data.as_pointer() not in BUSY:
        sky_materials.update(self.id_data)


def look_changed(self, context):
    if not supports_sky():
        return
    if self.id_data.as_pointer() not in BUSY:
        sky_materials.update(self.id_data)
        update(self.id_data)


@persistent
def frame_update(scene, *args):
    if not supports_sky():
        return
    if hasattr(scene,'mcbedrock_sky'):
        s=scene.mcbedrock_sky
        follow_compositor(scene)
        tone=s.compositor.nodes.get('MC Film response') if s.compositor else None
        old_grade=s.compositor and (tone is None or tone.get('mc_grade_version',0)<2)
        old_world=s.world and (s.world.get('mc_sky_shader_version',0)!=SKY_SHADER_VERSION or 'Ambient light color' not in s.world.node_tree.nodes)
        old_clouds=s.collection and s.collection.get('cloud_layout_version') != CLOUD_LAYOUT_VERSION
        missing_rig=not s.collection
        missing_clouds=s.clouds>0 and owned(scene,'Cloud') is None
        old_eevee=s.collection and s.collection.get('eevee_setup_version') != EEVEE_SETUP_VERSION
        changed_engine=s.collection and s.collection.get('contact_shading_engine')!=scene.render.engine
        if s.active and s.world and (missing_rig or missing_clouds or old_world or old_grade or old_clouds or old_eevee or changed_engine) and scene.as_pointer() not in BUSY:
            BUSY.add(scene.as_pointer())
            try:
                update(scene)
            finally:
                BUSY.discard(scene.as_pointer())
        position_clouds(scene)
        sun = owned(scene,'Sun') if s.active and s.collection else None
        if sun is not None and hand_rotated(sun) and scene.as_pointer() not in BUSY:
            # Shadows follow a hand-rotated sun live, not only on the next update.
            BUSY.add(scene.as_pointer())
            try:
                update(scene)
            finally:
                BUSY.discard(scene.as_pointer())
        if s.active:
            aim_character_fill(scene)
            from . import camera_grade
            camera_grade.follow_cut(scene)


@persistent
def render_update(scene, *args):
    if not supports_sky():
        return
    if hasattr(scene,'mcbedrock_sky'):
        if scene.mcbedrock_sky.active and (scene.mcbedrock_sky.loocs_enabled or atmosphere.enabled(scene)) and scene.mcbedrock_sky.auto_materials:
            # Slots already matched and nothing structural changed since:
            # preparing again redid the same work before every render (0.4 s
            # on an imported world, every frame of an animation). The first
            # render after loading a file still prepares.
            if not lighting_pipeline.materials_unchanged(scene):
                sky_materials.prepare(scene)
            lighting_pipeline.setup(scene)
        update(scene)


@persistent
def cycle_update(scene, *args):
    if not supports_sky():
        return
    if not hasattr(scene, 'mcbedrock_sky') or scene.as_pointer() in BUSY:
        return
    s = scene.mcbedrock_sky
    if s.active and s.cycle_enabled:
        BUSY.add(scene.as_pointer())
        try:
            # Every frame moves the sun: values go to the shaders through
            # their drivers, without a node-tree update per value.
            with sky_drive.per_frame():
                update(scene)
        finally:
            BUSY.discard(scene.as_pointer())


class SkyLightState(bpy.types.PropertyGroup):
    object: bpy.props.PointerProperty(type=bpy.types.Object)
    render: bpy.props.BoolProperty()
    viewport: bpy.props.BoolProperty()


class SkyMaterialState(bpy.types.PropertyGroup):
    object: bpy.props.PointerProperty(type=bpy.types.Object)
    original: bpy.props.PointerProperty(type=bpy.types.Material)
    prepared: bpy.props.PointerProperty(type=bpy.types.Material)
    index: bpy.props.IntProperty()
    link: bpy.props.StringProperty()


def pipeline_changed(self, context):
    if not supports_sky():
        return
    scene=self.id_data
    if scene.as_pointer() in BUSY or not self.active:return
    native_color.select(scene, self.loocs_enabled)
    if atmosphere.enabled(scene):
        lighting_pipeline.update(scene)
        return
    BUSY.add(scene.as_pointer())
    try:
        sky_materials.prepare(scene)
        lighting_pipeline.setup(scene)
        update(scene)
    finally:BUSY.discard(scene.as_pointer())


class MCBEDROCK_OT_lighting_group(SkyOperator, bpy.types.Operator):
    bl_idname='mcbedrock.lighting_group'
    bl_label='Assign lighting grade group'
    bl_options={'REGISTER','UNDO'}
    group:bpy.props.IntProperty(default=1,min=0,max=4)
    def execute(self,context):
        targets=set(context.selected_objects)
        for obj in context.selected_objects:targets.update(obj.children_recursive)
        for obj in targets:
            if not obj.library:obj['mc_lighting_group']=self.group
        if context.scene.mcbedrock_sky.active:
            sky_materials.prepare(context.scene)
            lighting_pipeline.setup(context.scene)
        return {'FINISHED'}


class SkySettings(bpy.types.PropertyGroup):
    # Zero is deliberate: existing saved looks require an explicit upgrade.
    atmosphere_version:bpy.props.IntProperty(default=0)
    atmosphere_sun_direction:bpy.props.FloatVectorProperty(size=3, default=(0, 0, 1), options={'HIDDEN'})
    atmosphere_density_uniform:bpy.props.FloatVectorProperty(size=3, default=(0, 20, 180), options={'HIDDEN'})
    atmosphere_height_uniform:bpy.props.FloatVectorProperty(size=3, default=(0, 40, 1), options={'HIDDEN'})
    atmosphere_render_uniform:bpy.props.FloatVectorProperty(size=3, default=(3, 0, 0), options={'HIDDEN'})
    atmosphere_height_enabled:bpy.props.BoolProperty(name='Height fog',default=True,update=haze_changed)
    atmosphere_auto_color:bpy.props.BoolProperty(name='Follow sky lighting',default=True,update=haze_changed)
    atmosphere_match_lighting:bpy.props.BoolProperty(name='Match sky and sun colors',description='Use the current horizon and sunlight colors for haze; manual colors remain stored',default=False,update=haze_changed)
    volume_height_fog:bpy.props.BoolProperty(name='Height fog',default=False,update=changed)
    volume_anisotropy:bpy.props.FloatProperty(name='Light shaft directionality',description='Higher values concentrate volumetric scattering toward the sun; requires volumetric fog and shadow casters',default=.2,min=0,max=.85,update=changed)
    edge_highlights:bpy.props.FloatProperty(name='Edge highlights',description='Subtle geometric edge lift; excludes named foliage and transparent materials. Requires compositing',default=0,min=0,max=1,update=edge_changed)
    preview_quality:bpy.props.EnumProperty(name='Viewport quality',items=[('AUTO','Auto','Use fast preview while navigating; refine after stopping'),('FULL','Full','Full viewport quality'),('FAST','Fast','Reduced resolution and stable shadows')],default='FULL',update=preview_quality.mode_changed)
    auto_materials:bpy.props.BoolProperty(name='Prepare new scene materials',default=True)
    loocs_enabled:bpy.props.BoolProperty(name='LOOCS color rendering',default=True,update=pipeline_changed)
    texture_compensation:bpy.props.FloatProperty(name='Texture compensation',default=1,min=0,max=1,update=changed)
    loocs_rendering:bpy.props.EnumProperty(name='Color rendering',items=[(key,name,'LOOCS '+name+' color rendering') for key,name in native_color.RENDERINGS],
        default='CHROMA_LUMA',update=lambda self,context:native_color.rendering_changed(self.id_data))
    # Explicit numbers: files store the number, so Sparkle classic joins without renumbering the rest.
    loocs_texture_correction:bpy.props.EnumProperty(name='Texture correction',items=[('MATCH','Match Rendering',"Sparkle classic for Chroma Luma; LOOCS's own correction for the other renderings",0),
        ('CLASSIC','Sparkle classic',"Sparkle's correction for Chroma Luma: the colors before LOOCS 0.12",10),
        *[(key,native_color.RENDERING_LABELS[key],'Correct textures for LOOCS '+native_color.RENDERING_LABELS[key],number) for number,key in enumerate(native_color.CORRECTIONS,1)],
        ('NONE','Off','Keep textures as they are',9)],default='MATCH',update=lambda self,context:native_color.correction_changed(self.id_data))
    loocs_auto_textures:bpy.props.BoolProperty(name='Correct textures automatically',default=True,
        description='Imported models and worlds use the LOOCS "sRGB TC" color space that matches the current rendering',
        update=lambda self,context:native_color.correction_changed(self.id_data))
    loocs_view:bpy.props.BoolProperty(name='Use LOOCS',description='Show the scene through the LOOCS color rendering',
        get=native_color.get_enabled,set=native_color.set_enabled)
    loocs_output:bpy.props.EnumProperty(name='Output',items=native_color.OUTPUTS,
        get=native_color.get_output,set=native_color.set_output)
    emissive_glow:bpy.props.FloatProperty(name='Glow strength',description='How much light blocks and other glowing surfaces spread light around them',default=.6,min=0,max=5,soft_max=2,update=changed)
    light_glow_size:bpy.props.FloatProperty(name='Glow size',description='Size of the glow around lights. It follows the image height, so it looks the same at any resolution',default=.5,min=0,max=1,update=changed)
    light_glow_wide:bpy.props.FloatProperty(name='Wide haze',description='The soft outer haze around lights, beyond their bright core',default=.5,min=0,max=1,update=changed)
    light_glow_warmth:bpy.props.FloatProperty(name='Warm haze',description='The outer haze turns warmer, as light does over a long path through air',default=.5,min=0,max=1,update=changed)
    sky_glow:bpy.props.FloatProperty(name='Sky glow',description='Bright sky spreads over the edges of terrain and buildings in front of it. Off by default: it painted dark buildings against the sky sky-blue',default=0,min=0,max=1,update=changed)
    sun_rays:bpy.props.FloatProperty(name='Sun rays',description='Light shafts streaking from the sun through gaps in trees and terrain, like the trailers. Shows in renders',default=0,min=0,max=1,update=changed)
    film_look:bpy.props.BoolProperty(name='Film look',description='Halation around bright lights, grain that sticks to the world and a slight gate weave, as in Element Animation films. Shows in renders',default=False,update=changed)
    fog_light:bpy.props.BoolProperty(name='Fog light',description='Light the volume fog with its own light, apart from the land: the sun and sky stop lighting the fog',default=False,update=changed)
    fog_light_strength:bpy.props.FloatProperty(name='Fog light strength',default=3.,min=0,max=50,soft_max=10,update=changed)
    fog_light_color:bpy.props.FloatVectorProperty(name='Fog light color',subtype='COLOR',size=3,default=(.72,.84,1.),min=0,max=1,update=changed)
    sun_rays_length:bpy.props.FloatProperty(name='Ray length',description='How far the shafts reach across the frame',default=.35,min=.02,max=1,update=changed)
    light_blocks:bpy.props.BoolProperty(name='Light up torches and lanterns',description='Torches, lanterns, glowstone and other light blocks glow in Sparkle scene materials',default=True,update=light_blocks_changed)
    block_lights:bpy.props.BoolProperty(name='Torches light their surroundings',description='A warm point light in every torch, lantern and other light block, so they light the blocks around them',default=True,update=light_blocks_changed)
    light_block_brightness:bpy.props.FloatProperty(name='Light block brightness',description='Brightness of the glowing pixels of light blocks',default=2,min=0,max=20,soft_max=5,update=light_blocks_changed)
    blue_noise_opacity:bpy.props.BoolProperty(name='Blue-noise opacity (experimental)',default=False,update=pipeline_changed)
    grade_0_exposure:bpy.props.FloatProperty(name='Background exposure',default=0,min=-3,max=3,update=changed)
    grade_0_saturation:bpy.props.FloatProperty(name='Background saturation',default=1,min=0,max=2,update=changed)
    grade_0_contrast:bpy.props.FloatProperty(name='Background contrast',default=0,min=-50,max=50,update=changed)
    grade_0_temperature:bpy.props.FloatProperty(name='Background temperature',default=0,min=-1,max=1,update=changed)
    grade_0_tint:bpy.props.FloatProperty(name='Background tint',default=0,min=-1,max=1,update=changed)
    grade_1_exposure:bpy.props.FloatProperty(name='Group 1 exposure',default=0,min=-3,max=3,update=changed)
    grade_1_saturation:bpy.props.FloatProperty(name='Group 1 saturation',default=1,min=0,max=2,update=changed)
    grade_2_exposure:bpy.props.FloatProperty(name='Group 2 exposure',default=0,min=-3,max=3,update=changed)
    grade_2_saturation:bpy.props.FloatProperty(name='Group 2 saturation',default=1,min=0,max=2,update=changed)
    grade_3_exposure:bpy.props.FloatProperty(name='Group 3 exposure',default=0,min=-3,max=3,update=changed)
    grade_3_saturation:bpy.props.FloatProperty(name='Group 3 saturation',default=1,min=0,max=2,update=changed)
    grade_4_exposure:bpy.props.FloatProperty(name='Group 4 exposure',default=0,min=-3,max=3,update=changed)
    grade_4_saturation:bpy.props.FloatProperty(name='Group 4 saturation',default=1,min=0,max=2,update=changed)

    grade_1_contrast:bpy.props.FloatProperty(name='Group 1 contrast',default=0,min=-50,max=50,update=changed)
    grade_1_temperature:bpy.props.FloatProperty(name='Group 1 temperature',default=0,min=-1,max=1,update=changed)
    grade_1_tint:bpy.props.FloatProperty(name='Group 1 tint',default=0,min=-1,max=1,update=changed)
    grade_2_contrast:bpy.props.FloatProperty(name='Group 2 contrast',default=0,min=-50,max=50,update=changed)
    grade_2_temperature:bpy.props.FloatProperty(name='Group 2 temperature',default=0,min=-1,max=1,update=changed)
    grade_2_tint:bpy.props.FloatProperty(name='Group 2 tint',default=0,min=-1,max=1,update=changed)
    grade_3_contrast:bpy.props.FloatProperty(name='Group 3 contrast',default=0,min=-50,max=50,update=changed)
    grade_3_temperature:bpy.props.FloatProperty(name='Group 3 temperature',default=0,min=-1,max=1,update=changed)
    grade_3_tint:bpy.props.FloatProperty(name='Group 3 tint',default=0,min=-1,max=1,update=changed)
    grade_4_contrast:bpy.props.FloatProperty(name='Group 4 contrast',default=0,min=-50,max=50,update=changed)
    grade_4_temperature:bpy.props.FloatProperty(name='Group 4 temperature',default=0,min=-1,max=1,update=changed)
    grade_4_tint:bpy.props.FloatProperty(name='Group 4 tint',default=0,min=-1,max=1,update=changed)
    active: bpy.props.BoolProperty(default=False)
    owner: bpy.props.StringProperty()
    source_pack: bpy.props.StringProperty(subtype='FILE_PATH')
    # Legacy texture reference, cleared when the original cloud system migrates.
    cloud_image: bpy.props.PointerProperty(type=bpy.types.Image)
    preset: bpy.props.StringProperty(default='DAY')
    preset_settings: bpy.props.StringProperty(options={'HIDDEN'})
    collection: bpy.props.PointerProperty(type=bpy.types.Collection)
    world: bpy.props.PointerProperty(type=bpy.types.World)
    previous_world: bpy.props.PointerProperty(type=bpy.types.World)
    compositor: bpy.props.PointerProperty(type=bpy.types.NodeTree)
    previous_compositor: bpy.props.PointerProperty(type=bpy.types.NodeTree)
    previous_use_nodes: bpy.props.BoolProperty()
    previous_transparent: bpy.props.BoolProperty()
    previous_lock_interface: bpy.props.BoolProperty()
    previous_use_compositing: bpy.props.BoolProperty(default=True)
    previous_eevee: bpy.props.StringProperty()
    previous_cycles: bpy.props.StringProperty()
    previous_display_settings: bpy.props.StringProperty()
    native_color_version: bpy.props.IntProperty(default=0)
    native_color_pending: bpy.props.BoolProperty(default=False)
    native_previous_view: bpy.props.StringProperty()
    native_previous_look: bpy.props.StringProperty()
    previous_mist_passes: bpy.props.StringProperty()
    previous_exposure: bpy.props.FloatProperty()
    previous_view: bpy.props.StringProperty()
    previous_look: bpy.props.StringProperty()
    preview_state: bpy.props.StringProperty()
    preview_previous_engine: bpy.props.StringProperty()
    fast_preview_state: bpy.props.StringProperty()
    lights: bpy.props.CollectionProperty(type=SkyLightState)
    materials: bpy.props.CollectionProperty(type=SkyMaterialState)
    finish_active: bpy.props.BoolProperty()
    finish_status: bpy.props.StringProperty()
    finish_skipped: bpy.props.IntProperty()
    look_strength: bpy.props.FloatProperty(name='Look strength',default=.75,min=0,max=1,update=look_changed)
    grade_amount: bpy.props.FloatProperty(default=1,min=0,max=1,update=changed)
    texture_vibrance: bpy.props.FloatProperty(name='Texture vibrance',default=0,min=0,max=1,update=surface_changed)
    texture_brightness: bpy.props.FloatProperty(name='Texture brightness',default=0,min=0,max=1,update=surface_changed)
    foliage_color: bpy.props.FloatProperty(name='Fresh greens',default=0,min=0,max=1,update=surface_changed)
    grass_vibrance: bpy.props.FloatProperty(name='Grass vibrance',description='Strengthen green grass pixels without shifting hue or affecting leaves and dirt',default=0,min=0,max=1,update=surface_changed)
    surface_sheen: bpy.props.FloatProperty(name='Surface highlights',default=0,min=0,max=1,update=surface_changed)
    surface_matte: bpy.props.FloatProperty(name='Matte surfaces',description='Soften plastic-looking highlights on ordinary surfaces; preserve ice, water, glass and mapped materials',default=.75,min=0,max=1,update=surface_changed)
    leaf_scattering: bpy.props.FloatProperty(name='Leaf scattering',description='Optional subsurface effect; can reveal block seams on imported leaves. Leave at zero for clean block faces',default=0,min=0,max=1,update=surface_changed)
    vegetation_translucency: bpy.props.FloatProperty(name='Grass and flower translucency',description='Allow backlighting through thin vegetation; preserve texture alpha',default=.3,min=0,max=.75,update=surface_changed)
    surface_tint: bpy.props.FloatVectorProperty(name='Surface tint',subtype='COLOR',size=3,default=(1,1,1),min=0,max=1,update=surface_changed)
    penumbra: bpy.props.FloatProperty(name='Soft light layers',default=.8,min=0,max=1,update=changed)
    time: bpy.props.FloatProperty(name='Time of day',default=10,min=0,max=24,update=changed)
    cycle_enabled: bpy.props.BoolProperty(default=False)
    cycle_start: bpy.props.FloatProperty()
    cycle_seconds: bpy.props.FloatProperty(name='Cycle length (seconds)',default=30,min=5,max=600,update=changed)
    cycle_end: bpy.props.IntProperty()
    previous_frame_end: bpy.props.IntProperty()
    previous_frame_start: bpy.props.IntProperty()
    brightness: bpy.props.FloatProperty(name='Brightness',default=1,min=.1,max=3,update=changed)
    warmth: bpy.props.FloatProperty(name='Warmth',default=.15,min=0,max=1,update=changed)
    clouds: bpy.props.FloatProperty(name='Clouds',default=.25,min=0,max=1,update=changed)
    stars: bpy.props.BoolProperty(name='Show stars',default=False,update=changed)
    star_brightness: bpy.props.FloatProperty(name='Star brightness',default=.7,min=0,max=2,update=changed)
    star_amount: bpy.props.FloatProperty(name='Star amount',default=.65,min=0,max=1,update=changed)
    star_rotation: bpy.props.FloatProperty(name='Star position',default=0,min=0,max=360,update=changed)
    azimuth: bpy.props.FloatProperty(name='Sun direction',default=270,min=0,max=360,update=changed)
    sun_size: bpy.props.FloatProperty(name='Sun size',default=9,min=1,max=25,update=changed)
    moon_size: bpy.props.FloatProperty(name='Moon size',default=8,min=1,max=25,update=changed)
    phase: bpy.props.EnumProperty(name='Moon phase',items=[(key,label,label) for key,label,_ in PHASES],update=changed)
    glow: bpy.props.FloatProperty(name='Glow',default=.5,min=0,max=1,update=changed)
    glow_spread: bpy.props.FloatProperty(name='Glow spread',default=.25,min=.05,max=.8,update=changed)
    glow_softness: bpy.props.FloatProperty(name='Edge softness',default=.5,min=0,max=1,update=changed)
    sun_haze: bpy.props.FloatProperty(name='Wide sun glow',default=.2,min=0,max=1,update=changed)
    sun_haze_radius: bpy.props.FloatProperty(name='Wide glow radius',default=25,min=15,max=90,update=changed)
    bloom: bpy.props.FloatProperty(name='Lens bloom',default=0,min=0,max=1,update=changed)
    bloom_size: bpy.props.FloatProperty(name='Bloom spread',default=.5,min=0,max=1,update=changed)
    fog: bpy.props.FloatProperty(name='Fog amount',default=0,min=0,max=1,update=changed)
    fog_distance: bpy.props.FloatProperty(name='Fog fade distance',description='Distance beyond the clear foreground over which fog builds up, in scene units',default=100,min=10,max=1000000,soft_max=2000,update=changed)
    exposure: bpy.props.FloatProperty(name='Exposure',default=0,min=-3,max=3,update=exposure_changed)
    contrast: bpy.props.FloatProperty(name='Contrast',default=0,min=-50,max=50,update=changed)
    saturation: bpy.props.FloatProperty(name='Saturation',default=1,min=0,max=2,update=changed)
    shadow_lift: bpy.props.FloatProperty(name='Shadow lift',default=0,min=-1,max=1,update=changed)
    midtone_light: bpy.props.FloatProperty(name='Midtone light',default=0,min=-1,max=1,update=changed)
    temperature: bpy.props.FloatProperty(name='Temperature',default=0,min=-1,max=1,update=changed)
    tint: bpy.props.FloatProperty(name='Tint',default=0,min=-1,max=1,update=changed)
    sky_brightness: bpy.props.FloatProperty(name='Sky brightness',default=1,min=.1,max=3,update=changed)
    horizon_height: bpy.props.FloatProperty(name='Horizon height',default=.22,min=.05,max=1,update=changed)
    horizon_haze: bpy.props.FloatProperty(name='Horizon haze',description='Blend the pale horizon into the upper sky; zero removes the pale wash without changing scene lighting',default=.4,min=0,max=1,update=changed)
    horizon_position: bpy.props.FloatProperty(name='Haze height',description='Move the sky haze down with negative values or up with positive values, in degrees',default=0,min=-90,max=90,update=changed)
    gradient_mapping: bpy.props.BoolProperty(name='Shape sky gradient',default=False,update=changed)
    gradient_start: bpy.props.FloatProperty(name='Gradient start',description='Vertical sky coordinate where the transition starts; 0.50 is the horizon',default=.5,min=0,max=1,update=changed)
    gradient_end: bpy.props.FloatProperty(name='Gradient end',description='Vertical sky coordinate where the transition ends; 1.00 is overhead',default=1,min=0,max=1,update=changed)
    gradient_power: bpy.props.FloatProperty(name='Gradient curve',description='Power applied to the clamped vertical range before the quadratic gradient',default=3,min=.25,max=8,update=changed)
    sky_color_mix: bpy.props.FloatProperty(name='Custom sky colors',default=0,min=0,max=1,update=changed)
    horizon_color: bpy.props.FloatVectorProperty(name='Horizon color',subtype='COLOR',size=3,default=(.48,.68,.77),min=0,max=1,update=changed)
    zenith_color: bpy.props.FloatVectorProperty(name='Upper sky color',subtype='COLOR',size=3,default=(.005,.28,.6),min=0,max=1,update=changed)
    plain_background: bpy.props.BoolProperty(name='Plain background',description='Show one flat color behind everything, like character key art. Lighting still follows the sky and time of day',default=False,update=changed)
    plain_background_color: bpy.props.FloatVectorProperty(name='Background color',subtype='COLOR',size=3,default=(.78,.26,.52),min=0,max=1,update=changed)
    sun_color: bpy.props.FloatVectorProperty(name='Sun tint',subtype='COLOR',size=3,default=(1,1,1),min=0,max=1,update=changed)
    moon_color: bpy.props.FloatVectorProperty(name='Moon tint',subtype='COLOR',size=3,default=(1,1,1),min=0,max=1,update=changed)
    cloud_color: bpy.props.FloatVectorProperty(name='Cloud color',subtype='COLOR',size=3,default=(.92,.95,1),min=0,max=1,update=changed)
    fog_color: bpy.props.FloatVectorProperty(name='Fog color',subtype='COLOR',size=3,default=(.8,.87,1),min=0,max=1,update=changed)
    # Flat fog: Element-style post-process fog in the sky's horizon color.
    flat_fog: bpy.props.FloatProperty(name='Flat fog',description='Element-style fog laid over the render: distance fades evenly into the horizon color, with no light inside it. Shows in renders. Off by default: stacked on Distance fog it veiled the whole image',default=0.,min=0,max=1,update=flat_fog_changed)
    flat_fog_start: bpy.props.FloatProperty(name='Flat fog start',description='Distance from the camera where the fog begins, in blocks',default=96,min=0,max=100000,soft_max=500,update=flat_fog_changed)
    flat_fog_distance: bpy.props.FloatProperty(name='Flat fog distance',description='How far the fog takes to build up, in blocks',default=120,min=1,max=100000,soft_max=1000,update=flat_fog_changed)
    flat_fog_color: bpy.props.FloatVectorProperty(name='Flat fog tint',description='Tint over the horizon color the fog takes',subtype='COLOR',size=3,default=(1,1,1),min=0,max=2,update=flat_fog_changed)
    softness: bpy.props.FloatProperty(name='Shadow softness',default=5,min=.1,max=15,update=changed)
    fill: bpy.props.FloatProperty(name='Shadow brightness',default=.45,min=0,max=3,update=changed)
    character_fill: bpy.props.FloatProperty(name='Character fill',description='A soft fill on characters and mobs only, from the camera side, so they read in shade, as trailers light characters apart from the set. Casts no shadows and leaves the world unchanged',default=.33,min=0,max=1,update=changed)
    sunlight: bpy.props.FloatProperty(name='Sunlight',default=1,min=0,max=5,update=changed)
    sun_light_warmth: bpy.props.FloatProperty(name='Surface warmth',description='Warm the direct sun on surfaces independently of the sky',default=0,min=0,max=1,update=changed)
    daylight_balance: bpy.props.FloatProperty(name='Daylight color balance',description='Reduce the yellow cast of high daylight while preserving its brightness and warm sunsets',default=0,min=0,max=1,update=changed)
    sun_light_color: bpy.props.FloatVectorProperty(name='Surface sunlight tint',subtype='COLOR',size=3,default=(1,1,1),min=0,max=1,update=changed)
    sun_heading_offset: bpy.props.FloatProperty(name='Light direction offset',description='Rotate the world lighting without moving the sun artwork',default=0,min=-180,max=180,update=changed)
    # Missing values in existing saved skies retain the aligned light direction.
    light_direction_mode: bpy.props.EnumProperty(name='Lighting direction',items=[
        ('TRAILER','Trailer lighting','Raised, crossed light that keeps block faces readable'),
        ('SUN','Follow sun','Match the key light to the visible sun; artist offsets still apply')],
        default='SUN',update=changed)
    sun_haze_mode: bpy.props.EnumProperty(name='Terrain haze',items=[('MIST','Native Mist','Blender Mist pass with the sky and clouds excluded'),('SUN','Sunlit haze','Directional ground haze toward the sun')],default='MIST',update=haze_changed)
    sun_world_haze: bpy.props.FloatProperty(name='Terrain haze',description='Terrain-only haze; preview passes in EEVEE, or render with Cycles',default=0,min=0,max=1,update=haze_changed)
    sun_world_haze_start: bpy.props.FloatProperty(name='Clear foreground distance',default=100,min=0,max=1000,update=haze_changed)
    sun_world_haze_distance: bpy.props.FloatProperty(name='Haze falloff distance',default=300,min=1,max=2000,update=haze_changed)
    sun_world_haze_spread: bpy.props.FloatProperty(name='Haze spread',description='Angle from the sun where haze drops to half strength, in degrees',default=40,min=5,max=85,update=haze_changed)
    sun_world_haze_radiance: bpy.props.FloatProperty(name='Haze brightness',default=1.4,min=0,max=8,update=haze_changed)
    sun_world_haze_height: bpy.props.FloatProperty(name='Haze layer height',description='World height where the ground haze reaches half density',default=0,min=-10000,max=10000,soft_min=-100,soft_max=300,update=haze_changed)
    sun_world_haze_height_falloff: bpy.props.FloatProperty(name='Haze height softness',description='Vertical distance over which ground haze clears, in scene units',default=16,min=.1,max=1000,soft_max=100,update=haze_changed)
    sun_world_haze_color: bpy.props.FloatVectorProperty(name='Sunlit haze tint',subtype='COLOR',size=3,default=(1,.62,.25),min=0,max=1,update=haze_changed)
    moonlight: bpy.props.FloatProperty(name='Moonlight',default=1,min=0,max=5,update=changed)
    trailer_lighting: bpy.props.BoolProperty(default=True,update=changed)
    ambient_light: bpy.props.FloatProperty(name='Ambient light',default=1,min=0,max=3,update=changed)
    contact_shadows: bpy.props.FloatProperty(name='EEVEE contact shading',default=.35,min=0,max=1,update=surface_changed)
    terrain_fog: bpy.props.BoolProperty(name='Terrain fog only',default=True,update=changed)
    fog_start: bpy.props.FloatProperty(name='Fog start distance',description='Keep surfaces within this distance of the current viewport or render camera clear, in scene units',default=40,min=0,max=1000000,soft_max=1000,update=changed)
    shadow_color: bpy.props.FloatVectorProperty(name='Shadow light tint',subtype='COLOR',size=3,default=(1,1,1),min=0,max=1,update=changed)
    key_height_offset: bpy.props.FloatProperty(name='Key height offset',default=0,min=-45,max=45,update=changed)
    # Trailer key aimed from the shot (aim_key). Set by the add-on, not presets.
    key_aimed: bpy.props.BoolProperty(default=False, options={'HIDDEN'})
    key_heading: bpy.props.FloatProperty(name='Key heading', default=0, options={'HIDDEN'})
    cloud_height: bpy.props.FloatProperty(name='Cloud height',default=256,min=-1000000,max=1000000,soft_min=0,soft_max=1024,update=changed)
    cloud_preset: bpy.props.EnumProperty(name='Cloud preset',items=[(key,values['label'], 'Original square cloud recipe inspired by '+values['label']) for key,values in cloud_pattern.PRESETS.items()],default='CLASSIC',update=cloud_preset_changed)
    dungeons_cloud_density: bpy.props.EnumProperty(name='Cloud density',items=[('SPARSE','Sparse','Fewer square cloud clusters with large gaps of open sky'),('DENSE','Dense','More square cloud clusters')],default='SPARSE',update=changed)
    dungeons_cloud_scale: bpy.props.FloatProperty(name='Cloud size',description='Size and spacing of the generated Dungeons cloud shapes',default=1.,min=.5,max=2.,update=changed)
    cloud_thickness: bpy.props.FloatProperty(name='Cloud thickness',default=4,min=.2,max=64,update=changed)
    moving: bpy.props.BoolProperty(name='Moving clouds',default=False,update=changed)
    cloud_speed: bpy.props.FloatProperty(name='Cloud speed',default=1.5,min=0,max=15,update=changed)
    cloud_direction: bpy.props.FloatProperty(name='Cloud direction',default=0,min=0,max=360,update=changed)
    cloud_streak: bpy.props.BoolProperty(name='Motion streaks',description='A long-exposure trail behind the clouds, along the cloud direction, like a timelapse',default=False,update=changed)
    cloud_streak_length: bpy.props.FloatProperty(name='Streak length',description='How far the trail reaches behind the clouds, in blocks',default=60,min=1,max=2000,soft_max=300,update=changed)
    # Outlines are a scene choice, independent of the look presets.


class MCBEDROCK_OT_sky(SkyOperator, bpy.types.Operator):
    bl_idname = 'mcbedrock.sky'
    bl_label = 'Sky & Lighting'
    bl_options = {'REGISTER','UNDO'}
    # SKIP_SAVE: sidebar buttons run operators with undo, which reuses the last
    # values. Without it, Create sky after Remove sky repeated the removal.
    action: bpy.props.StringProperty(default='APPLY',options={'SKIP_SAVE'})
    preset: bpy.props.StringProperty(default='DAY',options={'SKIP_SAVE'})

    def execute(self,context):
        scene = context.scene
        if scene.library:
            self.report({'ERROR'}, 'Make this scene local before editing its sky')
            return {'CANCELLED'}
        try:
            if self.action == 'RESTORE':
                restore(scene)
            elif self.action == 'UPDATE_LOOK':
                apply(scene,scene.mcbedrock_sky.preset)
                preview_lighting(scene,context.screen,activate=True)
            elif self.action in {'RESET_LIGHTING','RESET_ATMOSPHERE','RESET_SKY','RESET_COLOR','RESET_GLOW'}:
                if not scene.mcbedrock_sky.active:return {'CANCELLED'}
                reset_section(scene,self.action.removeprefix('RESET_'))
            elif self.action == 'AIM_KEY':
                s = scene.mcbedrock_sky
                if not s.active:
                    return {'CANCELLED'}
                forward = view_forward(context)
                if forward is None and scene.camera is None:
                    self.report({'WARNING'}, 'Add a camera first: the key is aimed from it')
                    return {'CANCELLED'}
                # From the view the artist is looking through (the camera when
                # in camera view); hand adjustments start over from there.
                aim_key(scene, forward)
                s['sun_heading_offset'] = 0.
                s['key_height_offset'] = 0.
                if s.light_direction_mode != 'TRAILER':
                    s.light_direction_mode = 'TRAILER'
                else:
                    update(scene)
            elif self.action == 'CLOUD_PRESET':
                if not scene.mcbedrock_sky.active:return {'CANCELLED'}
                apply_cloud_preset(scene,self.preset)
            elif self.action == 'CLOUD_DENSITY':
                if not scene.mcbedrock_sky.active:return {'CANCELLED'}
                scene.mcbedrock_sky.dungeons_cloud_density=self.preset
            elif self.action == 'FAST_PREVIEW':
                if not scene.mcbedrock_sky.active:return {'CANCELLED'}
                from .preview_quality import toggle
                toggle(scene)
            elif self.action in {'MEADOW','ADVENTURE'}:
                if not scene.mcbedrock_sky.active:return {'CANCELLED'}
                from .shot_lighting import palette
                palette(scene,self.action)
            elif self.action=='WARM_BACKLIGHT':
                if not scene.mcbedrock_sky.active:return {'CANCELLED'}
                from .shot_lighting import warm_backlight
                warm_backlight(scene)
                if context.screen:
                    bpy.ops.mcbedrock.sky(action='PREVIEW')
            elif self.action == 'REPAIR_WORLD':
                # The sky's world was deleted or another world replaced it.
                if not repair_world(scene):
                    return {'CANCELLED'}
            elif self.action == 'MOVE_CLOUDS':
                cloud=owned(scene,'Cloud')
                if cloud is None:
                    return {'CANCELLED'}
                if context.object and context.object.mode!='OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                for obj in context.selected_objects:
                    obj.select_set(False)
                cloud.hide_select=False
                cloud.hide_set(False)
                cloud.select_set(True)
                context.view_layer.objects.active=cloud
                area=context.area if context.area and context.area.type=='VIEW_3D' else next((a for a in context.screen.areas if a.type=='VIEW_3D'),None)
                if area and not bpy.app.background:
                    with context.temp_override(area=area):
                        bpy.ops.wm.tool_set_by_id(name='builtin.move')
            elif self.action in {'SURFACES','RESTORE_SURFACES'}:
                if not scene.mcbedrock_sky.active:
                    return {'CANCELLED'}
                if self.action == 'SURFACES':
                    scene.mcbedrock_sky.auto_materials=True
                    sky_materials.prepare(scene)
                    bpy.ops.mcbedrock.sky(action='PREVIEW')
                    self.report({'INFO'},scene.mcbedrock_sky.finish_status)
                else:
                    scene.mcbedrock_sky.auto_materials=False
                    sky_materials.restore(scene)
            elif self.action == 'CYCLE':
                if not scene.mcbedrock_sky.active:
                    return {'CANCELLED'}
                toggle_cycle(scene)
                if not bpy.app.background and context.screen:
                    if scene.mcbedrock_sky.cycle_enabled and not context.screen.is_animation_playing:
                        bpy.ops.mcbedrock.sky(action='PREVIEW')
                        bpy.ops.screen.animation_play()
                    elif not scene.mcbedrock_sky.cycle_enabled and context.screen.is_animation_playing:
                        bpy.ops.screen.animation_cancel(restore_frame=False)
            elif self.action in {'NATURAL','TRAILER','GRADE_RESET'}:
                if not scene.mcbedrock_sky.active:
                    return {'CANCELLED'}
                lighting_look(scene,self.action)
                bpy.ops.mcbedrock.sky(action='PREVIEW')
            elif self.action in {'PREVIEW','PREVIEW_EEVEE','PREVIEW_RETURN'}:
                s=scene.mcbedrock_sky
                if not s.active:return {'CANCELLED'}
                if self.action=='PREVIEW_EEVEE':
                    if scene.render.engine!='BLENDER_EEVEE':
                        s.preview_previous_engine=scene.render.engine
                        scene.render.engine='BLENDER_EEVEE'
                elif self.action=='PREVIEW_RETURN' and s.preview_previous_engine:
                    scene.render.engine=s.preview_previous_engine
                    s.preview_previous_engine=''
                update(scene)
                preview_lighting(scene, context.screen, activate=True)
            else:
                apply(scene,scene.mcbedrock_sky.preset if self.action=='RESET' else self.preset)
                preview_lighting(scene, context.screen, activate=True)
        except Exception as exc:
            self.report({'ERROR'}, 'Sky setup: '+str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


PRESET_FIELDS=tuple(name for name,prop in SkySettings.__annotations__.items()
                    if prop.keywords.get('update') in {changed,exposure_changed,haze_changed,surface_changed,look_changed,pipeline_changed,edge_changed,cloud_preset_changed})+(
                        'source_pack','auto_materials','trailer_lighting','cycle_enabled')
GLOW_FIELDS=('emissive_glow','light_glow_size','light_glow_wide','light_glow_warmth','sky_glow','sun_rays','sun_rays_length','light_blocks','block_lights','light_block_brightness')

from .shot_lighting import MCBEDROCK_OT_shot_light
from . import sun_placement
from .sun_placement import MCBEDROCK_OT_place_sun, MCBEDROCK_GT_sky_body, MCBEDROCK_GGT_sky_bodies, MCBEDROCK_OT_sky_body_menu, MCBEDROCK_MT_sky_body
from .bounce_lighting import MCBEDROCK_OT_bounce_lighting
CLASSES = (*native_color.CLASSES,SkyLightState,SkyMaterialState,SkySettings,MCBEDROCK_OT_sky,MCBEDROCK_OT_lighting_group,MCBEDROCK_OT_shot_light,MCBEDROCK_OT_place_sun,MCBEDROCK_OT_bounce_lighting,MCBEDROCK_GT_sky_body,MCBEDROCK_GGT_sky_bodies,MCBEDROCK_MT_sky_body,MCBEDROCK_OT_sky_body_menu)


def register():
    # A failed install can leave only some RNA classes, with no Scene property.
    # Resolve and remove those registrations before attempting a fresh setup.
    unregister()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mcbedrock_sky = bpy.props.PointerProperty(type=SkySettings)
    from . import camera_grade
    camera_grade.register()
    native_color.register_handlers()
    if not supports_sky():
        return
    preview_quality.register()
    sun_placement.register()
    bpy.app.timers.register(lighting_pipeline.sync_materials,first_interval=1,persistent=True)
    bpy.app.handlers.frame_change_post.append(frame_update)
    bpy.app.handlers.frame_change_post.append(cycle_update)
    bpy.app.handlers.render_pre.append(render_update)
    bpy.app.handlers.depsgraph_update_post.append(frame_update)
    bpy.app.handlers.depsgraph_update_post.append(lighting_pipeline.materials_changed)
    bpy.app.handlers.load_post.append(clear_update_cache)
    bpy.app.handlers.undo_post.append(clear_update_cache)
    bpy.app.handlers.redo_post.append(clear_update_cache)


def unregister():
    from . import camera_grade
    camera_grade.unregister()
    native_color.unregister_handlers()
    sun_placement.unregister()
    if supports_sky():
        preview_quality.unregister()
    if bpy.app.timers.is_registered(lighting_pipeline.sync_materials):bpy.app.timers.unregister(lighting_pipeline.sync_materials)
    lighting_pipeline._signatures.clear()
    lighting_pipeline._update_signatures.clear()
    lighting_pipeline._material_counts.clear()
    lighting_pipeline._material_dirty.clear()
    if lighting_pipeline.materials_changed in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(lighting_pipeline.materials_changed)
    for handlers in (bpy.app.handlers.load_post,bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):
        if clear_update_cache in handlers:handlers.remove(clear_update_cache)
    for handlers, callback in [(bpy.app.handlers.frame_change_post,frame_update),(bpy.app.handlers.frame_change_post,cycle_update),(bpy.app.handlers.render_pre,render_update),(bpy.app.handlers.depsgraph_update_post,frame_update)]:
        if callback in handlers:
            handlers.remove(callback)
    if hasattr(bpy.types.Scene,'mcbedrock_sky'):
        del bpy.types.Scene.mcbedrock_sky
    for cls in reversed(CLASSES):
        # A ZIP update may have replaced the Python class objects while Blender
        # still holds the old registrations. Unregister the actual RNA types.
        base = next(base for base in (bpy.types.Operator, bpy.types.PropertyGroup, bpy.types.GizmoGroup, bpy.types.Gizmo, bpy.types.Menu)
                    if issubclass(cls, base))
        registered = base.bl_rna_get_subclass_py(cls.__name__)
        if registered is not None:
            bpy.utils.unregister_class(registered)

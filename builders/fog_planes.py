"""Fog layers: painted-style distance fog on one card in front of the camera.

Render artists add distance fog in Photoshop as soft layers: the near ground
and the character stay clear, the far ground fades in steps toward a pale
haze, densest low and clearing upward, and the sky is never fogged, so a
hazed treeline meets it with a clean edge. Unreal's height fog works the same
way (Start Distance, Max Opacity, Height Falloff, a cutoff that keeps the sky
clear, and Directional Inscattering: a glow toward the sun in the sun's
color).

One unlit, transparent card sits in front of the camera, where the fog
starts. Its shader looks straight past it, along the view, and measures how
far the land is behind it, then lays every layer over that land at once:

- each layer covers what lies beyond it, fading in over Soft edges blocks, so
  layers blend into hills and trees instead of slicing them;
- each layer thins out above Height (Softness blocks of fade), measured where
  that layer crosses the view, as painted fog bands do;
- where nothing is behind the card (the sky), there is no fog;
- the fog is lit like the world, the way Element's environment fog works
  (Bram Stout): near the camera it takes the average color of the upper sky
  (the light the sky gives everything, so a sunset's red horizon does not
  paint the near fog red) plus sunlight on the haze; far away it becomes the
  sky itself in the view direction, so distant land fades into exactly the
  sky behind it; and a brighter glow toward the sun in the sun's color (by
  night the moon's);
- land near the edge of an imported world fades fully into the sky's
  horizon color (End of world), so the place the export stops dissolves into
  the horizon instead of ending in a cliff.

One card, not one per layer: a second card behind the first would be "land"
to the first card's ray, and the sky would be fogged. The card sits in front
of whatever you look through: your viewport while you work (Material Preview
or Rendered), the scene camera for renders and when you look through it. It
never casts shadows or lights anything and cannot be selected; Starts and
Ends move the fog. Each scene has its own card.
"""
import math

import bpy
from mathutils import Matrix, Vector

from . import sky_drive

TAG = 'mc_fog_planes'
COLLECTION = 'Sparkle Fog planes'
MATERIAL = 'Sparkle Fog plane'
OPACITY = 'mc_fog_opacity'          # object property: each layer's share of the fog
VERSION = 6                         # 6: near fog from the upper sky, far fog from the sky behind
VERSION_TAG = 'mc_fog_version'
PLACED = 'mc_fog_placed'            # object: the depth update() last put the card at
MANUAL = 'mc_fog_distance'          # object: a start distance the user chose by moving it
LAYERS = 8                          # most layers one card draws
SKY_LIGHT = .7                      # share of the horizon color in the fog
SUN_LIGHT = .35                     # sunlight on the haze, at the horizon's brightness
CLOUD_CLEARANCE = 12.               # blocks below the cloud layer where land ends and sky begins
EDGE = 24.                          # End of world: blocks from the world's edge that fade out
CARD = .97                          # the card sits this far inside the first layer's sphere
UNUSED = 1e9                        # distance of a layer that is not in use


def _settings(scene):
    return scene.mcbedrock_fog_planes


_card_names = {}  # scene pointer -> (object count, names of its fog cards)


def planes(scene):
    """This scene's fog card(s), whatever their collection came to be called.

    Orbiting with fog on asks ten times a second; the walk over a large world
    is kept until objects are added or removed.
    """
    count = len(bpy.data.objects)
    cached = _card_names.get(scene.as_pointer())
    if cached is not None and cached[0] == count:
        found = [bpy.data.objects.get(name) for name in cached[1]]
        if all(obj is not None and obj.get(TAG) for obj in found):
            return found
    found = sorted((o for o in scene.objects if o.get(TAG)), key=lambda o: o.get('mc_fog_index', 0))
    _card_names[scene.as_pointer()] = (count, [o.name for o in found])
    return found


def fog_color(scene):
    """The fog's own color. Matched with a sky: lit like the world, the
    sky's horizon color plus sunlight on the haze (sky_light). Custom, or
    without a sky: the chosen or background color, never brighter than 1."""
    s = _settings(scene)
    sky = getattr(scene, 'mcbedrock_sky', None)
    if s.color_mode == 'AUTO' and sky is not None and sky.active and sky.world:
        return sky_light(scene)[0]
    color = _source_color(scene)
    peak = max(max(color), 1e-6)
    if peak > 1:
        color = tuple(c / peak for c in color)
    return tuple(color)


def _luminance(color):
    return .2126 * color[0] + .7152 * color[1] + .0722 * color[2]


def _sky_nodes(scene):
    sky = getattr(scene, 'mcbedrock_sky', None)
    nodes = sky.world.node_tree.nodes if sky is not None and sky.active and sky.world and sky.world.node_tree else None
    if nodes is None or nodes.get('Sky colors') is None or nodes.get('Visible sky') is None:
        return None
    return nodes


def _sky_curve(nodes):
    """The sky's mapping from a view's height to its color ramp:
    (horizon shift, curve power); ramp position = max(height - shift, 0) ** power."""
    shift = nodes.get('Horizon position')
    curve = nodes.get('Sky horizon curve')
    return (sky_drive.value(shift.inputs[1]) if shift is not None else 0.,
            sky_drive.value(curve.inputs[1]) if curve is not None else 1.)


def hemisphere_color(scene):
    """The average sky over the upper hemisphere, scene-linear: Element's
    near fog color. Heights are evenly spaced, which spaces directions evenly
    over the dome."""
    nodes = _sky_nodes(scene)
    if nodes is None:
        return horizon_color(scene)
    ramp = nodes['Sky colors'].color_ramp
    shift, power = _sky_curve(nodes)
    strength = sky_drive.value(nodes['Visible sky'].inputs['Strength'])
    samples = 32
    total = [0., 0., 0.]
    for index in range(samples):
        height = (index + .5) / samples
        color = ramp.evaluate(min(1., max(height - shift, 0.) ** power))
        for channel in range(3):
            total[channel] += color[channel]
    return tuple(t / samples * strength for t in total)


def horizon_color(scene):
    """The sky right at the horizon, scene-linear: what End of world fades to."""
    sky = getattr(scene, 'mcbedrock_sky', None)
    nodes = sky.world.node_tree.nodes if sky is not None and sky.active and sky.world and sky.world.node_tree else None
    if nodes is None or nodes.get('Sky colors') is None or nodes.get('Visible sky') is None:
        return _source_color(scene)
    horizon = nodes['Sky colors'].color_ramp.elements[0].color[:3]
    strength = sky_drive.value(nodes['Visible sky'].inputs['Strength'])
    return tuple(h * strength for h in horizon)


def sky_light(scene):
    """The fog lit like the world, from the sky rig: (fog color, direction
    toward the sun or moon, sunlight on the haze). The sky's horizon color
    is the light the sky gives everything (the ambient, GI side); the sun or
    moon lights the haze itself in its own color."""
    from .sky import time_state, current_time, owned, smoothstep
    sky = scene.mcbedrock_sky
    horizon = horizon_color(scene)
    level = _luminance(horizon)
    dome = hemisphere_color(scene)
    direction = time_state(current_time(scene), sky.azimuth)[0]
    up = direction.z > -.05
    body = owned(scene, 'Sun' if up else 'Moon')
    tint = tuple(body.data.color) if body is not None else (1., .9, .7)
    tint = tuple(c / max(_luminance(tint), 1e-6) for c in tint)
    # Sunlight on the haze fades as the sun sets; the moon lights it faintly.
    lit = smoothstep(-.1, .15, direction.z) if up else .35 * smoothstep(-.1, .15, -direction.z)
    sunlit = tuple(level * lit * t for t in tint)
    color = tuple(SKY_LIGHT * h + SUN_LIGHT * c for h, c in zip(dome, sunlit))
    return color, (direction if up else -direction), sunlit


def _source_color(scene):
    """The horizon of the sky, the flat background, or the custom color."""
    s = _settings(scene)
    if s.color_mode == 'CUSTOM':
        return tuple(s.color)
    sky = getattr(scene, 'mcbedrock_sky', None)
    if sky is not None and sky.active:
        from .flat_fog import fog_color as horizon
        return horizon(scene) if hasattr(sky, 'flat_fog_color') else tuple(s.color)
    flat = getattr(scene, 'mcbedrock_flat', None)
    if flat is not None and flat.active:
        return tuple(flat.color)
    if scene.world and scene.world.use_nodes:
        background = next((n for n in scene.world.node_tree.nodes if n.type == 'BACKGROUND'), None)
        if background is not None and not background.inputs['Color'].is_linked:
            return tuple(background.inputs['Color'].default_value[:3])
    return tuple(s.color)


def sun_glow(scene):
    """Direction toward the sun (by night the moon), and the glow's color:
    that light on the haze, times Sun glow."""
    s = _settings(scene)
    sky = getattr(scene, 'mcbedrock_sky', None)
    if sky is None or not sky.active or not sky.world or s.glow <= 0:
        return (0., 0., 1.), (0., 0., 0.)
    _, toward, sunlit = sky_light(scene)
    return tuple(toward), tuple(c * s.glow for c in sunlit)


_bounds_cache = {}


def world_bounds(scene):
    """(x min, x max, y min, y max) of the imported MiEx worlds in the scene,
    or None: End of world only applies to an exported region's edge."""
    worlds = [c for c in scene.collection.children_recursive if c.get('mcaddon_miex_export')]
    key = tuple((c.name, len(c.all_objects)) for c in worlds)
    cached = _bounds_cache.get(scene.as_pointer())
    if cached is not None and cached[0] == key:
        return cached[1]
    points = [obj.matrix_world @ Vector(corner) for c in worlds for obj in c.all_objects
              if obj.type == 'MESH' for corner in obj.bound_box]
    bounds = None
    if points:
        bounds = (min(p.x for p in points), max(p.x for p in points),
                  min(p.y for p in points), max(p.y for p in points))
        if min(bounds[1] - bounds[0], bounds[3] - bounds[2]) < 6 * EDGE:
            bounds = None                   # too small to have a horizon to hide
    _bounds_cache[scene.as_pointer()] = (key, bounds)
    return bounds


def _value(tree, name, default=0.):
    node = tree.nodes.new('ShaderNodeValue')
    node.name = node.label = name
    node.outputs[0].default_value = default
    return node.outputs[0]


# The nodes update() writes. A renamed or deleted one rebuilds the material
# instead of failing on every orbit step and frame.
NODES = ('Fog color', 'Fog sky colors', 'Fog sky horizon', 'Fog sky power', 'Fog sky strength', 'Fog sky mix',
         'Fog horizon color', 'Fog edge width', 'Fog sky height', 'Fog sun glow', 'Fog sun direction',
         'Fog glow exponent', 'Fog top', 'Fog fade', 'Fog soft edges', 'Fog share', 'Fog card depth', 'Fog reach',
         'Fog color near', 'Fog color far',
         *('Fog world ' + side for side in ('x min', 'x max', 'y min', 'y max')),
         *('Fog layer %d' % (index + 1) for index in range(LAYERS)))


def _intact(material):
    tree = material.node_tree
    return tree is not None and all(tree.nodes.get(name) is not None for name in NODES)


def _hide_in_solid(material):
    """Solid and wireframe views draw a material in its viewport color, which
    made the card an opaque grey sheet over the view. Fully transparent there,
    the card shows only where its nodes render (Material Preview, Rendered and
    final renders, which never read the viewport color)."""
    color = tuple(material.diffuse_color)
    if color[3] != 0.:
        material.diffuse_color = (*color[:3], 0.)


def build_material(material=None, force=False):
    """The fog card's material. material: a card's own one to bring up to date
    in place, keeping its name and slot; force rebuilds a damaged one."""
    material = material if material is not None else bpy.data.materials.get(MATERIAL)
    if material is not None and material.get(TAG) and material.get(VERSION_TAG) == VERSION and not force:
        _hide_in_solid(material)
        return material
    if material is None or not material.get(TAG):
        material = bpy.data.materials.new(MATERIAL)
    material[TAG] = True
    material[VERSION_TAG] = VERSION
    material.use_nodes = True
    if hasattr(material, 'surface_render_method'):
        material.surface_render_method = 'BLENDED'
    tree = material.node_tree
    tree.nodes.clear()
    from .lighting_pipeline import Graph
    g = Graph(tree)
    op = g.op
    nodes, links = tree.nodes, tree.links
    geometry = nodes.new('ShaderNodeNewGeometry')
    camera = nodes.new('ShaderNodeCameraData')
    position = nodes.new('ShaderNodeSeparateXYZ')
    links.new(geometry.outputs['Position'], position.inputs[0])
    away = nodes.new('ShaderNodeVectorMath')
    away.operation = 'SCALE'
    links.new(geometry.outputs['Incoming'], away.inputs[0])
    away.inputs['Scale'].default_value = -1
    view = nodes.new('ShaderNodeSeparateXYZ')
    links.new(away.outputs[0], view.inputs[0])
    reach = _value(tree, 'Fog reach', 500.)
    soft = _value(tree, 'Fog soft edges', 10.)
    top = _value(tree, 'Fog top', 4.)
    fade = _value(tree, 'Fog fade', 10.)
    share = _value(tree, 'Fog share', .1)
    depth = _value(tree, 'Fog card depth', 10.)
    # How far the land is behind the card, straight along the view.
    ray = nodes.new('ShaderNodeRaycast')
    ray.name = 'Fog room behind'
    links.new(geometry.outputs['Position'], ray.inputs['Position'])
    links.new(away.outputs[0], ray.inputs['Direction'])
    links.new(reach, ray.inputs['Length'])
    here = camera.outputs['View Distance']
    land = op('ADD', here, ray.outputs['Hit Distance'])
    low = op('SUBTRACT', top, fade)
    clear = 1.
    for index in range(LAYERS):
        distance = _value(tree, 'Fog layer %d' % (index + 1), UNUSED)
        # Covers land beyond the layer, fading in over Soft edges blocks.
        behind = nodes.new('ShaderNodeMapRange')
        behind.clamp = True
        behind.interpolation_type = 'SMOOTHSTEP'
        links.new(land, behind.inputs['Value'])
        links.new(distance, behind.inputs['From Min'])
        links.new(op('ADD', distance, op('MAXIMUM', soft, .01)), behind.inputs['From Max'])
        # Height where this layer crosses the view: dense low, clear above Height.
        height = op('ADD', position.outputs['Z'], op('MULTIPLY', view.outputs['Z'], op('SUBTRACT', distance, here)))
        band = nodes.new('ShaderNodeMapRange')
        band.clamp = True
        band.interpolation_type = 'SMOOTHSTEP'
        links.new(height, band.inputs['Value'])
        links.new(low, band.inputs['From Min'])
        links.new(top, band.inputs['From Max'])
        band.inputs['To Min'].default_value, band.inputs['To Max'].default_value = 1., 0.
        layer = op('MULTIPLY', share, op('MULTIPLY', behind.outputs[0], band.outputs[0]))
        clear = op('MULTIPLY', clear, op('SUBTRACT', 1, layer)) if not isinstance(clear, float) else op('SUBTRACT', 1, layer)
    fog = op('SUBTRACT', 1, clear)
    # Only through the scene camera: from any other view the card's depth
    # no longer matches, and it disappears instead of hanging in the world.
    mismatch = op('DIVIDE', op('ABSOLUTE', op('SUBTRACT', camera.outputs['View Z Depth'], depth)), op('MAXIMUM', depth, .01))
    through_camera = nodes.new('ShaderNodeMapRange')
    through_camera.clamp = True
    links.new(mismatch, through_camera.inputs['Value'])
    through_camera.inputs['From Min'].default_value, through_camera.inputs['From Max'].default_value = .01, .03
    through_camera.inputs['To Min'].default_value, through_camera.inputs['To Max'].default_value = 1., 0.
    # Nothing behind the card is sky: never fogged. Clouds belong to the sky
    # too: anything at cloud height counts as sky.
    hit = nodes.new('ShaderNodeSeparateXYZ')
    links.new(ray.outputs['Hit Position'], hit.inputs[0])
    sky_floor = _value(tree, 'Fog sky height', UNUSED)
    land_hit = op('MULTIPLY', ray.outputs['Is Hit'], op('LESS_THAN', hit.outputs['Z'], sky_floor))
    shown = op('MULTIPLY', land_hit, through_camera.outputs[0])
    layers = op('MULTIPLY', fog, shown)
    # End of world: land near the edge of the imported region fades fully
    # into the horizon, measured where the view meets the land.
    x_min, x_max = _value(tree, 'Fog world x min', -UNUSED), _value(tree, 'Fog world x max', UNUSED)
    y_min, y_max = _value(tree, 'Fog world y min', -UNUSED), _value(tree, 'Fog world y max', UNUSED)
    inside = op('MINIMUM', op('MINIMUM', op('SUBTRACT', hit.outputs['X'], x_min), op('SUBTRACT', x_max, hit.outputs['X'])),
                op('MINIMUM', op('SUBTRACT', hit.outputs['Y'], y_min), op('SUBTRACT', y_max, hit.outputs['Y'])))
    width = _value(tree, 'Fog edge width', 0.)
    edge_ramp = nodes.new('ShaderNodeMapRange')
    edge_ramp.clamp = True
    edge_ramp.interpolation_type = 'SMOOTHSTEP'
    links.new(inside, edge_ramp.inputs['Value'])
    links.new(op('MAXIMUM', width, .01), edge_ramp.inputs['From Max'])
    edge_ramp.inputs['From Min'].default_value = 0.
    edge_ramp.inputs['To Min'].default_value, edge_ramp.inputs['To Max'].default_value = 1., 0.
    edge = op('MULTIPLY', op('MULTIPLY', edge_ramp.outputs[0], op('GREATER_THAN', width, 0)), shown)
    alpha = op('SUBTRACT', 1, op('MULTIPLY', op('SUBTRACT', 1, layers), op('SUBTRACT', 1, edge)))
    alpha.node.name = 'Fog opacity'
    color = nodes.new('ShaderNodeRGB')
    color.name = 'Fog color'
    glow = nodes.new('ShaderNodeRGB')
    glow.name = 'Fog sun glow'
    glow.outputs[0].default_value = (0, 0, 0, 1)
    horizon = nodes.new('ShaderNodeRGB')
    horizon.name = 'Fog horizon color'
    # Element's environment fog: the near color above ('Fog color', from the
    # upper sky) gives way with distance to the sky itself in the view
    # direction, so the farthest land fades into exactly the sky behind it.
    shift = _value(tree, 'Fog sky horizon', 0.)
    power = _value(tree, 'Fog sky power', 1.)
    above = op('POWER', op('MAXIMUM', op('SUBTRACT', view.outputs['Z'], shift), 0), power)
    behind_sky = nodes.new('ShaderNodeValToRGB')
    behind_sky.name = 'Fog sky colors'
    links.new(above, behind_sky.inputs['Fac'])
    sky_color = nodes.new('ShaderNodeVectorMath')
    sky_color.operation = 'SCALE'
    links.new(behind_sky.outputs['Color'], sky_color.inputs[0])
    links.new(_value(tree, 'Fog sky strength', 1.), sky_color.inputs['Scale'])
    toward_far = nodes.new('ShaderNodeMapRange')
    toward_far.clamp = True
    toward_far.interpolation_type = 'SMOOTHSTEP'
    links.new(land, toward_far.inputs['Value'])
    links.new(_value(tree, 'Fog color near', 0.), toward_far.inputs['From Min'])
    links.new(_value(tree, 'Fog color far', UNUSED), toward_far.inputs['From Max'])
    distant = nodes.new('ShaderNodeMix')
    distant.name = 'Fog near to far'
    distant.data_type = 'RGBA'
    links.new(op('MULTIPLY', toward_far.outputs[0], _value(tree, 'Fog sky mix', 0.)), distant.inputs['Factor'])
    links.new(color.outputs[0], distant.inputs['A'])
    links.new(sky_color.outputs[0], distant.inputs['B'])
    sun = nodes.new('ShaderNodeCombineXYZ')
    sun.name = 'Fog sun direction'
    exponent = _value(tree, 'Fog glow exponent', 3.)
    facing = nodes.new('ShaderNodeVectorMath')
    facing.operation = 'DOT_PRODUCT'
    links.new(away.outputs[0], facing.inputs[0])
    links.new(sun.outputs[0], facing.inputs[1])
    toward_sun = op('POWER', op('MAXIMUM', facing.outputs['Value'], 0), exponent)
    lit = nodes.new('ShaderNodeVectorMath')
    lit.operation = 'MULTIPLY_ADD'
    links.new(glow.outputs[0], lit.inputs[0])
    links.new(toward_sun, lit.inputs[1])
    links.new(distant.outputs['Result'], lit.inputs[2])
    # Layers first, End of world over them: (1-e)*layers*fog + e*horizon,
    # divided by the combined opacity the mix applies.
    layered = nodes.new('ShaderNodeVectorMath')
    layered.operation = 'SCALE'
    links.new(lit.outputs[0], layered.inputs[0])
    links.new(op('MULTIPLY', layers, op('SUBTRACT', 1, edge)), layered.inputs['Scale'])
    combined = nodes.new('ShaderNodeVectorMath')
    combined.operation = 'MULTIPLY_ADD'
    links.new(horizon.outputs[0], combined.inputs[0])
    links.new(edge, combined.inputs[1])
    links.new(layered.outputs[0], combined.inputs[2])
    final = nodes.new('ShaderNodeVectorMath')
    final.operation = 'SCALE'
    links.new(combined.outputs[0], final.inputs[0])
    links.new(op('DIVIDE', 1, op('MAXIMUM', alpha, 1e-4)), final.inputs['Scale'])
    emission = nodes.new('ShaderNodeEmission')
    links.new(final.outputs[0], emission.inputs['Color'])
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    mix = nodes.new('ShaderNodeMixShader')
    links.new(alpha, mix.inputs['Fac'])
    links.new(transparent.outputs[0], mix.inputs[1])
    links.new(emission.outputs[0], mix.inputs[2])
    output = nodes.new('ShaderNodeOutputMaterial')
    links.new(mix.outputs[0], output.inputs['Surface'])
    _hide_in_solid(material)
    return material


def _card_material(obj):
    """The card's own fog material, kept whole: rebuilt in place when a node
    update() writes was renamed or deleted, made again when the material is
    gone, and copied for this card when another scene's card shares it. The
    values are fitted to one scene's camera, so a shared material showed one
    scene's fog in the other."""
    material = obj.active_material
    if material is None or not material.get(TAG):
        material = build_material()
    elif material.get(VERSION_TAG) != VERSION or not _intact(material):
        material = build_material(material, force=True)
    else:
        _hide_in_solid(material)
    assigned = obj.active_material == material
    if material.users - int(material.use_fake_user) - int(assigned) > 0:
        material = material.copy()
        assigned = False
    if not assigned:
        slots = obj.data.materials
        if len(slots):
            slots[0] = material
        else:
            slots.append(material)
    return material


def _material_views(scene):
    """Whether an open 3D view shows this scene with its materials (Material
    Preview or Rendered), where the card draws as fog."""
    manager = bpy.context.window_manager
    for window in manager.windows if manager else ():
        if window.scene != scene:
            continue
        for area in window.screen.areas:
            if area.type == 'VIEW_3D' and area.spaces.active.shading.type in {'MATERIAL', 'RENDERED'}:
                return True
    return False


def show_in_viewports(scene, found=None):
    """With every view of the scene in Solid or wireframe the card waits in
    front of the camera, where those views drew it as a flat sheet: hide it
    from viewports until a view shows materials. Final renders never read
    viewport visibility, so the fog still renders."""
    hidden = not _material_views(scene)
    for obj in planes(scene) if found is None else found:
        if obj.hide_viewport != hidden:
            obj.hide_viewport = hidden


_reported = set()


def _report_once(what):
    """Print a background error once. A timer that raises is unregistered for
    the rest of the session, so the caller keeps running instead."""
    import traceback
    message = traceback.format_exc()
    if message not in _reported:
        _reported.add(message)
        print(f'Sparkle: {what} hit an error and keeps running:\n{message}')


def create(scene):
    """Add the fog card (replacing this scene's)."""
    remove(scene)
    if scene.camera is None:
        raise RuntimeError('Add a camera first: the fog sits in front of it')
    collection = bpy.data.collections.new(COLLECTION)
    collection[TAG] = True
    scene.collection.children.link(collection)
    material = build_material()
    s = _settings(scene)
    mesh = bpy.data.meshes.new('Sparkle Fog plane')
    mesh.from_pydata([(-.5, -.5, 0), (.5, -.5, 0), (.5, .5, 0), (-.5, .5, 0)], [], [(0, 1, 2, 3)])
    mesh.materials.append(material)
    obj = bpy.data.objects.new('Sparkle Fog', mesh)
    obj[TAG] = True
    obj['mc_fog_index'] = 0
    for key in ('visible_shadow', 'visible_diffuse', 'visible_glossy', 'visible_transmission', 'visible_volume_scatter'):
        if hasattr(obj, key):
            setattr(obj, key, False)
    # Placed by update() in front of the eye; Starts and Ends move the fog.
    obj.hide_select = True
    obj.lock_location = obj.lock_rotation = obj.lock_scale = (True, True, True)
    collection.objects.link(obj)
    s.active = True
    update(scene)
    if not bpy.app.background:
        show_in_viewports(scene, [obj])


def _upgrade(scene):
    """Older fog drew one plane per layer; rebuild it as one card."""
    found = planes(scene)
    material = found[0].active_material if found else None
    if len(found) > 1 or (material is not None and material.get(TAG) and material.get(VERSION_TAG) != VERSION):
        create(scene)
        return planes(scene)
    return found


def layer_distances(s, start):
    """Distances from the camera of the layers, the first at start: spaced
    wider with distance, as each layer's share of the view shrinks."""
    count = max(1, min(s.count, LAYERS))
    far = max(s.far, start + 1) if s.far > start else start + max(s.far - s.near, 1)
    if count == 1:
        return [start]
    return [start + (far - start) * (i / (count - 1)) ** 1.5 for i in range(count)]


_rendering = set()          # scenes rendering: their fog sits at the scene camera


def camera_eye(scene):
    """The scene camera as an eye: (matrix, orthographic, half width, aspect, clip end)."""
    camera = scene.camera
    if camera is None:
        return None
    data = camera.data
    render = scene.render
    aspect = (render.resolution_x * render.pixel_aspect_x) / max(render.resolution_y * render.pixel_aspect_y, 1)
    ortho = data.type == 'ORTHO'
    if ortho:
        half = data.ortho_scale / 2
    else:
        half = math.tan(data.angle / 2) if aspect >= 1 else math.tan(data.angle / 2) * aspect
    location, rotation, _scale = camera.matrix_world.decompose()
    return Matrix.LocRotScale(location, rotation, None), ortho, half, aspect, data.clip_end


def view_eye(scene):
    """The largest viewport showing this scene's materials, unless it looks
    through the camera (then the camera is the eye), or None."""
    manager = bpy.context.window_manager
    best = None
    for window in manager.windows if manager else ():
        if window.scene != scene:
            continue
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            space = area.spaces.active
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None or space.region_3d is None or space.shading.type not in {'MATERIAL', 'RENDERED'}:
                continue
            if best is None or region.width * region.height > best[0]:
                best = (region.width * region.height, space, region)
    if best is None:
        return None
    _size, space, region = best
    view = space.region_3d
    if view.view_perspective == 'CAMERA':
        return None
    aspect = region.width / max(region.height, 1)
    # Blender's viewport frames a 72 mm wide sensor at the view's lens.
    half = 36. / max(space.lens, 1.)
    ortho = view.view_perspective == 'ORTHO'
    if ortho:
        half = view.view_distance * half
    elif aspect < 1:
        half *= aspect
    location, rotation, _scale = view.view_matrix.inverted().decompose()
    return Matrix.LocRotScale(location, rotation, None), ortho, half, aspect, space.clip_end


def current_eye(scene):
    if scene.as_pointer() not in _rendering and not bpy.app.background:
        eye = view_eye(scene)
        if eye is not None:
            return eye
    return camera_eye(scene)


def update(scene, eye=None):
    """Place and size the card in front of the eye, and set its layers and colors."""
    s = _settings(scene)
    if not s.active:
        return
    eye = eye or current_eye(scene)
    if eye is None:
        return
    found = _upgrade(scene)
    if not found:
        return
    matrix, ortho, half_width, aspect, clip_end = eye
    if ortho:
        corner = 1.
    else:
        # The frame's corner ray: the card must sit inside the first layer's
        # sphere all the way out to it.
        half_height = half_width / aspect
        corner = 1 / math.sqrt(1 + half_width ** 2 + half_height ** 2)
    obj = found[0]
    for key in (MANUAL,):
        if key in obj:
            del obj[key]
    start = s.near
    card = start * corner * CARD
    width = 2 * half_width if ortho else 2 * card * half_width
    height = width / aspect
    if obj.parent is not None:
        obj.parent = None
    obj.matrix_world = matrix @ Matrix.Translation((0, 0, -card)) @ Matrix.Diagonal((width * 1.3, height * 1.3, 1, 1))
    obj[PLACED] = card
    distances = layer_distances(s, start)
    share = 1 - (1 - min(s.density, .999)) ** (1 / len(distances))
    obj[OPACITY] = share
    # Solid views set to Object color draw the object's own color.
    if obj.color[3] != 0.:
        obj.color = (*obj.color[:3], 0.)
    obj.update_tag()
    material = _card_material(obj)
    nodes = material.node_tree.nodes

    def set_value(socket, name, value):
        # Driven (sky_drive): with a moving day/night cycle these change every frame.
        sky_drive.write(socket, name, value, owner=material, tolerance=1e-6)

    set_value(nodes['Fog color'].outputs[0], 'default_value', (*fog_color(scene), 1))
    world = _sky_nodes(scene) if s.color_mode == 'AUTO' else None
    if world is not None:
        _copy_ramp(world['Sky colors'].color_ramp, nodes['Fog sky colors'].color_ramp, scene.mcbedrock_sky.world)
        shift, power = _sky_curve(world)
        set_value(nodes['Fog sky horizon'].outputs[0], 'default_value', shift)
        set_value(nodes['Fog sky power'].outputs[0], 'default_value', power)
        set_value(nodes['Fog sky strength'].outputs[0], 'default_value', sky_drive.value(world['Visible sky'].inputs['Strength']))
    set_value(nodes['Fog sky mix'].outputs[0], 'default_value', 1. if world is not None else 0.)
    set_value(nodes['Fog horizon color'].outputs[0], 'default_value', (*horizon_color(scene), 1))
    bounds = world_bounds(scene) if s.edge else None
    for name, value in zip(('x min', 'x max', 'y min', 'y max'), bounds or (-UNUSED, UNUSED, -UNUSED, UNUSED)):
        set_value(nodes['Fog world ' + name].outputs[0], 'default_value', value)
    set_value(nodes['Fog edge width'].outputs[0], 'default_value', EDGE if bounds else 0.)
    sky = getattr(scene, 'mcbedrock_sky', None)
    clouds = sky is not None and sky.active and sky.clouds > 0
    set_value(nodes['Fog sky height'].outputs[0], 'default_value',
              sky.cloud_height - CLOUD_CLEARANCE if clouds else UNUSED)
    toward, glow = sun_glow(scene)
    set_value(nodes['Fog sun glow'].outputs[0], 'default_value', (*glow, 1))
    for socket, value in zip(nodes['Fog sun direction'].inputs, toward):
        set_value(socket, 'default_value', value)
    exponent = math.log(.5) / math.log(math.cos(math.radians(max(1., min(s.glow_spread, 89.)))))
    set_value(nodes['Fog glow exponent'].outputs[0], 'default_value', exponent)
    # Height follows the shot's camera, so it reads the same while you navigate.
    anchor = scene.camera.matrix_world.translation.z if scene.camera else matrix.translation.z
    top = anchor + s.top if s.relative_top else s.top
    set_value(nodes['Fog top'].outputs[0], 'default_value', top)
    set_value(nodes['Fog fade'].outputs[0], 'default_value', max(s.fade, .01))
    set_value(nodes['Fog soft edges'].outputs[0], 'default_value', max(s.soft, .01))
    set_value(nodes['Fog share'].outputs[0], 'default_value', share)
    set_value(nodes['Fog card depth'].outputs[0], 'default_value', card)
    # Stop the look-behind ray short of the camera's far clip: EEVEE counts a
    # ray that leaves the depth range as a hit, which would fog the sky.
    set_value(nodes['Fog reach'].outputs[0], 'default_value', max(1., min(5000., clip_end * .97 - card / corner)))
    for index in range(LAYERS):
        value = distances[index] if index < len(distances) else UNUSED
        set_value(nodes['Fog layer %d' % (index + 1)].outputs[0], 'default_value', value)
    set_value(nodes['Fog color near'].outputs[0], 'default_value', distances[0])
    set_value(nodes['Fog color far'].outputs[0], 'default_value', max(distances[-1], distances[0] + 1.))


def _copy_ramp(source, target, world=None):
    """Make target the same color ramp as source, touching only what differs.
    Stops the sky's world keeps (sky_drive) are followed through drivers."""
    if target.interpolation != source.interpolation:
        target.interpolation = source.interpolation
    if target.color_mode != source.color_mode:
        target.color_mode = source.color_mode
    while len(target.elements) < len(source.elements):
        target.elements.new(1.)
    while len(target.elements) > len(source.elements):
        target.elements.remove(target.elements[-1])
    for mine, theirs in zip(tuple(target.elements), tuple(source.elements)):
        for name in ('position', 'color'):
            if world is not None and sky_drive.follow(mine, name, world, sky_drive.key_of(theirs, name), 1e-6):
                continue
            if name == 'position' and abs(mine.position - theirs.position) > 1e-6:
                mine.position = theirs.position
            if name == 'color' and any(abs(a - b) > 1e-6 for a, b in zip(mine.color, theirs.color)):
                mine.color = theirs.color


def remove(scene):
    """Remove this scene's fog; other scenes keep theirs."""
    s = _settings(scene)
    materials = [bpy.data.materials.get(MATERIAL)]
    for obj in planes(scene):
        mesh = obj.data
        if mesh is not None:
            materials += list(mesh.materials)    # this scene's own copy, if it has one
        bpy.data.objects.remove(obj)
        if mesh is not None and not mesh.users:
            bpy.data.meshes.remove(mesh)
    for collection in [c for c in scene.collection.children_recursive if c.get(TAG)]:
        if not collection.all_objects and not collection.children:
            bpy.data.collections.remove(collection)
    for material in {m.as_pointer(): m for m in materials if m is not None}.values():
        if material.get(TAG) and not material.users:
            bpy.data.materials.remove(material)
    s.active = False


PENDING = 'mc_fog_planes_pending'     # scene: add the fog once a camera exists
_last_camera = {}


@bpy.app.handlers.persistent
def follow_camera(scene, *args):
    """Re-fit when the camera changes or moves (height follows it), add a
    pending fog card once the scene has a camera, and keep the glow on the sun."""
    settings = getattr(scene, 'mcbedrock_fog_planes', None)
    if settings is None:
        return
    camera = scene.camera
    if camera is None:
        return
    if scene.get(PENDING) and not settings.active:
        del scene[PENDING]
        create(scene)
        return
    if not settings.active:
        return
    _follow(scene)


def _follow(scene):
    """Re-fit when the eye (viewport or camera) or the sky changes."""
    eye = current_eye(scene)
    if eye is None:
        return
    sky = getattr(scene, 'mcbedrock_sky', None)
    matrix, ortho, half, aspect, clip_end = eye
    key = (tuple(round(v, 4) for row in matrix for v in row), ortho, round(half, 5), round(aspect, 4), round(clip_end, 3),
           scene.camera.as_pointer() if scene.camera else 0,
           round(scene.camera.matrix_world.translation.z, 3) if scene.camera else 0.,
           (round(sky.time, 3), round(sky.azimuth, 3), sky.preset, sky.active) if sky is not None else None)
    if _last_camera.get(scene.as_pointer()) != key:
        _last_camera[scene.as_pointer()] = key
        update(scene, eye)


def _poll_views():
    """Viewport navigation changes no data: watch the views ten times a second."""
    try:
        manager = bpy.context.window_manager
        scenes = {window.scene for window in manager.windows} if manager else set()
        for scene in scenes:
            settings = getattr(scene, 'mcbedrock_fog_planes', None)
            if settings is not None and settings.active and scene.as_pointer() not in _rendering:
                _follow(scene)
                show_in_viewports(scene)
    except Exception:
        _report_once('Distance fog')
    return .1


@bpy.app.handlers.persistent
def before_render(scene, *args):
    if getattr(scene, 'mcbedrock_fog_planes', None) and scene.mcbedrock_fog_planes.active:
        _rendering.add(scene.as_pointer())
        update(scene, camera_eye(scene))


@bpy.app.handlers.persistent
def after_render(scene, *args):
    if scene.as_pointer() in _rendering:
        _rendering.discard(scene.as_pointer())
        _last_camera.pop(scene.as_pointer(), None)     # back to the viewport


@bpy.app.handlers.persistent
def frame_change(scene, *args):
    """A moving day/night cycle moves the sun: keep the glow with it."""
    settings = getattr(scene, 'mcbedrock_fog_planes', None)
    sky = getattr(scene, 'mcbedrock_sky', None)
    if settings and settings.active and sky is not None and sky.active and sky.cycle_enabled:
        with sky_drive.per_frame():  # its sky colors reach the card through drivers
            update(scene)


def _changed(self, context):
    update(self.id_data)


def _spacing_changed(self, context):
    update(self.id_data)


class FogPlaneSettings(bpy.types.PropertyGroup):
    active: bpy.props.BoolProperty(options={'HIDDEN'})
    count: bpy.props.IntProperty(name='Layers', default=6, min=1, max=LAYERS, update=_changed,
        description='How many fog layers lie between where the fog starts and ends')
    density: bpy.props.FloatProperty(name='Density', default=.35, min=0, max=1, update=_changed,
        description='How much of the farthest land the layers hide together; the sky is never fogged')
    near: bpy.props.FloatProperty(name='Starts', default=96, min=.5, max=10000, soft_max=200, update=_spacing_changed,
        description='Distance from the camera where the fog starts, in blocks. Everything nearer stays clear')
    far: bpy.props.FloatProperty(name='Ends', default=300, min=1, max=100000, soft_max=1000, update=_changed,
        description='Distance of the last layer from the camera, in blocks')
    top: bpy.props.FloatProperty(name='Height', default=16, min=-1000, max=1000, soft_min=-20, soft_max=60, update=_changed,
        description='Where the fog clears, in blocks above the camera (or world height when not relative)')
    relative_top: bpy.props.BoolProperty(name='Height follows the camera', default=True, update=_changed)
    fade: bpy.props.FloatProperty(name='Softness', default=24, min=.01, max=500, soft_max=60, update=_changed,
        description='How far below its top the fog fades in, in blocks')
    soft: bpy.props.FloatProperty(name='Soft edges', default=6, min=0, max=200, soft_max=40, update=_changed,
        description='Blocks over which each layer fades in where land or trees pass through it, so it never cuts a line')
    glow: bpy.props.FloatProperty(name='Sun glow', default=.4, min=0, max=3, soft_max=1.5, update=_changed,
        description="Brighten the fog toward the sun, in the sun's color (the moon's by night)")
    glow_spread: bpy.props.FloatProperty(name='Glow spread', default=35, min=1, max=89, update=_changed,
        description='Angle from the sun where the glow falls to half, in degrees')
    edge: bpy.props.BoolProperty(name='End of world', default=True, update=_changed,
        description="Fade land near the edge of an imported world into the sky's horizon, so the world doesn't end in a cliff")
    color_mode: bpy.props.EnumProperty(name='Color', default='AUTO', update=_changed, items=(
        ('AUTO', 'Match', 'The sky horizon, or the flat lighting background'),
        ('CUSTOM', 'Custom', 'One fog color')))
    color: bpy.props.FloatVectorProperty(name='Fog color', subtype='COLOR', size=3, default=(.75, .8, .9),
        min=0, max=1, update=_changed)


class MCBEDROCK_OT_fog_planes(bpy.types.Operator):
    bl_idname = 'mcbedrock.fog_planes'
    bl_label = 'Fog Layers'
    bl_description = 'Add or remove painted-style distance fog in front of the camera'
    bl_options = {'REGISTER', 'UNDO'}
    enable: bpy.props.BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        try:
            if self.enable and context.scene.camera is None:
                # No camera yet: it appears as soon as the scene has one.
                context.scene[PENDING] = True
                self.report({'INFO'}, 'Distance fog will appear once the scene has a camera')
            elif self.enable:
                create(context.scene)
            else:
                remove(context.scene)
                context.scene.pop(PENDING, None)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (FogPlaneSettings, MCBEDROCK_OT_fog_planes)


# Smoke pockets (boxes of volume placed at the cursor) were removed: they read
# as smoke lying on the ground. Files saved with them lose them on load.
SMOKE_TAG = 'mc_smoke_pocket'


def remove_smoke_pockets():
    """Delete smoke pockets left in a file: the objects, then their collection
    and material once nothing else uses them. Returns the number removed."""
    removed = 0
    for obj in [o for o in bpy.data.objects if o.get(SMOKE_TAG) and not o.library]:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and not mesh.users:
            bpy.data.meshes.remove(mesh)
        removed += 1
    for collection in [c for c in bpy.data.collections
                       if c.get(SMOKE_TAG) and not c.library and not c.all_objects and not c.children]:
        bpy.data.collections.remove(collection)
    for material in [m for m in bpy.data.materials if m.get(SMOKE_TAG) and not m.library and not m.users]:
        bpy.data.materials.remove(material)
    return removed


@bpy.app.handlers.persistent
def _drop_smoke_pockets(*_unused):
    remove_smoke_pockets()


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    bpy.types.Scene.mcbedrock_fog_planes = bpy.props.PointerProperty(type=FogPlaneSettings)
    for handlers, function in ((bpy.app.handlers.depsgraph_update_post, follow_camera),
                               (bpy.app.handlers.render_pre, before_render),
                               (bpy.app.handlers.render_post, after_render),
                               (bpy.app.handlers.render_cancel, after_render),
                               (bpy.app.handlers.frame_change_post, frame_change),
                               (bpy.app.handlers.load_post, _drop_smoke_pockets)):
        if function not in handlers:
            handlers.append(function)
    if not bpy.app.background and not bpy.app.timers.is_registered(_poll_views):
        bpy.app.timers.register(_poll_views, first_interval=.5, persistent=True)
    # The file already open when the add-on updates: once, outside registration.
    if not bpy.app.timers.is_registered(_drop_smoke_pockets):
        bpy.app.timers.register(_drop_smoke_pockets, first_interval=.1)


def unregister():
    if bpy.app.timers.is_registered(_drop_smoke_pockets):
        bpy.app.timers.unregister(_drop_smoke_pockets)
    if bpy.app.timers.is_registered(_poll_views):
        bpy.app.timers.unregister(_poll_views)
    for handlers, function in ((bpy.app.handlers.depsgraph_update_post, follow_camera),
                               (bpy.app.handlers.render_pre, before_render),
                               (bpy.app.handlers.render_post, after_render),
                               (bpy.app.handlers.render_cancel, after_render),
                               (bpy.app.handlers.frame_change_post, frame_change),
                               (bpy.app.handlers.load_post, _drop_smoke_pockets)):
        if function in handlers:
            handlers.remove(function)
    if hasattr(bpy.types.Scene, 'mcbedrock_fog_planes'):
        del bpy.types.Scene.mcbedrock_fog_planes
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

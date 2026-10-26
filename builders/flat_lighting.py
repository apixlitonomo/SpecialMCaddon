"""Flat lighting: a Workbench-style flat look rendered by EEVEE, live in the viewport.

Render artists light Minecraft key art flat and paint the light in later. The
look is built in the compositor from EEVEE's passes, a trick shared by the
artist hexioq: every surface shows its texture color (Diffuse Color, so biome
tints survive) shaded by one very wide sun (Diffuse Direct). A 180-degree sun
gives each face its own flat tone and keeps soft shadows and contact
darkening, the "enhanced dark and light spots"; a narrow sun gives crisp
shadows, and the shadow floor keeps them one flat tone. Emission is added
on top.

It replaces Sky & Lighting rather than layering on it: one owned sun, a plain
world (a color, a gradient or transparent) that only the camera sees,
Standard color, and a compact compositor. Everything it changes is saved and
put back by remove().

The default Studio look is character key art: a small sun from the camera's
side and above, so models get soft-edged cast shadows and clear face tones,
standing on a ground that is the backdrop itself, darkened only where shadow
falls on it.

The gradient runs across the frame at any angle, out from a point (radial)
or up from the horizon like a real sky, with an optional middle color;
Position and Spread place the blend and set how wide it is.
"""
import json
import math

import bpy
from mathutils import Vector

TAG = 'mc_flat_lighting'
LIGHT = 'Sparkle Flat light'
WORLD = 'Sparkle Flat world'
TREE = 'Sparkle Flat lighting'
GROUND = 'Sparkle Flat ground'
BACKDROP = 'Sparkle Flat backdrop'
VERSION = 5
WORLD_VERSION = 3
GROUND_VERSION = 3
GROUND_TAG = 'mc_flat_ground_version'
GROUND_SIZE = 4000.
WORLD_TAG = 'mc_flat_world_version'
PASSES = ('use_pass_diffuse_direct', 'use_pass_diffuse_color', 'use_pass_emit', 'use_pass_environment')
# Blended surfaces (whitewater, glass) render into this pass only.
EEVEE_PASSES = ('use_pass_transparent',)
STYLES = ('LINEAR', 'RADIAL', 'HORIZON')
# One-click looks: (Light size in degrees, Shadow brightness).
# Studio (shown as Sharp) is key art like the user's practice render: a
# near-hard sun gives flat, crisp shadows, over a fill bright enough that
# shadowed faces keep their texture. Soft shadows is the same with a 5-degree
# sun, crisp where shadows touch and softening toward their far end. Soft
# light is hexioq's recipe: one 180-degree sun and almost no fill, so every
# face gets its own tone and creases, contacts and the shade under a head go
# dark, the "enhanced dark and light spots".
LOOKS = {'STUDIO': (.25, .5), 'FALLOFF': (5., .5), 'SOFT': (180., .08)}
# Where the sun sits when flat lighting starts, seen from the camera: this
# far round to its right (0 is straight behind the camera) and this high. A
# low key from just right of the camera, as in the user's practice render:
# faces the camera sees are lit and long shadows fall back and to the left.
SUN_AROUND = 25.
SUN_HEIGHT = 22.


def look(settings):
    """The look the settings match, or '' once either value is moved."""
    for key, (softness, floor) in LOOKS.items():
        if abs(settings.softness - softness) < .01 and abs(settings.shadow_floor - floor) < .001:
            return key
    return ''


def _settings(scene):
    return scene.mcbedrock_flat


def _owned(kind, name):
    collection = getattr(bpy.data, kind)
    item = collection.get(name)
    return item if item is not None and item.get(TAG) else None


# ---------------------------------------------------------------- world

def _value(nodes, name, amount=0.):
    node = nodes.new('ShaderNodeValue')
    node.name = node.label = name
    node.outputs[0].default_value = amount
    return node.outputs[0]


def _math(tree, operation, a, b=None):
    node = tree.nodes.new('ShaderNodeMath')
    node.operation = operation
    for index, source in enumerate((a, b)):
        if isinstance(source, (int, float)):
            node.inputs[index].default_value = source
        elif source is not None:
            tree.links.new(source, node.inputs[index])
    return node.outputs[0]


def backdrop_group():
    """The background as a shader group, shared by the world and the ground.

    Window is the point on screen and View the direction the eye looks
    along, so an object can show exactly the background behind it. The color
    is a ramp read at one gradient coordinate, blended from three: across the
    frame at an angle, out from a center point, and up from the horizon.
    update() picks one and writes the colors.
    """
    group = bpy.data.node_groups.get(BACKDROP)
    if group is not None and group.get(TAG) == WORLD_VERSION:
        return group
    if group is None:
        group = bpy.data.node_groups.new(BACKDROP, 'ShaderNodeTree')
    group.nodes.clear()
    group.interface.clear()
    group.interface.new_socket('Window', in_out='INPUT', socket_type='NodeSocketVector')
    group.interface.new_socket('View', in_out='INPUT', socket_type='NodeSocketVector')
    group.interface.new_socket('Color', in_out='OUTPUT', socket_type='NodeSocketColor')
    nodes, links = group.nodes, group.links
    inputs = nodes.new('NodeGroupInput')
    outputs = nodes.new('NodeGroupOutput')

    def value(name, amount=0.):
        return _value(nodes, name, amount)

    def op(operation, a, b=None):
        return _math(group, operation, a, b)

    window = nodes.new('ShaderNodeSeparateXYZ')
    links.new(inputs.outputs['Window'], window.inputs[0])
    x, y = window.outputs['X'], window.outputs['Y']
    # Across the frame: the point, from the center, onto the gradient direction.
    linear = op('ADD', op('ADD', op('MULTIPLY', op('SUBTRACT', x, .5), value('Flat linear x')),
                          op('MULTIPLY', op('SUBTRACT', y, .5), value('Flat linear y', 1.))), .5)
    # Out from the center point, round on screen whatever the frame shape.
    across = op('MULTIPLY', op('SUBTRACT', x, value('Flat center x', .5)), value('Flat aspect', 1.))
    up = op('SUBTRACT', y, value('Flat center y', .5))
    radial = op('MULTIPLY', op('SQRT', op('ADD', op('MULTIPLY', across, across), op('MULTIPLY', up, up))),
                value('Flat radius scale', 1.))
    # Up from the horizon: 0.5 at the horizon, 1 at 45 degrees up.
    direction = nodes.new('ShaderNodeVectorMath')
    direction.operation = 'NORMALIZE'
    links.new(inputs.outputs['View'], direction.inputs[0])
    height = nodes.new('ShaderNodeSeparateXYZ')
    links.new(direction.outputs[0], height.inputs[0])
    horizon = op('ADD', op('MULTIPLY', op('ARCSINE', height.outputs['Z']), 2 / math.pi), .5)
    position = op('ADD', op('ADD', op('MULTIPLY', linear, value('Flat weight linear', 1.)),
                            op('MULTIPLY', radial, value('Flat weight radial'))),
                  op('MULTIPLY', horizon, value('Flat weight horizon')))
    ramp_in = nodes.new('ShaderNodeMapRange')
    ramp_in.name = 'Flat gradient'
    ramp_in.clamp = True
    links.new(position, ramp_in.inputs['Value'])
    ramp = nodes.new('ShaderNodeValToRGB')
    ramp.name = 'Flat ramp'
    links.new(ramp_in.outputs[0], ramp.inputs['Fac'])
    links.new(ramp.outputs['Color'], outputs.inputs['Color'])
    group[TAG] = WORLD_VERSION
    return group


def backdrop_nodes():
    """The nodes holding the background's colors and gradient."""
    return backdrop_group().nodes


def build_world(world):
    """Camera rays see the background; everything else sees the shadow fill.

    The fill is the light in shadow (Shadow tint x Shadow brightness), so the
    plain render, which is what the Rendered viewport shows, is already lit
    like the finished look; the compositor only refines it.
    """
    world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()
    nodes, links = tree.nodes, tree.links
    coords = nodes.new('ShaderNodeTexCoord')
    backdrop = nodes.new('ShaderNodeGroup')
    backdrop.name = 'Flat backdrop'
    backdrop.node_tree = backdrop_group()
    links.new(coords.outputs['Window'], backdrop.inputs['Window'])
    links.new(coords.outputs['Generated'], backdrop.inputs['View'])
    background = nodes.new('ShaderNodeBackground')
    background.name = 'Flat background'
    links.new(backdrop.outputs['Color'], background.inputs['Color'])
    rays = nodes.new('ShaderNodeLightPath')
    black = nodes.new('ShaderNodeBackground')
    black.name = 'Flat fill'
    camera_only = nodes.new('ShaderNodeMixShader')
    links.new(rays.outputs['Is Camera Ray'], camera_only.inputs[0])
    links.new(black.outputs[0], camera_only.inputs[1])
    links.new(background.outputs[0], camera_only.inputs[2])
    output = nodes.new('ShaderNodeOutputWorld')
    links.new(camera_only.outputs[0], output.inputs['Surface'])
    world[WORLD_TAG] = WORLD_VERSION


# ---------------------------------------------------------------- ground

def build_ground_material(material):
    """The ground is the background behind it, darkened where shadow falls.

    A white diffuse surface measures the light reaching the ground (Shader
    to RGB). Less the fill and over what the sun gives open ground, that is
    how much sunlight gets there: 1 in the open, 0 in full shadow. Full
    shadow is as deep as a model's shadow side next to its lit side, at any
    sun height. The background seen through the ground is scaled by it and
    emitted, so open ground melts into the backdrop with no horizon line, in
    the plain render and the Rendered viewport as well as in the flat
    compositor.

    Shadow fade lightens the shadow away from the models, as artists erase
    a shadow layer toward its end: from the edge of their footprint to as
    far as the sun casts their height, so at 100% it is gone where the
    shadow would end.
    """
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    nodes, links = tree.nodes, tree.links

    def value(name, amount=0.):
        return _value(nodes, name, amount)

    def op(operation, a, b=None):
        return _math(tree, operation, a, b)

    diffuse = nodes.new('ShaderNodeBsdfDiffuse')
    diffuse.inputs['Color'].default_value = (1, 1, 1, 1)
    measured = nodes.new('ShaderNodeShaderToRGB')
    links.new(diffuse.outputs[0], measured.inputs[0])
    light = nodes.new('ShaderNodeRGBToBW')
    links.new(measured.outputs['Color'], light.inputs[0])
    # The sun on open ground: its strength at its height above the horizon.
    height = value('Flat sun height', 1.)
    sun = op('MULTIPLY', value('Flat ground key', .5), op('MAXIMUM', height, 0.))
    reached = op('MINIMUM', op('MAXIMUM', op('DIVIDE', op('SUBTRACT', light.outputs[0], value('Flat ground fill', .5)),
                                             op('MAXIMUM', sun, 1e-4)), 0.), 1.)
    depth = value('Flat ground depth', .5)
    lit = op('ADD', depth, op('MULTIPLY', op('SUBTRACT', 1., depth), reached))
    geometry = nodes.new('ShaderNodeNewGeometry')
    # Shadow fade: how far outside the models' footprint this point is, over
    # how far past it their shadow runs (shadow_reach).
    point = nodes.new('ShaderNodeSeparateXYZ')
    links.new(geometry.outputs['Position'], point.inputs[0])

    def outside(axis):
        offset = op('ABSOLUTE', op('SUBTRACT', point.outputs[axis], value('Flat fade center ' + axis.lower())))
        return op('MAXIMUM', op('SUBTRACT', offset, value('Flat fade half ' + axis.lower())), 0.)

    dx, dy = outside('X'), outside('Y')
    away = op('SQRT', op('ADD', op('MULTIPLY', dx, dx), op('MULTIPLY', dy, dy)))
    along = nodes.new('ShaderNodeMapRange')
    along.name = 'Flat fade along'
    along.interpolation_type = 'SMOOTHSTEP'
    along.clamp = True
    links.new(away, along.inputs['Value'])
    along.inputs['From Min'].default_value = 0.
    links.new(value('Flat fade length', 4.), along.inputs['From Max'])
    kept = op('SUBTRACT', 1., op('MULTIPLY', value('Flat ground fade'), along.outputs['Result']))
    # Ground shadow sets how far toward that depth the ground goes.
    shadow = op('MULTIPLY', value('Flat ground shadow', 1.), kept)
    shade = op('ADD', 1., op('MULTIPLY', shadow, op('SUBTRACT', lit, 1.)))
    coords = nodes.new('ShaderNodeTexCoord')
    view = nodes.new('ShaderNodeVectorMath')
    view.operation = 'SCALE'
    links.new(geometry.outputs['Incoming'], view.inputs[0])
    view.inputs['Scale'].default_value = -1.
    backdrop = nodes.new('ShaderNodeGroup')
    backdrop.name = 'Flat backdrop'
    backdrop.node_tree = backdrop_group()
    links.new(coords.outputs['Window'], backdrop.inputs['Window'])
    links.new(view.outputs[0], backdrop.inputs['View'])
    shaded = nodes.new('ShaderNodeVectorMath')
    shaded.operation = 'SCALE'
    links.new(backdrop.outputs['Color'], shaded.inputs[0])
    links.new(shade, shaded.inputs['Scale'])
    emission = nodes.new('ShaderNodeEmission')
    links.new(shaded.outputs[0], emission.inputs['Color'])
    output = nodes.new('ShaderNodeOutputMaterial')
    links.new(emission.outputs[0], output.inputs['Surface'])
    material[GROUND_TAG] = GROUND_VERSION


def _drive_sun_height(material, light):
    """The ground's open light follows the sun as it is turned."""
    path = 'nodes["Flat sun height"].outputs[0].default_value'
    tree = material.node_tree
    tree.driver_remove(path)
    driver = tree.driver_add(path).driver
    driver.type = 'SCRIPTED'
    variable = driver.variables.new()
    variable.name = 'up'
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id_type = 'OBJECT'
    variable.targets[0].id = light
    # The sun shines down its -Z axis: the height of +Z is how high it is.
    variable.targets[0].data_path = 'matrix_world[2][2]'
    driver.expression = 'up'


def _catchers(scene):
    """The scene's own shadow-catcher planes: the ground stands in for them."""
    return [obj for obj in scene.objects if obj.type == 'MESH' and not obj.get(TAG) and obj.is_shadow_catcher]


def _on_camera(obj):
    """Lens rigs parent their bokeh and filter planes to the camera."""
    while obj.parent is not None:
        obj = obj.parent
        if obj.type == 'CAMERA':
            return True
    return False


def _shown(scene):
    """Meshes in the shot: rendered and visible, not flat lighting's own, not
    riding on the camera, and with geometry (an empty mesh's bounds are a point)."""
    return [obj for obj in scene.objects if obj.type == 'MESH' and not obj.get(TAG) and not obj.hide_render
            and obj.visible_get() and not _on_camera(obj) and len({tuple(c) for c in obj.bound_box}) > 1]


def ground_height(scene):
    """Where models stand: a shadow catcher's top, else the lowest point shown."""
    # Catchers count even once flat lighting has hidden them.
    catchers = [max((obj.matrix_world @ Vector(corner)).z for corner in obj.bound_box) for obj in _catchers(scene)]
    if catchers:
        return max(catchers)
    lows = [min((obj.matrix_world @ Vector(corner)).z for corner in obj.bound_box) for obj in _shown(scene)]
    return min(lows) if lows else 0.


def casters(scene):
    """What casts the ground's shadow: meshes in the shot that cast shadows,
    less shadow catchers."""
    return [obj for obj in _shown(scene) if obj.visible_shadow and not obj.is_shadow_catcher]


def footprint(scene, base):
    """Where the models stand: (center x, center y, half width x, half width y,
    height above base). A posed rig's bounds follow its pose."""
    points = [obj.matrix_world @ Vector(corner) for obj in casters(scene) for corner in obj.bound_box]
    if not points:
        return 0., 0., 0., 0., 1.
    low = [min(p[i] for p in points) for i in range(3)]
    high = [max(p[i] for p in points) for i in range(3)]
    return ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2, (high[0] - low[0]) / 2, (high[1] - low[1]) / 2,
            max(high[2] - base, .1))


def shadow_reach(scene, base):
    """The footprint on the ground (center x, center y, half x, half y) and
    how far past its edge the shadow of the models' top falls."""
    x, y, half_x, half_y, tall = footprint(scene, base)
    light = _owned('objects', LIGHT)
    down = light.matrix_world.to_quaternion() @ Vector((0, 0, -1)) if light is not None else Vector((0, 0, -1))
    run = math.hypot(down.x, down.y)
    length = tall * run / max(-down.z, .1)
    if run > 1e-6:
        # The shadow leaves the footprint this far from its middle.
        length -= (half_x * abs(down.x) + half_y * abs(down.y)) / run
    return x, y, half_x, half_y, max(length, tall * .25)


FOOTPRINT = ('Flat fade center x', 'Flat fade center y', 'Flat fade half x', 'Flat fade half y', 'Flat fade length')


def _write_footprint(scene, ground, nodes):
    from .lighting_pipeline import set_value
    for name, amount in zip(FOOTPRINT, shadow_reach(scene, ground.matrix_world.translation.z)):
        set_value(nodes[name].outputs[0], 'default_value', amount)


def add_ground(scene):
    ground = _owned('objects', GROUND)
    if ground is not None:
        return ground
    mesh = bpy.data.meshes.new(GROUND)
    mesh[TAG] = True
    half = GROUND_SIZE / 2
    mesh.from_pydata([(-half, -half, 0), (half, -half, 0), (half, half, 0), (-half, half, 0)], [], [(0, 1, 2, 3)])
    material = bpy.data.materials.new(GROUND)
    material[TAG] = True
    build_ground_material(material)
    mesh.materials.append(material)
    ground = bpy.data.objects.new(GROUND, mesh)
    ground[TAG] = True
    # Not a model: no outlines or masks, and no shadow of its own.
    ground['mc_outlines'] = False
    ground.is_shadow_catcher = True
    ground.visible_shadow = False
    ground.hide_select = True
    focus = scene.camera.matrix_world.translation if scene.camera else Vector()
    ground.location = (focus.x, focus.y, ground_height(scene))
    scene.collection.objects.link(ground)
    light = _owned('objects', LIGHT)
    if light is not None:
        _drive_sun_height(material, light)
    return ground


def remove_ground():
    for kind in ('objects', 'meshes', 'materials'):
        item = _owned(kind, GROUND)
        if item is not None and (kind == 'objects' or not item.users):
            getattr(bpy.data, kind).remove(item)


def aim_sun(scene, light, look=None):
    """A low key from just right of the camera (or of a view looking along
    `look`): the faces it sees are lit and long shadows fall back and to the left."""
    camera = scene.camera
    if look is None and camera is None:
        light.rotation_euler = (math.radians(40), 0, math.radians(35))
        return
    if look is None:
        look = camera.matrix_world.to_quaternion() @ Vector((0, 0, -1))
    heading = math.atan2(look.y, look.x) if look.xy.length > 1e-6 else 0.
    # Toward the sun, as a heading: behind the camera turned round to its right.
    around = heading + math.pi + math.radians(SUN_AROUND)
    height = math.radians(SUN_HEIGHT)
    toward = Vector((math.cos(around) * math.cos(height), math.sin(around) * math.cos(height), math.sin(height)))
    light.rotation_euler = (-toward).to_track_quat('-Z', 'Y').to_euler()


def _aspect(scene):
    r = scene.render
    return (r.resolution_x * r.pixel_aspect_x) / max(r.resolution_y * r.pixel_aspect_y, 1e-6)


def _write_ramp(ramp, stops, smooth):
    """Set a color ramp to (position, color) stops, touching only what changed."""
    from .lighting_pipeline import set_value
    elements = ramp.elements
    while len(elements) > len(stops):
        elements.remove(elements[len(elements) - 1])
    while len(elements) < len(stops):
        elements.new(.5)
    for element, (position, color) in zip(elements, stops):
        set_value(element, 'position', position)
        set_value(element, 'color', (*color, 1))
    set_value(ramp, 'interpolation', 'EASE' if smooth else 'LINEAR')


def _write_gradient(scene, nodes, s):
    from .lighting_pipeline import set_value

    def put(name, amount):
        set_value(nodes[name].outputs[0], 'default_value', amount)

    gradient = s.background == 'GRADIENT'
    aspect = _aspect(scene)
    style = s.gradient_style if gradient else 'LINEAR'
    for key in STYLES:
        put('Flat weight ' + key.lower(), float(style == key))
    # The angle turns the gradient on screen (0 runs bottom to top); the
    # scale keeps it ending in the frame's far corners at any angle.
    angle = math.radians(s.gradient_angle) if gradient else 0.
    extent = abs(math.sin(angle)) * aspect + abs(math.cos(angle))
    put('Flat linear x', math.sin(angle) * aspect / extent)
    put('Flat linear y', math.cos(angle) / extent)
    put('Flat aspect', aspect)
    put('Flat center x', s.center_x)
    put('Flat center y', s.center_y)
    put('Flat radius scale', 1 / (.5 * math.hypot(aspect, 1)))
    spread = max(s.gradient_spread, .002) if gradient else 1.
    centre = s.gradient_position if gradient else .5
    ramp_in = nodes['Flat gradient']
    set_value(ramp_in.inputs['From Min'], 'default_value', centre - spread / 2)
    set_value(ramp_in.inputs['From Max'], 'default_value', centre + spread / 2)
    if not gradient:
        stops = [(0., tuple(s.color)), (1., tuple(s.color))]
    elif s.use_middle:
        stops = [(0., tuple(s.color)), (.5, tuple(s.middle_color)), (1., tuple(s.top_color))]
    else:
        stops = [(0., tuple(s.color)), (1., tuple(s.top_color))]
    _write_ramp(nodes['Flat ramp'].color_ramp, stops, s.gradient_smooth)


# ---------------------------------------------------------------- compositor

# Key-art finish: shadowed faces get richer, not greyer, and lit faces go
# past their texture color and ease off channel by channel, as under Filmic
# High Contrast in the user's practice render: red reaches the shoulder
# before green, so orange skin turns tan while cyan cloth stays cyan.
SHADOW_RICHNESS = .3       # extra saturation in full shadow
HIGHLIGHT_KNEE = .55       # linear value where a channel starts to ease off


def build_tree(scene, tree):
    """Diffuse Color x mix(1, Diffuse Direct, shading) + Emission, over the background.

    Shadowed faces get richer color and lit faces ease off channel by
    channel (see SHADOW_RICHNESS and HIGHLIGHT_KNEE).
    """
    from .lighting_pipeline import Graph
    tree.nodes.clear()
    tree.interface.clear()
    tree.interface.new_socket('Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    output = tree.nodes.new('NodeGroupOutput')
    render = tree.nodes.new('CompositorNodeRLayers')
    render.name = 'Flat passes'
    render.scene = scene
    g = Graph(tree)
    shading = tree.nodes.new('ShaderNodeValue')
    shading.name = 'Flat shading'
    background_on = tree.nodes.new('ShaderNodeValue')
    background_on.name = 'Flat background on'
    floor = tree.nodes.new('ShaderNodeValue')
    floor.name = 'Flat shadow floor'
    # EEVEE's diffuse light already holds both the key (the sun) and the
    # fill (the world, Shadow tint x Shadow brightness): texture color times
    # it is the flat look, without specular. Shading blends from plain
    # texture color (0) to the full light (1).
    light = g.split(render.outputs['Diffuse Direct'])
    shaped = [g.op('ADD', 1, g.op('MULTIPLY', shading.outputs[0], g.op('SUBTRACT', c, 1))) for c in light]
    color = g.split(render.outputs['Diffuse Color'])
    # How deep in shadow: 0 where the sun reaches fully, 1 at the fill alone.
    level = g.op('DIVIDE', g.op('ADD', g.op('ADD', shaped[0], shaped[1]), shaped[2]), 3)
    shadow = g.op('MINIMUM', g.op('MAXIMUM', g.op('DIVIDE', g.op('SUBTRACT', 1, level),
                                                    g.op('MAXIMUM', g.op('SUBTRACT', 1, floor.outputs[0]), .05)), 0), 1)
    grey = g.op('ADD', g.op('ADD', g.op('MULTIPLY', color[0], .2126), g.op('MULTIPLY', color[1], .7152)),
                g.op('MULTIPLY', color[2], .0722))
    boost = g.op('ADD', 1, g.op('MULTIPLY', shadow, SHADOW_RICHNESS))
    rich = [g.op('MAXIMUM', g.op('ADD', grey, g.op('MULTIPLY', g.op('SUBTRACT', c, grey), boost)), 0) for c in color]
    lit = [g.op('MULTIPLY', a, b) for a, b in zip(rich, shaped)]
    # Past the knee each channel eases toward 1 on its own.
    room = 1 - HIGHLIGHT_KNEE
    graded = []
    for c in lit:
        excess = g.op('MAXIMUM', g.op('SUBTRACT', c, HIGHLIGHT_KNEE), 0)
        graded.append(g.op('ADD', g.op('MINIMUM', c, HIGHLIGHT_KNEE),
                           g.op('DIVIDE', g.op('MULTIPLY', excess, room), g.op('ADD', excess, room))))
    emitted = [g.op('ADD', a, b) for a, b in zip(graded, g.split(render.outputs['Emission']))]
    # The background is the Environment pass: what the camera sees of the
    # world, already held out wherever geometry covers it.
    environment = g.split(render.outputs['Environment'])
    flat = [g.op('ADD', a, g.op('MULTIPLY', b, background_on.outputs[0]))
            for a, b in zip(emitted, environment)]
    # Blended surfaces (whitewater, glass) are left out of the passes above;
    # EEVEE renders them, lit, into the premultiplied Transparent pass, which
    # goes over the top.
    transparent = render.outputs.get('Transparent')
    if transparent is not None:
        over = tree.nodes.new('CompositorNodeSeparateColor')
        over.mode = 'RGB'
        tree.links.new(transparent, over.inputs[0])
        behind = g.op('SUBTRACT', 1, over.outputs['Alpha'])
        flat = [g.op('ADD', a, g.op('MULTIPLY', b, behind)) for a, b in zip(list(over.outputs)[:3], flat)]
    final = g.combine(flat, g.op('MAXIMUM', render.outputs['Alpha'], background_on.outputs[0]))
    tree.links.new(final, output.inputs['Image'])
    tree[TAG] = VERSION


# ---------------------------------------------------------------- flat faces

NORMAL = 'Sparkle Flat normal'


def _scene_materials(scene):
    found = {}
    for obj in scene.objects:
        if obj.type == 'MESH':
            for slot in obj.material_slots:
                material = slot.material
                if material is not None and material.node_tree is not None and not material.library:
                    found[material.as_pointer()] = material
    return list(found.values())


def flatten_faces(scene, enabled):
    """Feed each surface its true face normal (hexioq's fix for heads with
    smoothed or custom normals). Only unlinked Normal inputs are touched, so
    normal maps and Sparkle outlines keep theirs; remove() undoes it."""
    for material in _scene_materials(scene):
        tree = material.node_tree
        ours = tree.nodes.get(NORMAL)
        # Bedrock models already feed a two-sided normal: switch its source
        # between the true and the shading normal instead of replacing it.
        geometry = tree.nodes.get('Bedrock Two-Sided Geometry')
        scale = tree.nodes.get('Bedrock Two-Sided Normal')
        if geometry is not None and scale is not None:
            socket = 'True Normal' if enabled else 'Normal'
            tree.links.new(geometry.outputs[socket], scale.inputs['Vector'])
        if not enabled:
            if ours is not None:
                tree.nodes.remove(ours)
            continue
        shaders = [n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED' and not n.inputs['Normal'].is_linked]
        if not shaders:
            continue
        if ours is None:
            ours = tree.nodes.new('ShaderNodeNewGeometry')
            ours.name = ours.label = NORMAL
            ours[TAG] = True
        for shader in shaders:
            tree.links.new(ours.outputs['True Normal'], shader.inputs['Normal'])


# ---------------------------------------------------------------- mode

def active(scene):
    return bool(getattr(scene, 'mcbedrock_flat', None) and scene.mcbedrock_flat.active)


def _enable_passes(scene):
    for layer in scene.view_layers:
        for name in PASSES:
            if not getattr(layer, name):
                setattr(layer, name, True)
        for name in EEVEE_PASSES:
            if hasattr(layer.eevee, name) and not getattr(layer.eevee, name):
                setattr(layer.eevee, name, True)
        layer.update_render_passes()


def _views():
    for window in getattr(bpy.context.window_manager, 'windows', ()):
        areas = list(window.screen.areas)
        for index, area in enumerate(areas):
            if area.type == 'VIEW_3D':
                yield window.screen.name, index, area.spaces.active.shading


def _viewport_compositor(scene, on):
    """The finish lives in the compositor, so Rendered views show it too
    while flat lighting is on (a few math nodes); they get their own back after."""
    s = _settings(scene)
    saved = json.loads(s.saved or '{}')
    if on:
        kept = saved.setdefault('views', [])
        for screen, index, shading in _views():
            if hasattr(shading, 'use_compositor') and shading.use_compositor != 'ALWAYS':
                kept.append([screen, index, shading.use_compositor])
                shading.use_compositor = 'ALWAYS'
        s.saved = json.dumps(saved)
        return
    wanted = {(screen, index): value for screen, index, value in saved.get('views', ())}
    for screen, index, shading in _views():
        value = wanted.get((screen, index))
        if value is not None:
            try:
                shading.use_compositor = value
            except TypeError:
                pass


def apply(scene):
    """Switch the scene to flat lighting, saving everything it replaces."""
    s = _settings(scene)
    sky = getattr(scene, 'mcbedrock_sky', None)
    if sky is not None and sky.active:
        raise RuntimeError('Flat lighting replaces Sky & Lighting: remove the sky first')
    if s.active:
        update(scene)
        return
    layer_passes = {layer.name: {name: getattr(layer, name) for name in PASSES} for layer in scene.view_layers}
    eevee_passes = {layer.name: {name: getattr(layer.eevee, name) for name in EEVEE_PASSES if hasattr(layer.eevee, name)}
                    for layer in scene.view_layers}
    saved = dict(
        world=scene.world.name if scene.world else '',
        compositor=scene.compositing_node_group.name if scene.compositing_node_group else '',
        use_compositing=scene.render.use_compositing,
        film_transparent=scene.render.film_transparent,
        view_transform=scene.view_settings.view_transform,
        look=scene.view_settings.look,
        engine=scene.render.engine,
        passes=layer_passes,
        eevee_passes=eevee_passes,
        eevee={key: getattr(scene.eevee, key) for key in ('use_raytracing', 'use_fast_gi', 'taa_render_samples',
                                                         'taa_samples', 'shadow_ray_count', 'shadow_step_count')},
        # The scene's own lights and shadow catchers: flat lighting is the
        # whole light, and its ground replaces the catchers.
        hidden={obj.name: (obj.hide_render, obj.hide_viewport) for obj in scene.objects
                if (obj.type == 'LIGHT' and not obj.get(TAG)) or obj in _catchers(scene)},
    )
    s.saved = json.dumps(saved)
    for name in saved['hidden']:
        obj = scene.objects[name]
        obj.hide_render = obj.hide_viewport = True
    # One wide sun, editable like any light.
    data = bpy.data.lights.new(LIGHT, 'SUN')
    data[TAG] = True
    light = bpy.data.objects.new(LIGHT, data)
    light[TAG] = True
    scene.collection.objects.link(light)
    aim_sun(scene, light)
    world = bpy.data.worlds.new(WORLD)
    world[TAG] = True
    build_world(world)
    scene.world = world
    scene.render.use_compositing = True
    scene.render.engine = 'BLENDER_EEVEE'
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    e = scene.eevee
    e.use_raytracing = False
    e.use_fast_gi = False
    e.taa_render_samples = 64          # jittered shadows converge over the samples
    e.taa_samples = 16
    e.shadow_ray_count = 2
    e.shadow_step_count = 6
    _enable_passes(scene)
    # Built after the passes are on, so the Render Layers node offers them.
    tree = bpy.data.node_groups.new(TREE, 'CompositorNodeTree')
    build_tree(scene, tree)
    scene.compositing_node_group = tree
    s.active = True
    _viewport_compositor(scene, True)
    flatten_faces(scene, s.flat_faces)
    update(scene, build=True)


def _luminance(color):
    return .2126 * color[0] + .7152 * color[1] + .0722 * color[2]


def update(scene, build=False):
    """Write the settings into the light, world, ground and compositor.

    build lets it add or remove the ground; renders only write values.
    """
    from .lighting_pipeline import set_value
    s = _settings(scene)
    if not s.active:
        return
    light = _owned('objects', LIGHT)
    if light is not None:
        set_value(light.data, 'angle', math.radians(s.softness))
        # Crisp, true shadow edges: jittered soft shadows instead of EEVEE's
        # blur filter, which smears small cast shadows into blobs.
        # Finer shadow maps than EEVEE's .001 default keep sharp edges sharp
        # in close-ups and 4K renders.
        for key, value in (('use_shadow_jitter', True), ('shadow_jitter_overblur', 0.),
                           ('shadow_filter_radius', .5), ('shadow_maximum_resolution', .0005)):
            if hasattr(light.data, key):
                set_value(light.data, key, value)
        # A face square to the sun gets the key's share of the light, so with
        # the fill it reaches its full texture color (diffuse = E cos / pi).
        set_value(light.data, 'energy', math.pi * s.strength * (1 - s.shadow_floor))
        set_value(light.data, 'use_shadow', s.shadows)
        set_value(light.data, 'color', tuple(s.light_color))
    world = _owned('worlds', WORLD)
    if world is not None and world.get(WORLD_TAG) != WORLD_VERSION:
        build_world(world)
    if world is not None:
        nodes = world.node_tree.nodes
        fill = tuple(c * s.shadow_floor for c in s.shadow_tint)
        set_value(nodes['Flat fill'].inputs['Color'], 'default_value', (*fill, 1))
        _write_gradient(scene, backdrop_nodes(), s)
    if build:
        if s.ground and s.background != 'TRANSPARENT':
            add_ground(scene)
        else:
            remove_ground()
    ground = _owned('objects', GROUND)
    if ground is not None:
        material = ground.active_material
        if material is not None and material.get(GROUND_TAG) != GROUND_VERSION:
            build_ground_material(material)
            if light is not None:
                _drive_sun_height(material, light)
        if material is not None:
            nodes = material.node_tree.nodes
            set_value(nodes['Flat ground fill'].outputs[0], 'default_value',
                      s.shadow_floor * _luminance(s.shadow_tint))
            set_value(nodes['Flat ground key'].outputs[0], 'default_value',
                      s.strength * (1 - s.shadow_floor) * _luminance(s.light_color) if s.shadows else 0.)
            set_value(nodes['Flat ground shadow'].outputs[0], 'default_value', s.ground_shadow)
            set_value(nodes['Flat ground fade'].outputs[0], 'default_value', s.ground_fade)
            if s.ground_fade > 0:
                _write_footprint(scene, ground, nodes)
            # As deep as a model's shadow side next to its lit side.
            full = s.shadow_floor + s.strength * (1 - s.shadow_floor)
            set_value(nodes['Flat ground depth'].outputs[0], 'default_value', s.shadow_floor / max(full, 1e-4))
    # A visible background in the viewport too; transparent only on request.
    set_value(scene.render, 'film_transparent', s.background == 'TRANSPARENT')
    tree = scene.compositing_node_group
    if tree is not None and tree.get(TAG) and tree.get(TAG) != VERSION:
        _enable_passes(scene)
        build_tree(scene, tree)
    if tree is not None and tree.get(TAG):
        set_value(tree.nodes['Flat shading'].outputs[0], 'default_value', s.shading)
        set_value(tree.nodes['Flat shadow floor'].outputs[0], 'default_value', s.shadow_floor)
        set_value(tree.nodes['Flat background on'].outputs[0], 'default_value', float(s.background != 'TRANSPARENT'))


def remove(scene):
    """Put back the world, compositor, color and render settings flat lighting replaced."""
    s = _settings(scene)
    if not s.active:
        return
    saved = json.loads(s.saved or '{}')
    _viewport_compositor(scene, False)
    scene.world = bpy.data.worlds.get(saved.get('world', '')) if saved.get('world') else None
    scene.compositing_node_group = bpy.data.node_groups.get(saved.get('compositor', '')) if saved.get('compositor') else None
    for key in ('use_compositing', 'film_transparent'):
        if key in saved:
            setattr(scene.render, key, saved[key])
    if 'engine' in saved:
        scene.render.engine = saved['engine']
    for key in ('view_transform', 'look'):
        if key in saved:
            try:
                setattr(scene.view_settings, key, saved[key])
            except TypeError:
                pass
    for key, value in saved.get('eevee', {}).items():
        setattr(scene.eevee, key, value)
    for layer in scene.view_layers:
        for name, value in saved.get('passes', {}).get(layer.name, {}).items():
            setattr(layer, name, value)
        for name, value in saved.get('eevee_passes', {}).get(layer.name, {}).items():
            setattr(layer.eevee, name, value)
    flatten_faces(scene, False)
    remove_ground()
    for name, (render, viewport) in saved.get('hidden', {}).items():
        obj = scene.objects.get(name)
        if obj is not None:
            obj.hide_render, obj.hide_viewport = render, viewport
    for kind, name in (('objects', LIGHT), ('lights', LIGHT), ('worlds', WORLD), ('node_groups', TREE)):
        item = _owned(kind, name)
        if item is not None and (kind == 'objects' or not item.users or kind == 'node_groups'):
            getattr(bpy.data, kind).remove(item)
    backdrop = bpy.data.node_groups.get(BACKDROP)
    if backdrop is not None and backdrop.get(TAG) and not backdrop.users:
        bpy.data.node_groups.remove(backdrop)
    s.saved = ''
    s.active = False


# ---------------------------------------------------------------- settings

def _changed(self, context):
    update(self.id_data)


def _ground_changed(self, context):
    update(self.id_data, build=True)


def _faces_changed(self, context):
    if self.active:
        flatten_faces(self.id_data, self.flat_faces)


class FlatSettings(bpy.types.PropertyGroup):
    active: bpy.props.BoolProperty(options={'HIDDEN'})
    saved: bpy.props.StringProperty(options={'HIDDEN'})
    shading: bpy.props.FloatProperty(name='Shading', default=1., min=0, max=1, update=_changed,
        description='How much the light shapes each face; 0 is pure texture color')
    shadow_floor: bpy.props.FloatProperty(name='Shadow brightness', default=LOOKS['STUDIO'][1], min=0, max=1, update=_changed,
        description='The darkest a face or shadow gets')
    softness: bpy.props.FloatProperty(name='Light size', default=LOOKS['STUDIO'][0], min=.25, max=180, update=_changed,
        description='Angle of the sun in degrees. Small gives crisp shadows; a few degrees softens their edges; 180 gives the softest light')
    shadows: bpy.props.BoolProperty(name='Shadows', default=True, update=_changed)
    ground: bpy.props.BoolProperty(name='Ground shadow', default=True, update=_ground_changed,
        description='A ground under the models that shows the background and catches their shadows')
    ground_shadow: bpy.props.FloatProperty(name='Ground shadow', default=1., min=0, max=1, update=_changed,
        description='How dark shadows on the ground get: 100% matches the shadows on the models')
    ground_fade: bpy.props.FloatProperty(name='Shadow fade', default=0., min=0, max=1, update=_changed,
        description='Fade the ground shadow out away from the models: at 100% it is gone where the shadow ends')
    flat_faces: bpy.props.BoolProperty(name='Flat faces', default=True, update=_faces_changed,
        description='Shade every block face flat, even on models with smoothed or custom normals')
    strength: bpy.props.FloatProperty(name='Light strength', default=1.6, min=0, max=4, soft_max=2, update=_changed,
        description='Brightness of the lit faces; above 1 lifts them past their texture color')
    shadow_tint: bpy.props.FloatVectorProperty(name='Shadow tint', subtype='COLOR', size=3, default=(.95, .96, 1.),
        min=0, max=1, update=_changed, description='Color of the light in shadow, like the sky or backdrop filling it in')
    light_color: bpy.props.FloatVectorProperty(name='Light color', subtype='COLOR', size=3, default=(1, 1, 1),
        min=0, max=1, update=_changed)
    background: bpy.props.EnumProperty(name='Background', default='SOLID', update=_changed, items=(
        ('SOLID', 'Color', 'One background color'),
        ('GRADIENT', 'Gradient', 'Two or three colors across the frame, out from a point or up from the horizon'),
        ('TRANSPARENT', 'Transparent', 'No background, for compositing in Photoshop')))
    color: bpy.props.FloatVectorProperty(name='Background color', subtype='COLOR', size=3, default=(.16, .42, .78),
        min=0, max=1, update=_changed)
    top_color: bpy.props.FloatVectorProperty(name='Top color', subtype='COLOR', size=3, default=(.45, .72, 1.),
        min=0, max=1, update=_changed)
    middle_color: bpy.props.FloatVectorProperty(name='Middle color', subtype='COLOR', size=3, default=(.3, .57, .9),
        min=0, max=1, update=_changed)
    use_middle: bpy.props.BoolProperty(name='Middle color', default=False, update=_changed,
        description='Blend through a third color at Position, like a horizon band')
    gradient_style: bpy.props.EnumProperty(name='Gradient', default='LINEAR', update=_changed, items=(
        ('LINEAR', 'Linear', 'Straight across the frame at any angle'),
        ('RADIAL', 'Radial', 'Out from a point, like a glow behind the subject'),
        ('HORIZON', 'Horizon', 'Up from the horizon like a real sky: tilt the camera and it moves')))
    gradient_position: bpy.props.FloatProperty(name='Position', default=.5, min=0, max=1, update=_changed,
        description='Where the blend sits: the middle of the gradient')
    gradient_spread: bpy.props.FloatProperty(name='Spread', default=1., min=0, max=2, soft_max=1, update=_changed,
        description='How wide the blend is; 0 is a hard line between the colors')
    gradient_angle: bpy.props.FloatProperty(name='Angle', default=0., min=-180, max=180, update=_changed,
        description='Turns a linear gradient; 0 runs bottom to top')
    gradient_smooth: bpy.props.BoolProperty(name='Smooth blend', default=True, update=_changed,
        description='Ease into and out of each color instead of a straight blend')
    center_x: bpy.props.FloatProperty(name='Center X', default=.5, min=0, max=1, update=_changed,
        description='Where a radial gradient starts, across the frame')
    center_y: bpy.props.FloatProperty(name='Center Y', default=.5, min=0, max=1, update=_changed,
        description='Where a radial gradient starts, up the frame')


class MCBEDROCK_OT_flat_lighting(bpy.types.Operator):
    bl_idname = 'mcbedrock.flat_lighting'
    bl_label = 'Flat Lighting'
    bl_description = 'Switch to or from flat, Workbench-style lighting rendered by EEVEE'
    bl_options = {'REGISTER', 'UNDO'}
    enable: bpy.props.BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    replace_sky: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        try:
            if self.enable:
                sky = getattr(scene, 'mcbedrock_sky', None)
                if self.replace_sky and sky is not None and sky.active:
                    bpy.ops.mcbedrock.sky(action='RESTORE')
                apply(scene)
            else:
                remove(scene)
        except RuntimeError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class MCBEDROCK_OT_flat_aim(bpy.types.Operator):
    bl_idname = 'mcbedrock.flat_aim'
    bl_label = 'Aim Light From This View'
    bl_description = 'Light the shot from just right of the view you are looking through, with long shadows falling away from it'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active(context.scene)

    def execute(self, context):
        from .sky import view_forward
        scene = context.scene
        light = _owned('objects', LIGHT)
        forward = view_forward(context)
        if light is None or (forward is None and scene.camera is None):
            self.report({'WARNING'}, 'Look at the scene in a 3D viewport, or add a camera')
            return {'CANCELLED'}
        aim_sun(scene, light, forward)
        return {'FINISHED'}


@bpy.app.handlers.persistent
def before_render(scene, *args):
    """The gradient's shape follows the frame size, which changes without a setting changing."""
    if active(scene):
        update(scene)


@bpy.app.handlers.persistent
def after_load(*args):
    """Files saved with an older flat lighting get the ground it now has."""
    for scene in bpy.data.scenes:
        if active(scene) and scene.mcbedrock_flat.ground and _owned('objects', GROUND) is None:
            update(scene, build=True)


def _follow_models(scene):
    if not active(scene) or scene.mcbedrock_flat.ground_fade <= 0:
        return
    ground = _owned('objects', GROUND)
    material = ground.active_material if ground is not None else None
    if material is not None and material.get(GROUND_TAG) == GROUND_VERSION:
        _write_footprint(scene, ground, material.node_tree.nodes)


@bpy.app.handlers.persistent
def after_change(scene, depsgraph):
    """A model moved or posed, or the sun turned: the shadow fade follows."""
    if depsgraph.id_type_updated('OBJECT'):
        _follow_models(scene)


@bpy.app.handlers.persistent
def after_frame(scene, *args):
    _follow_models(scene)


CLASSES = (FlatSettings, MCBEDROCK_OT_flat_lighting, MCBEDROCK_OT_flat_aim)


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    bpy.types.Scene.mcbedrock_flat = bpy.props.PointerProperty(type=FlatSettings)
    if before_render not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(before_render)
    if after_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(after_load)
    if after_change not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(after_change)
    if after_frame not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(after_frame)


def unregister():
    if before_render in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(before_render)
    if after_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(after_load)
    if after_change in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(after_change)
    if after_frame in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(after_frame)
    if hasattr(bpy.types.Scene, 'mcbedrock_flat'):
        del bpy.types.Scene.mcbedrock_flat
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

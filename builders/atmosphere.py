"""One optical-depth evaluation, one AOV, one atmosphere composite.

R carries opacity, G carries sunlight direction, B carries surface coverage.
Sun scattering changes the in-scattered colour, never evaluates another depth
or applies another layer of fog. Clouds use the same shader; the world sky is
the boundary colour and is deliberately not fogged a second time.
"""
import math
import bpy

VERSION = 1
UNIFORMS = (
    ('Density', 'atmosphere_density_uniform', ('Amount', 'Clear distance', 'Distance')),
    ('Height', 'atmosphere_height_uniform', ('Height', 'Height falloff', 'Use height')),
    ('Render', 'atmosphere_render_uniform', ('Sun exponent', 'Material coverage')),
)


def enabled(scene):
    return scene.mcbedrock_sky.atmosphere_version >= VERSION


def sun_direction_attribute(tree):
    """Read the changing direction as a uniform, without recompiling materials."""
    if tree.nodes.get('Sun direction attribute'):
        return
    node = tree.nodes.new('ShaderNodeAttribute')
    node.name = 'Sun direction attribute'
    node.attribute_type = 'VIEW_LAYER'
    node.attribute_name = 'mcbedrock_sky.atmosphere_sun_direction'
    tree.links.new(node.outputs['Vector'], tree.nodes['Sun direction'].inputs[1])


def parameter_attributes(tree):
    """Upgrade shared constants once; subsequent drags update scene uniforms."""
    if tree.get('mc_atmosphere_uniform_version') == 1:
        return
    for label, property_name, names in UNIFORMS:
        attribute=tree.nodes.new('ShaderNodeAttribute')
        attribute.name='Atmosphere '+label.lower()+' attribute'
        attribute.attribute_type='VIEW_LAYER'
        attribute.attribute_name='mcbedrock_sky.'+property_name
        separate=tree.nodes.new('ShaderNodeSeparateXYZ')
        separate.name='Atmosphere '+label.lower()+' parameters'
        tree.links.new(attribute.outputs['Vector'],separate.inputs[0])
        for index,name in enumerate(names):
            constant=tree.nodes.get(name)
            if constant:
                for link in list(constant.outputs[0].links):
                    tree.links.new(separate.outputs[index],link.to_socket)
                tree.nodes.remove(constant)
    tree['mc_atmosphere_uniform_version']=1


def weight_group(scene):
    from .lighting_pipeline import group, socket, Graph
    tree = group(scene, 'Atmosphere weight', 'ShaderNodeTree')
    if tree.nodes:
        sun_direction_attribute(tree)
        parameter_attributes(tree)
        return tree
    socket(tree, 'Coverage', kind='NodeSocketFloat', default=1)
    socket(tree, 'Weight', 'OUTPUT')
    inp = tree.nodes.new('NodeGroupInput')
    out = tree.nodes.new('NodeGroupOutput')
    g = Graph(tree)
    camera = tree.nodes.new('ShaderNodeCameraData')
    geometry = tree.nodes.new('ShaderNodeNewGeometry')
    position = tree.nodes.new('ShaderNodeSeparateXYZ')
    incoming = tree.nodes.new('ShaderNodeSeparateXYZ')
    tree.links.new(geometry.outputs['Position'], position.inputs[0])
    tree.links.new(geometry.outputs['Incoming'], incoming.inputs[0])
    for name, value in (('Amount', 0), ('Clear distance', 20), ('Distance', 180),
                        ('Height', 0), ('Height falloff', 40), ('Use height', 1),
                        ('Sun exponent', 3), ('Material coverage', 0)):
        node = tree.nodes.new('ShaderNodeValue')
        node.name = name
        node.outputs[0].default_value = value
    value = lambda name: tree.nodes[name].outputs[0]
    distance = g.op('MAXIMUM', g.op('SUBTRACT', camera.outputs['View Distance'], value('Clear distance')), 0)
    # Smooth density integrated along the view ray. One shared evaluation for
    # both base haze and sun scattering, including the height transition.
    samples = []
    for fraction, coefficient in ((.1127016654, 5/18), (.5, 8/18), (.8872983346, 5/18)):
        height = g.op('ADD', position.outputs['Z'], g.op('MULTIPLY', incoming.outputs['Z'], g.op('MULTIPLY', distance, 1-fraction)))
        altitude = g.op('DIVIDE', g.op('SUBTRACT', height, value('Height')), value('Height falloff'))
        density = g.op('DIVIDE', 1, g.op('ADD', 1, g.op('EXPONENT', g.op('MINIMUM', 20, g.op('MAXIMUM', -20, altitude)))))
        samples.append(g.op('MULTIPLY', density, coefficient))
    density = g.op('ADD', samples[0], g.op('ADD', samples[1], samples[2]))
    density = g.op('ADD', 1, g.op('MULTIPLY', value('Use height'), g.op('SUBTRACT', density, 1)))
    optical_depth = g.op('DIVIDE', g.op('MULTIPLY', distance, g.op('MULTIPLY', density, value('Amount'))), value('Distance'))
    weight = g.op('SUBTRACT', 1, g.op('EXPONENT', g.op('MULTIPLY', -3, optical_depth)))
    direction = tree.nodes.new('ShaderNodeVectorMath')
    direction.operation = 'DOT_PRODUCT'
    direction.name = 'Sun direction'
    tree.links.new(geometry.outputs['Incoming'], direction.inputs[0])
    angular = g.op('POWER', g.op('MAXIMUM', 0, g.op('MULTIPLY', -1, direction.outputs['Value'])), value('Sun exponent'))
    # EEVEE already weights AOV samples by stochastic surface coverage.
    # Cycles writes the AOV before transparency and needs material coverage.
    coverage = g.op('ADD', 1, g.op('MULTIPLY', value('Material coverage'), g.op('SUBTRACT', inp.outputs['Coverage'], 1)))
    packed = g.combine([g.op('MULTIPLY', weight, coverage), g.op('MULTIPLY', angular, coverage), coverage])
    tree.links.new(packed, out.inputs[0])
    sun_direction_attribute(tree)
    parameter_attributes(tree)
    return tree


def prepare_material(scene, material):
    from .lighting_pipeline import pass_name, set_value
    tree = material.node_tree
    if tree is None: return
    old_passes = {pass_name(scene, name) for name in ('Terrain fog', 'Sun haze', 'Terrain mask')}
    for old in list(tree.nodes):
        if (old.type == 'OUTPUT_AOV' and old.aov_name in old_passes) or old.name in {'MC Terrain fog weight', 'MC Sun haze weight'}:
            tree.nodes.remove(old)
    node = tree.nodes.get('MC Atmosphere weight')
    if node is None:
        node = tree.nodes.new('ShaderNodeGroup')
        node.name = 'MC Atmosphere weight'
        node.node_tree = weight_group(scene)
        aov = tree.nodes.new('ShaderNodeOutputAOV')
        aov.name = 'MC Atmosphere buffer'
        aov.aov_name = pass_name(scene, 'Atmosphere')
        tree.links.new(node.outputs[0], aov.inputs['Color'])
    shaders = [n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED']
    # Preserve cutouts even when Cycles evaluates AOVs before transparency.
    # Multiple closures still get fog; native renderer coverage resolves them.
    if len(shaders) == 1:
        alpha = shaders[0].inputs['Alpha']
        target = node.inputs['Coverage']
        if alpha.links:
            source = alpha.links[0].from_socket
            if not target.links or target.links[0].from_socket != source:tree.links.new(source, target)
        else:
            for link in list(target.links):tree.links.remove(link)
            set_value(target, 'default_value', alpha.default_value)


def setup(scene, tree, render, destination):
    from .lighting_pipeline import Graph, TAG, pass_name
    if tree.nodes.get('MC Atmosphere'): return
    # Remove serial legacy fog stages from the image path when the user opts
    # into the updated look. Old saved looks keep their old graph until then.
    source = destination.links[0].from_socket
    while source.node.name in {'MC Terrain fog', 'MC Sun on terrain'}:
        source = source.node.inputs[6].links[0].from_socket
    tree.links.new(source, destination)
    for name in ('MC Terrain fog', 'MC Sun on terrain'):
        node = tree.nodes.get(name)
        if node: tree.nodes.remove(node)
    g = Graph(tree)
    weight, direction, coverage = g.split(render.outputs[pass_name(scene, 'Atmosphere')])
    direction = g.op('DIVIDE', direction, g.op('MAXIMUM', coverage, 1e-6))
    sun = tree.nodes.new('ShaderNodeMix')
    sun.name = 'MC Atmosphere color'
    sun.data_type = 'RGBA'
    sun.clamp_result = False
    amount = tree.nodes.new('ShaderNodeMath')
    amount.operation = 'MULTIPLY'
    amount.name = 'MC Atmosphere sun'
    tree.links.new(direction, amount.inputs[0])
    tree.links.new(amount.outputs[0], sun.inputs[0])
    mix = tree.nodes.new('ShaderNodeMix')
    mix.name = 'MC Atmosphere'
    mix.data_type = 'RGBA'
    mix.clamp_result = False
    alpha = tree.nodes.new('CompositorNodeSeparateColor')
    tree.links.new(source, alpha.inputs[0])
    safe_alpha = g.op('MAXIMUM', alpha.outputs[3], 1e-6)
    # AOV opacity includes coverage. Unpremultiply both the image and weight
    # before blending, then restore alpha; otherwise half-covered edges retain
    # too much foreground and form halos over another background.
    factor = g.op('MINIMUM', 1, g.op('MAXIMUM', 0, g.op('DIVIDE', weight, safe_alpha)))
    straight = g.combine([g.op('DIVIDE', c, safe_alpha) for c in g.split(source)])
    tree.links.new(factor, mix.inputs[0])
    tree.links.new(straight, mix.inputs[6])
    tree.links.new(sun.outputs[2], mix.inputs[7])
    result = g.combine([g.op('MULTIPLY', c, alpha.outputs[3]) for c in g.split(mix.outputs[2])], alpha.outputs[3])
    tree.links.new(result, destination)
    for node in (sun, amount, mix, alpha): node[TAG] = scene.mcbedrock_sky.owner


def retire_legacy_passes(scene):
    from .lighting_pipeline import pass_name, native_mist
    names = {pass_name(scene, name) for name in ('Terrain fog', 'Sun haze', 'Terrain mask')}
    for layer in scene.view_layers:
        for aov in list(layer.aovs):
            if aov.name in names:layer.aovs.remove(aov)
    native_mist.restore(scene)


def lighting_colors(scene):
    """Art-directed haze from the rig's scene-linear horizon and sunlight."""
    from .sky import owned
    s = scene.mcbedrock_sky
    from . import sky_drive
    nodes = s.world.node_tree.nodes
    horizon = nodes['Sky colors'].color_ramp.elements[1].color[:3]
    strength = sky_drive.value(nodes['Visible sky'].inputs['Strength'])
    ambient = sky_drive.value(nodes['Ambient light color'].outputs[0])[:3]
    # Keep distant terrain subdued while following custom skies and twilight.
    base = tuple(.65 * h * strength + .35 * a for h, a in zip(horizon, ambient))
    sun = owned(scene, 'Sun')
    direct = tuple(sun.data.color) if sun else (1, 1, 1)
    return base, tuple(c * s.sun_world_haze_radiance for c in direct)


def update(scene):
    from .lighting_pipeline import set_value
    from .sky import time_state, current_time, smoothstep, mix
    s = scene.mcbedrock_sky
    weight_group(scene)
    direction, day, twilight = time_state(current_time(scene), s.azimuth)
    set_value(s,'atmosphere_density_uniform',(s.fog if s.terrain_fog else 0,s.fog_start,s.fog_distance))
    set_value(s,'atmosphere_height_uniform',(s.sun_world_haze_height,s.sun_world_haze_height_falloff,
                                           float(s.atmosphere_height_enabled)))
    exponent=math.log(.5)/math.log(math.cos(math.radians(s.sun_world_haze_spread)))
    set_value(s,'atmosphere_render_uniform',(exponent,float(scene.render.engine=='CYCLES'),0))
    set_value(s, 'atmosphere_sun_direction', tuple(direction))
    comp = s.compositor
    if comp and comp.nodes.get('MC Atmosphere color'):
        base = tuple(s.fog_color)
        if s.atmosphere_auto_color:
            base = mix((.025, .045, .10), (.48, .64, .78), day)
            base = mix(base, (.85, .48, .16), twilight*smoothstep(-.15, .06, direction.z)*.6)
        sunlight = tuple(c*s.sun_world_haze_radiance for c in s.sun_world_haze_color)
        amount = s.sun_world_haze*smoothstep(-.18, .08, direction.z)
        if s.atmosphere_auto_color and s.atmosphere_match_lighting:
            base, sunlight = lighting_colors(scene)
            amount *= min(1, s.sunlight)
        # These follow the sun every frame of a moving cycle: driven (sky_drive).
        from . import sky_drive
        color = comp.nodes['MC Atmosphere color']
        sky_drive.write(color.inputs[6], 'default_value', (*base, 1), tolerance=1e-6)
        sky_drive.write(color.inputs[7], 'default_value', (*sunlight, 1), tolerance=1e-6)
        sky_drive.write(comp.nodes['MC Atmosphere sun'].inputs[1], 'default_value', amount, tolerance=1e-6)


def defaults(scene):
    """An explicit new-look recipe, never applied passively to an old scene."""
    from .sky_materials import scene_meshes
    from mathutils import Vector
    s = scene.mcbedrock_sky
    points = [obj.matrix_world @ Vector(corner) for obj in scene_meshes(scene) for corner in obj.bound_box]
    low = min((p.z for p in points), default=0)
    high = max((p.z for p in points), default=80)
    span = max((max(p[i] for p in points)-min(p[i] for p in points) for i in (0, 1)), default=240) if points else 240
    # No distance haze by default: it washed whole worlds pale. The sky adds
    # the flat fog planes in front of the camera instead (sky.apply).
    values = dict(atmosphere_version=VERSION, terrain_fog=False, atmosphere_height_enabled=True,
                  atmosphere_auto_color=True, atmosphere_match_lighting=False,
                  volume_height_fog=False, volume_anisotropy=.2,
                  fog=0.,
                  fog_start=max(40, min(250, span*.2)), fog_distance=max(120, min(2000, span*.8)),
                  sun_world_haze_height=low+(high-low)*.45,
                  sun_world_haze_height_falloff=max(16, (high-low)*.45),
                  sun_world_haze=0.,
                  sun_world_haze_radiance=1.4, sun_world_haze_spread=55)
    for key, value in values.items(): s[key] = value

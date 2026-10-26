"""Cloud motion streaks: a long-exposure trail behind the sky rig's clouds.

A toggle on the Sky & Lighting page (Sky & Clouds). When on, a Geometry
Nodes modifier on the clouds lays faint copies of them behind themselves,
against the cloud direction, out to **Streak length** blocks; the copies
carry a 0..1 position along the trail (`mc_cloud_streak`, 0 at the clouds
themselves) and the cloud material fades them out by it, so the clouds stay
solid and the trail thins to nothing. While it is on, the cloud material
renders Blended in EEVEE: Dithered turns the faint overlapping copies into
a stipple that more samples never smooth out. It reads like a timelapse in a still
and while the clouds drift, the same in EEVEE and Cycles. Off removes the
modifier; the material fade is inert without the attribute (it reads 0).
"""
import math

import bpy
from mathutils import Vector

MODIFIER = 'MC Cloud streak'
GROUP = 'MC Cloud streak'
ATTRIBUTE = 'mc_cloud_streak'
FADE = 'Cloud streak fade'
COPIES = 32            # copies along the trail, the clouds included
TRAIL = .12            # the first trailing copy's opacity; it thins to 0 at the end
VERSION = 2
METHOD = 'mc_streak_render_method'   # material: its render method before the streak


def _group(owner):
    """The trail, one group per sky rig; its copies and step live on nodes
    inside it (Blender 5 keeps modifier inputs out of reach of Python)."""
    name = GROUP + ' ' + owner
    group = bpy.data.node_groups.get(name)
    if group is not None and group.get('mc_version') == VERSION:
        return group
    if group is None:
        group = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    group['mc_version'] = VERSION
    group['mc_sky_owner'] = owner
    group.nodes.clear()
    group.interface.clear()
    group.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    nodes, links = group.nodes, group.links
    inputs = nodes.new('NodeGroupInput')
    outputs = nodes.new('NodeGroupOutput')
    copies = nodes.new('FunctionNodeInputInt')
    copies.name = copies.label = 'Streak copies'
    copies.integer = COPIES
    step = nodes.new('FunctionNodeInputVector')
    step.name = step.label = 'Streak step'
    line = nodes.new('GeometryNodeMeshLine')
    line.mode = 'OFFSET'
    links.new(copies.outputs[0], line.inputs['Count'])
    links.new(step.outputs[0], line.inputs['Offset'])
    place = nodes.new('GeometryNodeInstanceOnPoints')
    links.new(line.outputs[0], place.inputs['Points'])
    links.new(inputs.outputs[0], place.inputs['Instance'])
    # Where a copy sits along the trail: 0 at the clouds, 1 at the end.
    index = nodes.new('GeometryNodeInputIndex')
    last = nodes.new('ShaderNodeMath')
    last.operation = 'SUBTRACT'
    links.new(copies.outputs[0], last.inputs[0])
    last.inputs[1].default_value = 1
    span = nodes.new('ShaderNodeMath')
    span.operation = 'MAXIMUM'
    links.new(last.outputs[0], span.inputs[0])
    span.inputs[1].default_value = 1
    along = nodes.new('ShaderNodeMath')
    along.operation = 'DIVIDE'
    links.new(index.outputs[0], along.inputs[0])
    links.new(span.outputs[0], along.inputs[1])
    store = nodes.new('GeometryNodeStoreNamedAttribute')
    store.data_type = 'FLOAT'
    store.domain = 'INSTANCE'
    store.inputs['Name'].default_value = ATTRIBUTE
    links.new(place.outputs[0], store.inputs['Geometry'])
    links.new(along.outputs[0], store.inputs['Value'])
    realize = nodes.new('GeometryNodeRealizeInstances')
    links.new(store.outputs[0], realize.inputs[0])
    links.new(realize.outputs[0], outputs.inputs[0])
    for x, item in enumerate((inputs, line, place, store, realize, outputs)):
        item.location = (x * 220, 0)
    for x, item in enumerate((copies, step, index, last, span, along)):
        item.location = (x * 160, -260)
    return group


def _fade(material):
    """Thin the trail in the cloud material: the clouds keep their alpha,
    copies behind them start at TRAIL and fade to nothing. The sky rewires the
    cloud alpha on every update (sky_clouds.sync), so this re-wraps whatever
    feeds it each time."""
    tree = material.node_tree
    shader = tree.nodes.get('Principled BSDF') if tree is not None else None
    if shader is None:
        return
    nodes, links = tree.nodes, tree.links
    fade = nodes.get(FADE)
    if fade is None:
        def math_node(operation, a, b):
            n = nodes.new('ShaderNodeMath')
            n.operation = operation
            for socket, value in zip(n.inputs, (a, b)):
                if isinstance(value, (int, float)):
                    socket.default_value = value
                else:
                    links.new(value, socket)
            return n
        attribute = nodes.new('ShaderNodeAttribute')
        attribute.attribute_type = 'GEOMETRY'
        attribute.attribute_name = ATTRIBUTE
        attribute.name = attribute.label = 'Cloud streak position'
        along = attribute.outputs['Fac']
        head = math_node('LESS_THAN', along, 1e-4).outputs[0]
        trail = math_node('MULTIPLY', math_node('SUBTRACT', 1., along).outputs[0], TRAIL).outputs[0]
        # head ? 1 : trail
        factor = math_node('ADD', head, math_node('MULTIPLY', math_node('SUBTRACT', 1., head).outputs[0], trail).outputs[0])
        fade = math_node('MULTIPLY', factor.outputs[0], 1.)
        fade.name = fade.label = FADE
        attribute.location = (shader.location.x - 700, shader.location.y - 500)
        fade.location = (shader.location.x - 200, shader.location.y - 500)
    alpha = shader.inputs['Alpha']
    base = alpha.links[0].from_socket if alpha.links else None
    if base is not None and base.node == fade:
        return
    if base is not None:
        if not fade.inputs[1].links or fade.inputs[1].links[0].from_socket != base:
            links.new(base, fade.inputs[1])
    else:
        for link in list(fade.inputs[1].links):
            links.remove(link)
        fade.inputs[1].default_value = alpha.default_value
    links.new(fade.outputs[0], alpha)


def clouds(scene):
    s = scene.mcbedrock_sky
    if not s.collection:
        return []
    return [o for o in s.collection.objects if o.get('role') == 'Cloud' and o.type == 'MESH']


def sync(scene):
    """Add, update or remove the streak on the rig's clouds to match the settings."""
    s = scene.mcbedrock_sky
    for cloud in clouds(scene):
        modifier = cloud.modifiers.get(MODIFIER)
        if not s.cloud_streak:
            if modifier is not None:
                cloud.modifiers.remove(modifier)
            for material in cloud.data.materials:
                if material is not None and METHOD in material:
                    material.surface_render_method = material[METHOD]
                    del material[METHOD]
            continue
        for material in cloud.data.materials:
            if material is not None:
                _fade(material)
                if hasattr(material, 'surface_render_method') and material.surface_render_method != 'BLENDED':
                    material[METHOD] = material.surface_render_method
                    material.surface_render_method = 'BLENDED'
        if modifier is None:
            modifier = cloud.modifiers.new(MODIFIER, 'NODES')
        group = _group(s.owner)
        if modifier.node_group != group:
            modifier.node_group = group
        # Behind the clouds: against the direction they drift, in the cloud's own space.
        angle = math.radians(s.cloud_direction)
        behind = Vector((-math.cos(angle), -math.sin(angle), 0)) * s.cloud_streak_length / (COPIES - 1)
        local = cloud.matrix_world.to_3x3().inverted_safe() @ behind
        step = group.nodes['Streak step']
        if any(abs(a - b) > 1e-6 for a, b in zip(step.vector, local)):
            step.vector = local


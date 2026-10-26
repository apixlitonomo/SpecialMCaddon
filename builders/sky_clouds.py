"""Owned generated cloud rendering: physical height fade and distant sky haze."""
import bpy
from . import sky_drive

TAG='mc_sky_owner'
_background_cache={}


def clear_cache():
    _background_cache.clear()


def socket_value(socket):
    value=socket.default_value
    return tuple(value) if hasattr(value,'__len__') else value


def assign(target, name, value):
    current=getattr(target,name)
    if hasattr(current,'__len__') and not isinstance(current,str):
        if tuple(current)==tuple(value):return
    elif current==value:return
    setattr(target,name,value)


def sync_dungeons(scene, materials):
    """Solid square undersides fade to transparent tops in actual geometry."""
    from .sky import node,math_node
    s=scene.mcbedrock_sky
    for material in materials:
        tree=material.node_tree
        shader=tree.nodes.get('Principled BSDF')
        if not shader:continue
        if 'Dungeons height fade' not in tree.nodes:
            height=node(tree,'ShaderNodeAttribute','Dungeons local height')
            height.attribute_name='mc_dungeons_cloud_height'
            fade=node(tree,'ShaderNodeMapRange','Dungeons height fade',clamp=True)
            fade.interpolation_type='SMOOTHSTEP'
            fade.inputs['From Min'].default_value=.25
            fade.inputs['From Max'].default_value=1.
            fade.inputs['To Min'].default_value=1.
            fade.inputs['To Max'].default_value=0.
            tree.links.new(height.outputs['Fac'],fade.inputs['Value'])
            opacity=node(tree,'ShaderNodeValue','Dungeons cloud opacity')
            alpha=math_node(tree,'MULTIPLY',fade.outputs[0],opacity.outputs[0])
            distance=math_node(tree,'MULTIPLY',alpha,tree.nodes['Distant cloud fade'].outputs[0])
            distance.node.name='Dungeons final alpha'
        assign(tree.nodes['Dungeons cloud opacity'].outputs[0],'default_value',s.clouds)
        final=tree.nodes['Dungeons final alpha'].outputs[0]
        alpha=shader.inputs['Alpha']
        if not alpha.is_linked or alpha.links[0].from_socket!=final:
            tree.links.new(final,alpha)
        # Do not replace transparent pixels with an opaque sampled sky. Real
        # background geometry and other clouds must show through the top fade.
        surface=tree.nodes['Cloud surface'].inputs[1]
        if not surface.is_linked or surface.links[0].from_node!=shader:
            tree.links.new(shader.outputs[0],surface)


def sync(scene):
    s=scene.mcbedrock_sky
    materials={slot.material for obj in s.collection.objects
               if obj.get('role')=='Cloud' and len(obj.data.polygons)
               for slot in obj.material_slots if slot.material}
    if not materials:return
    if s.cloud_preset=='DUNGEONS':
        sync_dungeons(scene,materials)
        return
    for material in materials:
        tree=material.node_tree
        shader=tree.nodes.get('Principled BSDF')
        if not shader:continue
        alpha=shader.inputs['Alpha']
        for link in list(alpha.links):tree.links.remove(link)
        assign(alpha,'default_value',1)
    world=s.world.node_tree
    display=world.nodes['Visible sky']
    cheap=(world.as_pointer(),s.owner,s.world.get('mc_sky_shader_version'),len(world.nodes),len(world.links))
    cached=_background_cache.get(scene.as_pointer())
    if cached and sky_drive.in_per_frame() and cached.get('generation')==sky_drive.generation():
        # A cycle frame: what moves reaches the copy through drivers, and the
        # sky's graph only changes with its settings, outside cycle frames.
        try:
            group=cached['group']
            if (cached.get('cheap')==cheap and len(group.nodes)==cached['nodes']
                    and len(group.links)==cached['links']):
                connect_background(materials,group)
                return
        except (ReferenceError,RuntimeError):
            pass
    source=display.inputs['Color'].links[0].from_socket
    # Owned world graphs change infrequently. Keep source/clone socket pairs so
    # fog and material edits can compare only actual background inputs, without
    # traversing dependencies or walking every clone socket again. Link identity
    # also detects equal-size rewires such as toggling the sky gradient mapping.
    key=(world.as_pointer(),s.owner,s.world.get('mc_sky_shader_version'),
         source.as_pointer(),len(world.nodes),
         tuple((link.from_socket.as_pointer(),link.to_socket.as_pointer()) for link in world.links))
    if cached and cached['key']==key:
        try:
            group=cached['group']
            valid=(len(group.nodes)==cached['nodes'] and len(group.links)==cached['links'])
            signature=tuple(socket_value(port) for port,_ in cached['ports'])
            ramps=tuple((ramp.interpolation,tuple((stop.position,tuple(stop.color)) for stop in ramp.elements))
                        for ramp,_ in cached['ramps'])
            radiance=display.inputs['Strength'].default_value
            if valid and (signature,ramps,radiance)==cached['values']:
                connect_background(materials,group)
                return
            if valid:
                copy_background_values(cached,signature,ramps,radiance,s.world)
                connect_background(materials,group)
                return
        except (ReferenceError,RuntimeError):
            # Deleted/replaced datablocks after undo or manual edits rebuild.
            pass
    needed={}
    incoming={}
    for link in world.links:incoming.setdefault(link.to_node.name,[]).append(link.from_node)
    def visit(n):
        if n.name in needed:return
        needed[n.name]=n
        for origin in incoming.get(n.name,()):visit(origin)
    visit(source.node)
    group=next((g for g in bpy.data.node_groups if g.get(TAG)==s.owner and g.get('mc_cloud_background')),None)
    if group is None:
        group=bpy.data.node_groups.new('MC Cloud sky fade','ShaderNodeTree')
        group[TAG]=s.owner;group['mc_cloud_background']=True
        group.interface.new_socket(name='Color',in_out='OUTPUT',socket_type='NodeSocketColor')
    topology='|'.join(sorted(needed))
    topology+='|'+repr(key[-1])
    if group.get('source_version')!=s.world.get('mc_sky_shader_version') or group.get('source_nodes')!=topology:
        group.nodes.clear()
        for name,n in needed.items():
            clone=group.nodes.new('ShaderNodeNewGeometry' if name=='Sky direction' else n.bl_idname)
            clone.name=name
        for name,n in needed.items():
            clone=group.nodes[name]
            # Set structural properties before copying input values and links.
            for prop in ('operation','blend_type','vector_type','gradient_type','voronoi_dimensions','feature','distance','interpolation_type','clamp','use_clamp','node_tree','image','interpolation','extension'):
                if hasattr(n,prop) and hasattr(clone,prop):setattr(clone,prop,getattr(n,prop))
        for name,n in needed.items():
            for index,port in enumerate(n.inputs):
                for link in port.links:
                    origin=group.nodes[link.from_node.name]
                    socket=origin.outputs['Incoming'] if link.from_node.name=='Sky direction' else origin.outputs[list(link.from_node.outputs).index(link.from_socket)]
                    group.links.new(socket,group.nodes[name].inputs[index])
        strength=group.nodes.new('ShaderNodeVectorMath');strength.name='Cloud sky radiance';strength.operation='SCALE'
        group.links.new(group.nodes[source.node.name].outputs[list(source.node.outputs).index(source)],strength.inputs[0])
        out=group.nodes.new('NodeGroupOutput')
        group.links.new(strength.outputs[0],out.inputs['Color'])
        group['source_version']=s.world.get('mc_sky_shader_version')
        group['source_nodes']=topology
    ports=[]
    ramps=[]
    for name,n in needed.items():
        clone=group.nodes[name]
        if name=='Sky direction':continue
        for port,copy in zip(n.inputs,clone.inputs):
            if not port.is_linked and hasattr(port,'default_value') and hasattr(copy,'default_value'):
                ports.append((port,copy))
        if n.type in {'VALUE','RGB'}:
            ports.extend((port,copy) for port,copy in zip(n.outputs,clone.outputs)
                         if hasattr(port,'default_value') and hasattr(copy,'default_value'))
        if n.type=='VALTORGB':
            ramp=clone.color_ramp
            while len(ramp.elements)<len(n.color_ramp.elements):ramp.elements.new(.5)
            while len(ramp.elements)>len(n.color_ramp.elements):ramp.elements.remove(ramp.elements[-1])
            ramps.append((n.color_ramp,ramp))
    cached=dict(key=key,cheap=cheap,group=group,ports=ports,ramps=ramps,radiance=display.inputs['Strength'],
                nodes=len(group.nodes),links=len(group.links))
    _background_cache[scene.as_pointer()]=cached
    signature=tuple(socket_value(port) for port,_ in ports)
    ramp_values=tuple((ramp.interpolation,tuple((stop.position,tuple(stop.color)) for stop in ramp.elements))
                      for ramp,_ in ramps)
    copy_background_values(cached,signature,ramp_values,display.inputs['Strength'].default_value,s.world)
    connect_background(materials,group)


def _follow(copy,name,source,value,world):
    """The copy reads the world's own value through a driver when the world
    keeps one (the cycle moves it every frame); else it is copied once."""
    if world is None or not sky_drive.follow(copy,name,world,sky_drive.key_of(source,name)):
        assign(copy,name,value)


def copy_background_values(cached,signature,ramps,radiance,world=None):
    for (port,copy),value in zip(cached['ports'],signature):
        _follow(copy,'default_value',port,value,world)
    for (source,ramp),(interpolation,values) in zip(cached['ramps'],ramps):
        while len(ramp.elements)<len(values):ramp.elements.new(.5)
        while len(ramp.elements)>len(values):ramp.elements.remove(ramp.elements[-1])
        assign(ramp,'interpolation',interpolation)
        for theirs,stop,(position,color) in zip(tuple(source.elements),tuple(ramp.elements),values):
            _follow(stop,'position',theirs,position,world);_follow(stop,'color',theirs,color,world)
    _follow(cached['group'].nodes['Cloud sky radiance'].inputs['Scale'],'default_value',
            cached['radiance'],radiance,world)
    cached['values']=(signature,ramps,radiance)
    cached['generation']=sky_drive.generation()


def connect_background(materials,group):
    for material in materials:
        tree=material.node_tree
        shader=tree.nodes.get('Principled BSDF')
        if not shader:continue
        if 'Cloud opaque distance fade' not in tree.nodes:
            background=tree.nodes.new('ShaderNodeGroup');background.name='Cloud sky background';background.node_tree=group
            emission=tree.nodes.new('ShaderNodeEmission');emission.name='Cloud distant sky'
            tree.links.new(background.outputs['Color'],emission.inputs['Color'])
            mix=tree.nodes.new('ShaderNodeMixShader');mix.name='Cloud opaque distance fade'
            tree.links.new(tree.nodes['Distant cloud fade'].outputs[0],mix.inputs[0])
            tree.links.new(emission.outputs[0],mix.inputs[1]);tree.links.new(shader.outputs[0],mix.inputs[2])
        elif tree.nodes['Cloud sky background'].node_tree!=group:tree.nodes['Cloud sky background'].node_tree=group
        mix=tree.nodes['Cloud opaque distance fade']
        emission=tree.nodes['Cloud distant sky']
        if not mix.inputs[1].is_linked or mix.inputs[1].links[0].from_node!=emission:
            tree.links.new(emission.outputs[0],mix.inputs[1])
        surface=tree.nodes['Cloud surface'].inputs[1]
        if not surface.is_linked or surface.links[0].from_node!=mix:
            tree.links.new(mix.outputs[0],surface)

"""Scene-owned LOOCS lighting, texture compensation, grading and emissive glow.

Independent of importers. Original material slots are owned/restored by sky_materials.
"""
import math
import json
from pathlib import Path
import bpy
from bpy.app.handlers import persistent
from . import native_mist, atmosphere, native_color, edge_details, flat_fog, film_look

TAG='mc_sky_owner'
ROOT=Path(__file__).resolve().parents[1]/'assets/loocs'
VERSION=1
_signatures={}
_update_signatures={}
_material_dirty=set()
_material_counts={}


@persistent
def materials_changed(scene, depsgraph):
    """Schedule slot discovery for structural edits, not camera/object motion."""
    s = getattr(scene, 'mcbedrock_sky', None)
    if not s or not s.active or not s.auto_materials:
        return
    for change in depsgraph.updates:
        item = change.id
        if isinstance(item, bpy.types.Collection) or (
                isinstance(item, (bpy.types.Object, bpy.types.Mesh))
                and not item.get(TAG)
                and (change.is_updated_geometry or change.is_updated_shading)):
            # Instance collections may also be used by another scene.
            _material_dirty.update(sc.as_pointer() for sc in bpy.data.scenes)
            break


def set_value(target,name,value):
    """Avoid invalidating shaders for unchanged RNA values."""
    old=getattr(target,name)
    if isinstance(value,(tuple,list)):
        same=len(old)==len(value) and all(abs(a-b)<1e-6 for a,b in zip(old,value))
    elif isinstance(value,float):same=abs(old-value)<1e-6
    else:same=old==value
    if not same:setattr(target,name,value)


class Graph:
    def __init__(self,tree):self.tree=tree;self.cache={}
    def op(self,name,*values):
        if all(isinstance(x,(int,float)) for x in values):
            from .loocs_transform import numpy_op
            return float(numpy_op(name,*values))
        key=(name,tuple(x if isinstance(x,(int,float)) else x.as_pointer() for x in values))
        if key in self.cache:return self.cache[key]
        n=self.tree.nodes.new('ShaderNodeMath');n.operation=name
        for socket,value in zip(n.inputs,values):self.put(socket,value)
        self.cache[key]=n.outputs[0]
        return n.outputs[0]
    def put(self,socket,value):
        if isinstance(value,(int,float,tuple,list)):
            if isinstance(value,(int,float)) and socket.type=='RGBA':value=(value,value,value,1)
            socket.default_value=value
        else:self.tree.links.new(value,socket)
    def split(self,color):
        n=self.tree.nodes.new('CompositorNodeSeparateColor' if self.tree.bl_idname=='CompositorNodeTree' else 'ShaderNodeSeparateColor')
        n.mode='RGB';self.put(n.inputs[0],color)
        return list(n.outputs)[:3]
    def combine(self,values,alpha=None):
        n=self.tree.nodes.new('CompositorNodeCombineColor' if self.tree.bl_idname=='CompositorNodeTree' else 'ShaderNodeCombineColor');n.mode='RGB'
        for s,v in zip(n.inputs,values):self.put(s,v)
        if alpha is not None and len(n.inputs)>3:self.put(n.inputs[3],alpha)
        return n.outputs[0]


def group(scene,name,kind):
    owner=scene.mcbedrock_sky.owner
    existing=next((g for g in bpy.data.node_groups if g.get(TAG)==owner and g.get('mc_lighting_kind')==name),None)
    if existing:return existing
    g=bpy.data.node_groups.new('MC Lighting · '+name,kind);g[TAG]=owner;g['mc_lighting_kind']=name
    return g


def socket(tree,name,direction='INPUT',kind='NodeSocketColor',default=None):
    s=tree.interface.new_socket(name=name,in_out=direction,socket_type=kind)
    if default is not None:s.default_value=default
    return s


def asset_image(name):
    key='MC LOOCS 0.11 · '+name
    image=bpy.data.images.get(key)
    if image:return image
    import numpy as np
    a=np.load(ROOT/(name+'.npy'),allow_pickle=False)
    if name=='inverse_srgb':
        # [blue, green, red] -> rows=green, x=blue*size+red, like correction_image.
        size=a.shape[0];a=a.transpose(1,0,2,3).reshape(size,size*size,3)
    else:a=np.repeat(a[...,None],3,axis=2)
    h,w=a.shape[:2];rgba=np.ones((h,w,4),dtype=np.float32);rgba[...,:3]=a
    image=bpy.data.images.new(key,width=w,height=h,alpha=True,float_buffer=True)
    image.colorspace_settings.name='Non-Color';image.pixels.foreach_set(rgba.ravel());image.pack()
    image['mc_lighting_asset']=True
    return image


def correction_image(key):
    """LOOCS 0.12 sRGB texture-correction LUT as a 2D atlas (blue slices along x)."""
    name='MC LOOCS 0.12 · TC '+native_color.RENDERING_LABELS[key]
    image=bpy.data.images.get(name)
    if image:return image
    import numpy as np
    rows=native_color.correction_table(key)
    size=round(len(rows)**(1/3))
    # .cube order is red fastest: (blue, green, red) -> rows=green, x=blue*size+red.
    a=rows.reshape(size,size,size,3).transpose(1,0,2,3).reshape(size,size*size,3)
    rgba=np.ones((size,size*size,4),dtype=np.float32);rgba[...,:3]=a
    image=bpy.data.images.new(name,width=size*size,height=size,alpha=True,float_buffer=True)
    image.colorspace_settings.name='Non-Color';image.pixels.foreach_set(rgba.ravel());image.pack()
    image['mc_lighting_asset']=True
    return image


def compensation(scene):
    """The texture correction for the active LOOCS rendering, as shader nodes.

    Sparkle classic (Chroma Luma's default) is Sparkle's inverse of the view:
    linear colour in, linear colour out, so the final surface colour shows as
    its texture under the key light. LOOCS's own corrections equal reading the
    texture in the "sRGB TC <rendering>" colour space: the original 8-bit sRGB
    value passes through Bram's LUT, then a 2.2 decode."""
    t=group(scene,'Texture compensation','ShaderNodeTree')
    working = native_color.working_space()
    key=native_color.correction(scene) or native_color.CLASSIC
    if t.get('mc_lut_sampling_version')==5 and t.get('mc_lut_working_space')==working and t.get('mc_lut_correction')==key:return t
    # Rebuild in place so saved material links and compensation amounts survive.
    t.nodes.clear()
    if not t.interface.items_tree:
        socket(t,'Color');socket(t,'Amount',kind='NodeSocketFloat',default=1);socket(t,'Color','OUTPUT')
    source=t.nodes.new('NodeGroupInput');out=t.nodes.new('NodeGroupOutput');g=Graph(t);op=g.op
    c=g.split(source.outputs['Color'])
    def convert(rgb, source_space, destination_space):
        if source_space == destination_space:
            return rgb
        matrix = native_color.linear_matrix(source_space, destination_space)
        return [op('ADD', op('ADD', op('MULTIPLY', rgb[0], row[0]),
                                  op('MULTIPLY', rgb[1], row[1])),
                          op('MULTIPLY', rgb[2], row[2])) for row in matrix]
    c=convert(c,working,'Linear Rec.709')
    classic=key==native_color.CLASSIC
    image=asset_image('inverse_srgb') if classic else correction_image(key)
    size=image.size[1];last=size-1
    # Re-encode to the texture's stored sRGB value, which addresses the LUT.
    coord=[]
    for x in c:
        x=op('MINIMUM',op('MAXIMUM',x,0),1)
        linear=op('MULTIPLY',x,12.92)
        curve=op('SUBTRACT',op('MULTIPLY',op('POWER',x,1/2.4),1.055),.055)
        f=op('GREATER_THAN',x,.0031308)
        coord.append(op('MULTIPLY',op('ADD',op('MULTIPLY',f,curve),op('MULTIPLY',op('SUBTRACT',1,f),linear)),last))
    low=[op('MINIMUM',op('FLOOR',c),last-1) for c in coord]
    high=[op('ADD',c,1) for c in low]
    fractions=[op('SUBTRACT',c,l) for c,l in zip(coord,low)]
    def lerp(a,b,f):
        n=t.nodes.new('ShaderNodeMixRGB');g.put(n.inputs[0],f);g.put(n.inputs[1],a);g.put(n.inputs[2],b)
        return n.outputs[0]
    # A LUT is data, not a surface texture. EEVEE's linear sampler uses colour
    # derivatives as UV derivatives and mipmaps across unrelated atlas slices at
    # texel boundaries, creating yellow fringes. Fetch eight exact grid entries
    # with Closest, then interpolate explicitly in colour space.
    slices=[]
    for blue in (low[2],high[2]):
        rows=[]
        for green in (low[1],high[1]):
            corners=[]
            for red in (low[0],high[0]):
                uv=t.nodes.new('ShaderNodeCombineXYZ')
                g.put(uv.inputs[0],op('DIVIDE',op('ADD',op('ADD',op('MULTIPLY',blue,size),red),.5),size*size))
                g.put(uv.inputs[1],op('DIVIDE',op('ADD',green,.5),size))
                tex=t.nodes.new('ShaderNodeTexImage');tex.image=image;tex.interpolation='Closest';tex.extension='EXTEND'
                t.links.new(uv.outputs[0],tex.inputs['Vector']);corners.append(tex.outputs['Color'])
            rows.append(lerp(*corners,fractions[0]))
        slices.append(lerp(*rows,fractions[1]))
    result=g.split(lerp(*slices,fractions[2]))
    if not classic:
        # LOOCS decodes sRGB textures with a pure 2.2 gamma.
        result=[op('POWER',op('MAXIMUM',x,0),2.2) for x in result]
    result=g.combine(convert(result,'Linear Rec.709',working))
    amount=t.nodes.new('ShaderNodeMixRGB');t.links.new(source.outputs['Amount'],amount.inputs[0]);t.links.new(source.outputs['Color'],amount.inputs[1]);t.links.new(result,amount.inputs[2]);t.links.new(amount.outputs[0],out.inputs[0])
    t['mc_lut_sampling_version']=5
    t['mc_lut_working_space']=working
    t['mc_lut_correction']=key
    return t


def insert_color(tree,port,node):
    if port.links:tree.links.new(port.links[0].from_socket,node.inputs['Color'])
    else:node.inputs['Color'].default_value=port.default_value
    tree.links.new(node.outputs[0],port)


def bound_diffuse_compensation(scene,material):
    """Limit compensation to the in-gamut segment from the original texture."""
    tree=material.node_tree
    comp=tree.nodes.get('MC compensate Base Color')
    shaders=[n for n in tree.nodes if n.type=='BSDF_PRINCIPLED']
    if comp is None or len(shaders)!=1:return
    if material.get('mc_diffuse_bound_version')==2:return
    bound=group(scene,'Diffuse compensation bound','ShaderNodeTree')
    if bound.get('version')!=2:
        bound.nodes.clear()
        if not bound.interface.items_tree:
            socket(bound,'Color');socket(bound,'Color','OUTPUT')
        if not any(p.name=='Original' and p.in_out=='INPUT' for p in bound.interface.items_tree):
            socket(bound,'Original')
        inp=bound.nodes.new('NodeGroupInput');out=bound.nodes.new('NodeGroupOutput')
        g=Graph(bound)
        rgb=[g.op('MAXIMUM',c,0) for c in g.split(inp.outputs['Color'])]
        original=[g.op('MINIMUM',1,g.op('MAXIMUM',c,0)) for c in g.split(inp.outputs['Original'])]
        delta=[g.op('SUBTRACT',c,o) for c,o in zip(rgb,original)]
        limits=[g.op('DIVIDE',g.op('SUBTRACT',1,o),g.op('MAXIMUM',d,1e-8)) for o,d in zip(original,delta)]
        # A non-increasing channel imposes no bound (including original white).
        limits=[g.op('ADD',g.op('MULTIPLY',g.op('GREATER_THAN',d,0),limit),g.op('SUBTRACT',1,g.op('GREATER_THAN',d,0))) for d,limit in zip(delta,limits)]
        amount=g.op('MINIMUM',1,g.op('MINIMUM',g.op('MINIMUM',limits[0],limits[1]),limits[2]))
        bound.links.new(g.combine([g.op('ADD',o,g.op('MULTIPLY',d,amount)) for o,d in zip(original,delta)]),out.inputs[0])
        bound['version']=2
    node=tree.nodes.get('MC Diffuse compensation bound') or tree.nodes.new('ShaderNodeGroup')
    node.name='MC Diffuse compensation bound';node.node_tree=bound
    tree.links.new(comp.outputs[0],node.inputs['Color'])
    original=comp.inputs['Color']
    if original.links:tree.links.new(original.links[0].from_socket,node.inputs['Original'])
    else:node.inputs['Original'].default_value=original.default_value
    tree.links.new(node.outputs[0],shaders[0].inputs['Base Color'])
    material['mc_diffuse_bound_version']=2


COVERAGE='MC AOV coverage'


def aov_coverage(scene,material):
    """Weight the lighting passes by the surface's alpha.

    Cycles writes AOVs at every surface a camera ray passes, transparent or
    not, and adds them up; EEVEE drops transparent pixels. A torch's model is
    mostly empty card: in Cycles its invisible pixels summed huge emission
    into the glow passes, the emission mask passed 1, and the glow stage
    turned the whole card black with a white ring around it. Weighted by
    alpha, cut-out pixels add nothing in either engine."""
    tree=material.node_tree
    shaders=[n for n in tree.nodes if n.type=='BSDF_PRINCIPLED']
    if len(shaders)!=1:return
    alpha=shaders[0].inputs['Alpha']
    source=alpha.links[0].from_socket if alpha.links else None
    names={pass_name(scene,suffix) for suffix in ['Emission','Emission mask']+[f'Grade {i}' for i in range(1,5)]}
    for aov in [n for n in tree.nodes if n.type=='OUTPUT_AOV' and n.aov_name in names]:
        for port in (aov.inputs['Color'],aov.inputs['Value']):
            if not port.links:continue
            node=port.links[0].from_node
            if not node.name.startswith(COVERAGE):
                value=port.links[0].from_socket
                if port.name=='Color':
                    node=tree.nodes.new('ShaderNodeVectorMath');node.operation='SCALE'
                    tree.links.new(value,node.inputs[0])
                else:
                    node=tree.nodes.new('ShaderNodeMath');node.operation='MULTIPLY'
                    tree.links.new(value,node.inputs[0])
                node.name=node.label=COVERAGE
                node.location=(aov.location.x-180,aov.location.y)
                tree.links.new(node.outputs[0],port)
            weight=node.inputs['Scale'] if node.bl_idname=='ShaderNodeVectorMath' else node.inputs[1]
            if source is not None:
                if not weight.links or weight.links[0].from_socket!=source:tree.links.new(source,weight)
            else:
                for link in list(weight.links):tree.links.remove(link)
                set_value(weight,'default_value',float(alpha.default_value))


def prepare_material(scene,material):
    fog_material(scene,material)
    edge_details.prepare_material(scene, material)
    # Upgrade already-prepared saved materials without wrapping them twice.
    bound_diffuse_compensation(scene,material)
    if material.get('mc_lighting_version')==VERSION:
        aov_coverage(scene,material)
        return
    t=material.node_tree;g=Graph(t)
    shaders=[n for n in t.nodes if n.type in {'BSDF_PRINCIPLED','EMISSION'}]
    if len(shaders)!=1:
        material['mc_lighting_unsupported']='Multiple surface shaders; original shading retained'
        return
    shader=shaders[0]
    # Auxiliary passes are unavailable for EEVEE's Blended surfaces. This is a
    # scene-owned copy; restoring Lighting restores the user's original method.
    if hasattr(material,'surface_render_method'):material.surface_render_method='DITHERED'
    for name in ('Base Color','Emission Color') if shader.type=='BSDF_PRINCIPLED' else ('Color',):
        port=shader.inputs[name]
        comp=t.nodes.new('ShaderNodeGroup');comp.node_tree=compensation(scene);comp.name='MC compensate '+name
        insert_color(t,port,comp)
    bound_diffuse_compensation(scene,material)
    color=shader.inputs['Emission Color' if shader.type=='BSDF_PRINCIPLED' else 'Color']
    strength=shader.inputs['Emission Strength' if shader.type=='BSDF_PRINCIPLED' else 'Strength']
    rgb=g.split(color.links[0].from_socket)
    strength_value=strength.links[0].from_socket if strength.links else strength.default_value
    emission=g.combine([g.op('MULTIPLY',c,strength_value) for c in rgb])
    aov=t.nodes.new('ShaderNodeOutputAOV');aov.aov_name=pass_name(scene,'Emission');t.links.new(emission,aov.inputs['Color'])
    mask=t.nodes.new('ShaderNodeOutputAOV');mask.aov_name=pass_name(scene,'Emission mask')
    g.put(mask.inputs['Value'],g.op('MULTIPLY',g.op('GREATER_THAN',strength_value,0),g.op('GREATER_THAN',g.op('MAXIMUM',g.op('MAXIMUM',rgb[0],rgb[1]),rgb[2]),0)))
    info=t.nodes.new('ShaderNodeAttribute');info.attribute_type='OBJECT';info.attribute_name='mc_lighting_group'
    for i in range(1,5):
        aov=t.nodes.new('ShaderNodeOutputAOV');aov.aov_name=pass_name(scene,f'Grade {i}')
        g.put(aov.inputs['Value'],g.op('COMPARE',info.outputs['Fac'],i,.1))
    if shader.type=='BSDF_PRINCIPLED':
        alpha=shader.inputs['Alpha'];original=alpha.links[0].from_socket if alpha.links else alpha.default_value
        tex=t.nodes.new('ShaderNodeTexImage');tex.name='MC blue noise';tex.image=asset_image('blue_noise');tex.interpolation='Closest';tex.extension='REPEAT'
        uv=t.nodes.new('ShaderNodeTexCoord')
        scale=t.nodes.new('ShaderNodeVectorMath');scale.operation='SCALE';scale.inputs['Scale'].default_value=8
        t.links.new(uv.outputs['UV'],scale.inputs[0]);t.links.new(scale.outputs[0],tex.inputs['Vector'])
        threshold=g.op('GREATER_THAN',original,tex.outputs['Color'])
        mix=t.nodes.new('ShaderNodeMixRGB');mix.name='MC opacity';g.put(mix.inputs[1],original);g.put(mix.inputs[2],threshold);t.links.new(mix.outputs[0],alpha)
    material['mc_lighting_version']=VERSION
    aov_coverage(scene,material)


def pass_name(scene,suffix):return 'MC '+scene.mcbedrock_sky.owner[:8]+' '+suffix


def fog_material(scene,material):
    """Only prepared scene surfaces write fog; sky and rig clouds never do."""
    if atmosphere.enabled(scene):
        if hasattr(material,'surface_render_method'):material.surface_render_method='DITHERED'
        atmosphere.prepare_material(scene,material)
        return
    native_mist.coverage(scene,material)
    t=material.node_tree
    if t is None or len([n for n in t.nodes if n.type in {'BSDF_PRINCIPLED','EMISSION'}])!=1:return
    sun_haze_material(scene,material)
    if t.nodes.get('MC Terrain fog weight'):return
    weight=group(scene,'Terrain fog weight','ShaderNodeTree')
    if not weight.nodes:
        socket(weight,'Weight','OUTPUT',kind='NodeSocketFloat')
        g=Graph(weight);out=weight.nodes.new('NodeGroupOutput')
        camera=weight.nodes.new('ShaderNodeCameraData')
        for name,value in [('Fog strength',0),('Fog start',10),('Fog distance',100)]:
            n=weight.nodes.new('ShaderNodeValue');n.name=name;n.outputs[0].default_value=value
        distance=g.op('MAXIMUM',g.op('SUBTRACT',camera.outputs['View Distance'],weight.nodes['Fog start'].outputs[0]),0)
        density=g.op('DIVIDE',weight.nodes['Fog strength'].outputs[0],weight.nodes['Fog distance'].outputs[0])
        g.put(out.inputs[0],g.op('SUBTRACT',1,g.op('POWER',math.e,g.op('MULTIPLY',distance,g.op('MULTIPLY',density,-1)))))
    n=t.nodes.new('ShaderNodeGroup');n.name='MC Terrain fog weight';n.node_tree=weight
    aov=t.nodes.new('ShaderNodeOutputAOV');aov.aov_name=pass_name(scene,'Terrain fog')
    t.links.new(n.outputs[0],aov.inputs['Value'])


def sun_haze_group(scene):
    weight=group(scene,'Sun haze weight','ShaderNodeTree')
    if weight.get('mc_sun_haze_version')!=2:
        if not weight.interface.items_tree:socket(weight,'Weight','OUTPUT',kind='NodeSocketFloat')
        weight.nodes.clear()
        g=Graph(weight);out=weight.nodes.new('NodeGroupOutput')
        camera=weight.nodes.new('ShaderNodeCameraData')
        geometry=weight.nodes.new('ShaderNodeNewGeometry')
        view=weight.nodes.new('ShaderNodeVectorMath');view.operation='SCALE';view.inputs['Scale'].default_value=-1
        weight.links.new(geometry.outputs['Incoming'],view.inputs[0])
        direction=weight.nodes.new('ShaderNodeVectorMath');direction.operation='DOT_PRODUCT';direction.name='Sun direction'
        weight.links.new(view.outputs[0],direction.inputs[0])
        for name,value in [('Sun haze strength',0),('Sun haze start',20),('Sun haze distance',180),('Sun haze exponent',3),('Sun haze height',0),('Sun haze height falloff',16)]:
            n=weight.nodes.new('ShaderNodeValue');n.name=name;n.outputs[0].default_value=value
        distance=g.op('MAXIMUM',g.op('SUBTRACT',camera.outputs['View Distance'],weight.nodes['Sun haze start'].outputs[0]),0)
        position=weight.nodes.new('ShaderNodeSeparateXYZ');weight.links.new(geometry.outputs['Position'],position.inputs[0])
        incoming=weight.nodes.new('ShaderNodeSeparateXYZ');weight.links.new(geometry.outputs['Incoming'],incoming.inputs[0])
        # Integrate a smooth ground layer along the visible ray after the clear
        # foreground. Three Gauss samples avoid a flat surface-height mask and
        # the cost of a volumetric grid; high ridges retain their contrast.
        densities=[]
        for fraction,coefficient in ((.1127016654,5/18),(.5,8/18),(.8872983346,5/18)):
            height=g.op('ADD',position.outputs['Z'],g.op('MULTIPLY',incoming.outputs['Z'],g.op('MULTIPLY',distance,1-fraction)))
            altitude=g.op('DIVIDE',g.op('SUBTRACT',height,weight.nodes['Sun haze height'].outputs[0]),weight.nodes['Sun haze height falloff'].outputs[0])
            # A logistic profile avoids a hard boundary at the layer height.
            altitude=g.op('MINIMUM',g.op('MAXIMUM',altitude,-20),20)
            density=g.op('DIVIDE',1,g.op('ADD',1,g.op('EXPONENT',altitude)))
            densities.append(g.op('MULTIPLY',density,coefficient))
        optical_distance=g.op('MULTIPLY',distance,g.op('ADD',g.op('ADD',densities[0],densities[1]),densities[2]))
        depth=g.op('SUBTRACT',1,g.op('EXPONENT',g.op('DIVIDE',g.op('MULTIPLY',optical_distance,-1),weight.nodes['Sun haze distance'].outputs[0])))
        angular=g.op('POWER',g.op('MAXIMUM',direction.outputs['Value'],0),weight.nodes['Sun haze exponent'].outputs[0])
        g.put(out.inputs[0],g.op('MULTIPLY',g.op('MULTIPLY',depth,angular),weight.nodes['Sun haze strength'].outputs[0]))
        weight['mc_sun_haze_version']=2
    return weight


def sun_haze_material(scene,material):
    t=material.node_tree
    weight=sun_haze_group(scene)
    if t.nodes.get('MC Sun haze weight'):return
    n=t.nodes.new('ShaderNodeGroup');n.name='MC Sun haze weight';n.node_tree=weight
    aov=t.nodes.new('ShaderNodeOutputAOV');aov.aov_name=pass_name(scene,'Sun haze')
    t.links.new(n.outputs[0],aov.inputs['Value'])


SUN_HAZE_NODES=('Sun direction','Sun haze strength','Sun haze start','Sun haze distance','Sun haze height',
                'Sun haze height falloff','Sun haze exponent')


def update_sun_haze(scene):
    if atmosphere.enabled(scene):
        atmosphere.update(scene)
        return
    """Time changes update one shared shader, without revisiting all materials."""
    s=scene.mcbedrock_sky
    weight=next((g for g in bpy.data.node_groups if g.get(TAG)==s.owner and g.get('mc_lighting_kind')=='Sun haze weight'),None)
    if weight is None:return
    if weight.get('mc_sun_haze_version')!=2 or any(weight.nodes.get(name) is None for name in SUN_HAZE_NODES):
        # A renamed or deleted node rebuilds the group in place instead of failing every update.
        weight['mc_sun_haze_version']=0
        weight=sun_haze_group(scene)
    if s.sun_world_haze==0:
        set_value(weight.nodes['Sun haze strength'].outputs[0],'default_value',0.)
        return
    from .sky import time_state,current_time,smoothstep
    direction=time_state(current_time(scene),s.azimuth)[0]
    set_value(weight.nodes['Sun direction'].inputs[1],'default_value',tuple(direction))
    set_value(weight.nodes['Sun haze strength'].outputs[0],'default_value',s.sun_world_haze*smoothstep(-.18,.08,direction.z))
    for name,value in [('Sun haze start',s.sun_world_haze_start),('Sun haze distance',s.sun_world_haze_distance),('Sun haze height',s.sun_world_haze_height),('Sun haze height falloff',s.sun_world_haze_height_falloff),('Sun haze exponent',math.log(.5)/math.log(math.cos(math.radians(s.sun_world_haze_spread))))]:
        set_value(weight.nodes[name].outputs[0],'default_value',value)


def ensure_passes(scene):
    for layer in scene.view_layers:
        fog_passes = [('Atmosphere','COLOR')] if atmosphere.enabled(scene) else [('Terrain fog','VALUE'),('Sun haze','VALUE'),('Terrain mask','VALUE')]
        edge_passes = list(edge_details.PASSES) if scene.mcbedrock_sky.edge_highlights > 0 else []
        if not edge_passes:
            names = {pass_name(scene, suffix) for suffix, _ in edge_details.PASSES}
            for aov in list(layer.aovs):
                if aov.name in names: layer.aovs.remove(aov)
        for suffix,kind in [('Emission','COLOR'),('Emission mask','VALUE')]+fog_passes+edge_passes+[(f'Grade {i}','VALUE') for i in range(1,5)]:
            name=pass_name(scene,suffix)
            if not any(a.name==name for a in layer.aovs):
                a=layer.aovs.add();a.name=name;a.type=kind
        layer.update_render_passes()


GLOW_VERSION=3
# Light glow spreads emission over Gaussian layers from tight to wide, the way
# game engines build bloom. Blur size as a fraction of the image height, and
# each layer's share of the glow.
GLOW_LAYERS=((.012,.40),(.04,.27),(.12,.19),(.36,.14))
# Wider layers redden, like light scattered over a longer path. Close to unit
# luminance, so warmth shifts the color without dimming the glow.
WARM_GLOW=(1.45,.82,.42)
# Bright sky spreads over the terrain in front of it, never over the sky.
SKY_GLOW_LAYERS=((.12,.5),(.36,.5))
LUMINANCE=(.2126,.7152,.0722)


def pipeline_group(scene):
    t=group(scene,'ChromaLuma SDR','CompositorNodeTree')
    if not t.nodes or t.get('mc_glow_version')!=GLOW_VERSION:
        build_pipeline(t)
    return t


# The nodes update() writes inside the LOOCS group.
PIPELINE_NODES=('Scene exposure','Glow strength','Light glow scale','Wide light glow','Warm light glow','Sky glow strength',
                *(f'Grade {i} {part}' for i in range(5) for part in ('exposure','saturation','contrast','balance')),
                'Grade 1 mask')


def pipeline_intact(t):
    kinds={n.type for n in t.nodes}
    return ('GROUP_INPUT' in kinds and 'GROUP_OUTPUT' in kinds
            and all(t.nodes.get(name) is not None for name in PIPELINE_NODES))


def build_pipeline(t):
    """Build or rebuild the group in place; links into its group node are kept.

    Every value inside is pushed from the scene settings by update().
    """
    inputs={item.name for item in t.interface.items_tree if item.item_type=='SOCKET' and item.in_out=='INPUT'}
    if not inputs:
        socket(t,'Image');socket(t,'Emission')
        socket(t,'Emission mask',kind='NodeSocketFloat')
        for i in range(1,5):socket(t,f'Grade {i}',kind='NodeSocketFloat')
        socket(t,'Image','OUTPUT')
    if 'Environment' not in inputs:
        socket(t,'Environment',default=(0,0,0,1))
    t.nodes.clear()
    inp=t.nodes.new('NodeGroupInput');out=t.nodes.new('NodeGroupOutput');g=Graph(t)
    image=light_glow(t,g,inp)
    for i in range(1,5):
        exposure=t.nodes.new('CompositorNodeExposure');exposure.name=f'Grade {i} exposure';t.links.new(image,exposure.inputs['Image'])
        saturation=t.nodes.new('CompositorNodeHueSat');saturation.name=f'Grade {i} saturation';t.links.new(exposure.outputs[0],saturation.inputs['Image'])
        contrast=t.nodes.new('CompositorNodeBrightContrast');contrast.name=f'Grade {i} contrast';t.links.new(saturation.outputs[0],contrast.inputs['Image'])
        balance=t.nodes.new('CompositorNodeColorBalance');balance.name=f'Grade {i} balance';t.links.new(contrast.outputs[0],balance.inputs['Image'])
        mix=t.nodes.new('ShaderNodeMix');mix.data_type='RGBA';mix.name=f'Grade {i} mask';mix.clamp_result=False
        t.links.new(_share(g,inp.outputs[f'Grade {i}']),mix.inputs[0]);t.links.new(image,mix.inputs[6]);t.links.new(balance.outputs[0],mix.inputs[7]);image=mix.outputs[2]
    exposure=t.nodes.new('CompositorNodeExposure');exposure.name='Scene exposure';t.links.new(image,exposure.inputs['Image'])
    # Native color management handles ChromaLuma after all scene-linear effects.
    t.links.new(exposure.outputs[0],out.inputs[0])
    background_grade(t)
    t['mc_glow_version']=GLOW_VERSION
    return t


def _share(g,mask):
    """A mask as a share of the pixel: 0 to 1. Summed AOVs can exceed 1."""
    return g.op('MINIMUM',1,g.op('MAXIMUM',mask,0))


def light_glow(t,g,inp):
    """Add light glow and sky glow to the image; returns the glowing image."""
    def value(name,default):
        node=t.nodes.new('ShaderNodeValue');node.name=node.label=name
        node.outputs[0].default_value=default
        return node.outputs[0]
    def vector(operation,a,b=None,scale=None):
        node=t.nodes.new('ShaderNodeVectorMath');node.operation=operation
        for socket_,source in ((node.inputs[0],a),(node.inputs[1],b),(node.inputs[3],scale)):
            if source is None:continue
            if isinstance(source,bpy.types.NodeSocket):t.links.new(source,socket_)
            else:socket_.default_value=source
        return node.outputs['Value'] if operation=='DOT_PRODUCT' else node.outputs['Vector']
    strength=value('Glow strength',.6)
    size=value('Light glow scale',1.)
    wide=value('Wide light glow',1.)
    warmth=value('Warm light glow',.5)
    sky=value('Sky glow strength',.1)
    def layers(source,sizes,prefix):
        """Gaussian blurs of the source; sizes follow the image height and the size control."""
        for k,(fraction,share) in enumerate(sizes):
            relative=vector('SCALE',(fraction,fraction,0),scale=size)
            pixels=t.nodes.new('CompositorNodeRelativeToPixel');pixels.data_type='VECTOR';pixels.reference_dimension='Y'
            t.links.new(relative,next(s for s in pixels.inputs if s.identifier=='Vector Value'))
            t.links.new(source,pixels.inputs['Image'])
            blur=t.nodes.new('CompositorNodeBlur');blur.name=f'{prefix} {k+1}'
            blur.inputs['Type'].default_value='Fast Gaussian'
            t.links.new(source,blur.inputs['Image'])
            t.links.new(next(s for s in pixels.outputs if s.identifier=='Vector Value'),blur.inputs['Size'])
            yield k,share,blur.outputs['Image']
    halo=None
    for k,share,layer in layers(inp.outputs['Emission'],GLOW_LAYERS,'Light glow'):
        weight=g.op('MULTIPLY',share,wide) if k>=2 else share
        tint=t.nodes.new('ShaderNodeMix');tint.data_type='RGBA';tint.clamp_result=False
        tint.name=f'Light glow {k+1} warmth'
        tint.inputs[6].default_value=(1,1,1,1);tint.inputs[7].default_value=(*WARM_GLOW,1)
        g.put(tint.inputs[0],g.op('MULTIPLY',warmth,k/(len(GLOW_LAYERS)-1)))
        layer=vector('MULTIPLY',vector('SCALE',layer,scale=weight),tint.outputs[2])
        halo=layer if halo is None else vector('ADD',halo,layer)
    # The source itself is not brightened; only its surroundings glow.
    halo=vector('SCALE',halo,scale=g.op('MULTIPLY',strength,g.op('SUBTRACT',1,_share(g,inp.outputs['Emission mask']))))
    sky_halo=None
    for k,share,layer in layers(inp.outputs['Environment'],SKY_GLOW_LAYERS,'Sky glow'):
        layer=vector('SCALE',layer,scale=share)
        sky_halo=layer if sky_halo is None else vector('ADD',sky_halo,layer)
    # Where the image is the sky itself, the environment matches it; keep the sky as it is.
    sky_share=g.op('DIVIDE',vector('DOT_PRODUCT',inp.outputs['Environment'],LUMINANCE),
                   g.op('MAXIMUM',vector('DOT_PRODUCT',inp.outputs['Image'],LUMINANCE),1e-4))
    terrain=g.op('SUBTRACT',1,g.op('MINIMUM',1,sky_share))
    sky_halo=vector('SCALE',sky_halo,scale=g.op('MULTIPLY',sky,terrain))
    glow=g.split(vector('ADD',halo,sky_halo))
    alpha=t.nodes.new('CompositorNodeSeparateColor');alpha.mode='RGB';t.links.new(inp.outputs['Image'],alpha.inputs[0])
    image=g.split(inp.outputs['Image'])
    return g.combine([g.op('ADD',c,h) for c,h in zip(image,glow)],alpha.outputs[3])


def background_grade(t):
    if t.nodes.get('Grade 0 exposure'):return
    g=Graph(t)
    inp=next(n for n in t.nodes if n.type=='GROUP_INPUT')
    first=t.nodes['Grade 1 exposure'].inputs['Image']
    source=first.links[0].from_socket
    mask=g.op('SUBTRACT',1,g.op('MINIMUM',1,g.op('ADD',g.op('ADD',inp.outputs['Grade 1'],inp.outputs['Grade 2']),g.op('ADD',inp.outputs['Grade 3'],inp.outputs['Grade 4']))))
    exposure=t.nodes.new('CompositorNodeExposure');exposure.name='Grade 0 exposure';t.links.new(source,exposure.inputs['Image'])
    saturation=t.nodes.new('CompositorNodeHueSat');saturation.name='Grade 0 saturation';t.links.new(exposure.outputs[0],saturation.inputs['Image'])
    contrast=t.nodes.new('CompositorNodeBrightContrast');contrast.name='Grade 0 contrast';t.links.new(saturation.outputs[0],contrast.inputs['Image'])
    balance=t.nodes.new('CompositorNodeColorBalance');balance.name='Grade 0 balance';t.links.new(contrast.outputs[0],balance.inputs['Image'])
    mix=t.nodes.new('ShaderNodeMix');mix.data_type='RGBA';mix.name='Grade 0 mask';mix.clamp_result=False
    t.links.new(mask,mix.inputs[0]);t.links.new(source,mix.inputs[6]);t.links.new(balance.outputs[0],mix.inputs[7])
    t.links.new(mix.outputs[2],first);t.links.new(mix.outputs[2],t.nodes['Grade 1 mask'].inputs[6])


def setup(scene):
    s=scene.mcbedrock_sky
    if not s.compositor:return
    if s.loocs_enabled or atmosphere.enabled(scene):compensation(scene)
    ensure_passes(scene)
    tree=s.compositor
    node=tree.nodes.get('MC LOOCS Lighting')
    if node is None:
        source=tree.nodes.get('MC Color balance')
        if source is None:return
        targets=[l.to_socket for l in source.outputs[0].links]
        node=tree.nodes.new('CompositorNodeGroup');node.name='MC LOOCS Lighting';node[TAG]=s.owner;node.node_tree=pipeline_group(scene)
        tree.links.new(source.outputs[0],node.inputs['Image'])
        for target in targets:tree.links.new(node.outputs[0],target)
    native_display_output(node.node_tree)
    render=tree.nodes.get('MC Lighting passes')
    if render is None:
        render=tree.nodes.new('CompositorNodeRLayers');render.name='MC Lighting passes';render[TAG]=s.owner;render.scene=scene
        source_layer=next((n for n in tree.nodes if n!=render and n.type=='R_LAYERS' and (n.scene is None or n.scene==scene)),None)
        if source_layer:render.layer=source_layer.layer
    if atmosphere.enabled(scene):
        atmosphere.setup(scene,tree,render,node.inputs['Image'])
        atmosphere.retire_legacy_passes(scene)
    else:
        setup_legacy_fog(scene,tree,render,node)
    flat_fog.setup(scene,tree,render,node.inputs['Image'])
    film_look.setup(scene,tree,render,node.inputs['Image'])
    for suffix in ['Emission','Emission mask']+[f'Grade {i}' for i in range(1,5)]:
        port=render.outputs.get(pass_name(scene,suffix))
        if port and (not node.inputs[suffix].links or node.inputs[suffix].links[0].from_socket!=port):tree.links.new(port,node.inputs[suffix])
    environment_pass(scene)
    edge_details.setup(scene, tree, render, node.outputs[0])
    update(scene,force=True)


def setup_legacy_fog(scene,tree,render,node):
    s=scene.mcbedrock_sky
    fog=tree.nodes.get('MC Terrain fog')
    if fog is None:
        fog=tree.nodes.new('ShaderNodeMix');fog.data_type='RGBA';fog.name='MC Terrain fog';fog[TAG]=s.owner;fog.clamp_result=False
        source=node.inputs['Image'].links[0].from_socket
        tree.links.new(source,fog.inputs[6]);tree.links.new(fog.outputs[2],node.inputs['Image'])
    tree.links.new(render.outputs[pass_name(scene,'Terrain fog')],fog.inputs[0])
    haze=tree.nodes.get('MC Sun on terrain')
    if haze is None:
        haze=tree.nodes.new('ShaderNodeMix');haze.data_type='RGBA';haze.name='MC Sun on terrain';haze[TAG]=s.owner;haze.clamp_result=False
        source=node.inputs['Image'].links[0].from_socket
        tree.links.new(source,haze.inputs[6]);tree.links.new(haze.outputs[2],node.inputs['Image'])
    tree.links.new(render.outputs[pass_name(scene,'Sun haze')],haze.inputs[0])
    native_mist.setup(scene,tree,render)


ENVIRONMENT='mc_sky_environment_pass'


def environment_pass(scene):
    """Sky glow reads Blender's environment pass, enabled only while it is used."""
    s=scene.mcbedrock_sky
    wanted=s.sky_glow>0
    for layer in scene.view_layers:
        if wanted and not layer.use_pass_environment:
            layer.use_pass_environment=True;layer[ENVIRONMENT]=s.owner
        elif not wanted and layer.get(ENVIRONMENT):
            layer.use_pass_environment=False;del layer[ENVIRONMENT]
    tree=s.compositor
    render=tree.nodes.get('MC Lighting passes') if tree else None
    node=tree.nodes.get('MC LOOCS Lighting') if tree else None
    port=render.outputs.get('Environment') if render else None
    target=node.inputs.get('Environment') if node else None
    if port and target and (not target.links or target.links[0].from_socket!=port):
        tree.links.new(port,target)


def update_glow(scene,t):
    s=scene.mcbedrock_sky
    set_value(t.nodes['Glow strength'].outputs[0],'default_value',s.emissive_glow)
    set_value(t.nodes['Light glow scale'].outputs[0],'default_value',.5*4**s.light_glow_size)
    set_value(t.nodes['Wide light glow'].outputs[0],'default_value',2*s.light_glow_wide)
    set_value(t.nodes['Warm light glow'].outputs[0],'default_value',s.light_glow_warmth)
    set_value(t.nodes['Sky glow strength'].outputs[0],'default_value',s.sky_glow)
    # A muted blur passes its image through; skip the work while a glow is off.
    for node in t.nodes:
        if node.type=='BLUR':
            off=s.sky_glow<=0 if node.name.startswith('Sky glow') else s.emissive_glow<=0
            set_value(node,'mute',off)


def native_display_output(tree):
    """Send scene-linear effects to Blender's native view transform exactly once."""
    out=next(n for n in tree.nodes if n.type=='GROUP_OUTPUT')
    source=tree.nodes['Scene exposure'].outputs[0]
    if not out.inputs[0].links or out.inputs[0].links[0].from_socket!=source:
        tree.links.new(source,out.inputs[0])
    set_value(tree.nodes['Scene exposure'].inputs['Exposure'],'default_value',0)



def update(scene,force=False):
    s=scene.mcbedrock_sky
    edge_details.update(scene)
    native_color.sync(scene)
    required = ('MC Atmosphere',) if atmosphere.enabled(scene) else ('MC Terrain fog','MC Sun on terrain','MC Native mist strength')
    required += (*flat_fog.REQUIRED,film_look.NODE+' halation')
    if s.compositor and s.compositor.nodes.get('MC Color balance') and any(not s.compositor.nodes.get(name) for name in required):
        setup(scene)
        return
    update_sun_haze(scene)
    flat_fog.update(scene)
    film_look.update(scene)
    # Time/sky changes do not change material LUTs, fog or grading. Writing the
    # same sockets still invalidates every material shader in EEVEE.
    fields=('loocs_enabled','texture_compensation','blue_noise_opacity',
            'terrain_fog','fog','fog_start','fog_distance','exposure','emissive_glow',
            'light_glow_size','light_glow_wide','light_glow_warmth','sky_glow',
            'sun_haze_mode','sun_world_haze','sun_world_haze_start','sun_world_haze_distance','sun_world_haze_spread','sun_world_haze_radiance','sun_world_haze_height','sun_world_haze_height_falloff')
    material_signature=(s.owner,len(s.materials),s.atmosphere_version,s.loocs_enabled,
                        s.texture_compensation,s.blue_noise_opacity,
                        native_color.working_space(),native_color.active(scene),
                        native_color.correction(scene),s.loocs_auto_textures)
    signature=(material_signature,s.compositor.as_pointer() if s.compositor else 0,
               tuple(getattr(s,name) for name in fields),tuple(s.fog_color),tuple(s.sun_world_haze_color),
               (scene.view_settings.exposure,scene.view_settings.gamma,
                scene.view_settings.view_transform,scene.view_settings.look,
                scene.view_settings.use_curve_mapping,scene.view_settings.use_white_balance,
                scene.display_settings.display_device),
               tuple(getattr(s,f'grade_{i}_{name}') for i in range(5)
                     for name in ('exposure','saturation','contrast','temperature','tint')))
    key=scene.as_pointer()
    previous=_update_signatures.get(key)
    if not force and previous==signature:return
    # Material structure changes on preparation/mode changes, not fog or grade
    # drags. prepare() invalidates this signature even for same-size slot edits.
    material_update=force or not previous or previous[0]!=material_signature
    prepared={e.prepared for e in s.materials if e.prepared} if material_update else ()
    if material_update and (s.loocs_enabled or atmosphere.enabled(scene)):compensation(scene)
    if s.compositor:
        fog=s.compositor.nodes.get('MC Terrain fog')
        if fog:
            set_value(fog.inputs[7],'default_value',(*s.fog_color,1))
            set_value(fog,'mute',False)
        haze=s.compositor.nodes.get('MC Sun on terrain')
        if haze:set_value(haze.inputs[7],'default_value',(*(c*s.sun_world_haze_radiance for c in s.sun_world_haze_color),1))
    for material in prepared:
        fog_material(scene,material)
    if not atmosphere.enabled(scene):
        native_mist.update(scene)
        for t in bpy.data.node_groups:
            if t.get(TAG)==s.owner and t.nodes.get('Fog strength'):
                set_value(t.nodes['Fog strength'].outputs[0],'default_value',s.fog if s.terrain_fog else 0)
                set_value(t.nodes['Fog start'].outputs[0],'default_value',s.fog_start)
                set_value(t.nodes['Fog distance'].outputs[0],'default_value',s.fog_distance)
    n=s.compositor.nodes.get('MC LOOCS Lighting') if s.compositor else None
    if n:
        set_value(n,'mute',not s.loocs_enabled and not atmosphere.enabled(scene))
        if n.node_tree is None:
            n.node_tree=pipeline_group(scene)
        t=n.node_tree
        # A renamed or deleted node rebuilds the group in place (links into it
        # are kept) instead of failing every update after it.
        if t.get('mc_glow_version')!=GLOW_VERSION or not pipeline_intact(t):
            build_pipeline(t)
        native_display_output(t)
        background_grade(t)
        update_glow(scene,t)
        environment_pass(scene)
        set_value(t.nodes['Scene exposure'].inputs['Exposure'],'default_value',0)
        for i in range(5):
            set_value(t.nodes[f'Grade {i} exposure'].inputs['Exposure'],'default_value',getattr(s,f'grade_{i}_exposure'))
            set_value(t.nodes[f'Grade {i} saturation'].inputs['Saturation'],'default_value',getattr(s,f'grade_{i}_saturation'))
            set_value(t.nodes[f'Grade {i} contrast'].inputs['Contrast'],'default_value',getattr(s,f'grade_{i}_contrast'))
            temperature=getattr(s,f'grade_{i}_temperature');tint=getattr(s,f'grade_{i}_tint')
            gain=(1+.2*temperature+.08*tint,1-.12*tint,1-.2*temperature+.08*tint)
            balance=t.nodes[f'Grade {i} balance']
            if hasattr(balance,'gain'):set_value(balance,'gain',gain)
            else:set_value(next(p for p in balance.inputs if p.name=='Gain' and p.type=='RGBA'),'default_value',(*gain,1))
        film=s.compositor.nodes.get('MC Film response')
        if film is not None:
            set_value(film.inputs['Fac'],'default_value',0)
    for material in prepared:
        for node in material.node_tree.nodes:
            if node.name.startswith('MC compensate '):set_value(node.inputs['Amount'],'default_value',native_color.compensation_amount(scene))
            if node.name=='MC opacity':set_value(node.inputs[0],'default_value',float(s.blue_noise_opacity))
    _update_signatures[key]=signature


def restore(scene):
    s=scene.mcbedrock_sky
    native_mist.restore(scene)
    flat_fog.restore(scene)
    film_look.restore(scene)
    for key,value in json.loads(s.previous_display_settings or '{}').items():
        target=scene.display_settings if key=='display_device' else scene.view_settings
        setattr(target,key,value)
    s.previous_display_settings=''
    _signatures.pop(scene.as_pointer(),None)
    _update_signatures.pop(scene.as_pointer(),None)
    _material_counts.pop(scene.as_pointer(),None)
    _material_dirty.discard(scene.as_pointer())
    prefix='MC '+scene.mcbedrock_sky.owner[:8]+' '
    for layer in scene.view_layers:
        for aov in list(layer.aovs):
            if aov.name.startswith(prefix):layer.aovs.remove(aov)
        if layer.get(ENVIRONMENT):
            layer.use_pass_environment=False;del layer[ENVIRONMENT]


_reported=set()


def _report_once(what):
    """Print a background error once. A timer that raises is unregistered for
    the rest of the session, so the caller keeps running instead."""
    import traceback
    message=traceback.format_exc()
    if message not in _reported:
        _reported.add(message)
        print(f'Sparkle: {what} hit an error and keeps running:\n{message}')


def sync_materials():
    """Main-thread timer: update newly imported/assigned materials without importer hooks."""
    try:
        _sync_materials()
    except Exception:
        _report_once('Sky material sync')
    return 1.0


def _sync_materials():
    from . import sky, sky_materials
    live=set()
    for scene in bpy.data.scenes:
        key=scene.as_pointer();live.add(key)
        if not hasattr(scene,'mcbedrock_sky'):continue
        s=scene.mcbedrock_sky
        if s.active and key not in sky.BUSY:
            try:
                update(scene)
            except Exception:
                # One scene's failure must not stop the others' materials.
                _report_once('Sky lighting update')
        if not s.active or not s.auto_materials or (not s.loocs_enabled and not atmosphere.enabled(scene)) or key in sky.BUSY:continue
        counts=(len(bpy.data.objects),len(bpy.data.materials))
        if key in _signatures and key not in _material_dirty and _material_counts.get(key)==counts:
            continue
        _material_dirty.discard(key)
        _material_counts[key]=counts
        signature=tuple((obj.as_pointer(),tuple(slot.material.as_pointer() if slot.material else 0 for slot in obj.material_slots)) for obj in sky_materials.scene_meshes(scene))
        if _signatures.get(key)==signature:continue
        sky.BUSY.add(key)
        try:
            sky_materials.prepare(scene);setup(scene)
            _signatures[key]=tuple((obj.as_pointer(),tuple(slot.material.as_pointer() if slot.material else 0 for slot in obj.material_slots)) for obj in sky_materials.scene_meshes(scene))
        except Exception as exc:
            s.finish_status='Lighting setup needs attention: '+str(exc)
            _signatures[key]=signature
        finally:sky.BUSY.discard(key)
    for key in set(_signatures)-live:
        _signatures.pop(key,None)
        _material_counts.pop(key,None)
        _material_dirty.discard(key)

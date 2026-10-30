"""Reversible surface finish after imported textures, biome tints and animation."""
import bpy
import re
from . import lighting_pipeline, atmosphere, edge_details, outlines, light_blocks

TAG = 'mc_sky_owner'
FINISH = 'mc_sky_surface_finish'


def vegetation_response(material, scene):
    """Add lighting response on owned copies, after colour compensation.

    Do not infer thin vegetation from 'grass' alone: grass blocks and moss are
    opaque terrain. Imported mapped/animated scattering takes precedence.
    """
    tree=material.node_tree
    name=(material.name+' '+' '.join(n.image.name for n in tree.nodes if n.type=='TEX_IMAGE' and n.image)).lower()
    leaf=bool(re.search(r'(?:^|[^a-z])(?:leaves|leaf)(?:[^a-z]|$)',name)) and 'litter' not in name
    thin=any(word in name for word in ('short_grass','tall_grass','fern','flower','dandelion','poppy','tulip','orchid','allium','oxeye','cornflower','lily_of_the_valley','vine'))
    if not (leaf or thin) or tree.animation_data:
        return
    s=scene.mcbedrock_sky
    amount=s.look_strength if s.trailer_lighting else 0
    for shader in list(tree.nodes):
        if shader.type!='BSDF_PRINCIPLED':continue
        if any(shader.inputs[k].is_linked or shader.inputs[k].default_value>0 for k in ('Metallic','Transmission Weight')):continue
        if leaf:
            keys=('Subsurface Weight','Subsurface Radius','Subsurface Scale')
            if any(shader.inputs[k].is_linked for k in keys):continue
            if 'mc_leaf_original_weight' not in shader:
                if shader.inputs['Subsurface Weight'].default_value>0:continue
                shader['mc_leaf_original_weight']=0.
                shader['mc_leaf_original_radius']=tuple(shader.inputs['Subsurface Radius'].default_value)
                shader['mc_leaf_original_scale']=shader.inputs['Subsurface Scale'].default_value
                shader['mc_leaf_original_method']=shader.subsurface_method
            scattering=s.leaf_scattering*amount
            shader.inputs['Subsurface Weight'].default_value=scattering
            # MiEx surfaces may be disconnected/open. Random Walk assumes a
            # closed volume and exposes internal block seams on these meshes.
            shader.subsurface_method='BURLEY' if scattering else shader.get('mc_leaf_original_method','RANDOM_WALK')
            # Neutral channel distances avoid introducing a new green/yellow tint.
            shader.inputs['Subsurface Radius'].default_value=(1,1,1) if scattering else shader['mc_leaf_original_radius']
            shader.inputs['Subsurface Scale'].default_value=.18 if scattering else shader['mc_leaf_original_scale']
        elif thin:
            _translucency(tree,s.vegetation_translucency*amount)
    if thin and 'vine' not in name:
        _two_sided(material)


TWO_SIDED='MC Vegetation two-sided'


def _two_sided(material):
    """Grass, ferns and flowers are crossed cards. Lit as opaque cards, every
    card turned away from the sun went near black whenever the camera looked
    toward the light, and a sunny meadow read as dark blades on bright ground.
    Like trailer grass they pass light: the card's own colour comes through
    from the lit side (an added Translucent BSDF, black where the texture is
    cut out), and with no thickness EEVEE lets all of it through. Both sides
    of a blade are then about as bright as the ground. (A shading normal
    pointing up did not work: Cycles never lights a card's back from behind,
    and EEVEE only did with no self-shadow.) Plants cast no shadows
    (PLANT_SHADOW), so nothing blocks the light on its way through."""
    tree=material.node_tree
    for shader in [n for n in tree.nodes if n.type=='BSDF_PRINCIPLED']:
        if shader.get('mc_two_sided'):
            continue
        targets=[link.to_socket for link in shader.outputs['BSDF'].links]
        if not targets:
            continue
        colour=tree.nodes.new('ShaderNodeVectorMath')
        colour.operation='SCALE'
        colour.name=colour.label=TWO_SIDED+' colour'
        base,alpha=shader.inputs['Base Color'],shader.inputs['Alpha']
        if base.links:tree.links.new(base.links[0].from_socket,colour.inputs[0])
        else:colour.inputs[0].default_value=tuple(base.default_value[:3])
        if alpha.links:tree.links.new(alpha.links[0].from_socket,colour.inputs['Scale'])
        else:colour.inputs['Scale'].default_value=alpha.default_value
        light=tree.nodes.new('ShaderNodeBsdfTranslucent')
        light.name=light.label=TWO_SIDED+' light'
        tree.links.new(colour.outputs[0],light.inputs['Color'])
        both=tree.nodes.new('ShaderNodeAddShader')
        both.name=both.label=TWO_SIDED
        tree.links.new(shader.outputs['BSDF'],both.inputs[0])
        tree.links.new(light.outputs[0],both.inputs[1])
        for target in targets:
            tree.links.new(both.outputs[0],target)
        for node,dx,dy in ((colour,-200,-520),(light,0,-520),(both,220,-300)):
            node.location=(shader.location.x+dx,shader.location.y+dy)
        shader['mc_two_sided']=True
    if hasattr(material,'thickness_mode'):
        material.thickness_mode='SLAB'
    for output in [n for n in tree.nodes if n.type=='OUTPUT_MATERIAL']:
        port=output.inputs.get('Thickness')
        if port is None or port.is_linked:
            continue
        zero=tree.nodes.new('ShaderNodeValue')
        zero.name=zero.label=TWO_SIDED+' thickness'
        zero.outputs[0].default_value=0.
        zero.location=(output.location.x-200,output.location.y-200)
        tree.links.new(zero.outputs[0],port)


def _translucency(tree,weight):
    """Let light through thin vegetation: a translucent share of the
    surface, in its own colour, cut out where the texture is empty. Not on
    leaves: sun leaking through the holes of stacked leaf textures streaked
    the undersides of canopies."""
    key='MC Vegetation'
    for shader in [n for n in tree.nodes if n.type=='BSDF_PRINCIPLED']:
        if any(shader.inputs[k].is_linked or shader.inputs[k].default_value>0 for k in ('Metallic','Transmission Weight')):continue
        mix=tree.nodes.get(shader.get('mc_vegetation_mix',''))
        if mix is None:
            targets=[link.to_socket for link in shader.outputs['BSDF'].links]
            if not targets:continue
            translucent=tree.nodes.new('ShaderNodeBsdfTranslucent')
            translucent.name=key+' light transmission'
            color=shader.inputs['Base Color']
            if color.links:tree.links.new(color.links[0].from_socket,translucent.inputs['Color'])
            else:translucent.inputs['Color'].default_value=color.default_value
            transparent=tree.nodes.new('ShaderNodeBsdfTransparent')
            cutout=tree.nodes.new('ShaderNodeMixShader');cutout.name=key+' original alpha'
            alpha=shader.inputs['Alpha']
            if alpha.links:tree.links.new(alpha.links[0].from_socket,cutout.inputs[0])
            else:cutout.inputs[0].default_value=alpha.default_value
            tree.links.new(transparent.outputs[0],cutout.inputs[1]);tree.links.new(translucent.outputs[0],cutout.inputs[2])
            mix=tree.nodes.new('ShaderNodeMixShader');mix.name=key
            shader['mc_vegetation_mix']=mix.name
            tree.links.new(shader.outputs['BSDF'],mix.inputs[1]);tree.links.new(cutout.outputs[0],mix.inputs[2])
            for target in targets:tree.links.new(mix.outputs[0],target)
        # Emissive plants retain their original emission response.
        emissive=shader.inputs['Emission Strength']
        emission_color=shader.inputs['Emission Color']
        has_emission=(emissive.is_linked or emissive.default_value>0) and (emission_color.is_linked or max(emission_color.default_value[:3])>0)
        lighting_pipeline.set_value(mix.inputs[0],'default_value',0. if has_emission else float(weight))


def scene_meshes(scene):
    pending = list(scene.objects)
    seen = set()
    while pending:
        obj = pending.pop()
        if obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        if obj.instance_collection:
            pending.extend(obj.instance_collection.all_objects)
        # Sparkle's own unlit helpers (fog planes) keep their material.
        if obj.type == 'MESH' and not obj.get(TAG) and not obj.get('mc_fog_planes'):
            yield obj


def finish_group(scene, foliage, grass=False):
    s = scene.mcbedrock_sky
    kind='grass' if grass else ('foliage' if foliage else 'surface')
    for group in bpy.data.node_groups:
        if group.get(TAG) == s.owner and group.get(FINISH) == kind:
            return group
    group = bpy.data.node_groups.new('MC Surface Finish · '+('Foliage' if foliage else 'Color'), 'ShaderNodeTree')
    group[TAG], group[FINISH] = s.owner, kind
    group.interface.new_socket(name='Color',in_out='INPUT',socket_type='NodeSocketColor')
    group.interface.new_socket(name='Color',in_out='OUTPUT',socket_type='NodeSocketColor')
    nodes, links = group.nodes, group.links
    source = nodes.new('NodeGroupInput')
    output = nodes.new('NodeGroupOutput')
    color = nodes.new('ShaderNodeHueSaturation')
    color.name = 'Texture richness'
    source.location=(-600,100)
    color.location=(-350,100)
    output.location=(250,100)
    links.new(source.outputs['Color'],color.inputs['Color'])
    result = color.outputs[0]
    if foliage:
        hsv=nodes.new('ShaderNodeSeparateColor')
        hsv.mode='HSV'
        links.new(source.outputs['Color'],hsv.inputs[0])
        # Only green/yellow-green pixels: autumn leaves and dirt side pixels stay intact.
        low=nodes.new('ShaderNodeMath'); low.operation='GREATER_THAN'; low.inputs[1].default_value=.13
        high=nodes.new('ShaderNodeMath'); high.operation='LESS_THAN'; high.inputs[1].default_value=.43
        saturation=nodes.new('ShaderNodeMath'); saturation.operation='GREATER_THAN'; saturation.inputs[1].default_value=.08
        for gate, socket in ((low,hsv.outputs[0]),(high,hsv.outputs[0]),(saturation,hsv.outputs[1])):
            links.new(socket,gate.inputs[0])
        mask=nodes.new('ShaderNodeMath'); mask.operation='MULTIPLY'
        links.new(low.outputs[0],mask.inputs[0]); links.new(high.outputs[0],mask.inputs[1])
        coverage=nodes.new('ShaderNodeMath'); coverage.operation='MULTIPLY'
        links.new(mask.outputs[0],coverage.inputs[0]); links.new(saturation.outputs[0],coverage.inputs[1])
        green=nodes.new('ShaderNodeHueSaturation'); green.name='Foliage color'
        green.location=(-50,100)
        links.new(result,green.inputs['Color'])
        links.new(coverage.outputs[0],green.inputs['Fac'])
        result=green.outputs[0]
        if grass:
            richness=nodes.new('ShaderNodeHueSaturation');richness.name='Grass richness'
            links.new(result,richness.inputs['Color']);links.new(coverage.outputs[0],richness.inputs['Fac'])
            result=richness.outputs[0]
    tint=nodes.new('ShaderNodeMixRGB')
    tint.name='Surface tint'
    tint.blend_type='MULTIPLY'
    tint.inputs[0].default_value=1
    tint.inputs[2].default_value=(1,1,1,1)
    tint.location=(200,100)
    output.location=(440,100)
    links.new(result,tint.inputs[1])
    links.new(tint.outputs[0],output.inputs['Color'])
    return group


# How dark grass, ferns and flowers shade the ground: not at all. Solid, every
# blade cast a black shadow and a sunny meadow turned to speckle; at .35 EEVEE
# drew the part-shadow as frozen dither, grainy smudges on the grass tops.
PLANT_SHADOW = 0.
# Leaves keep solid shadows in both engines. See-through leaf shadows stacked
# up through a canopy (one leaf face, two, three) into V-shaped wedges on
# trunks and characters. Shade under trees is lit by bounced light instead
# (Cycles' real bounce, EEVEE's ground bounce: ground_bounce).
PLANT_MARK = 'mc_plant_shadow'


def _names(material):
    return (material.name + ' ' + ' '.join(n.image.name for n in material.node_tree.nodes
                                           if n.type == 'TEX_IMAGE' and n.image)).lower() if material.node_tree else material.name.lower()


def is_plant(material):
    from .trailer_materials import PLANTS, LEAVES
    name = _names(material)
    return bool(PLANTS.search(name)) and not LEAVES.search(name)


def shadow_share(material):
    """How opaque a material is to shadow rays: thin plants let light
    through, everything else (None) keeps its full shadow."""
    return PLANT_SHADOW if is_plant(material) else None


def soften_plant_shadow(material, share=PLANT_SHADOW):
    """Grass, ferns and flowers are thin, translucent cards: as opaque planes
    every blade cast a black shadow and a sunny meadow turned to speckle.
    Shadow rays see them `share` opaque (PLANT_SHADOW: clear); the camera sees
    them unchanged."""
    tree = material.node_tree
    for output in [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL']:
        port = output.inputs['Surface']
        if not port.links:
            continue
        source = port.links[0].from_socket
        rays = tree.nodes.new('ShaderNodeLightPath')
        rays.name = rays.label = 'Plant shadow rays'
        clear = tree.nodes.new('ShaderNodeBsdfTransparent')
        clear.name = clear.label = 'Plant shadow light'
        amount = tree.nodes.new('ShaderNodeMath')
        amount.name = amount.label = 'Plant shadow see-through'
        amount.operation = 'MULTIPLY'
        tree.links.new(rays.outputs['Is Shadow Ray'], amount.inputs[0])
        amount.inputs[1].default_value = 1 - share
        mix = tree.nodes.new('ShaderNodeMixShader')
        mix.name = mix.label = 'Plant shadow'
        tree.links.new(amount.outputs[0], mix.inputs[0])
        tree.links.new(source, mix.inputs[1])
        tree.links.new(clear.outputs[0], mix.inputs[2])
        tree.links.new(mix.outputs[0], port)
        for node, dx, dy in ((rays, -420, -260), (amount, -220, -260), (clear, -220, -420), (mix, -160, 0)):
            node.location = (output.location.x + dx, output.location.y + dy)
    if hasattr(material, 'use_transparent_shadow'):
        material.use_transparent_shadow = True
    material[PLANT_MARK] = share


def prepare(scene):
    from .water_foam import prepare_atmosphere as prepare_foam_atmosphere
    from .water_caustics import prepare_atmosphere as prepare_caustics_atmosphere
    prepare_foam_atmosphere(scene)
    prepare_caustics_atmosphere(scene)
    # Material replacements can leave the number of slots unchanged.
    lighting_pipeline._update_signatures.pop(scene.as_pointer(), None)
    s=scene.mcbedrock_sky
    skipped=0
    shared={obj for other in bpy.data.scenes if other!=scene for obj in scene_meshes(other)}
    discarded=set()
    # Saved finishes and meshes which gained/lost corner UVs need the matching
    # variant. Rebuild from their original instead of mutating a shared copy.
    for index in range(len(s.materials)-1,-1,-1):
        entry=s.materials[index]
        obj=entry.object
        if (not obj or obj.type!='MESH' or obj.library or obj.override_library or obj in shared or
                not entry.original or not entry.prepared or entry.index>=len(obj.material_slots)):
            continue
        corners=False
        slot=obj.material_slots[entry.index]
        if slot.material==entry.prepared and (corners!=bool(entry.prepared.get(edge_details.CORNER))
                                              or light_blocks.needs_rebuild(entry.original,entry.prepared)
                                              or entry.prepared.get(PLANT_MARK)!=shadow_share(entry.original)):
            discarded.add(entry.prepared)
            slot.material,slot.link=entry.original,entry.link
            s.materials.remove(index)
    cache={(entry.original,bool(entry.prepared.get(edge_details.CORNER))):entry.prepared
           for entry in s.materials if entry.original and entry.prepared}
    fresh=set()
    for obj in scene_meshes(scene):
        # Object overrides preserve mesh sharing; linked/shared objects cannot be changed safely.
        if obj.library or obj.override_library or obj in shared:
            skipped+=1
            continue
        for index,slot in enumerate(obj.material_slots):
            original=slot.material
            if not original or original.get(TAG)==s.owner:
                continue
            if original.get('mcaddon_water_preset'):
                from .water import prepare_atmosphere
                prepare_atmosphere(scene, original)
                continue  # Water presets own their color, transmission and animated normals.
            if original.get(TAG) or not original.use_nodes:
                skipped+=1
                continue
            shaders=[n for n in original.node_tree.nodes if n.type=='BSDF_PRINCIPLED']
            # Directly animated color defaults cannot be replaced by a fixed input.
            emission_shaders=[n for n in original.node_tree.nodes if n.type=='EMISSION']
            if (not shaders and not emission_shaders and not atmosphere.enabled(scene)) or (original.node_tree.animation_data and any(not n.inputs[key].is_linked for n in shaders+emission_shaders for key in (('Base Color','Emission Color','Alpha') if n.type=='BSDF_PRINCIPLED' else ('Color',)))):
                skipped+=1
                continue
            corners=False
            cache_key=(original,corners)
            prepared=cache.get(cache_key)
            if prepared is None:
                prepared=original.copy()
                prepared.name='MC Finish · '+original.name
                prepared[TAG]=s.owner
                prepared[FINISH]=True
                name=(original.name+' '+' '.join(n.image.name for n in original.node_tree.nodes if n.type=='TEX_IMAGE' and n.image)).lower()
                foliage=any(key in name for key in ('grass','leaves','leaf','fern','vine','moss'))
                group=finish_group(scene,foliage,grass='grass' in name)
                for shader in (n for n in prepared.node_tree.nodes if n.type=='BSDF_PRINCIPLED'):
                    port=shader.inputs['Base Color']
                    layer=prepared.node_tree.nodes.new('ShaderNodeGroup')
                    layer.node_tree=group
                    layer.name='MC Surface Finish'
                    layer.label='Texture color · original input preserved'
                    layer.location=(shader.location.x-220,shader.location.y+120)
                    if port.links:
                        prepared.node_tree.links.new(port.links[0].from_socket,layer.inputs['Color'])
                    else:
                        layer.inputs['Color'].default_value=port.default_value
                    prepared.node_tree.links.new(layer.outputs['Color'],port)
                    # Never override mapped roughness/metal/transmission or emissive inputs.
                    for key in ('Roughness','Specular IOR Level'):
                        shader['mc_original_'+key]=shader.inputs[key].default_value
                share=shadow_share(original)
                if share is not None:
                    soften_plant_shadow(prepared,share)
                if corners:
                    edge_details.prepare_corners(scene,prepared)
                # Before the lighting passes, which read the emission.
                light_blocks.prepare_material(scene,prepared,original)
                lighting_pipeline.prepare_material(scene,prepared)
                cache[cache_key]=prepared
                fresh.add(prepared)
            entry=s.materials.add()
            entry.object=obj
            entry.index=index
            entry.original=original
            entry.prepared=prepared
            entry.link=slot.link
            slot.link='OBJECT'
            slot.material=prepared
    for material in discarded:
        if not material.users:
            bpy.data.materials.remove(material)
    s.finish_active=bool(s.materials)
    s.finish_status=f'{len(cache)} materials prepared'
    s.finish_skipped=skipped
    # Upgrade saved copies; the ones made above were prepared a moment ago.
    for material in {entry.prepared for entry in s.materials if entry.prepared}-fresh:
        lighting_pipeline.prepare_material(scene,material)
    # Last, so the lines wrap the finished, compensated surface color.
    outlines.prepare(scene)
    light_blocks.place_lights(scene)
    unsupported=sum(bool(m.get('mc_lighting_unsupported')) for m in cache.values())
    if unsupported:s.finish_status+=f'; {unsupported} complex materials keep their original colour pipeline'
    update(scene)
    lighting_pipeline.update(scene)
    lighting_pipeline.materials_current(scene)


def update(scene):
    s=scene.mcbedrock_sky
    if not s.finish_active:
        return
    if s.collection and not s.collection.get('mc_leaf_scattering_opt_in'):
        # Migrate only our previous automatic .22 recipe on already-prepared
        # leaves. Keep other user strengths and original material scattering.
        legacy=any('mc_leaf_original_weight' in node
                   for material in {entry.prepared for entry in s.materials if entry.prepared}
                   for node in material.node_tree.nodes)
        if legacy and abs(s.leaf_scattering-.22)<1e-5:
            s['leaf_scattering']=0.
        s.collection['mc_leaf_scattering_opt_in']=True
    for material in {entry.prepared for entry in s.materials if entry.prepared}:
        name=(material.name+' '+' '.join(n.image.name for n in material.node_tree.nodes if n.type=='TEX_IMAGE' and n.image)).lower()
        if 'grass' not in name:continue
        for layer in material.node_tree.nodes:
            if layer.type=='GROUP' and layer.node_tree and layer.node_tree.get(FINISH)=='foliage':
                grass=finish_group(scene,True,grass=True)
                update_contact_shading(grass,scene)
                layer.node_tree=grass
    for group in bpy.data.node_groups:
        if group.get(TAG)!=s.owner or not group.get(FINISH):
            continue
        update_contact_shading(group,scene)
        color=group.nodes['Texture richness']
        color.inputs['Saturation'].default_value=1+.22*s.texture_vibrance*s.look_strength
        color.inputs['Value'].default_value=1+.4*s.texture_brightness*s.look_strength
        group.nodes['Surface tint'].inputs[2].default_value=(*s.surface_tint,1)
        green=group.nodes.get('Foliage color')
        grass=group.nodes.get('Grass richness')
        if grass:
            grass.inputs['Saturation'].default_value=1+.4*s.grass_vibrance*s.look_strength
            grass.inputs['Value'].default_value=1+.08*s.grass_vibrance*s.look_strength
        if green:
            green.inputs['Hue'].default_value=.5+.07*s.foliage_color*s.look_strength
            green.inputs['Saturation'].default_value=1+.3*s.foliage_color*s.look_strength
            green.inputs['Value'].default_value=1+.3*s.foliage_color*s.look_strength
    light_blocks.update(scene)
    for material in {entry.prepared for entry in s.materials if entry.prepared}:
        edge_details.update_corners(scene,material)
        vegetation_response(material,scene)
        for shader in material.node_tree.nodes:
            if shader.type!='BSDF_PRINCIPLED' or 'mc_original_Roughness' not in shader:
                continue
            port=shader.inputs['Base Color']
            layer=next((n for n in material.node_tree.nodes if n.type=='GROUP' and n.node_tree and n.node_tree.get(FINISH)),None)
            if layer and layer.type=='GROUP' and layer.node_tree.get(FINISH):
                # Keep liquid/glass/metal response and alpha/emission untouched.
                eligible=not any(word in material.name.lower() for word in ('water','glass','ice'))
                eligible &= not any(shader.inputs[k].is_linked or shader.inputs[k].default_value>0 for k in ('Metallic','Transmission Weight'))
                eligible &= shader.inputs['Alpha'].is_linked or shader.inputs['Alpha'].default_value>=1
                layer.inputs['Contact shading'].default_value=float(eligible)
            if material.node_tree.animation_data:
                continue  # Animated socket values take precedence over the sheen preset.
            if any(word in material.name.lower() for word in ('water','glass','ice')):
                continue
            if any(shader.inputs[key].is_linked or shader.inputs[key].default_value>0 for key in ('Metallic','Transmission Weight')):
                continue
            for key,target in (('Roughness',.55),('Specular IOR Level',.35)):
                port=shader.inputs[key]
                if port.is_linked:
                    continue
                original=shader['mc_original_'+key]
                endpoint=min(original,target) if key=='Roughness' else max(original,target)
                value=original+(endpoint-original)*s.surface_sheen
                matte=max(value,.85) if key=='Roughness' else min(value,.25)
                value+=(matte-value)*s.surface_matte
                lighting_pipeline.set_value(port,'default_value',value)


def update_contact_shading(group,scene):
    """Restrained contact depth and diffuse bounce, without changing camera color."""
    nodes,links=group.nodes,group.links
    if 'Diffuse bounce restraint' not in nodes:
        # Cycles evaluates secondary diffuse rays against the receiving material.
        # Reduce their albedo contribution instead of darkening camera-visible
        # textures or disabling GI globally (which would also affect glass/water).
        tint=nodes['Surface tint']
        incoming=tint.inputs[1].links[0].from_socket
        rays=nodes.new('ShaderNodeLightPath')
        rays.name='Surface ray visibility'
        amount=nodes.new('ShaderNodeMath')
        amount.name='Diffuse bounce amount'
        amount.operation='MULTIPLY'
        links.new(rays.outputs['Is Diffuse Ray'],amount.inputs[0])
        bounce=nodes.new('ShaderNodeMixRGB')
        bounce.name='Diffuse bounce restraint'
        bounce.blend_type='MULTIPLY'
        bounce.inputs[2].default_value=(0,0,0,1)
        links.new(amount.outputs[0],bounce.inputs[0])
        links.new(incoming,bounce.inputs[1])
        links.new(bounce.outputs[0],tint.inputs[1])
    if 'Eevee contact shading' not in nodes:
        socket=group.interface.new_socket(name='Contact shading',in_out='INPUT',socket_type='NodeSocketFloat')
        socket.default_value=1
        socket.min_value=0
        socket.max_value=1
        source=next(n for n in nodes if n.type=='GROUP_INPUT')
        ao=nodes.new('ShaderNodeAmbientOcclusion')
        ao.name='Eevee nearby surfaces'
        ao.inputs['Distance'].default_value=.65
        amount=nodes.new('ShaderNodeMath')
        amount.name='Eevee contact amount'
        amount.operation='MULTIPLY'
        links.new(source.outputs['Contact shading'],amount.inputs[0])
        contact=nodes.new('ShaderNodeMixRGB')
        contact.name='Eevee contact shading'
        contact.blend_type='MULTIPLY'
        links.new(amount.outputs[0],contact.inputs[0])
        links.new(nodes['Surface tint'].outputs[0],contact.inputs[1])
        links.new(ao.outputs['AO'],contact.inputs[2])
    if 'Diffuse bounce eligibility' not in nodes:
        gate=nodes.new('ShaderNodeMath')
        gate.name='Diffuse bounce eligibility'
        gate.operation='MULTIPLY'
        source=next(n for n in nodes if n.type=='GROUP_INPUT')
        links.new(source.outputs['Contact shading'],gate.inputs[0])
        links.new(nodes['Surface ray visibility'].outputs['Is Diffuse Ray'],gate.inputs[1])
        links.new(gate.outputs[0],nodes['Diffuse bounce amount'].inputs[0])
    s=scene.mcbedrock_sky
    # Update existing saved groups too. Broad, strong AO makes an artificial
    # dark-to-bright rim across whole blocks rather than a small contact shadow.
    nodes['Eevee nearby surfaces'].inputs['Distance'].default_value=.65
    nodes['Eevee contact amount'].inputs[1].default_value=s.contact_shadows*s.look_strength
    nodes['Diffuse bounce amount'].inputs[1].default_value=(
        .2*s.look_strength if s.trailer_lighting and scene.render.engine=='CYCLES' else 0)
    enabled=scene.render.engine=='BLENDER_EEVEE' and s.trailer_lighting and s.look_strength>0
    source=nodes['Eevee contact shading' if enabled else 'Surface tint'].outputs[0]
    destination=next(n for n in nodes if n.type=='GROUP_OUTPUT').inputs['Color']
    if not destination.links or destination.links[0].from_socket!=source:
        links.new(source,destination)


def restore(scene):
    from .water import clear_atmosphere
    clear_atmosphere(scene)
    s=scene.mcbedrock_sky
    prepared={entry.prepared for entry in s.materials if entry.prepared}
    for entry in s.materials:
        obj=entry.object
        if not obj or entry.index>=len(obj.material_slots):
            continue
        if any(other!=scene and other.mcbedrock_sky.active and any(e.object==obj and e.prepared==entry.prepared for e in other.mcbedrock_sky.materials) for other in bpy.data.scenes):
            continue
        slot=obj.material_slots[entry.index]
        if slot.material!=entry.prepared:
            continue  # The user assigned a different material after setup.
        slot.material=entry.original
        slot.link=entry.link
    s.materials.clear()
    s.finish_active=False
    s.finish_skipped=0
    s.finish_status='Original materials restored'
    for material in prepared:
        if not material.users:
            bpy.data.materials.remove(material)
    for group in list(bpy.data.node_groups):
        if group.get(TAG)==s.owner and group.get(FINISH) and not group.users:
            bpy.data.node_groups.remove(group)
    for image in list(bpy.data.images):
        if image.get(TAG)==s.owner and image.get(edge_details.CORNER) and not image.users:
            bpy.data.images.remove(image)

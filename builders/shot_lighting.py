"""Editable, scene-owned lights for shaping a shot and isolating characters."""
import bpy
from .compatibility import SkyOperator
from mathutils import Vector

PALETTES={
    'MEADOW':dict(horizon_color=(.32,.62,.9),zenith_color=(.018,.16,.62),sun_color=(1,.98,.92),shadow_color=(.9,.95,1),warmth=.08,saturation=1.,ambient_light=1.1,fill=.85,softness=4.,penumbra=.8,key_height_offset=-5.),
    'ADVENTURE':dict(horizon_color=(.9,.46,.19),zenith_color=(.015,.19,.38),sun_color=(1,.84,.62),shadow_color=(.45,.7,1),warmth=.35,saturation=.95,ambient_light=.55,fill=.4,softness=4.,penumbra=.85,key_height_offset=-10.),
}

def palette(scene,name):
    from . import sky
    sky.lighting_look(scene,'TRAILER')
    s=scene.mcbedrock_sky
    sky.BUSY.add(scene.as_pointer())
    try:
        for key,value in dict(PALETTES[name],sky_color_mix=.65,texture_vibrance=0,
                              texture_brightness=0,foliage_color=0,loocs_enabled=True).items():
            setattr(s,key,value)
    finally:sky.BUSY.discard(scene.as_pointer())
    sky.update(scene)


def warm_backlight(scene):
    """Warm surface highlights and gentle bloom; preserve the sky and time."""
    from . import sky
    s=scene.mcbedrock_sky
    sky.BUSY.add(scene.as_pointer())
    try:
        # Raise the key enough to catch horizontal snow and canopy tops. The
        # previous negative offset left these surfaces mostly in cool fill.
        # Keep bloom restrained: the direct light supplies the bright surfaces.
        for key,value in dict(sunlight=3.2,sun_light_warmth=.35,sun_light_color=(1,1,1),
                              sun_heading_offset=0,
                              key_height_offset=10,fill=.6,ambient_light=.75,
                              bloom=.15,bloom_size=.35,
                              sun_haze_mode='MIST',sun_world_haze=.12,sun_world_haze_start=100,sun_world_haze_distance=300,
                              sun_world_haze_spread=25,sun_world_haze_radiance=1.4,
                              sun_world_haze_color=(1,.82,.5)).items():setattr(s,key,value)
        # Start just below the shot's eye level; users can move the layer for
        # valleys or elevated cameras without moving lights or sky artwork.
        eye=scene.camera.matrix_world.translation if scene.camera else None
        if eye is None and bpy.context.screen:
            areas=[a for a in bpy.context.screen.areas if a.type=='VIEW_3D']
            if areas:eye=max(areas,key=lambda a:a.width*a.height).spaces.active.region_3d.view_matrix.inverted().translation
        if eye is not None:s.sun_world_haze_height=eye.z-4
        s.sun_world_haze_height_falloff=16
        s.auto_materials=True
        sky.sky_materials.prepare(scene);sky.lighting_pipeline.setup(scene)
    finally:sky.BUSY.discard(scene.as_pointer())
    sky.update(scene)


def selected_meshes(context):
    pending=list(context.selected_objects);seen=set();meshes=[]
    while pending:
        obj=pending.pop()
        if obj in seen:continue
        seen.add(obj);pending.extend(obj.children)
        if obj.type=='MESH':meshes.append(obj)
    return meshes


def bounds(objects):
    points=[o.matrix_world@Vector(p) for o in objects for p in o.bound_box]
    low=Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high=Vector(tuple(max(p[i] for p in points) for i in range(3)))
    return (low+high)/2,high-low


def add_object(scene,name,data=None):
    obj=bpy.data.objects.new(name,data)
    obj['mc_sky_owner']=scene.mcbedrock_sky.owner
    scene.mcbedrock_sky.collection.objects.link(obj)
    return obj


def area(scene,name,parent,position,target,power,size,color,receivers=None):
    data=bpy.data.lights.new(name,'AREA');data.energy=power;data.shape='DISK';data.size=size;data.color=color
    data['mc_sky_owner']=scene.mcbedrock_sky.owner
    data.use_shadow=True
    obj=add_object(scene,name,data);obj.parent=parent;obj.location=position
    aim=obj.constraints.new('DAMPED_TRACK');aim.target=target;aim.track_axis='TRACK_NEGATIVE_Z'
    if receivers:obj.light_linking.receiver_collection=receivers
    return obj


class MCBEDROCK_OT_shot_light(SkyOperator, bpy.types.Operator):
    bl_idname='mcbedrock.shot_light'
    bl_label='Add shot lighting'
    bl_options={'REGISTER','UNDO'}
    kind:bpy.props.EnumProperty(items=[('CHARACTER','Character rig','Four lights linked to the selected character'),('TERRAIN','Terrain light','Large area light aimed at the selected terrain')])

    def execute(self,context):
        scene=context.scene;s=scene.mcbedrock_sky
        if not s.active:
            self.report({'ERROR'},'Choose a sky look first');return {'CANCELLED'}
        meshes=selected_meshes(context)
        if not meshes:
            self.report({'ERROR'},'Select the character or terrain to light');return {'CANCELLED'}
        if self.kind=='CHARACTER' and scene.render.engine=='BLENDER_EEVEE' and bpy.app.version<(5,2,0):
            self.report({'ERROR'},'Character-only lights require a Blender version with EEVEE light linking');return {'CANCELLED'}
        center,dimensions=bounds(meshes)
        root=add_object(scene,'MC Character lights' if self.kind=='CHARACTER' else 'MC Terrain light')
        root.location=center;root.empty_display_type='PLAIN_AXES'
        target=add_object(scene,'MC Light aim');target.parent=root
        if self.kind=='CHARACTER':
            anchor=context.object or meshes[0]
            root.parent=anchor;root.matrix_parent_inverse=anchor.matrix_world.inverted()
            height=max(dimensions.z,.5)
            target.location.z=height*.15
            receivers=bpy.data.collections.new('MC Character light receivers');receivers['mc_sky_owner']=s.owner
            for obj in meshes:receivers.objects.link(obj)
            facing=add_object(scene,'MC Camera-facing lights');facing.parent=root
            facing['mc_camera_follow']=True
            if scene.camera:
                track=facing.constraints.new('LOCKED_TRACK');track.target=scene.camera;track.track_axis='TRACK_Y';track.lock_axis='LOCK_Z'
            else:
                self.report({'INFO'},'Rig created; add a scene camera to constrain its camera-facing lights')
            scale=height*height
            for name,parent,position,power,size,color in (
                ('Key',root,(-1.5,1.5,1.2),110,1.2,(1,.88,.72)),
                ('Fill',facing,(1.5,1.5,.4),30,1.8,(.62,.78,1)),
                ('Face',facing,(0,1.3,.35),18,.8,(1,.97,.92)),
                ('Back',facing,(.5,-1.3,.9),90,1.,(.55,.75,1))):
                area(scene,'MC Character '+name,parent,Vector(position)*height,target,power*scale,size*height,color,receivers)
        else:
            extent=max(dimensions.x,dimensions.y,dimensions.z,4)
            area(scene,'MC Terrain softbox',root,(-extent*.35,extent*.25,extent*.65),target,35*extent*extent,extent*.6,(.72,.84,1))
        for obj in context.selected_objects:obj.select_set(False)
        root.select_set(True);context.view_layer.objects.active=root
        return {'FINISHED'}

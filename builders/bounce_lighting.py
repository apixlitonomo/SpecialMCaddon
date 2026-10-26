"""Small, artist-placed EEVEE bounce volumes using Blender's native bake job."""
import bpy
from mathutils import Vector

from .compatibility import SkyOperator


def add_volume(context):
    from .sky import TAG, owned
    scene = context.scene
    s = scene.mcbedrock_sky
    objects = set(context.selected_objects)
    for obj in tuple(objects): objects.update(obj.children_recursive)
    points = [obj.matrix_world @ Vector(corner) for obj in objects
              if obj.type == 'MESH' and not obj.get(TAG) for corner in obj.bound_box]
    if not points:
        raise ValueError('Select the terrain or objects around which to add bounce lighting')
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    probe = owned(scene, 'Bounce volume')
    if probe and any(other != scene for other in probe.users_scene):
        raise ValueError('This volume is shared with another scene; reapply a sky preset to make this rig independent')
    if probe is None:
        data = bpy.data.lightprobes.new('Sparkle bounce lighting', 'VOLUME')
        data[TAG] = s.owner
        probe = bpy.data.objects.new('Sparkle bounce lighting', data)
        probe[TAG], probe['role'] = s.owner, 'Bounce volume'
        s.collection.objects.link(probe)
        # Bounded default: 216 samples in space, with a modest surfel density.
        data.resolution_x = data.resolution_y = data.resolution_z = 6
        data.bake_samples = 128
        data.surfel_density = 8
        data.capture_world = False
        data.capture_indirect = True
        data.capture_emission = True
    probe.location = (low + high) * .5
    probe.scale = tuple(max(2, min(16, (high[i]-low[i])*.5+2)) for i in range(3))
    if high.z-low.z < 2:
        # A ground selection needs samples in the air above it, not underground.
        probe.scale.z = 4
        probe.location.z = high.z + 4.2
    probe.data.capture_distance = 24
    return probe


class MCBEDROCK_OT_bounce_lighting(SkyOperator, bpy.types.Operator):
    bl_idname = 'mcbedrock.bounce_lighting'
    bl_label = 'Bounce lighting'
    bl_options = {'UNDO'}
    action: bpy.props.EnumProperty(items=[('ADD', 'Add volume', ''), ('BAKE', 'Bake lighting', ''), ('REMOVE', 'Remove volume', '')])

    @classmethod
    def poll(cls, context):
        return (super().poll(context) and context.scene.mcbedrock_sky.active and
                context.scene.render.engine == 'BLENDER_EEVEE' and context.mode == 'OBJECT')

    def invoke(self, context, event):
        return self._run(context, 'INVOKE_DEFAULT')

    def execute(self, context):
        return self._run(context, 'EXEC_DEFAULT')

    def _run(self, context, invocation):
        from .sky import owned
        try:
            if self.action == 'ADD':
                add_volume(context)
                self.report({'INFO'}, 'Added a local bounce volume (up to 32 units per side). Bake to capture lighting.')
            else:
                probe = owned(context.scene, 'Bounce volume')
                if probe is None:
                    self.report({'WARNING'}, 'Add a bounce volume first')
                    return {'CANCELLED'}
                if any(other != context.scene for other in probe.users_scene):
                    raise ValueError('This volume is shared with another scene; reapply a sky preset before changing it')
                if self.action == 'REMOVE':
                    data = probe.data
                    bpy.data.objects.remove(probe, do_unlink=True)
                    if not data.users: bpy.data.lightprobes.remove(data)
                else:
                    with context.temp_override(object=probe, active_object=probe,
                                               selected_objects=[probe], selected_editable_objects=[probe]):
                        result = bpy.ops.object.lightprobe_cache_bake(invocation, subset='ACTIVE')
                    if 'CANCELLED' in result: return {'CANCELLED'}
            return {'FINISHED'}
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

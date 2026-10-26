"""A colour grade per camera: each shot carries its own, on top of the sky's look.

Element Animation grades every shot (Bram Stout: a colourist grades in
Premiere; their engine also grades parts of the world locally). Here each
camera has exposure, contrast, saturation, temperature and tint
(`Camera.mcbedrock_grade`). The scene camera's grade is added to the
sky's grade on its compositor nodes (`MC Color contrast`, `MC Color
saturation`, `MC Color balance`; exposure as gain), and is
reapplied whenever the shot camera changes, so cutting between cameras
on the timeline cuts the grade too. Turning a grade on shows the
compositor in camera view (Rendered or Material Preview), so the grade,
Flat fog and glow can be judged live while lighting.
"""
import bpy

LAST = 'mc_camera_grade'      # sky collection: the grade last applied, to notice a cut
MID_GREY = .18                # contrast turns around this scene-linear value
LAST_CAMERA = 'mc_camera_grade_camera'


def grade_of(scene):
    """(exposure, contrast, saturation, temperature, tint) the shot camera adds."""
    camera = scene.camera
    data = camera.data if camera is not None and camera.type == 'CAMERA' else None
    grade = getattr(data, 'mcbedrock_grade', None) if data is not None else None
    if grade is None or not grade.use:
        return 0., 0., 1., 0., 0.
    return grade.exposure, grade.contrast, grade.saturation, grade.temperature, grade.tint


def apply(scene):
    """Write the sky's grade plus the shot camera's onto the compositor and view."""
    from .lighting_pipeline import set_value
    s = getattr(scene, 'mcbedrock_sky', None)
    if s is None or not s.active or s.compositor is None:
        return
    nodes = s.compositor.nodes
    contrast, saturation, balance = (nodes.get('MC Color contrast'), nodes.get('MC Color saturation'),
                                     nodes.get('MC Color balance'))
    if contrast is None or saturation is None or balance is None:
        return
    exposure, add_contrast, times_saturation, add_temperature, add_tint = grade_of(scene)
    temperature, tint = s.temperature + add_temperature, s.tint + add_tint
    power, pivot = nodes.get('MC Grade contrast power'), nodes.get('MC Grade contrast pivot')
    total = s.contrast + add_contrast
    if power is not None and pivot is not None:
        # Filmic contrast around mid-grey: out = 0.18 * (in / 0.18) ** k.
        k = max(.2, 1. + total / 100.)
        set_value(contrast.inputs['Contrast'], 'default_value', 0.)
        set_value(power.outputs[0], 'default_value', k)
        set_value(pivot.outputs[0], 'default_value', MID_GREY ** (1. - k))
    else:
        set_value(contrast.inputs['Contrast'], 'default_value', total)
    set_value(saturation.inputs['Saturation'], 'default_value', s.saturation * times_saturation)
    # Exposure as gain in scene-linear light, before the view transform: the
    # view exposure is the sky's own Exposure (native colour syncs it back).
    stops = 2. ** exposure
    gain = tuple(stops * g for g in (1 + .2 * temperature + .08 * tint, 1 - .12 * tint, 1 - .2 * temperature + .08 * tint))
    if hasattr(balance, 'gain'):
        set_value(balance, 'gain', gain)
    else:
        set_value(next(socket for socket in balance.inputs if socket.name == 'Gain' and socket.type == 'RGBA'),
                  'default_value', (*gain, 1))
    if s.collection is not None:
        s.collection[LAST] = [exposure, add_contrast, times_saturation, add_temperature, add_tint]
        s.collection[LAST_CAMERA] = scene.camera.name if scene.camera else ''


def follow_cut(scene):
    """Reapply when the shot camera (or its grade) changed since the last write."""
    s = getattr(scene, 'mcbedrock_sky', None)
    if s is None or not s.active or s.collection is None:
        return
    wanted = grade_of(scene)
    last = list(s.collection.get(LAST, []))
    camera = scene.camera.name if scene.camera else ''
    if (s.collection.get(LAST_CAMERA) != camera or len(last) != len(wanted)
            or any(abs(a - b) > 1e-6 for a, b in zip(last, wanted))):
        apply(scene)


def show_live(context):
    """Show the compositor in camera view in every 3D viewport, so the grade is live."""
    screen = context.window.screen if context.window else None
    for area in screen.areas if screen else ():
        if area.type == 'VIEW_3D':
            shading = area.spaces.active.shading
            if hasattr(shading, 'use_compositor') and shading.use_compositor == 'DISABLED':
                shading.use_compositor = 'CAMERA'


def _changed(self, context):
    if context is None:
        return
    if self.use:
        show_live(context)
    for scene in bpy.data.scenes:
        camera = scene.camera
        if camera is not None and camera.data == self.id_data:
            apply(scene)


class CameraGrade(bpy.types.PropertyGroup):
    use: bpy.props.BoolProperty(name='Shot grade', description='Grade this shot on top of the sky look; shows live in camera view',
                                default=False, update=_changed)
    exposure: bpy.props.FloatProperty(name='Exposure', description='Brighter or darker, in stops', default=0,
                                      min=-5, max=5, soft_min=-2, soft_max=2, update=_changed)
    contrast: bpy.props.FloatProperty(name='Contrast', default=0, min=-50, max=50, soft_min=-25, soft_max=25, update=_changed)
    saturation: bpy.props.FloatProperty(name='Saturation', default=1, min=0, max=2, update=_changed)
    temperature: bpy.props.FloatProperty(name='Temperature', description='Cooler (blue) or warmer (orange)',
                                         default=0, min=-1, max=1, update=_changed)
    tint: bpy.props.FloatProperty(name='Tint', description='Green or magenta', default=0, min=-1, max=1, update=_changed)


class MCBEDROCK_OT_add_shot_camera(bpy.types.Operator):
    bl_idname = 'mcbedrock.add_shot_camera'
    bl_label = 'Add Shot Camera'
    bl_description = 'A camera from the current view, made the scene camera, with its own colour grade shown live'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        scene = context.scene
        data = bpy.data.cameras.new('Shot camera')
        camera = bpy.data.objects.new('Shot camera', data)
        context.collection.objects.link(camera)
        region_3d = getattr(context.space_data, 'region_3d', None) if context.space_data else None
        if region_3d is not None:
            camera.matrix_world = region_3d.view_matrix.inverted()
            data.lens = getattr(context.space_data, 'lens', data.lens)
        elif scene.camera is not None:
            camera.matrix_world = scene.camera.matrix_world.copy()
        scene.camera = camera
        data.mcbedrock_grade.use = True
        for other in context.selected_objects:
            other.select_set(False)
        camera.select_set(True)
        context.view_layer.objects.active = camera
        if region_3d is not None:
            region_3d.view_perspective = 'CAMERA'
        return {'FINISHED'}


CLASSES = (CameraGrade, MCBEDROCK_OT_add_shot_camera)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Camera.mcbedrock_grade = bpy.props.PointerProperty(type=CameraGrade)


def unregister():
    if hasattr(bpy.types.Camera, 'mcbedrock_grade'):
        del bpy.types.Camera.mcbedrock_grade
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

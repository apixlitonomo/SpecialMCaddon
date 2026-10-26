"""Sparkle in Blender's own menus and Properties tabs.

The Sparkle sidebar stays the full workspace; these put the everyday sky
controls where a Blender user already looks, drawn with Blender's own widgets:

* World Properties > Sparkle Sky: the look, time of day, sun, both fogs,
  god rays and clouds (with motion streaks).
* Camera Properties > Shot Grade, and Add > Camera > Shot Camera (graded):
  each camera's own colour grade, live in camera view.
* Render Properties > Color Management, inline LOOCS section: the LOOCS
  colour rendering, log output and texture correction.

Every control is the same setting or operator the sidebar uses.
"""
import bpy

LOOKS = (('DAY', 'Daylight'), ('GOLDEN', 'Golden Hour'), ('SUNSET', 'Sunset'), ('NIGHT', 'Night'))


def draw_add_camera(self, context):
    """Add > Camera: a shot camera that carries its own live colour grade."""
    self.layout.operator('mcbedrock.add_shot_camera', text='Shot Camera (graded)', icon='OUTLINER_OB_CAMERA')


class SPARKLE_PT_camera_grade(bpy.types.Panel):
    bl_label = 'Shot Grade'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'data'

    @classmethod
    def poll(cls, context):
        return context.camera is not None and hasattr(context.camera, 'mcbedrock_grade')

    def draw_header(self, context):
        self.layout.prop(context.camera.mcbedrock_grade, 'use', text='')

    def draw(self, context):
        layout = self.layout
        grade = context.camera.mcbedrock_grade
        layout.use_property_split = True
        layout.use_property_decorate = False
        sky = getattr(context.scene, 'mcbedrock_sky', None)
        if sky is None or not sky.active:
            layout.label(text='Needs a Sparkle sky.', icon='INFO')
        col = layout.column(align=True)
        col.active = grade.use
        for name in ('exposure', 'contrast', 'saturation', 'temperature', 'tint'):
            col.prop(grade, name)
        if grade.use:
            layout.label(text='Live in camera view (Rendered).', icon='HIDE_OFF')


class SPARKLE_PT_world_sky(bpy.types.Panel):
    bl_label = 'Sparkle Sky'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'world'

    @classmethod
    def poll(cls, context):
        return getattr(context.scene, 'mcbedrock_sky', None) is not None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        s = scene.mcbedrock_sky
        layout.use_property_split = True
        layout.use_property_decorate = False
        looks = layout.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
        looks.use_property_split = False
        for key, label in LOOKS:
            op = looks.operator('mcbedrock.sky', text=label, depress=s.active and s.preset == key)
            op.action, op.preset = 'APPLY', key
        if not s.active:
            layout.label(text='Pick a look to add the Sparkle sky.', icon='INFO')
            return
        from ..builders.sky import world_status
        status = world_status(scene)
        if status:
            # The sky's world was deleted (nothing below would change), or the
            # scene shows another world: offer the one-click way back.
            box = layout.box()
            box.label(text="The sky's world was deleted." if status == 'MISSING' else 'The scene shows another world.',
                      icon='ERROR' if status == 'MISSING' else 'INFO')
            box.operator('mcbedrock.sky', text='Rebuild Sky World' if status == 'MISSING' else 'Show Sparkle Sky',
                         icon='WORLD').action = 'REPAIR_WORLD'
            if status == 'MISSING':
                return
        col = layout.column()
        col.prop(s, 'time')
        col.prop(s, 'sunlight', text='Sun Strength')
        col.prop(s, 'fill', text='Shadow Fill')
        col.prop(s, 'character_fill')


def _sky_with_world(context):
    """A sky whose world still exists: without it these controls change nothing."""
    s = getattr(context.scene, 'mcbedrock_sky', None)
    return s is not None and s.active and s.world is not None


class SPARKLE_PT_world_fog(bpy.types.Panel):
    bl_label = 'Fog'
    bl_parent_id = 'SPARKLE_PT_world_sky'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'world'

    @classmethod
    def poll(cls, context):
        return _sky_with_world(context)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        s = scene.mcbedrock_sky
        layout.use_property_split = True
        layout.use_property_decorate = False
        fog = getattr(scene, 'mcbedrock_fog_planes', None)
        if fog is not None:
            row = layout.row()
            row.use_property_split = False
            row.operator('mcbedrock.fog_planes', text='Distance Fog', depress=fog.active,
                         icon='CHECKBOX_HLT' if fog.active else 'CHECKBOX_DEHLT').enable = not fog.active
            if fog.active:
                col = layout.column(align=True)
                col.prop(fog, 'near', text='Starts')
                col.prop(fog, 'far', text='Ends')
                col.prop(fog, 'density')
        layout.separator()
        layout.prop(s, 'flat_fog', text='Flat Fog')
        if s.flat_fog > 0:
            col = layout.column(align=True)
            col.prop(s, 'flat_fog_start', text='Starts')
            col.prop(s, 'flat_fog_distance', text='Build Up')
        layout.prop(s, 'sun_rays', text='God Rays')
        if s.sun_rays > 0:
            layout.prop(s, 'sun_rays_length', text='Ray Length')


class SPARKLE_PT_world_clouds(bpy.types.Panel):
    bl_label = 'Clouds'
    bl_parent_id = 'SPARKLE_PT_world_sky'
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'world'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _sky_with_world(context)

    def draw(self, context):
        layout = self.layout
        s = context.scene.mcbedrock_sky
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, 'cloud_preset', text='Style')
        layout.prop(s, 'clouds', text='Amount')
        layout.prop(s, 'moving', text='Moving')
        if s.moving:
            col = layout.column(align=True)
            col.prop(s, 'cloud_speed', text='Speed')
        layout.prop(s, 'cloud_streak')
        if s.moving or s.cloud_streak:
            layout.prop(s, 'cloud_direction', text='Direction')
        if s.cloud_streak:
            layout.prop(s, 'cloud_streak_length')


def draw_color_management(self, context):
    """Render Properties > Color Management: an inline LOOCS section."""
    s = getattr(context.scene, 'mcbedrock_sky', None)
    if s is None:
        return
    from ..builders import native_color as nc
    scene = context.scene
    installed = nc.available(scene)
    header, body = self.layout.panel('SPARKLE_loocs_color', default_closed=False)
    header.use_property_split = False
    if installed:
        header.prop(s, 'loocs_view', text='')
    header.label(text='LOOCS')
    if body is None:
        return
    body.use_property_split = True
    body.use_property_decorate = False
    if not installed:
        version = nc.installed_version()
        if s.native_color_pending:
            body.label(text='Save and restart Blender to finish setup.', icon='INFO')
        elif version >= 2:
            body.label(text='LOOCS renderings use the sRGB display.', icon='INFO')
        else:
            body.operator('mcbedrock.sky_color_profile', icon='COLOR',
                          text='Update to LOOCS 0.12' if version == 1 else 'Set Up LOOCS Colors')
            body.label(text='One-time setup. Restart Blender afterward.')
        return
    if nc.installed_version() == 2:
        # v2 profiles showed LOOCS colours too saturated in the viewport.
        if s.native_color_pending:
            body.label(text='Save and restart Blender to finish the update.', icon='INFO')
        else:
            body.operator('mcbedrock.sky_color_profile', icon='COLOR', text='Update LOOCS Colors')
            body.label(text='Fixes oversaturated viewport colors. Restart Blender afterward.')
    col = body.column()
    col.active = s.loocs_view
    col.prop(s, 'loocs_rendering', text='Rendering')
    col.prop(s, 'loocs_output', text='Output')
    log = nc.LOG_VIEWS.get(scene.view_settings.view_transform)
    if log:
        col.label(text=f'Grade with the LOOCS {nc.LOG_LABELS[log]} input DCTL.', icon='INFO')
    col = body.column(heading='Textures')
    col.prop(s, 'loocs_auto_textures', text='Auto-Correct')
    col.prop(s, 'loocs_texture_correction', text='Correction')
    if not s.loocs_auto_textures and nc.correction(scene) not in (None, nc.CLASSIC):
        row = col.row(align=True)
        row.operator('mcbedrock.texture_correction', text='Correct Selected').enable = True
        row.operator('mcbedrock.texture_correction', text='Restore sRGB').enable = False


MENUS = ((bpy.types.VIEW3D_MT_camera_add, draw_add_camera),
         (bpy.types.RENDER_PT_color_management, draw_color_management))
CLASSES = (SPARKLE_PT_camera_grade, SPARKLE_PT_world_sky, SPARKLE_PT_world_fog, SPARKLE_PT_world_clouds)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for menu, function in MENUS:
        try:
            menu.remove(function)               # never twice after a reload
        except ValueError:
            pass
        menu.append(function)


def unregister():
    for menu, function in MENUS:
        try:
            menu.remove(function)
        except (ValueError, AttributeError):
            pass
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

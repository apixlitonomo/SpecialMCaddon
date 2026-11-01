"""Blender navigation gizmos placed beside the expanded Sparkle panel.

Uses Blender's own gizmo types and view operators; no camera/navigation math is
reimplemented. The native group's visibility is restored by the runtime.
"""
import bpy


class MCBEDROCK_OT_orbit_click(bpy.types.Operator):
    bl_idname = 'mcbedrock.orbit_click'
    bl_label = 'Drag to Orbit'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        # A center click has no axis to snap to. Drag is handled by the keymap.
        return {'FINISHED'}


def session_for(context):
    from .runtime import _ACTIVE, _pointer
    return next((s for s in _ACTIVE.values()
                 if not s._finished and _pointer(s.area) == _pointer(context.area)
                 and getattr(s, '_native_navigation', None) is True
                 and s.area.spaces.active.show_region_ui
                 and getattr(s.region, 'active_panel_category', '') == 'Sparkle'), None)


# A GIZMO_GT_button_2d reads its icon once, on its first draw, and keeps it
# (button2d_gizmo.cc, `is_init`). One button whose icon followed the view kept
# its first state: the padlock stayed open after Lock Camera to View was turned
# on. As in Blender's own group (view3d_gizmo_navigate.cc), each state has its
# own button, and draw_prepare shows the one that matches. Never set `icon`
# after setup.
BUTTONS = (  # name, operator, icon
    ('zoom', 'view3d.zoom', 'VIEW_ZOOM'),
    ('move', 'view3d.move', 'VIEW_PAN'),
    ('camera_off', 'view3d.view_camera', 'VIEW_CAMERA_UNSELECTED'),
    ('camera_on', 'view3d.view_camera', 'VIEW_CAMERA'),
    ('persp', 'view3d.view_persportho', 'VIEW_PERSPECTIVE'),
    ('ortho', 'view3d.view_persportho', 'VIEW_ORTHO'),
    ('lock', 'wm.context_toggle', 'VIEW_LOCKED'),
    ('unlock', 'wm.context_toggle', 'VIEW_UNLOCKED'),
)
_DRAG = {'zoom', 'move'}


def shown_buttons(show_navigate, camera, perspective, lock_camera, lock_rotation=False):
    """The buttons Blender's navigation group shows, top to bottom.

    Mirrors WIDGETGROUP_navigate_draw_prepare: Preferences > Navigation
    Controls shows them; a camera view swaps Perspective/Orthographic for the
    Lock Camera to View padlock; a rotation-locked view has no camera or
    projection button. (Python cannot read the zoom and location locks.)
    """
    if not show_navigate:
        return ()
    shown = ['zoom', 'move']
    if not lock_rotation:
        shown.append('camera_on' if camera else 'camera_off')
        if not camera:
            shown.append('persp' if perspective else 'ortho')
    if camera:
        shown.append('lock' if lock_camera else 'unlock')
    return tuple(shown)


class MCBEDROCK_GGT_panel_navigation(bpy.types.GizmoGroup):
    bl_idname = 'MCBEDROCK_GGT_panel_navigation'
    bl_label = 'Sparkle Viewport Navigation'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return context.space_data.show_gizmo and session_for(context) is not None

    def setup(self, context):
        self.buttons = {}
        for name, operator, icon in BUTTONS:
            drag = name in _DRAG
            gz = self.gizmos.new('GIZMO_GT_button_2d')
            props = gz.target_set_operator(operator)
            if operator == 'wm.context_toggle':
                props.data_path = 'space_data.lock_camera'
            if drag:
                props.use_cursor_init = False
            gz.icon = icon
            gz.show_drag = drag
            gz.use_draw_modal = True
            gz.use_grab_cursor = drag
            gz.draw_options = {'BACKDROP', 'OUTLINE'}
            gz.color = (0, 0, 0)
            gz.alpha = .2
            gz.color_highlight = (.5, .5, .5)
            gz.alpha_highlight = .5
            gz.scale_basis = 14
            gz.hide = True
            self.buttons[name] = gz

    def draw_prepare(self, context):
        session = session_for(context)
        if session is None:
            return
        scale = session.ui_scale
        # Basis coordinates are physical pixels; SCALE applies to gizmo size.
        edge = session.navigation_edge(context)  # beside the panel, gliding while it opens or folds
        size = context.preferences.view.gizmo_size_navigate_v3d
        top = session.region.y - context.region.y + session.region.height
        show_axis = context.preferences.view.mini_axis_type == 'GIZMO'
        first_y = top - (size + 30 if show_axis else 24) * scale
        view = context.region_data
        shown = shown_buttons(context.preferences.view.show_navigate_ui,
                              view.view_perspective == 'CAMERA', view.is_perspective,
                              context.space_data.lock_camera, view.lock_rotation)
        for name, gz in self.buttons.items():
            gz.hide = name not in shown
        for slot, name in enumerate(shown):
            self.buttons[name].matrix_basis.translation = (edge - 26 * scale, first_y - slot * 30 * scale, 0)


class MCBEDROCK_GGT_panel_orbit(bpy.types.GizmoGroup):
    bl_idname = 'MCBEDROCK_GGT_panel_orbit'
    bl_label = 'Sparkle Viewport Orbit'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return (context.space_data.show_gizmo and session_for(context) is not None
                and context.preferences.view.mini_axis_type == 'GIZMO')

    @classmethod
    def setup_keymap(cls, keyconfig):
        # The native orbit gizmo's drag_part setting is not exposed in Python.
        # Give the axis its own keymap: release clicks snap; drags invoke native
        # orbit regardless of which axis is highlighted. Zoom/pan keep theirs.
        keymap = keyconfig.keymaps.new(cls.bl_idname, space_type='VIEW_3D', region_type='WINDOW')
        for item in list(keymap.keymap_items):
            keymap.keymap_items.remove(item)
        keymap.keymap_items.new('gizmogroup.gizmo_tweak', 'LEFTMOUSE', 'CLICK')
        drag = keymap.keymap_items.new('view3d.rotate', 'LEFTMOUSE', 'CLICK_DRAG')
        drag.properties.use_cursor_init = False
        return keymap

    def setup(self, context):
        self.axis = self.gizmos.new('VIEW3D_GT_navigate_rotate')
        self.axis.color = (0, 0, 0)
        self.axis.use_draw_modal = True
        self.axis.use_grab_cursor = True
        self.axis.alpha = 0
        self.axis.color_highlight = (.5, .5, .5)
        self.axis.alpha_highlight = .5
        self.axis.target_set_operator('mcbedrock.orbit_click')
        for i, axis in enumerate(('LEFT', 'RIGHT', 'FRONT', 'BACK', 'BOTTOM', 'TOP'), 1):
            self.axis.target_set_operator('view3d.view_axis', index=i).type = axis

    def draw_prepare(self, context):
        session = session_for(context)
        if session is None:
            return
        scale = session.ui_scale
        size = context.preferences.view.gizmo_size_navigate_v3d
        edge = session.navigation_edge(context)  # beside the panel, gliding while it opens or folds
        top = session.region.y - context.region.y + session.region.height
        self.axis.scale_basis = size / 2
        self.axis.matrix_basis.translation = (edge - (size / 2 + 12) * scale,
                                              top - (size / 2 + 10) * scale, 0)
        self.axis.matrix_offset = context.region_data.view_matrix.to_3x3().to_4x4()

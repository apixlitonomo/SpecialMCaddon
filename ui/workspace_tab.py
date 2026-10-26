"""Native workspace tab with an intentionally empty framework canvas."""

import bpy
from bpy.app.handlers import persistent

from .gpu_framework import Rect, Theme

WORKSPACE_KEY = "mcbedrock_workspace"
_draw_handle = None
_pending_setup = []


def find_workspace():
    return next((w for w in bpy.data.workspaces if w.get(WORKSPACE_KEY)), None)


def ensure_workspace(context):
    existing = find_workspace()
    if existing is not None:
        return existing
    if context.window is None:
        return None
    before = set(bpy.data.workspaces)
    if bpy.ops.workspace.duplicate() != {'FINISHED'}:
        return None
    workspace = next(w for w in bpy.data.workspaces if w not in before)
    workspace.name = "Sparkle"
    # Configure only the new copy. Scene data and the user's other workspaces
    # are untouched. The image editor provides a harmless empty drawing host.
    for screen in workspace.screens:
        areas = [a for a in screen.areas if a.type not in {'TOPBAR', 'STATUSBAR'}]
        if not areas:
            continue
        area = max(areas, key=lambda a: a.width * a.height)
        with context.temp_override(window=context.window, screen=screen, area=area):
            area.type = 'IMAGE_EDITOR'
            space = area.spaces.active
            space.image = None
    workspace[WORKSPACE_KEY] = True
    _pending_setup.append((workspace, 3))
    if not bpy.app.background and not bpy.app.timers.is_registered(_finish_setup):
        bpy.app.timers.register(_finish_setup, first_interval=0.2)
    return workspace


def _finish_setup():
    # Editor replacement rebuilds its regions on the next UI update. Hiding
    # them before that would toggle the previous editor's regions instead.
    pending = list(_pending_setup)
    _pending_setup.clear()
    for workspace, passes in pending:
        for screen in workspace.screens:
            area = max(screen.areas, key=lambda a: a.width * a.height)
            if area.type != 'IMAGE_EDITOR':
                continue
            with bpy.context.temp_override(window=bpy.context.window, screen=screen, area=area):
                space = area.spaces.active
                space.show_region_ui = False
                space.show_region_toolbar = False
                space.show_region_header = False
                space.show_region_tool_header = False
                space.show_region_hud = False
            area.tag_redraw()
        if passes > 1:
            _pending_setup.append((workspace, passes - 1))
    return 0.2 if _pending_setup else None


def draw_canvas():
    context = bpy.context
    if not context.workspace or not context.workspace.get(WORKSPACE_KEY):
        return
    area = context.area
    if area != max(context.screen.areas, key=lambda a: a.width * a.height):
        return
    from .gpu_framework.renderer import BlenderRenderer
    renderer = BlenderRenderer(context.region)
    renderer.linear_output = True
    renderer.begin()
    try:
        renderer.rect(Rect(0, 0, context.region.width, context.region.height), Theme().canvas)
    finally:
        renderer.end()


@persistent
def _on_load(_unused):
    _pending_setup.clear()
    # Loading a file must never create, select, or modify a workspace.
    if bpy.app.timers.is_registered(_finish_setup):
        bpy.app.timers.unregister(_finish_setup)


class MCBEDROCK_OT_open_workspace(bpy.types.Operator):
    bl_idname = "mcbedrock.open_workspace"
    bl_label = "Open Sparkle Workspace"
    bl_description = "Open the empty Sparkle workspace prototype"

    def execute(self, context):
        workspace = ensure_workspace(context)
        if workspace is None:
            return {'CANCELLED'}
        context.window.workspace = workspace
        return {'FINISHED'}


def register():
    global _draw_handle
    bpy.utils.register_class(MCBEDROCK_OT_open_workspace)
    _draw_handle = bpy.types.SpaceImageEditor.draw_handler_add(draw_canvas, (), 'WINDOW', 'POST_PIXEL')
    bpy.app.handlers.load_post.append(_on_load)
    _on_load(None)


def unregister():
    global _draw_handle
    if bpy.app.timers.is_registered(_finish_setup):
        bpy.app.timers.unregister(_finish_setup)
    _pending_setup.clear()
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    if _draw_handle is not None:
        bpy.types.SpaceImageEditor.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    if hasattr(bpy.types, MCBEDROCK_OT_open_workspace.__name__):
        bpy.utils.unregister_class(MCBEDROCK_OT_open_workspace)
    # Keep saved/user-customized workspace layouts when disabling the add-on.

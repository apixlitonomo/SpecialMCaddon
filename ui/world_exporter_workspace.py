"""Dedicated full-window GPU world-selection workspace."""
import bpy
from bpy.app.handlers import persistent

KEY = 'mcaddon_world_exporter_workspace'
LAYOUT_VERSION = 4
NAME = 'World Importer'
# Tabs created before the rename keep working and pick up the new name.
OLD_NAMES = {'World Exporter'}


def _prepare_canvas(window,screen,area):
    # Render and preferences windows have temporary screens, which context
    # overrides refuse. Copying a workspace while one is open (or saved open
    # in the .blend) copies its screen too. Leave those screens alone.
    if screen.is_temporary:return False
    with bpy.context.temp_override(window=window,screen=screen,area=area):
        area.type='IMAGE_EDITOR'
    # Visibility setters resolve their own owning area. An area override here
    # makes Blender toggle the newly-created regions back on.
    with bpy.context.temp_override(window=window):
        space=area.spaces.active
        if space.type!='IMAGE_EDITOR':return False
        space.image=None
        for prop in ('show_region_ui','show_region_toolbar','show_region_header','show_region_tool_header','show_region_hud'):
            if hasattr(space,prop):setattr(space,prop,False)
    return True


def ensure_workspace(context):
    workspace = next((w for w in bpy.data.workspaces if w.get(KEY)), None)
    if workspace is not None and workspace.name in OLD_NAMES and NAME not in bpy.data.workspaces:
        workspace.name = NAME
    if workspace is None and context.window:
        original = context.window.workspace
        before = set(bpy.data.workspaces)
        try:
            bpy.ops.workspace.duplicate()
            workspace = next(w for w in bpy.data.workspaces if w not in before)
            workspace.name = NAME
            workspace[KEY] = 1
            if 'mcbedrock_workspace' in workspace:
                del workspace['mcbedrock_workspace']
            # Prepare the hidden workspace before its first visible frame.
            # Region flags require the window's actual active workspace,
            # not only a temporary screen override. Restore before yielding.
            context.window.workspace=workspace
            for screen in workspace.screens:
                for area in screen.areas:_prepare_canvas(context.window,screen,area)
            if not bpy.app.background:_activate_panel()
        finally:
            context.window.workspace = original
    if not bpy.app.background and not bpy.app.timers.is_registered(_activate_panel):
        bpy.app.timers.register(_activate_panel, first_interval=0.)
    return workspace


def _activate_panel():
    from . import world_exporter_canvas as canvas
    for window in bpy.context.window_manager.windows:
        if not window.workspace.get(KEY):
            continue
        screen = window.screen
        # A render or file browser window opened from this tab uses its
        # workspace, but never shows the map.
        if screen.is_temporary or screen not in list(window.workspace.screens):
            continue
        areas = list(screen.areas)
        if not areas:
            continue
        if window.workspace.get(KEY) != LAYOUT_VERSION:
            area = max(areas, key=lambda a: a.width * a.height)
            for candidate in areas:_prepare_canvas(window,screen,candidate)
            # Only displayed screens may be joined. Finish in one timer pass
            # rather than showing the copied layout across several frames.
            for _ in range(len(areas)-1):
                others = [a for a in screen.areas if a != area]
                if not others:break
                target = min(others, key=lambda a: a.width*a.height)
                with bpy.context.temp_override(window=window, screen=screen, area=target):
                    bpy.ops.screen.area_close()
            if not _prepare_canvas(window,screen,area):continue
            window.workspace[KEY] = LAYOUT_VERSION
        for area in screen.areas:
            if area.type == 'IMAGE_EDITOR':
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region and area.as_pointer() not in canvas.running:
                    with bpy.context.temp_override(window=window,area=area,region=region):
                        bpy.ops.mcbedrock.world_canvas('INVOKE_DEFAULT')
                    area.tag_redraw()
    return .1


def _startup():
    if bpy.app.background:
        return None
    window = bpy.context.window
    if window and window.screen.is_temporary:
        # Set the tab up from a main window, not a render or preferences one.
        window = next((w for w in bpy.context.window_manager.windows if not w.screen.is_temporary), None)
    if not window:
        return .5
    from .custom_ui import _addon_preferences
    preferences = _addon_preferences(bpy.context)
    if not preferences or preferences.miex_auto_workspace:
        with bpy.context.temp_override(window=window):
            ensure_workspace(bpy.context)
        # The world picker starts discovery on demand. Starting Java here can
        # take desktop focus whenever the add-on is enabled or a file is loaded.
    return None


@persistent
def _load(_unused=None):
    if bpy.app.background:return
    # The map's input session starts from _activate_panel even when the tab is not
    # created automatically: a file can still bring its own World Importer tab.
    for timer in (_startup,_activate_panel):
        if not bpy.app.timers.is_registered(timer):
            bpy.app.timers.register(timer,first_interval=0.)


class MCBEDROCK_OT_world_workspace(bpy.types.Operator):
    bl_idname = 'mcbedrock.world_workspace'
    bl_label = 'Open World Importer'

    def execute(self,context):
        workspace = ensure_workspace(context)
        if not workspace:
            return {'CANCELLED'}
        context.window.workspace = workspace
        if not bpy.app.background:_activate_panel()
        return {'FINISHED'}


def register():
    bpy.utils.register_class(MCBEDROCK_OT_world_workspace)
    bpy.app.handlers.load_post.append(_load)
    _load()


def unregister():
    for timer in (_startup,_activate_panel):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    if _load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load)
    if hasattr(bpy.types,MCBEDROCK_OT_world_workspace.__name__):
        bpy.utils.unregister_class(MCBEDROCK_OT_world_workspace)

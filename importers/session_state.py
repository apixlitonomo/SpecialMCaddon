"""Session-only resource-pack paths and Blender load handlers."""

from __future__ import annotations


SESSION_PATH_PROPERTIES = (
    "mcbedrock_resource_pack_path",
    "mcbedrock_browser_root",
)


def clear_unsaved_scene_paths(scenes, blend_filepath):
    """Clear legacy saved paths only for a new/unsaved Blender session."""
    if str(blend_filepath or ""):
        return 0
    cleared = 0
    for scene in scenes:
        for property_name in SESSION_PATH_PROPERTIES:
            if not hasattr(scene, property_name):
                continue
            if getattr(scene, property_name, ""):
                setattr(scene, property_name, "")
                cleared += 1
    return cleared


try:
    import bpy
    from bpy.app.handlers import persistent
except ImportError:
    bpy = None

    def persistent(function):
        return function


@persistent
def reset_unsaved_session_paths(_unused=None):
    if bpy is None:
        return
    clear_unsaved_scene_paths(
        bpy.data.scenes,
        bpy.data.filepath,
    )


def register():
    if bpy is None:
        return
    handlers = bpy.app.handlers.load_post
    if reset_unsaved_session_paths not in handlers:
        handlers.append(reset_unsaved_session_paths)


def unregister():
    if bpy is None:
        return
    handlers = bpy.app.handlers.load_post
    if reset_unsaved_session_paths in handlers:
        handlers.remove(reset_unsaved_session_paths)


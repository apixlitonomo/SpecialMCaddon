"""Selection helpers that do not depend on the active Blender editor."""


def deselect_all_objects(context):
    """Deselect objects without invoking a context-sensitive operator.

    ``bpy.ops.object.select_all`` only polls successfully in an object-capable
    editor context in Blender 5.2. Asset Browser imports can originate from
    Properties or another editor, so use the object data API instead.
    """
    for obj in tuple(getattr(context, "selected_objects", ())):
        obj.select_set(False)

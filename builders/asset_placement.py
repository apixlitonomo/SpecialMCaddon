"""Place a newly imported asset as a unit, without moving existing scene data."""
def place_new_roots_at_cursor(context, previous_objects, cursor=None):
    offset=context.scene.cursor.location.copy() if cursor is None else cursor.copy()
    context.view_layer.update()
    created={obj for obj in set(context.scene.objects)-set(previous_objects) if not obj.get('mc_sky_owner')}
    roots=[obj for obj in created if obj.parent not in created]
    for obj in roots:
        matrix=obj.matrix_world.copy()
        if obj.get('mcbedrock_presentation_root',False):
            matrix.translation=offset
        else:
            matrix.translation+=offset
        obj.matrix_world=matrix
    return roots

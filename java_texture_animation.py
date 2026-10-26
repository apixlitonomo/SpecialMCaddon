"""Pure helpers for Java .png.mcmeta texture animation (no bpy)."""


def mcmeta_frame_location_y(frame_index, frame_count):
    """Blender Mapping Location.Y for Minecraft vertical strip frame ``frame_index``.

    Minecraft stores frame 0 at the TOP of the PNG. Blender UV V=0 is the
    bottom of the image, so the strip must be indexed from the top down or
    animations play backwards.
    """
    frame_count = max(1, int(frame_count))
    frame_index = max(0, min(frame_count - 1, int(frame_index)))
    return (frame_count - 1 - frame_index) / float(frame_count)


def iter_action_fcurves(action):

    """Return F-curves from legacy and Blender 5 layered Actions."""
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return list(fcurves)

    curves = []
    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            for channelbag in getattr(strip, "channelbags", []) or []:
                bag_curves = getattr(channelbag, "fcurves", None)
                if bag_curves is not None:
                    curves.extend(bag_curves)
    return curves

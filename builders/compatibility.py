"""Feature gates for Blender versions older than the full lighting target."""
import bpy


SKY_REQUIREMENT = 'Sky & Lighting requires Blender 5.2 or newer.'


def supports_sky():
    # The rig targets 5.2's compositor, color and EEVEE APIs. Keep its RNA
    # registered on older Blender so opening a file preserves saved settings.
    return bpy.app.version >= (5, 2, 0)


class SkyOperator:
    @classmethod
    def poll(cls, context):
        if not supports_sky():
            cls.poll_message_set(SKY_REQUIREMENT)
            return False
        return True

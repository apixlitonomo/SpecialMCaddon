"""Older Bedrock imports get today's culling and backface shading when a file opens.

Their shader cut back faces whatever Material > Backface Culling said, so
turning it off did nothing; and double-sided faces forced their outward normal
when seen from behind, which Cycles shades black. Opening a file (or enabling
Sparkle) gives those materials the driven cut and the plain normal new imports
get; nothing else about them changes.
"""

import bpy

from .bedrock_mcblend.materials import follow_backface_culling_setting


_FORCED_NORMAL_NODES = (
    "Bedrock Two-Sided Geometry", "Bedrock Backface Normal Sign", "Bedrock Two-Sided Normal",
)


def drop_forced_backface_normal(material):
    """Remove the outward normal older imports forced on faces seen from behind."""
    tree = material.node_tree if material.use_nodes else None
    if tree is None or tree.nodes.get("Bedrock Two-Sided Normal") is None:
        return False
    for name in _FORCED_NORMAL_NODES:
        node = tree.nodes.get(name)
        if node is not None:
            tree.nodes.remove(node)
    material["mcbedrock_two_sided_lighting"] = False
    return True


def upgrade():
    """Upgrade every local Bedrock material in the open file. Returns how many changed."""
    changed = 0
    for material in bpy.data.materials:
        if material.library is not None or "mcbedrock_backface_culling" not in material:
            continue
        culling = bool(material.get("mcbedrock_backface_culling")) and follow_backface_culling_setting(material)
        changed += bool(culling | drop_forced_backface_normal(material))
    return changed


@bpy.app.handlers.persistent
def _loaded(*_args):
    upgrade()


def _open_file():
    upgrade()
    return None


def register():
    if _loaded not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_loaded)
    # The file that is open when Sparkle is enabled.
    if not bpy.app.timers.is_registered(_open_file):
        bpy.app.timers.register(_open_file, first_interval=0.)


def unregister():
    if _loaded in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_loaded)
    if bpy.app.timers.is_registered(_open_file):
        bpy.app.timers.unregister(_open_file)

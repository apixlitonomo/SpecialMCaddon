"""Put lights back the way they were before the Light Mixer, which was removed.

The Light Mixer (removed 2026-10-16) scaled, tinted, muted and soloed lights by
writing new values onto them and remembering what lay underneath. A file saved
with a mix kept those values, so without the mixer a muted light would stay
dark and an "All lights" change would stay baked in. When a file opens, every
value the mixer wrote is replaced by the light's own value (or, where the light
was edited by hand after mixing, by that edit without the mix), and the mixer's
bookkeeping is removed. Files without a mix are not touched.
"""
import bpy

ENERGY, APPLIED, FACTOR = 'mc_mix_energy', 'mc_mix_applied', 'mc_mix_factor'
COLOR, APPLIED_COLOR, TINT = 'mc_mix_color', 'mc_mix_applied_color', 'mc_mix_tint'
DATA_KEYS = (ENERGY, APPLIED, FACTOR, COLOR, APPLIED_COLOR, TINT)
OBJECT_KEYS = ('mc_mix_mute', 'mc_mix_solo', 'mc_light_group')
MIXER = 'mcbedrock_light_mixer'


def _unmixed(current, base, applied, factor):
    """The value under the mix: what the mixer last wrote is the base; a later hand
    edit is an edit of the mixed value, so the mix factor is taken back out."""
    if applied is not None and abs(current - applied) <= 1e-6 * max(1., abs(current)):
        return base
    return current / factor if factor is not None and factor > 1e-6 else current


def restore(owner, value):
    """(new value or None) for one mixed scalar on owner, and its bookkeeping removed."""
    if ENERGY not in owner:
        return None
    return _unmixed(value, owner[ENERGY], owner.get(APPLIED), owner.get(FACTOR))


def _forget(owner, keys):
    for key in keys:
        if key in owner:
            del owner[key]


def restore_light(data):
    if not any(key in data for key in DATA_KEYS):
        return False
    energy = restore(data, data.energy)
    if energy is not None:
        data.energy = energy
    if COLOR in data:
        current = tuple(data.color)
        applied = data.get(APPLIED_COLOR)
        if applied is not None and all(abs(a - b) <= 1e-6 for a, b in zip(current, applied)):
            data.color = tuple(data[COLOR])
        else:
            tint = tuple(data.get(TINT, (1., 1., 1.)))
            data.color = tuple(min(1., c / t) if t > 1e-6 else c for c, t in zip(current, tint))
    _forget(data, DATA_KEYS)
    return True


def clean(*_args):
    """Undo every light mix in the open file. Returns how many lights and sky lights changed."""
    changed = 0
    for data in bpy.data.lights:
        if data.library is None and restore_light(data):
            changed += 1
    for world in bpy.data.worlds:
        if world.library is not None or world.node_tree is None:
            continue
        for node in world.node_tree.nodes:
            if ENERGY not in node or node.type != 'BACKGROUND':
                continue
            strength = node.inputs['Strength']
            value = restore(node, strength.default_value)
            if value is not None and not strength.is_linked:
                strength.default_value = value
            _forget(node, DATA_KEYS)
            changed += 1
    for obj in bpy.data.objects:
        if obj.library is None:
            _forget(obj, OBJECT_KEYS)
    for scene in bpy.data.scenes:
        if scene.library is None and MIXER in scene:
            del scene[MIXER]
    return changed


@bpy.app.handlers.persistent
def _loaded(*_args):
    clean()


def _open_file():
    clean()
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

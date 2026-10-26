"""Remember the last committed panel size independently of blend files."""
import json
import math
import os
import tempfile
from pathlib import Path

import bpy


def storage_path():
    root = bpy.utils.user_resource('CONFIG', path='mcbedrock_particles', create=True)
    if not root:
        raise OSError('Blender configuration directory is unavailable')
    return Path(root) / 'panel_size.json'


def dimensions(state):
    result = {}
    for key, minimum in (('_panel_width', 240.), ('_panel_height', 180.)):
        value = state.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= minimum:
            result[key] = float(value)
    return result


def load_size():
    try:
        data = json.loads(storage_path().read_text(encoding='utf-8'))
        return dimensions(data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_size(state):
    temporary = None
    try:
        path = storage_path()
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = stream.name
            json.dump(dimensions(state), stream)
        os.replace(temporary, path)
        return True
    except OSError as error:
        print(f'Sparkle: could not save panel size: {error}')
        return False
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)

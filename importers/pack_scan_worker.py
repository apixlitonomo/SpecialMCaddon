"""Refresh installed-pack metadata away from the interactive Blender process."""
import importlib
from pathlib import Path
import sys

import bpy

addon_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(addon_root.parent))
addon = importlib.import_module(addon_root.name)
addon.register()
try:
    args = sys.argv[sys.argv.index('--') + 1:]
    bpy.context.scene.mcbedrock_scan_depth = int(args[0])
    bpy.context.scene.mcbedrock_include_archives = bool(int(args[1]))
    bpy.ops.mcbedrock.scan_resource_packs()
finally:
    addon.unregister()

bl_info = {
    "name": "Sparkle",
    "author": "MrRobot and TylerBits",
    "version": (1, 3, 1),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > Sparkle",
    "description": "A minecraft blender suite",
    "warning": "",
    "doc_url": "",
    "category": "Import-Export",
}
# Blender deletes bl_info from extension modules after loading; keep the version.
ADDON_VERSION = tuple(bl_info["version"])


try:
    import bpy
    from bpy.app.handlers import persistent
    from bpy.types import AddonPreferences
    from bpy_extras.io_utils import ExportHelper, ImportHelper
except ImportError:
    bpy = None
    ExportHelper = object
    ImportHelper = object

    def persistent(function):
        return function

if "materials" in locals():
    import importlib
    importlib.reload(materials)
    importlib.reload(molang_parser)
    importlib.reload(parser)
    importlib.reload(java_parser)
    importlib.reload(simulator)
    importlib.reload(builder)
    if "bedrock_builder" in locals():
        importlib.reload(bedrock_builder)
    if "asset_browser" in locals():
        importlib.reload(asset_browser)
    else:
        from .ui import asset_browser
    if "pack_manager" in locals():
        importlib.reload(pack_manager)
    else:
        from .ui import pack_manager
    if "schematic_importer" in locals():
        importlib.reload(schematic_importer)
    else:
        from .ui import schematic_importer
    if "custom_ui" in locals():
        importlib.reload(custom_ui)
    else:
        from .ui import custom_ui
else:
    from . import materials
    from . import molang_parser
    from . import parser
    from . import java_parser
    from . import simulator
    from . import builder
    from .builders import bedrock_builder
    from .ui import asset_browser
    from .ui import custom_ui
    from .ui import pack_manager
    from .ui import schematic_importer

from .importers import session_state


def _particle_control_request(operator):
    return {
        "mode": str(getattr(
            operator, "emission_mode", builder.EMISSION_SOURCE
        ) or builder.EMISSION_SOURCE),
        "burst_count": int(getattr(operator, "burst_count", 10)),
        "rate": float(getattr(operator, "emission_rate", 10.0)),
        "max_active": int(getattr(operator, "max_active", 100)),
        "motion_mode": str(getattr(
            operator, "motion_mode", builder.MOTION_SOURCE
        ) or builder.MOTION_SOURCE),
        "vertical_speed": float(getattr(
            operator, "vertical_speed", 1.0
        )),
        "vertical_acceleration": float(getattr(
            operator, "vertical_acceleration", 0.0
        )),
        "lifetime_mode": str(getattr(
            operator, "lifetime_mode", builder.LIFETIME_SOURCE
        ) or builder.LIFETIME_SOURCE),
        "collision_mode": str(getattr(
            operator, "collision_mode", builder.COLLISION_SOURCE
        ) or builder.COLLISION_SOURCE),
        "random_seed": int(getattr(operator, "random_seed", 0)),
    }


def _draw_particle_import_controls(layout, operator):
    emission = layout.box()
    emission.label(text="Emission", icon='PARTICLES')
    emission.prop(operator, "emission_mode", text="Mode")
    if operator.emission_mode == builder.EMISSION_BURST:
        emission.prop(operator, "burst_count")
    elif operator.emission_mode == builder.EMISSION_CONTINUOUS:
        emission.prop(operator, "emission_rate")
        emission.prop(operator, "max_active")

    motion = layout.box()
    motion.label(text="Motion & Lifetime", icon='FORCE_FORCE')
    motion.prop(operator, "motion_mode", text="Vertical Motion")
    if operator.motion_mode != builder.MOTION_SOURCE:
        motion.prop(operator, "vertical_speed")
        motion.prop(operator, "vertical_acceleration")
    motion.prop(operator, "lifetime_mode")
    motion.prop(operator, "collision_mode")
    motion.prop(operator, "random_seed")


def _particle_import_property_annotations():
    return {
        "emission_mode": bpy.props.EnumProperty(
            name="Emission",
            description="Use source emission or replace the root emitter schedule",
            items=(
                (builder.EMISSION_SOURCE, "Authored", "Use the particle file's authored emission"),
                (builder.EMISSION_BURST, "Burst", "Emit a fixed number once at the clip start"),
                (builder.EMISSION_CONTINUOUS, "Continuous", "Emit at a stable rate through the clip"),
            ),
            default=builder.EMISSION_SOURCE,
        ),
        "burst_count": bpy.props.IntProperty(
            name="Particle Count", default=10, min=0,
        ),
        "emission_rate": bpy.props.FloatProperty(
            name="Particles / Second", default=10.0, min=0.0,
        ),
        "max_active": bpy.props.IntProperty(
            name="Max Active", default=100, min=1,
        ),
        "motion_mode": bpy.props.EnumProperty(
            name="Vertical Motion",
            description="Preserve authored motion or override only the world vertical axis",
            items=(
                (builder.MOTION_SOURCE, "Authored", "Use authored particle motion"),
                (builder.MOTION_RISE, "Rise", "Force particles upward"),
                (builder.MOTION_FALL, "Fall", "Force particles downward"),
                (builder.MOTION_CUSTOM, "Custom", "Use signed vertical speed and acceleration"),
            ),
            default=builder.MOTION_SOURCE,
        ),
        "vertical_speed": bpy.props.FloatProperty(
            name="Vertical Speed", default=1.0,
            description="World-space vertical speed in blocks per second",
        ),
        "vertical_acceleration": bpy.props.FloatProperty(
            name="Vertical Acceleration", default=0.0,
            description="World-space vertical acceleration in blocks per second squared",
        ),
        "lifetime_mode": bpy.props.EnumProperty(
            name="Lifetime",
            items=(
                (builder.LIFETIME_SOURCE, "Authored", "Use the authored particle lifetime"),
                (builder.LIFETIME_CLIP, "Through Clip", "Keep particles alive through the selected clip"),
            ),
            default=builder.LIFETIME_SOURCE,
        ),
        "collision_mode": bpy.props.EnumProperty(
            name="Collisions",
            items=(
                (builder.COLLISION_SOURCE, "Authored", "Use authored collision and expiration behavior"),
                (builder.COLLISION_IGNORE, "Ignore", "Do not collide with or expire against the world"),
            ),
            default=builder.COLLISION_SOURCE,
        ),
        "random_seed": bpy.props.IntProperty(
            name="Simulation Seed",
            description="Zero chooses a fresh Blockbench-style seed; nonzero values reproduce a preview exactly",
            default=0,
            min=0,
            max=2147483647,
        ),
    }


class MCBEDROCK_OT_import_brarchive_dialog(bpy.types.Operator):
    """Select a JSON file to extract from the .brarchive"""
    bl_idname = "mcbedrock.import_brarchive_dialog"
    bl_label = "Select Particle"
    bl_options = {'REGISTER', 'UNDO'}
    __annotations__ = _particle_import_property_annotations()


    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    manual_texture: bpy.props.StringProperty(default="")
    force_loop: bpy.props.BoolProperty(default=False)
    interpolation: bpy.props.StringProperty(default="Closest")

    def get_brarchive_items(self, context):
        if not hasattr(bpy.types.Scene, "_brarchive_cache_file") or bpy.types.Scene._brarchive_cache_file != self.filepath:
            import sys
            import os
            # Avoid direct import to prevent reloading issues, use the module
            from .importers.brarchive_reader import get_brarchive_entries
            try:
                entries = get_brarchive_entries(self.filepath)
                # Items must be (identifier, name, description)
                bpy.types.Scene._brarchive_cache_entries = [(e, e, "") for e in entries]
            except Exception as e:
                print(f"Brarchive parse error: {e}")
                bpy.types.Scene._brarchive_cache_entries = [("ERROR", "Error parsing .brarchive", "")]
            bpy.types.Scene._brarchive_cache_file = self.filepath

        return bpy.types.Scene._brarchive_cache_entries

    selected_entry: bpy.props.EnumProperty(
        name="Particle",
        description="Select the particle to import from the archive",
        items=get_brarchive_items,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "selected_entry")
        layout.prop(self, "manual_texture")
        layout.prop(self, "force_loop")
        layout.prop(self, "interpolation")
        _draw_particle_import_controls(layout, self)

    def execute(self, context):
        import os
        import tempfile
        from .importers.brarchive_reader import extract_brarchive
        from . import builder

        if self.selected_entry == "ERROR" or not self.selected_entry:
            self.report({'ERROR'}, "Failed to parse archive or no entry selected")
            return {'CANCELLED'}

        try:
            with tempfile.TemporaryDirectory(
                prefix="mcbedrock_archive_"
            ) as temp_dir:
                extracted = extract_brarchive(self.filepath, temp_dir)
                temp_filepath = extracted.get(self.selected_entry)
                if not temp_filepath:
                    raise FileNotFoundError(
                        f"Archive entry not found: {self.selected_entry}"
                    )
                result = builder.build_particle_system(
                    context,
                    temp_filepath,
                    texture_override=self.manual_texture,
                    spawn_offset=tuple(context.scene.cursor.location),
                    force_loop=self.force_loop,
                    interpolation=self.interpolation,
                    particle_controls=_particle_control_request(self),
                )
            emitter = result.get("emitter") if result else None
            if emitter:
                builder.stamp_emitter_metadata(
                    emitter,
                    source_path=self.filepath,
                    texture_override=self.manual_texture,
                    force_loop=self.force_loop,
                    interpolation=self.interpolation,
                    effect_identifier=emitter.get(
                        "mcbedrock_effect_identifier", ""
                    ),
                    source_kind="brarchive",
                    logical_entry=self.selected_entry,
                    backend=emitter.get(
                        "mcbedrock_backend",
                        builder.BACKEND_LEGACY_OBJECTS,
                    ),
                )
                update_scales(emitter, context)
                builder.apply_particle_preview_frame(context, result)
            self.report(
                {'INFO'},
                f"Successfully imported {self.selected_entry}",
            )
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {str(e)}")
            return {'CANCELLED'}


class MCBEDROCK_OT_import_particle_dialog(bpy.types.Operator):
    """Import a Bedrock particle with settings"""
    bl_idname = "mcbedrock.import_particle_dialog"
    bl_label = "Particle Settings"
    bl_options = {'REGISTER', 'UNDO'}
    __annotations__ = _particle_import_property_annotations()



    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    manual_texture: bpy.props.StringProperty(
        name="Texture Override",
        description="(Optional) Manually select a .png texture",
        subtype='FILE_PATH',
        default=""
    )
    force_loop: bpy.props.BoolProperty(
        name="Force Loop",
        description="Ignore emitter lifetimes and force particles to loop endlessly",
        default=False
    )
    interpolation: bpy.props.EnumProperty(
        name="Interpolation",
        items=[('Closest', "Closest", ""), ('Linear', "Linear", "")],
        default="Closest"
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "manual_texture")
        layout.prop(self, "force_loop")
        layout.prop(self, "interpolation")
        _draw_particle_import_controls(layout, self)

    def execute(self, context):
        from . import builder
        try:
            result = builder.build_particle_system(
                context,
                self.filepath,
                texture_override=bpy.path.abspath(self.manual_texture) if self.manual_texture else "",
                spawn_offset=tuple(context.scene.cursor.location),
                force_loop=self.force_loop,
                interpolation=self.interpolation,
                particle_controls=_particle_control_request(self),
            )
            builder.apply_particle_preview_frame(context, result)
            self.report({'INFO'}, f"Successfully imported particle")
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {str(e)}")
        return {'FINISHED'}


class MCBEDROCK_OT_import_particle(bpy.types.Operator, ImportHelper):
    """Import a Minecraft Bedrock or Java Particle JSON file"""
    bl_idname = "mcbedrock.import_particle"
    bl_label = "Import Particle JSON"
    bl_options = {'REGISTER', 'UNDO'}
    __annotations__ = _particle_import_property_annotations()


    filter_glob: bpy.props.StringProperty(
        default="*.json;*.brarchive",
        options={'HIDDEN'},
        maxlen=255,
    )

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    manual_texture: bpy.props.StringProperty(
        name="Manual Texture Override",
        description="(Optional) Manually select a .png texture to use instead of auto-resolving from the JSON",
        subtype='FILE_PATH',
        default="",
    )

    force_loop: bpy.props.BoolProperty(
        name="Force Endless Loop",
        description="Override 'once' lifetime and force the particle to loop continuously",
        default=False,
    )

    interpolation: bpy.props.EnumProperty(
        name="Texture Interpolation",
        description="Choose how textures are rendered. Closest (Pixel Art) or Linear (Foggy/Soft)",
        items=[
            ('Closest', "Closest (Pixel Art)", "Minecraft default, sharp pixelated edges"),
            ('Linear', "Linear (Soft/Foggy)", "Soft edges, good for glows, smoke, tractor beams"),
        ],
        default='Closest',
    )

    def execute(self, context):
        import os
        try:
            tex_override = bpy.path.abspath(self.manual_texture) if self.manual_texture else ""
            warnings = []

            filepaths = [
                os.path.join(self.directory, item.name)
                for item in self.files
            ]
            if not filepaths and self.filepath:
                filepaths = [bpy.path.abspath(self.filepath)]

            for filepath in filepaths:

                if filepath.endswith('.brarchive'):
                    bpy.ops.mcbedrock.import_brarchive_dialog(
                        'INVOKE_DEFAULT',
                        filepath=filepath,
                        manual_texture=tex_override,
                        force_loop=self.force_loop,
                        interpolation=self.interpolation,
                        emission_mode=self.emission_mode,
                        burst_count=self.burst_count,
                        emission_rate=self.emission_rate,
                        max_active=self.max_active,
                        motion_mode=self.motion_mode,
                        vertical_speed=self.vertical_speed,
                        vertical_acceleration=self.vertical_acceleration,
                        lifetime_mode=self.lifetime_mode,
                        collision_mode=self.collision_mode,
                        random_seed=self.random_seed,
                    )
                    continue

                result = builder.build_particle_system(
                    context,
                    filepath,
                    texture_override=tex_override,
                    spawn_offset=tuple(context.scene.cursor.location),
                    force_loop=self.force_loop,
                    interpolation=self.interpolation,
                    particle_controls=_particle_control_request(self),
                )

                emitter = result.get("emitter") if result else None
                if emitter:
                    # The builder owns v3 source/backend metadata. Apply the
                    # same live-control contract used by existing scenes.
                    update_scales(emitter, context)
                    builder.apply_particle_preview_frame(context, result)

                if result and result.get("warning"):
                    warnings.append(f"{os.path.basename(filepath)}: {result['warning']}")

            if warnings:
                self.report({'WARNING'}, " | ".join(warnings))
            else:
                self.report({'INFO'}, f"Successfully imported {len(filepaths)} particles")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to import particles: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "manual_texture")
        layout.label(text="Leave blank to auto-resolve from resource pack.", icon='INFO')
        layout.separator()
        layout.prop(context.scene, "mcbedrock_bake_frames", text="Bake Frames")
        layout.prop(self, "force_loop")
        layout.separator()
        layout.prop(self, "interpolation")
        _draw_particle_import_controls(layout, self)


_QUICK_IMPORT_ENUM_ITEMS = {}
_QUICK_GEO_SCAN_STATE = {}
_QUICK_JSON_ASSET_TYPES = {}


def _quick_clip_label(identifier):
    name = str(identifier or "").split(".")[-1]
    name = name.replace("_", " ").replace("-", " ").strip()
    return name.title() if name else "Animation"


def _quick_geo_animation_items(self, context):
    import os
    import threading

    empty_items = [(
        "__NONE__", "No animation",
        "Import the geometry in its authored rest pose",
    )]
    filepath = bpy.path.abspath(getattr(self, "filepath", "") or "")
    enum_key = ("geo", filepath)
    if not filepath.lower().endswith(".json") or not os.path.isfile(filepath):
        _QUICK_IMPORT_ENUM_ITEMS[enum_key] = empty_items
        return _QUICK_IMPORT_ENUM_ITEMS[enum_key]
    if _quick_json_asset_type(filepath) != "model":
        _QUICK_IMPORT_ENUM_ITEMS[enum_key] = empty_items
        return _QUICK_IMPORT_ENUM_ITEMS[enum_key]

    preferred_root = (
        getattr(context.scene, "mcbedrock_resource_pack_path", "")
        if context else ""
    )
    preferred_root = bpy.path.abspath(preferred_root) if preferred_root else ""
    try:
        source_mtime = os.path.getmtime(filepath)
    except OSError:
        source_mtime = 0.0
    scan_key = (
        os.path.normcase(os.path.abspath(filepath)),
        source_mtime,
        os.path.normcase(os.path.abspath(preferred_root)) if preferred_root else "",
        str(getattr(self, "geometry_identifier", "") or "").casefold(),
        int(getattr(self, "geometry_index", 0) or 0),
    )
    state = _QUICK_GEO_SCAN_STATE.get(scan_key)
    if state and state.get("status") == "done":
        _QUICK_IMPORT_ENUM_ITEMS[enum_key] = state["items"]
        return _QUICK_IMPORT_ENUM_ITEMS[enum_key]
    if state and state.get("status") == "error":
        _QUICK_IMPORT_ENUM_ITEMS[enum_key] = empty_items
        return _QUICK_IMPORT_ENUM_ITEMS[enum_key]

    if state is None:
        state = {"status": "loading", "items": empty_items}
        _QUICK_GEO_SCAN_STATE[scan_key] = state
        geometry_identifier = str(
            getattr(self, "geometry_identifier", "") or ""
        )
        geometry_index = int(getattr(self, "geometry_index", 0) or 0)

        def scan_worker():
            try:
                from .importers.bedrock_assets import geometry_animation_clips

                clips = geometry_animation_clips(
                    filepath,
                    preferred_root,
                    geometry_identifier,
                    geometry_index,
                )
                items = list(empty_items)
                items.extend((
                    clip["identifier"],
                    _quick_clip_label(clip["identifier"]),
                    f'{clip["identifier"]} ({clip["matching_bones"]} matching bones)',
                ) for clip in clips)
                if clips:
                    items.append((
                        "__ALL__", "All animations",
                        "Import all matching clips as a sequential playable NLA preview",
                    ))
                state["items"] = items
                state["status"] = "done"
            except Exception as exc:
                state["error"] = str(exc)
                state["status"] = "error"

        threading.Thread(
            target=scan_worker,
            name="MCBedrockGeoAnimationScan",
            daemon=True,
        ).start()

        def redraw_when_ready():
            if state.get("status") == "loading":
                return 0.1
            try:
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == "FILE_BROWSER":
                            area.tag_redraw()
            except Exception:
                pass
            return None

        bpy.app.timers.register(redraw_when_ready, first_interval=0.1)

    loading_items = list(empty_items)
    loading_items.append((
        "__LOADING__",
        "Loading matching animations...",
        "Animation files are being indexed in the background",
    ))
    _QUICK_IMPORT_ENUM_ITEMS[enum_key] = loading_items
    return _QUICK_IMPORT_ENUM_ITEMS[enum_key]


def _quick_json_asset_type(filepath):
    import os

    try:
        modified = os.path.getmtime(filepath)
    except OSError:
        modified = 0.0
    key = (os.path.normcase(os.path.abspath(filepath)), modified)
    if key not in _QUICK_JSON_ASSET_TYPES:
        try:
            from .importers.bedrock_assets import detect_json_asset
            _QUICK_JSON_ASSET_TYPES[key] = detect_json_asset(filepath)
        except Exception:
            _QUICK_JSON_ASSET_TYPES[key] = "unknown"
    return _QUICK_JSON_ASSET_TYPES[key]


def _quick_bbmodel_animation_items(self, context):
    filepath = bpy.path.abspath(getattr(self, "filepath", "") or "")
    if not filepath.lower().endswith(".bbmodel") or not __import__("os").path.isfile(filepath):
        return [("__ALL__", "All animations", "Import every animation stored in the project")]
    try:
        from .importers.bbmodel import animation_items
        items = animation_items(filepath)
        all_item = next((item for item in items if item[0] == "__ALL__"), None)
        if all_item is not None:
            items.remove(all_item)
            items.insert(0, all_item)
        _QUICK_IMPORT_ENUM_ITEMS[("bbmodel", filepath)] = items
        return _QUICK_IMPORT_ENUM_ITEMS[("bbmodel", filepath)]
    except Exception:
        return [("__ALL__", "All animations", "Import every readable animation stored in the project")]


class MCBEDROCK_OT_import_asset_json(bpy.types.Operator, ImportHelper):
    """Import Minecraft JSON assets or a Blockbench project"""
    bl_idname = "mcbedrock.import_asset_json"
    bl_label = "Import Model/Entity/Blockbench"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: bpy.props.StringProperty(
        default="*.json;*.bbmodel",
        options={'HIDDEN'},
        maxlen=255,
    )

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    manual_texture: bpy.props.StringProperty(
        name="Manual Texture Override",
        description="Optional texture for imported model/entity meshes",
        subtype='FILE_PATH',
        default="",
    )

    geometry_identifier: bpy.props.StringProperty(
        name="Geometry Identifier",
        description="Optional Bedrock geometry identifier to import from multi-geometry model files",
        default="",
        options={'HIDDEN'},
    )

    geometry_index: bpy.props.IntProperty(
        name="Geometry Index",
        description="Optional Bedrock geometry index to import from multi-geometry model files",
        default=0,
        options={'HIDDEN'},
    )

    display_name: bpy.props.StringProperty(
        name="Display Name",
        description="Friendly asset-browser name to use for imported Blender objects",
        default="",
        options={'HIDDEN'},
    )

    animation_filter: bpy.props.StringProperty(
        name="Animation Filter",
        description="Animation identifier selected by the asset browser, or __NONE__ for rest pose",
        default="__NONE__",
        options={'HIDDEN'},
    )

    quick_geo_animation: bpy.props.EnumProperty(
        name="Animation / Cutscene",
        description="Animation to apply when quickly importing a loose Bedrock geometry file",
        items=_quick_geo_animation_items,
    )

    bbmodel_animation: bpy.props.EnumProperty(
        name="Blockbench Animation",
        description="Animation content to import from the Blockbench project",
        items=_quick_bbmodel_animation_items,
    )

    pose_preset: bpy.props.EnumProperty(
        name="Pose Mode",
        description="Choose the in-game neutral assembly or raw authored geometry",
        items=[
            ('DEFAULT_IDLE', "In-game Neutral", "Deterministic renderer setup and safe idle-controller output"),
            ('REST_BIND', "Raw Authored", "Geometry and bind transforms without renderer assembly or animation"),
        ],
        default='DEFAULT_IDLE',
        options={'HIDDEN'},
    )

    import_all_variants: bpy.props.BoolProperty(
        name="Import All Geometry Variants",
        description="Import every geometry/pose variant referenced by render controllers instead of the single static-pose selection",
        default=False,
        options={'HIDDEN'},
    )

    preview_variant: bpy.props.IntProperty(
        name="Entity Variant", default=0, min=0, options={'HIDDEN'},
    )
    preview_mark_variant: bpy.props.IntProperty(
        name="Marking Variant", default=0, min=0, options={'HIDDEN'},
    )
    preview_armor_slot: bpy.props.IntProperty(
        name="Armor Texture Slot", default=0, min=0, options={'HIDDEN'},
    )
    preview_is_baby: bpy.props.BoolProperty(
        name="Baby", default=False, options={'HIDDEN'},
    )
    preview_is_saddled: bpy.props.BoolProperty(
        name="Saddled", default=False, options={'HIDDEN'},
    )
    preview_has_rider: bpy.props.BoolProperty(
        name="Has Rider", default=False, options={'HIDDEN'},
    )
    preview_is_chested: bpy.props.BoolProperty(
        name="Chested / Storage", default=False, options={'HIDDEN'},
    )
    use_entity_preview_state: bpy.props.BoolProperty(
        name="Use Entity Preview State", default=False, options={'HIDDEN'},
    )
    entity_effects: bpy.props.EnumProperty(
        name="Entity Effects",
        items=[
            ('PREVIEW', "Preview", "Import deterministic controller/timeline particle effects"),
            ('OFF', "Off", "Do not import entity-driven particles"),
        ],
        default='PREVIEW',
        options={'HIDDEN'},
    )

    interpolation: bpy.props.EnumProperty(
        name="Texture Interpolation",
        description="Choose how model textures are rendered",
        items=[
            ('Closest', "Closest (Pixel Art)", "Minecraft default, sharp pixelated edges"),
            ('Linear', "Linear (Soft)", "Soft texture interpolation"),
        ],
        default='Closest',
    )

    append_animation_nla: bpy.props.BoolProperty(
        name="Store Animations as Muted NLA Tracks",
        description="Also place imported animation actions into muted NLA tracks for easier browsing",
        default=True,
    )

    def execute(self, context):
        import os
        from .importers.bedrock_assets import (
            detect_json_asset,
            geometry_animation_clips,
        )
        from .builders import bedrock_builder
        from .builders.bedrock_mcblend.animation import arrange_nla_tracks_for_preview

        tex_override = bpy.path.abspath(self.manual_texture) if self.manual_texture else ""
        imported = []
        warnings = []
        active_armature = context.active_object if context.active_object and context.active_object.type == 'ARMATURE' else None
        previous_objects=set(context.scene.objects)
        import_cursor=context.scene.cursor.location.copy()

        try:
            selected_files = list(self.files) or [None]
            for sel_file in selected_files:
                filepath = os.path.join(self.directory, sel_file.name) if sel_file else self.filepath
                if filepath.lower().endswith(".bbmodel"):
                    from .builders.bbmodel_builder import import_bbmodel
                    result = import_bbmodel(
                        context,
                        filepath,
                        texture_override=tex_override,
                        interpolation=self.interpolation,
                        display_name=self.display_name,
                        animation_filter=self.bbmodel_animation,
                        append_animation_nla=self.append_animation_nla,
                    )
                    active_armature = result["armature"]
                    imported.append(
                        f"{os.path.basename(filepath)} "
                        f"({len(result.get('animations', []))} animations)"
                    )
                    continue
                asset_type = detect_json_asset(filepath)
                entity_context = None
                if self.use_entity_preview_state:
                    entity_context = {
                        "variant": int(self.preview_variant),
                        "mark_variant": int(self.preview_mark_variant),
                        "armor_texture_slot": int(self.preview_armor_slot),
                        "is_baby": bool(self.preview_is_baby),
                        "is_saddled": bool(self.preview_is_saddled),
                        "has_rider": bool(self.preview_has_rider),
                        "is_chested": bool(self.preview_is_chested),
                    }

                if asset_type == "model":
                    contextual_entity = ""
                    if entity_context and self.geometry_identifier:
                        from .importers.bedrock_assets import find_contextual_entity_file
                        pack_root = context.scene.mcbedrock_resource_pack_path or self.directory
                        contextual_entity = find_contextual_entity_file(
                            bpy.path.abspath(pack_root),
                            self.geometry_identifier,
                            self.display_name,
                        )
                    if contextual_entity:
                        result = bedrock_builder.import_client_entity(
                            context,
                            contextual_entity,
                            texture_override=tex_override,
                            interpolation=self.interpolation,
                            display_name=self.display_name,
                            animation_filter=self.animation_filter,
                            import_profile="rest",
                            entity_context=entity_context,
                            entity_effects=self.entity_effects,
                            pose_preset=self.pose_preset,
                        )
                        active_armature = result["armature"]
                        imported.append(os.path.basename(filepath))
                        continue
                    if not tex_override:
                        stem = os.path.splitext(os.path.basename(filepath))[0]
                        search_root = context.scene.mcbedrock_resource_pack_path or self.directory
                        for tex_root_dir, _tex_subdirs, tex_filenames in os.walk(search_root):
                            if 'textures' in tex_root_dir.replace('\\', '/').lower():
                                for tex_filename in tex_filenames:
                                    if os.path.splitext(tex_filename)[0] == stem and tex_filename.lower().endswith(('.png', '.tga', '.jpg')):
                                        tex_override = os.path.join(tex_root_dir, tex_filename)
                                        break
                                if tex_override:
                                    break

                    result = bedrock_builder.build_bedrock_model_from_file(
                        context,
                        filepath,
                        texture_path=tex_override,
                        geometry_identifier=self.geometry_identifier,
                        geometry_index=self.geometry_index,
                        interpolation=self.interpolation,
                        display_name=self.display_name,
                    )
                    active_armature = result["armature"]
                    warnings.extend(
                        f"{os.path.basename(filepath)}: {message}"
                        for message in result.get("warnings", ())
                    )
                    if self.quick_geo_animation not in {"__NONE__", "__LOADING__"}:
                        preferred_root = context.scene.mcbedrock_resource_pack_path or self.directory
                        clips = geometry_animation_clips(
                            filepath,
                            bpy.path.abspath(preferred_root) if preferred_root else "",
                            self.geometry_identifier,
                            self.geometry_index,
                        )
                        selected = clips if self.quick_geo_animation == "__ALL__" else [
                            clip for clip in clips
                            if clip["identifier"] == self.quick_geo_animation
                        ]
                        actions = []
                        for clip in selected:
                            actions.extend(bedrock_builder.apply_bedrock_animations(
                                context,
                                active_armature,
                                clip["filepath"],
                                animation_filter=clip["identifier"],
                                append_nla=(
                                    self.append_animation_nla
                                    and self.quick_geo_animation == "__ALL__"
                                ),
                            ))
                        if self.quick_geo_animation == "__ALL__" and actions:
                            arrange_nla_tracks_for_preview(context, active_armature)
                        if not actions:
                            warnings.append(
                                f"{os.path.basename(filepath)}: no compatible quick-import animation was found"
                            )
                    imported.append(os.path.basename(filepath))

                elif asset_type == "java_model":
                    from .builders import java_builder
                    result = java_builder.build_java_model_from_file(
                        context,
                        filepath,
                        interpolation=self.interpolation,
                        display_name=self.display_name,
                    )
                    imported.append(f"{os.path.basename(filepath)} ({result.get('model_id', 'Java model')})")

                elif asset_type == "client_entity":
                    entity_context = None
                    if self.use_entity_preview_state:
                        entity_context = {
                            "variant": int(self.preview_variant),
                            "mark_variant": int(self.preview_mark_variant),
                            "armor_texture_slot": int(self.preview_armor_slot),
                            "is_baby": bool(self.preview_is_baby),
                            "is_saddled": bool(self.preview_is_saddled),
                            "has_rider": bool(self.preview_has_rider),
                            "is_chested": bool(self.preview_is_chested),
                        }
                    result = bedrock_builder.import_client_entity(
                        context,
                        filepath,
                        texture_override=tex_override,
                        interpolation=self.interpolation,
                        display_name=self.display_name,
                        animation_filter=self.animation_filter,
                        import_profile="all" if self.import_all_variants else "rest",
                        controller_context={} if self.animation_filter == "__ALL__" else None,
                        entity_context=entity_context,
                        entity_effects=self.entity_effects,
                        pose_preset=self.pose_preset,
                    )
                    active_armature = result["armature"]
                    imported.append(os.path.basename(filepath))
                    if self.animation_filter != "__NONE__" and not result.get("animations"):
                        warnings.append(f"{os.path.basename(filepath)}: entity imported, no referenced animations were resolved")

                elif asset_type == "animation":
                    target = active_armature or (context.active_object if context.active_object and context.active_object.type == 'ARMATURE' else None)
                    if not target:
                        warnings.append(f"{os.path.basename(filepath)}: select/import an armature before animation JSON")
                        continue
                    actions = bedrock_builder.apply_bedrock_animations(
                        context,
                        target,
                        filepath,
                        append_nla=self.append_animation_nla,
                    )
                    imported.append(f"{os.path.basename(filepath)} ({len(actions)} animations)")

                elif asset_type == "particle":
                    warnings.append(f"{os.path.basename(filepath)}: use the existing Import Particle JSON button")

                else:
                    warnings.append(f"{os.path.basename(filepath)}: unrecognized Minecraft asset JSON")

            if warnings:
                self.report({'WARNING'}, " | ".join(warnings[:4]))
            if imported:
                from .builders.asset_placement import place_new_roots_at_cursor
                place_new_roots_at_cursor(context,previous_objects,import_cursor)
                self.report({'INFO'}, f"Imported {len(imported)} asset file(s)")
                return {'FINISHED'}
            return {'CANCELLED'}

        except Exception as e:
            self.report({'ERROR'}, f"Asset import failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

    def draw(self, context):
        import os
        layout = self.layout
        layout.prop(self, "manual_texture")
        layout.prop(self, "interpolation")
        filepath = bpy.path.abspath(self.filepath or "")
        if filepath.lower().endswith(".bbmodel"):
            layout.prop(self, "bbmodel_animation", text="Animation / Cutscene")
            layout.prop(self, "append_animation_nla")
        elif (
            filepath.lower().endswith(".json")
            and os.path.isfile(filepath)
            and _quick_json_asset_type(filepath) == "model"
        ):
            layout.prop(self, "quick_geo_animation", text="Animation / Cutscene")
            layout.prop(self, "append_animation_nla")


def _object_armature(obj):
    """Return an armature from an armature, child mesh, or parent hierarchy."""
    if obj is None:
        return None
    if getattr(obj, "type", "") == "ARMATURE":
        return obj
    try:
        armature = obj.find_armature()
    except (AttributeError, RuntimeError):
        armature = None
    if armature is not None:
        return armature
    parent = getattr(obj, "parent", None)
    while parent is not None:
        if getattr(parent, "type", "") == "ARMATURE":
            return parent
        parent = getattr(parent, "parent", None)
    return None


def _bbmodel_source(armature):
    """Recover the originating project from current and legacy import tags."""
    if armature is None:
        return ""
    source = str(armature.get("mcbedrock_source_path", "") or "")
    if source.lower().endswith(".bbmodel"):
        return source
    animation_data = getattr(armature, "animation_data", None)
    actions = []
    if animation_data is not None:
        if animation_data.action is not None:
            actions.append(animation_data.action)
        actions.extend(
            strip.action
            for track in animation_data.nla_tracks
            for strip in track.strips
            if strip.action is not None
        )
    for action in actions:
        source = str(action.get("mcbedrock_bbmodel_source", "") or "")
        if source.lower().endswith(".bbmodel"):
            return source
    return ""


def _is_bbmodel_armature(armature):
    if armature is None or getattr(armature, "type", "") != "ARMATURE":
        return False
    if _bbmodel_source(armature):
        return True
    return (
        armature.get("mcbedrock_asset_type", "") == "bbmodel"
        and bool(armature.get("mcbedrock_bbmodel_bone_by_group", ""))
    )


def _selected_armature(context):
    candidates = []

    def add(obj):
        armature = _object_armature(obj)
        if armature is not None and armature not in candidates:
            candidates.append(armature)

    add(getattr(context, "active_object", None))
    for obj in getattr(context, "selected_objects", ()) or ():
        add(obj)
    for armature in candidates:
        if _is_bbmodel_armature(armature):
            return armature

    # Bone/child selection and reopened .blend files can leave no model object
    # active. If the scene contains exactly one eligible project, use it.
    view_layer = getattr(context, "view_layer", None)
    scene_candidates = []
    for obj in getattr(view_layer, "objects", ()) or ():
        armature = _object_armature(obj)
        if (
            armature is not None
            and armature not in scene_candidates
            and _is_bbmodel_armature(armature)
        ):
            scene_candidates.append(armature)
    return scene_candidates[0] if len(scene_candidates) == 1 else None


def _active_bbmodel_armature(context):
    armature = _selected_armature(context)
    return armature if _is_bbmodel_armature(armature) else None

class MCBEDROCK_OT_export_bbmodel(bpy.types.Operator, ExportHelper):
    """Add the current Blender animation as a new Blockbench variant"""

    bl_idname = "export_scene.mcbedrock_bbmodel"
    bl_label = "Blockbench Project (.bbmodel)"
    bl_options = {'REGISTER'}

    filename_ext = ".bbmodel"
    filter_glob: bpy.props.StringProperty(
        default="*.bbmodel",
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        return _active_bbmodel_armature(context) is not None

    def invoke(self, context, event):
        import os

        armature = _active_bbmodel_armature(context)
        source = _bbmodel_source(armature)
        if source and not self.filepath:
            stem, _extension = os.path.splitext(source)
            self.filepath = f"{stem}_edited.bbmodel"
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        armature = _active_bbmodel_armature(context)
        if armature is None:
            self.report(
                {'ERROR'},
                "Select a model imported from a Blockbench .bbmodel project",
            )
            return {'CANCELLED'}
        source = _bbmodel_source(armature)
        if source and not armature.get("mcbedrock_source_path", ""):
            armature["mcbedrock_source_path"] = source
        try:
            from .exporters.bbmodel import (
                bbmodel_actions,
                export_bbmodel_animations,
            )

            animation_data = armature.animation_data
            active_action = (
                animation_data.action if animation_data else None
            )
            frame = float(context.scene.frame_current_final)
            strips = [
                strip
                for track in (
                    animation_data.nla_tracks if animation_data else []
                )
                if not track.mute
                for strip in track.strips
                if strip.action is not None
            ]
            strip = next((
                candidate for candidate in strips
                if candidate.frame_start <= frame <= candidate.frame_end
            ), None)
            if strip is None and strips:
                strip = min(
                    strips,
                    key=lambda candidate: min(
                        abs(frame - float(candidate.frame_start)),
                        abs(frame - float(candidate.frame_end)),
                    ),
                )
            nla_action = strip.action if strip is not None else None
            overlay_action = None
            if (
                active_action is not None
                and active_action.get("mcbedrock_bbmodel_source", "")
            ):
                action = active_action
            elif active_action is not None and nla_action is not None:
                action = nla_action
                overlay_action = active_action
            else:
                action = active_action or nla_action
            if action is None:
                available = bbmodel_actions(armature)
                action = available[0] if len(available) == 1 else None
            if action is None:
                raise ValueError(
                    "Select the animation to edit, or move the timeline "
                    "inside its NLA strip"
                )

            result = export_bbmodel_animations(
                armature,
                self.filepath,
                actions=[action],
                scene=context.scene,
                overlay_action=overlay_action,
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Blockbench export failed: {exc}")
            return {'CANCELLED'}
        self.report(
            {'INFO'},
            f"Exported {result['variants'][0]} with "
            f"{result['bones']} Blockbench bones",
        )
        return {'FINISHED'}


def _menu_export_bbmodel(self, _context):
    self.layout.operator(
        MCBEDROCK_OT_export_bbmodel.bl_idname,
        text="Blockbench Project (.bbmodel)",
    )


class MCBEDROCK_OT_set_resource_pack(bpy.types.Operator, ImportHelper):
    """Select the root folder of your resource pack (Bedrock or Java)"""
    bl_idname = "mcbedrock.set_resource_pack"
    bl_label = "Set Resource Pack Path"
    bl_options = {'REGISTER'}

    filter_glob: bpy.props.StringProperty(
        default="*.*",
        options={'HIDDEN'},
        maxlen=255,
    )

    directory: bpy.props.StringProperty(
        subtype='DIR_PATH',
    )

    def execute(self, context):
        import os
        folder = os.path.dirname(self.filepath) if self.filepath else self.directory
        # The legacy folder picker intentionally creates a one-pack stack. The
        # pack manager mirrors index zero back into both historical path fields
        # and refreshes the Asset Browser for backwards compatibility.
        pack_manager.set_single_active_pack(
            context,
            folder,
            name=os.path.basename(folder.rstrip("\\/")),
            source_path=folder,
        )
        result = {'FINISHED'}
        self.report({'INFO'}, f"Resource pack set and refreshed: {folder}")
        return result


class MCBEDROCK_OT_install_vanilla_pack(bpy.types.Operator, ImportHelper):
    """Install the downloaded bedrock-samples.zip permanently into the addon"""
    bl_idname = "mcbedrock.install_vanilla_pack"
    bl_label = "Install Vanilla ZIP"
    bl_options = {'REGISTER'}

    filter_glob: bpy.props.StringProperty(
        default="*.zip",
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        import os
        import zipfile

        if not self.filepath or not self.filepath.endswith('.zip'):
            self.report({'ERROR'}, "Please select a .zip file")
            return {'CANCELLED'}

        addon_dir = os.path.dirname(os.path.abspath(__file__))
        dest_dir = os.path.join(addon_dir, "vanilla_pack")

        try:
            # Create the vanilla_pack directory if it doesn't exist
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)

            self.report({'INFO'}, "Extracting ZIP, this may take a moment...")
            # Extract the zip
            with zipfile.ZipFile(self.filepath, 'r') as zip_ref:
                zip_ref.extractall(dest_dir)

            self.report({'INFO'}, "Vanilla pack installed permanently!")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to install zip: {e}")
            return {'CANCELLED'}

class MCBEDROCK_OT_set_texture(bpy.types.Operator, ImportHelper):
    """Manually pick a .png texture to apply to the selected particle emitter"""
    bl_idname = "mcbedrock.set_texture"
    bl_label = "Set Particle Texture"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: bpy.props.StringProperty(
        default="*.png;*.tga;*.jpg;*.jpeg",
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        import os
        from . import builder
        from .materials import replace_particle_texture
        from .particle_backend_v3 import update_cache_texture_uv

        emitter = _get_active_emitter(context)
        if emitter is None:
            self.report(
                {'ERROR'},
                "Select a particle emitter or one of its render objects",
            )
            return {'CANCELLED'}
        if not os.path.isfile(self.filepath):
            self.report({'ERROR'}, f"File not found: {self.filepath}")
            return {'CANCELLED'}

        render_objects = builder.particle_render_objects(emitter)
        try:
            updated = replace_particle_texture(
                render_objects,
                self.filepath,
                interpolation=emitter.get(
                    "mcbedrock_interpolation", "Closest"
                ),
            )
            uv_warnings = (
                update_cache_texture_uv(render_objects, self.filepath)
                if emitter.get("mcbedrock_backend", "")
                == builder.BACKEND_GN_CACHE_V2
                else []
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Texture replacement failed: {exc}")
            return {'CANCELLED'}
        if not updated:
            self.report(
                {'ERROR'},
                "Emitter has no particle material to update",
            )
            return {'CANCELLED'}

        emitter["mcbedrock_texture"] = os.path.abspath(self.filepath)
        update_scales(emitter, context)
        message = (
            f"Applied texture to {updated} material(s): "
            f"{os.path.basename(self.filepath)}"
        )
        self.report(
            {'WARNING'} if uv_warnings else {'INFO'},
            f"{message} | {' | '.join(uv_warnings)}"
            if uv_warnings else message,
        )
        return {'FINISHED'}


class MCBEDROCK_OT_bake_world_space(bpy.types.Operator):
    """Re-bakes the particle system using the Emitter's animation to create World Space trails"""
    bl_idname = "mcbedrock.bake_world_space"
    bl_label = "Re-Bake Simulation (World Space Trails)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        import json
        import os
        from . import builder

        emitter = _get_active_emitter(context)
        if emitter is None:
            self.report({'ERROR'}, "Select an Emitter first")
            return {'CANCELLED'}

        source_path = str(
            emitter.get("mcbedrock_source_path", "")
            or emitter.get("mcbedrock_filepath", "")
        )
        if not source_path or not os.path.isfile(source_path):
            self.report(
                {'ERROR'},
                "Original particle source not found; cannot re-bake",
            )
            return {'CANCELLED'}

        tex_override = str(emitter.get("mcbedrock_texture", ""))
        force_loop = bool(emitter.get("mcbedrock_force_loop", False))
        interpolation = str(
            emitter.get("mcbedrock_interpolation", "Closest")
        )
        context_json = str(emitter.get("mcbedrock_context_json", ""))
        try:
            molang_context = json.loads(context_json) if context_json else None
        except (TypeError, ValueError):
            molang_context = None

        try:
            # Archive-backed emitters are materialized only for this rebuild;
            # their physical archive/logical-entry identity remains on emitter.
            with builder.materialized_emitter_source(emitter) as filepath:
                result = builder.build_particle_system(
                    context,
                    filepath,
                    texture_override=tex_override,
                    spawn_offset=(0.0, 0.0, 0.0),
                    force_loop=force_loop,
                    existing_emitter=emitter,
                    interpolation=interpolation,
                    molang_context=molang_context,
                )
            update_scales(emitter, context)
            builder.apply_particle_preview_frame(context, result)
        except Exception as exc:
            self.report({'ERROR'}, f"Particle re-bake failed: {exc}")
            return {'CANCELLED'}

        if result and result.get("warning"):
            self.report({'WARNING'}, result["warning"])
        else:
            self.report(
                {'INFO'},
                "Successfully re-baked particles in World Space!",
            )
        return {'FINISHED'}


def _get_active_emitter(context):
    obj = context.active_object
    if not obj:
        return None
    if obj.name.startswith("Emitter_") or "mcbedrock_is_emitter" in obj:
        return obj
    owner_id = str(obj.get("mcbedrock_particle_owner_id", ""))
    if owner_id:
        owner = next(
            (
                candidate
                for candidate in bpy.data.objects
                if str(candidate.get("mcbedrock_emitter_id", "")) == owner_id
            ),
            None,
        )
        if owner is not None:
            return owner
    owner_name = str(obj.get("mcbedrock_particle_owner", ""))
    if owner_name:
        owner = bpy.data.objects.get(owner_name)
        if owner is not None:
            return owner
    parent = obj.parent
    if parent and (
        parent.name.startswith("Emitter_")
        or "mcbedrock_is_emitter" in parent
    ):
        return parent
    return None


class MCBEDROCK_PT_import(bpy.types.Panel):
    bl_label = "Quick Import"
    bl_idname = "MCBEDROCK_PT_import"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sparkle'
    bl_order = 5

    @classmethod
    def poll(cls, context):
        from .ui.custom_ui import legacy_ui_enabled
        return legacy_ui_enabled(context)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("mcbedrock.import_particle", text="Import Particle", icon='PARTICLES')
        col.operator("mcbedrock.import_asset_json", text="Import Model / Entity", icon='OUTLINER_OB_ARMATURE')

class MCBEDROCK_OT_particle_clip_frame(bpy.types.Operator):
    """Set a particle playback boundary to the current timeline frame."""
    bl_idname = "mcbedrock.particle_clip_frame"
    bl_label = "Set Particle Clip Frame"
    bl_options = {'REGISTER', 'UNDO'}

    boundary: bpy.props.EnumProperty(
        items=(("START", "Start", "Set the clip start"),
               ("END", "End", "Set the clip end")),
        default="START",
    )

    def execute(self, context):
        emitter = _get_active_emitter(context)
        if emitter is None:
            self.report({'ERROR'}, "Select a particle emitter")
            return {'CANCELLED'}
        frame = int(context.scene.frame_current)
        if self.boundary == "START":
            emitter.mcbedrock_start_frame = frame
        else:
            emitter.mcbedrock_end_frame = frame
        return {'FINISHED'}


class MCBEDROCK_OT_randomize_particle_seed(bpy.types.Operator):
    """Choose a new deterministic seed and rebuild the selected effect."""
    bl_idname = "mcbedrock.randomize_particle_seed"
    bl_label = "Reroll Seed"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        import secrets

        emitter = _get_active_emitter(context)
        if emitter is None:
            self.report({'ERROR'}, "Select a particle emitter")
            return {'CANCELLED'}
        emitter.mcbedrock_random_seed = secrets.randbelow(2147483647) + 1
        return bpy.ops.mcbedrock.rebuild_particle_cache()

class MCBEDROCK_OT_rebuild_particle_cache(bpy.types.Operator):
    """Apply emission settings and rebuild the selected particle cache."""
    bl_idname = "mcbedrock.rebuild_particle_cache"
    bl_label = "Apply & Rebuild"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        import json
        from . import builder

        emitter = _get_active_emitter(context)
        if emitter is None:
            self.report({'ERROR'}, "Select a particle emitter")
            return {'CANCELLED'}
        if str(emitter.get("mcbedrock_backend", "")) == "JAVA_NATIVE":
            apply_java_emission_controls(emitter, context)
            update_scales(emitter, context)
            self.report({'INFO'}, "Applied Java particle controls")
            return {'FINISHED'}

        context_json = str(emitter.get("mcbedrock_context_json", ""))
        try:
            molang_context = json.loads(context_json) if context_json else None
        except (TypeError, ValueError):
            molang_context = None
        original_frame = int(context.scene.frame_current)
        try:
            with builder.materialized_emitter_source(emitter) as filepath:
                result = builder.build_particle_system(
                    context,
                    filepath,
                    texture_override=str(emitter.get("mcbedrock_texture", "")),
                    force_loop=bool(emitter.get("mcbedrock_force_loop", False)),
                    existing_emitter=emitter,
                    interpolation=str(emitter.get("mcbedrock_interpolation", "Closest")),
                    molang_context=molang_context,
                )
            emitter.mcbedrock_emission_dirty = False
            update_scales(emitter, context)
        except Exception as exc:
            self.report({'ERROR'}, f"Particle rebuild failed: {exc}")
            return {'CANCELLED'}
        finally:
            context.scene.frame_set(original_frame)
        warning = str((result or {}).get("warning") or "").strip()
        self.report({'WARNING'} if warning else {'INFO'}, warning or "Particle cache rebuilt")
        return {'FINISHED'}


class MCBEDROCK_PT_particle_settings(bpy.types.Panel):
    bl_label = "Particle Live Settings"
    bl_idname = "MCBEDROCK_PT_particle_settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Sparkle'

    @classmethod
    def poll(cls, context):
        from .ui.custom_ui import legacy_ui_enabled
        return (
            legacy_ui_enabled(context)
            and _get_active_emitter(context) is not None
        )

    def draw(self, context):
        layout = self.layout
        emitter = _get_active_emitter(context)

        box = layout.box()
        box.label(text=emitter.name, icon='OBJECT_DATA')

        if bool(emitter.get("mcbedrock_timing_is_looping", False)):
            cycle = float(emitter.get("mcbedrock_timing_cycle_time_seconds", 0.0))
            frames = int(emitter.get("mcbedrock_timing_cycle_frames", 0))
            active = float(emitter.get("mcbedrock_timing_active_time_seconds", 0.0))
            sleep = float(emitter.get("mcbedrock_timing_sleep_time_seconds", 0.0))
            box.label(text=f"Loop: {cycle:g}s ({frames} frames)", icon='FILE_REFRESH')
            if sleep > 0.0:
                box.label(text=f"Active {active:g}s / Sleep {sleep:g}s")
        else:
            duration = float(emitter.get("mcbedrock_timing_active_time_seconds", 0.0))
            if duration > 0.0:
                box.label(text=f"Emitter duration: {duration:g}s")
        particle_life = float(emitter.get(
            "mcbedrock_timing_maximum_particle_lifetime_seconds", 0.0
        ))
        if particle_life > 0.0:
            box.label(text=f"Particle max life: {particle_life:g}s")

        playback = layout.box()
        playback.label(text="Playback", icon='TIME')
        row = playback.row(align=True)
        row.prop(emitter, "mcbedrock_start_frame", text="Start")
        op = row.operator("mcbedrock.particle_clip_frame", text="", icon='KEYFRAME_HLT')
        op.boundary = "START"
        row = playback.row(align=True)
        row.prop(emitter, "mcbedrock_end_frame", text="End")
        op = row.operator("mcbedrock.particle_clip_frame", text="", icon='KEYFRAME_HLT')
        op.boundary = "END"

        emission = layout.box()
        emission.label(text="Emission", icon='PARTICLES')
        emission.prop(emitter, "mcbedrock_emission_mode", text="Mode")
        mode = emitter.mcbedrock_emission_mode
        if mode == builder.EMISSION_BURST:
            emission.prop(emitter, "mcbedrock_burst_count", text="Particle Count")
        elif mode == builder.EMISSION_CONTINUOUS:
            emission.prop(emitter, "mcbedrock_emission_rate", text="Particles / Second")
            emission.prop(emitter, "mcbedrock_max_active", text="Max Active")
        motion = layout.box()
        motion.label(text="Motion & Lifetime", icon='FORCE_FORCE')
        motion.prop(emitter, "mcbedrock_motion_mode", text="Vertical Motion")
        if emitter.mcbedrock_motion_mode != builder.MOTION_SOURCE:
            motion.prop(emitter, "mcbedrock_vertical_speed", text="Vertical Speed")
            motion.prop(
                emitter,
                "mcbedrock_vertical_acceleration",
                text="Vertical Acceleration",
            )
        motion.prop(emitter, "mcbedrock_lifetime_mode", text="Lifetime")
        motion.prop(emitter, "mcbedrock_collision_mode", text="Collisions")
        seed = motion.row(align=True)
        seed.prop(emitter, "mcbedrock_random_seed", text="Simulation Seed")
        seed.operator("mcbedrock.randomize_particle_seed", text="", icon='FILE_REFRESH')
        motion.operator("mcbedrock.rebuild_particle_cache", icon='FILE_REFRESH')
        if emitter.mcbedrock_emission_dirty:
            warning = motion.row()
            warning.alert = True
            warning.label(text="Rebuild required", icon='ERROR')

        col = box.column(align=True)
        col.prop(emitter, "mcbedrock_animation_scale", text="Animation Scale")
        col.prop(emitter, "mcbedrock_particle_scale", text="Particle Size")

        col.separator()
        col.prop(emitter, "mcbedrock_speed_scale", text="Simulation Speed")
        col.prop(emitter, "mcbedrock_density", text="Density")

        col.separator()
        col.prop(emitter, "mcbedrock_color_tint", text="Color Tint")



from . import updater

class MCBEDROCK_OT_check_update(bpy.types.Operator):
    """Check GitHub for updates"""
    bl_idname = "mcbedrock.check_update"
    bl_label = "Check for Updates"

    def execute(self, context):
        if _update_check:
            self.report({'INFO'}, "Already checking for updates")
            return {'CANCELLED'}
        import threading

        def work():
            # Network and unzip run off Blender's thread: the download used to
            # freeze Blender ("Not Responding") until it finished.
            try:
                has_update, latest, url_or_msg = updater.check_for_update(ADDON_VERSION)
                if not has_update:
                    result = ('INFO', url_or_msg)
                else:
                    success, msg = updater.install_update(url_or_msg)
                    result = ('INFO' if success else 'ERROR', msg)
            except Exception as exc:
                result = ('ERROR', f"Update check failed: {exc}")
            _update_check[0]['result'] = result

        _update_check.append({'thread': threading.Thread(target=work, daemon=True), 'result': None})
        _update_check[0]['thread'].start()
        bpy.app.timers.register(_finish_update_check, first_interval=.5)
        self.report({'INFO'}, "Checking for updates…")
        return {'FINISHED'}


_update_check = []  # the running check: {'thread', 'result'}


def _finish_update_check():
    """Tell the user how the background update check went."""
    if not _update_check:
        return None
    if _update_check[0]['result'] is None:
        return .5
    level, message = _update_check.pop()['result']
    print(f"[Sparkle] {message}")
    try:
        bpy.context.window_manager.popup_menu(
            lambda menu, _context: menu.layout.label(text=message), title="Sparkle",
            icon='ERROR' if level == 'ERROR' else 'INFO')
    except Exception:
        pass
    return None

_model_keymaps = []
_MODEL_TRANSFORM_TOOL_IDS = frozenset({
    "builtin.move",
    "builtin.rotate",
    "builtin.scale",
    "builtin.transform",
})
_model_selection_redirecting = False
_MODEL_TOOL_POLL_INTERVAL = 0.1
_model_workspace_tool_ids = {}
_model_tool_timer_token = object()
_model_tool_timers = globals().get("_model_tool_timers", [])


def _owned_model_meshes(owner):
    """Yield deformation meshes explicitly tagged as members of the owner."""
    if owner is None or owner.type != "ARMATURE":
        return
    stack = list(owner.children)
    while stack:
        child = stack.pop()
        stack.extend(child.children)
        if (
            child.type == "MESH"
            and child.get("mcbedrock_move_with_armature", "") == owner.name
        ):
            yield child


def _owned_model_armature(obj):
    """Resolve a tagged mesh, armature, or presentation wrapper to its rig."""
    if obj is None:
        return None
    owner = None
    if obj.type == "MESH":
        owner_name = obj.get("mcbedrock_move_with_armature", "")
        candidate = bpy.data.objects.get(owner_name) if owner_name else None
        if candidate is not None and candidate.type == "ARMATURE":
            owner = candidate
    elif obj.type == "ARMATURE":
        owner = obj
    elif obj.get("mcbedrock_presentation_root", False):
        source_name = obj.get("mcbedrock_source_object", "")
        candidate = bpy.data.objects.get(source_name) if source_name else None
        if (
            candidate is not None
            and candidate.type == "ARMATURE"
            and candidate.parent is obj
        ):
            owner = candidate
        else:
            owner = next(
                (
                    child for child in obj.children
                    if child.type == "ARMATURE"
                    and child.get("mcbedrock_presentation_root", "") == obj.name
                ),
                None,
            )

    if owner is None:
        return None
    asset_type = str(owner.get("mcbedrock_asset_type", "")).lower()
    if (
        asset_type not in {"model", "client_entity"}
        and not owner.get("mcbedrock_presentation_root", "")
        and not any(_owned_model_meshes(owner))
    ):
        return None
    return owner


def _owned_model_transform_root(obj):
    """Return the one object that owns whole-model object transforms."""
    # Locators, effect helpers, and other visible imported descendants do not
    # all carry the deformation-mesh tag. Resolve them generically through
    # the nearest validated presentation wrapper in their parent hierarchy.
    ancestor = obj
    while ancestor is not None:
        if (
            ancestor.type == "EMPTY"
            and bool(ancestor.get("mcbedrock_presentation_root", False))
            and _owned_model_armature(ancestor) is not None
        ):
            return ancestor
        ancestor = ancestor.parent

    owner = _owned_model_armature(obj)
    if owner is None:
        return None
    wrapper = owner.parent
    if (
        wrapper is not None
        and wrapper.get("mcbedrock_presentation_root", False)
    ):
        return wrapper
    return owner


def _owned_model_transform_members(context, transform_root):
    """Yield visible assembly objects that make body-drag transforms possible."""
    for member in _owned_model_hierarchy(transform_root):
        if member is transform_root:
            yield member
            continue
        if bool(getattr(member, "hide_select", False)):
            continue
        try:
            visible = member.visible_get(view_layer=context.view_layer)
        except TypeError:
            try:
                visible = member.visible_get()
            except (ReferenceError, RuntimeError):
                continue
        except (ReferenceError, RuntimeError):
            continue
        if visible:
            yield member


def _activate_owned_model_transform_root(context, obj):
    """Make the whole-model root active while preserving unrelated selections."""
    transform_root = _owned_model_transform_root(obj)
    if transform_root is None:
        return None
    for member in _owned_model_transform_members(context, transform_root):
        try:
            member.select_set(True)
        except (ReferenceError, RuntimeError, TypeError):
            continue
    context.view_layer.objects.active = transform_root
    return transform_root


def _active_view3d_tool_id(context):
    """Return the active Object Mode tool without creating workspace state."""
    workspace = getattr(context, "workspace", None)
    if workspace is None:
        return ""
    try:
        tool = workspace.tools.from_space_view3d_mode(
            "OBJECT", create=False
        )
    except (AttributeError, RuntimeError, TypeError):
        return ""
    return str(getattr(tool, "idname", "") or "")


def _redirect_owned_model_viewport_selection(context):
    """Convert transform-tool child selections into presentation-root selections.

    Blender's Move/Rotate/Scale tools select the visible mesh before exposing
    their gizmo. That would let a user pull a deformation mesh away from its
    rig. This redirect is deliberately limited to Object Mode transform tools
    in a 3D View; ordinary Select tools, the Outliner, and pose/edit workflows
    retain Blender's native child-selection behavior.
    """
    global _model_selection_redirecting

    area = getattr(context, "area", None)
    if (
        _model_selection_redirecting
        or getattr(context, "mode", "") != "OBJECT"
        or area is None
        or getattr(area, "type", "") != "VIEW_3D"
        or _active_view3d_tool_id(context) not in _MODEL_TRANSFORM_TOOL_IDS
    ):
        return None

    view_layer = getattr(context, "view_layer", None)
    active = view_layer.objects.active if view_layer is not None else None
    active_root = _owned_model_transform_root(active)
    if (
        active_root is None
        or active_root.type != "EMPTY"
        or not bool(active_root.get("mcbedrock_presentation_root", False))
    ):
        return None
    # Never mutate selection during a native gizmo press on the wrapper or
    # visible armature. Changing the active object from a depsgraph callback at
    # that moment makes Blender abandon the transform and start box selection.
    # Both objects already move the complete deforming hierarchy correctly.
    if active is active_root or getattr(active, "type", "") == "ARMATURE":
        return active_root

    roots = {}
    for selected in list(getattr(context, "selected_objects", ()) or ()):
        root = _owned_model_transform_root(selected)
        if (
            root is not None
            and root.type == "EMPTY"
            and bool(root.get("mcbedrock_presentation_root", False))
        ):
            roots[root.as_pointer()] = root
    roots[active_root.as_pointer()] = active_root

    members_by_root = {
        key: tuple(_owned_model_transform_members(context, root))
        for key, root in roots.items()
    }
    needs_update = active is not active_root or any(
        not member.select_get()
        for members in members_by_root.values()
        for member in members
    )
    if not needs_update:
        return active_root

    _model_selection_redirecting = True
    try:
        # Keep visible members selected with their active root. Blender then
        # recognizes a body drag as a transform (instead of fallback box
        # selection) and automatically excludes descendants whose parent is
        # also selected, so each assembly moves exactly once.
        for members in members_by_root.values():
            for member in members:
                try:
                    member.select_set(True)
                except (ReferenceError, RuntimeError, TypeError):
                    continue
        view_layer.objects.active = active_root
    finally:
        _model_selection_redirecting = False
    return active_root


@persistent
def _redirect_owned_model_selection_handler(_scene=None, _depsgraph=None):
    """Depsgraph hook used because Blender exposes no parent-select callback."""
    if bpy is None:
        return
    try:
        _redirect_owned_model_viewport_selection(bpy.context)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        # A handler can run while a window, view layer, or selected object is
        # being destroyed. Those transient contexts must not affect Blender.
        return


def _is_owned_model_selection_handler(handler):
    return (
        handler is _redirect_owned_model_selection_handler
        or (
            getattr(handler, "__module__", "") == __name__
            and getattr(handler, "__name__", "")
            == _redirect_owned_model_selection_handler.__name__
        )
    )


def _unregister_model_selection_handler():
    global _model_selection_redirecting
    _model_selection_redirecting = False
    if bpy is None:
        return
    handlers = bpy.app.handlers.depsgraph_update_post
    for handler in tuple(handlers):
        if _is_owned_model_selection_handler(handler):
            handlers.remove(handler)


def _register_model_selection_handler():
    if bpy is None:
        return
    _unregister_model_selection_handler()
    bpy.app.handlers.depsgraph_update_post.append(
        _redirect_owned_model_selection_handler
    )


def _active_window_view3d():
    """Return the active window's first usable 3D View and window region."""
    if bpy is None:
        return None
    context = bpy.context
    window = getattr(context, "window", None)
    screen = getattr(window, "screen", None)
    if window is None or screen is None:
        return None
    area = next(
        (candidate for candidate in screen.areas
         if candidate.type == "VIEW_3D"),
        None,
    )
    if area is None:
        return None
    region = next(
        (candidate for candidate in area.regions
         if candidate.type == "WINDOW"),
        None,
    )
    if region is None:
        return None
    return window, area, region


def _snapshot_owned_model_transform_tools():
    """Record current tool state so registration never changes selection."""
    _model_workspace_tool_ids.clear()
    view = _active_window_view3d()
    if view is None:
        return
    window, area, region = view
    try:
        with bpy.context.temp_override(
            window=window, area=area, region=region
        ):
            workspace = bpy.context.workspace
            _model_workspace_tool_ids[workspace.as_pointer()] = (
                _active_view3d_tool_id(bpy.context)
            )
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return


def _poll_owned_model_transform_tool(
    _token=_model_tool_timer_token,
):
    """Catch a child that was selected before switching to a transform tool."""
    if bpy is None or _token is not _model_tool_timer_token:
        return None
    try:
        view = _active_window_view3d()
        if view is None:
            return _MODEL_TOOL_POLL_INTERVAL
        window, area, region = view
        with bpy.context.temp_override(
            window=window, area=area, region=region
        ):
            workspace = bpy.context.workspace
            key = workspace.as_pointer()
            tool_id = _active_view3d_tool_id(bpy.context)
            previous = _model_workspace_tool_ids.get(key, tool_id)
            _model_workspace_tool_ids[key] = tool_id
            if (
                tool_id in _MODEL_TRANSFORM_TOOL_IDS
                and previous not in _MODEL_TRANSFORM_TOOL_IDS
            ):
                _redirect_owned_model_viewport_selection(bpy.context)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        pass
    return _MODEL_TOOL_POLL_INTERVAL


def _unregister_model_tool_timer():
    _model_workspace_tool_ids.clear()
    if bpy is None:
        _model_tool_timers.clear()
        return
    for timer in tuple(_model_tool_timers):
        try:
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    _model_tool_timers.clear()


def _register_model_tool_timer():
    if bpy is None:
        return
    _unregister_model_tool_timer()
    _snapshot_owned_model_transform_tools()
    bpy.app.timers.register(
        _poll_owned_model_transform_tool,
        first_interval=_MODEL_TOOL_POLL_INTERVAL,
        persistent=True,
    )
    _model_tool_timers.append(_poll_owned_model_transform_tool)


def _owned_model_hierarchy(transform_root):
    """Yield a presentation wrapper (or bare rig) and all of its descendants."""
    stack = [transform_root]
    seen = set()
    while stack:
        obj = stack.pop()
        if obj in seen:
            continue
        seen.add(obj)
        yield obj
        stack.extend(obj.children)


def _expand_owned_model_selection(context):
    """Select every descendant of each owned model in the current selection."""
    roots = {}
    for obj in list(context.selected_objects):
        transform_root = _owned_model_transform_root(obj)
        if transform_root is not None:
            roots[transform_root.as_pointer()] = transform_root

    # Preserve Blender's normal behavior for unrelated selected objects while
    # extending the selection to every member of each imported assembly.
    for transform_root in roots.values():
        for member in _owned_model_hierarchy(transform_root):
            member.hide_select = False
            try:
                member.hide_set(False)
                member.hide_viewport = False
                member.select_set(True)
            except (RuntimeError, TypeError):
                continue
    return list(roots.values())


class MCBEDROCK_OT_transform_owned_model(bpy.types.Operator):
    """Transform the owning rig when an imported deformation mesh is active"""
    bl_idname = "mcbedrock.transform_owned_model"
    bl_label = "Transform Minecraft Model"
    bl_options = {"INTERNAL"}

    action: bpy.props.EnumProperty(
        items=(
            ("MOVE", "Move", "Move the complete imported model"),
            ("ROTATE", "Rotate", "Rotate the complete imported model"),
            ("SCALE", "Scale", "Scale the complete imported model"),
        ),
        options={"HIDDEN", "SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "OBJECT"
            and _owned_model_transform_root(context.active_object) is not None
        )

    def invoke(self, context, event):
        transform_root = _activate_owned_model_transform_root(
            context, context.active_object
        )
        if transform_root is None:
            return {"CANCELLED"}
        operator = {
            "MOVE": bpy.ops.transform.translate,
            "ROTATE": bpy.ops.transform.rotate,
            "SCALE": bpy.ops.transform.resize,
        }[self.action]
        return operator("INVOKE_DEFAULT")


class MCBEDROCK_OT_delete_owned_model(bpy.types.Operator):
    """Delete a complete imported model assembly from any owned selection."""
    bl_idname = "mcbedrock.delete_owned_model"
    bl_label = "Delete Minecraft Model"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "OBJECT"
            and any(
                _owned_model_transform_root(obj) is not None
                for obj in context.selected_objects
            )
        )

    def execute(self, context):
        if not _expand_owned_model_selection(context):
            return {"CANCELLED"}
        bpy.ops.object.delete(use_global=False, confirm=False)
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)


def _register_model_keymaps():
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        return
    keymap = keyconfig.keymaps.new(name="Object Mode", space_type="EMPTY")
    for key, action in (("G", "MOVE"), ("R", "ROTATE"), ("S", "SCALE")):
        item = keymap.keymap_items.new(
            "mcbedrock.transform_owned_model", key, "PRESS"
        )
        item.properties.action = action
        _model_keymaps.append((keymap, item))
    for key in ("X", "DEL"):
        item = keymap.keymap_items.new(
            "mcbedrock.delete_owned_model", key, "PRESS"
        )
        _model_keymaps.append((keymap, item))


def _unregister_model_keymaps():
    for keymap, item in reversed(_model_keymaps):
        try:
            keymap.keymap_items.remove(item)
        except Exception:
            pass
    _model_keymaps.clear()


def _redraw_mcassets_ui(_self, context):
    if context is None:
        return
    # Preference edits must preserve the page, focus and per-panel dimensions.
    context.preferences.is_dirty = True
    for session in tuple(custom_ui.runtime._ACTIVE.values()):
        session.state['_tree_dirty'] = True
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _change_native_interface(self, context):
    custom_ui.runtime.cancel_all()
    _redraw_mcassets_ui(self, context)


class MCBEDROCK_preferences(AddonPreferences):
    bl_idname = __name__

    miex_auto_workspace: bpy.props.BoolProperty(
        name="Create World Exporter Workspace", default=True,
        description="Add World Exporter alongside Layout and Modeling when opening Blender files",
    )

    ui_theme: bpy.props.EnumProperty(
        name="Theme", items=[('BLENDER', 'Follow Blender', 'Use your Blender theme'),
                             ('ORIGINAL_DARK', 'Original Dark', 'Use the original dark appearance')],
        default='BLENDER', update=_redraw_mcassets_ui,
    )
    adaptive_ui_scale: bpy.props.BoolProperty(
        name="Fit Interface to Viewport", default=True,
        description="Use a more compact interface in shorter viewports",
        update=_redraw_mcassets_ui,
    )
    custom_ui_scale: bpy.props.FloatProperty(
        name="Interface Scale", default=1.0, min=.75, max=1.5,
        description="Adjust text, controls and spacing together",
        update=_redraw_mcassets_ui,
    )

    reduce_motion: bpy.props.BoolProperty(
        name="Reduce Interface Motion", default=False,
        description="Turn off Sparkle's interface animations in the sidebar and the World Exporter",
        update=_redraw_mcassets_ui,
    )

    use_native_ui: bpy.props.BoolProperty(
        name="Use Native Interface",
        description="Restore the original Blender panels instead of the styled Sparkle sidebar",
        default=False,
        update=_change_native_interface,
    )

    def draw(self, context):
        layout = self.layout
        from .ui.gpu_framework.theme_adapter import THEME_SETTINGS_ENABLED
        layout.label(text="Sparkle")
        layout.label(text="Created by MrRobot and TylerBits")
        if THEME_SETTINGS_ENABLED:
            layout.prop(self, "ui_theme")
        layout.prop(self, "use_native_ui")
        layout.prop(self, "miex_auto_workspace")
        layout.operator("mcbedrock.world_workspace", icon='WORKSPACE')
        layout.prop(self, "adaptive_ui_scale")
        layout.prop(self, "custom_ui_scale")
        layout.prop(self, "reduce_motion")
        row = layout.row()
        row.label(text=f"Current Version: {'.'.join(map(str, ADDON_VERSION))}")
        row.operator("mcbedrock.check_update", icon='FILE_REFRESH')
        row = layout.row()
        row.label(text="Thumbnails, pack icons and indexes")
        row.operator("mcbedrock.clear_cache", icon='TRASH')

classes = (
    MCBEDROCK_OT_check_update,
    MCBEDROCK_OT_transform_owned_model,
    MCBEDROCK_OT_delete_owned_model,
    MCBEDROCK_preferences,
    MCBEDROCK_OT_import_particle_dialog,
    MCBEDROCK_OT_import_particle,
    MCBEDROCK_OT_import_asset_json,
    MCBEDROCK_OT_export_bbmodel,
    MCBEDROCK_OT_import_brarchive_dialog,
    MCBEDROCK_OT_set_texture,
    MCBEDROCK_OT_particle_clip_frame,
    MCBEDROCK_OT_randomize_particle_seed,
    MCBEDROCK_OT_rebuild_particle_cache,
    MCBEDROCK_OT_bake_world_space,
    MCBEDROCK_OT_set_resource_pack,
    MCBEDROCK_OT_install_vanilla_pack,
    MCBEDROCK_PT_import,
    MCBEDROCK_PT_particle_settings,
)


def _set_cache_modifier_input(obj, names, value):
    """Set a named Geometry Nodes input without relying on Socket_N order."""
    wanted = {str(name).casefold() for name in names}
    from .particle_backend_v3 import set_modifier_input_value
    changed = False
    for modifier in getattr(obj, "modifiers", ()):
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        interface = getattr(modifier.node_group, "interface", None)
        items = getattr(interface, "items_tree", ()) if interface else ()
        for item in items:
            if (
                getattr(item, "item_type", "") != "SOCKET"
                or getattr(item, "in_out", "") != "INPUT"
                or str(getattr(item, "name", "")).casefold() not in wanted
            ):
                continue
            identifier = getattr(item, "identifier", "")
            if identifier:
                set_modifier_input_value(modifier, identifier, value)
                changed = True
    if changed:
        obj.update_tag()
    return changed


def _java_particle_settings(emitter):
    systems = tuple(getattr(emitter, "particle_systems", ()) or ())
    return systems[0].settings if systems else None


def apply_java_emission_controls(emitter, context):
    """Apply the shared controls to Blender's native Java backend."""
    return builder.apply_java_particle_controls(emitter, context.scene)

def update_particle_playback(self, context):
    """Keep the inclusive clip valid and update lightweight backend inputs."""
    emitter = self
    if int(emitter.mcbedrock_end_frame) < int(emitter.mcbedrock_start_frame):
        emitter.mcbedrock_end_frame = int(emitter.mcbedrock_start_frame)
        return
    baked = int(emitter.get("mcbedrock_baked_duration_frames", 0))
    requested = int(emitter.mcbedrock_end_frame) - int(emitter.mcbedrock_start_frame) + 1
    if (
        emitter.mcbedrock_emission_mode == builder.EMISSION_CONTINUOUS
        and baked > 0
        and requested > baked
    ):
        emitter.mcbedrock_emission_dirty = True
    update_scales(emitter, context)


def mark_particle_emission_dirty(self, context):
    self.mcbedrock_emission_dirty = True


def update_scales(self, context):
    """Apply the shared animation controls to every particle backend."""
    emitter = self
    if not emitter:
        return

    anim_val = max(0.001, emitter.mcbedrock_animation_scale)
    particle_val = emitter.mcbedrock_particle_scale
    speed_val = max(0.001, emitter.mcbedrock_speed_scale)
    density_val = max(0.0, min(1.0, emitter.mcbedrock_density))
    tint = tuple(emitter.mcbedrock_color_tint)

    emitter.scale = (anim_val, anim_val, anim_val)
    particle_compensation = particle_val / anim_val
    nla_scale = 1.0 / speed_val
    backend = str(
        emitter.get(
            "mcbedrock_backend",
            builder.BACKEND_LEGACY_OBJECTS,
        )
    )
    render_objects = builder.particle_render_objects(emitter)
    cache_backend = backend == builder.BACKEND_GN_CACHE_V2
    java_settings = _java_particle_settings(emitter) if backend == "JAVA_NATIVE" else None
    if java_settings is not None and java_settings.instance_object is not None:
        render_objects = (java_settings.instance_object,)

    if cache_backend:
        for child in render_objects:
            child["mcbedrock_animation_scale"] = anim_val
            child["mcbedrock_particle_scale"] = particle_val
            child["mcbedrock_speed_scale"] = speed_val
            child["mcbedrock_density"] = density_val
            _set_cache_modifier_input(
                child,
                ("Animation Scale", "Emitter Scale"),
                anim_val,
            )
            _set_cache_modifier_input(
                child,
                ("Particle Scale", "Particle Size"),
                particle_val,
            )
            _set_cache_modifier_input(
                child,
                ("Speed", "Simulation Speed", "Time Scale"),
                speed_val,
            )
            _set_cache_modifier_input(
                child,
                ("Density",),
                density_val,
            )
            _set_cache_modifier_input(
                child, ("Start Frame",), int(emitter.mcbedrock_start_frame)
            )
            _set_cache_modifier_input(
                child, ("End Frame",), int(emitter.mcbedrock_end_frame)
            )
    elif java_settings is not None:
        source_size = float(emitter.get("mcbedrock_java_source_size", 1.0))
        source_speed = float(emitter.get("mcbedrock_java_source_speed", 1.0))
        java_settings.particle_size = max(0.001, source_size * particle_val)
        java_settings.normal_factor = source_speed * speed_val
        if hasattr(java_settings, "display_percentage"):
            java_settings.display_percentage = int(round(density_val * 100.0))
        apply_java_emission_controls(emitter, context)
    else:
        # Preserve the established Object/NLA behavior exactly for old scenes.
        legacy_objects = [
            child
            for child in render_objects
            if child.type == "MESH"
            and child.name.startswith("Particle_")
        ]
        active_count = int(len(legacy_objects) * density_val)
        for index, child in enumerate(legacy_objects):
            if index >= active_count:
                child.delta_scale = (0.0, 0.0, 0.0)
            else:
                child.delta_scale = (
                    particle_compensation,
                    particle_compensation,
                    1.0,
                )
            animation_data = child.animation_data
            if animation_data and animation_data.nla_tracks:
                for track in animation_data.nla_tracks:
                    for strip in track.strips:
                        strip.scale = nla_scale

    # Child effects may use different materials, so update every unique one.
    seen_materials = set()
    for child in render_objects:
        data = getattr(child, "data", None)
        child_materials = getattr(data, "materials", ()) if data else ()
        for material in child_materials:
            if material is None:
                continue
            pointer = material.as_pointer()
            if pointer in seen_materials:
                continue
            seen_materials.add(pointer)
            materials.set_global_tint(material, tint)

    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()


def _register_impl():
    if bpy:
        for cls in classes:
            bpy.utils.register_class(cls)
        bpy.types.TOPBAR_MT_file_export.append(_menu_export_bbmodel)
        # Imported presentation roots use Blender's native selection and transform tools.
        bpy.types.Scene.mcbedrock_resource_pack_path = bpy.props.StringProperty(
            name="Resource Pack Path",
            description="Path to Bedrock resource pack (for texture resolution)",
            default="",
            subtype='DIR_PATH',
            options={"SKIP_SAVE"},
        )
        bpy.types.Scene.mcbedrock_bake_frames = bpy.props.IntProperty(
            name="Bake Duration",
            description="Frames to simulate",
            default=400,
            min=1
        )
        bpy.types.Scene.mcbedrock_asset_advanced_show = bpy.props.BoolProperty(
            name="Advanced",
            description="Show advanced asset import settings",
            default=False,
        )
        bpy.types.Scene.mcbedrock_thumb_quality = bpy.props.EnumProperty(
            name="Thumbnail Quality",
            description="Resolution of generated asset thumbnails",
            items=[
                ('LOW', "Low", "128 px"),
                ('MEDIUM', "Medium", "256 px"),
                ('HIGH', "High", "512 px"),
            ],
            default='LOW',
        )
        bpy.types.Scene.mcbedrock_thumb_generating = bpy.props.BoolProperty(
            name="Generating Thumbnails",
            description="True while thumbnails are being rendered",
            default=False,
        )



        bpy.types.Object.mcbedrock_particle_scale = bpy.props.FloatProperty(
            name="Particle Size Multiplier",
            description="Scales the size of each individual particle plane dynamically",
            default=1.0,
            min=0.001,
            update=update_scales
        )
        bpy.types.Object.mcbedrock_animation_scale = bpy.props.FloatProperty(
            name="Animation Scale Multiplier",
            description="Scales the overall motion/spawn radius of the particle system dynamically",
            default=1.0,
            min=0.001,
            update=update_scales
        )
        bpy.types.Object.mcbedrock_speed_scale = bpy.props.FloatProperty(
            name="Simulation Speed",
            description="Speeds up or slows down the particle animation dynamically",
            default=1.0,
            min=0.001,
            update=update_scales
        )
        bpy.types.Object.mcbedrock_density = bpy.props.FloatProperty(
            name="Density",
            description="Percentage of particles to show (0.0 to 1.0)",
            default=1.0,
            min=0.0,
            max=1.0,
            subtype='FACTOR',
            update=update_scales
        )
        bpy.types.Object.mcbedrock_color_tint = bpy.props.FloatVectorProperty(
            name="Color Tint",
            description="Multiplies the color of the particles dynamically",
            subtype='COLOR',
            size=4,
            default=(1.0, 1.0, 1.0, 1.0),
            min=0.0, max=1.0,
            update=update_scales
        )
        bpy.types.Object.mcbedrock_start_frame = bpy.props.IntProperty(
            name="Start Frame", description="Inclusive playback start",
            default=1, update=update_particle_playback,
        )
        bpy.types.Object.mcbedrock_end_frame = bpy.props.IntProperty(
            name="End Frame", description="Inclusive playback end",
            default=400, update=update_particle_playback,
        )
        bpy.types.Object.mcbedrock_emission_mode = bpy.props.EnumProperty(
            name="Emission Mode",
            items=((builder.EMISSION_SOURCE, "Source", "Use the authored emitter"),
                   (builder.EMISSION_BURST, "Burst", "Emit a fixed count at Start"),
                   (builder.EMISSION_CONTINUOUS, "Continuous", "Emit steadily through the clip")),
            default=builder.EMISSION_SOURCE,
            options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_burst_count = bpy.props.IntProperty(
            name="Particle Count", default=10, min=0,
            options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_emission_rate = bpy.props.FloatProperty(
            name="Particles per Second", default=10.0, min=0.0,
            options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_max_active = bpy.props.IntProperty(
            name="Max Active", default=100, min=1,
            options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_emission_dirty = bpy.props.BoolProperty(
            name="Emission Rebuild Required", default=False, options={'HIDDEN'},
        )
        bpy.types.Object.mcbedrock_motion_mode = bpy.props.EnumProperty(
            name="Vertical Motion",
            description="Use authored motion or override its vertical channel",
            items=((builder.MOTION_SOURCE, "Authored", "Use source motion"),
                   (builder.MOTION_RISE, "Rise", "Move upward"),
                   (builder.MOTION_FALL, "Fall", "Move downward"),
                   (builder.MOTION_CUSTOM, "Custom", "Use signed speed and acceleration")),
            default=builder.MOTION_SOURCE,
            options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_vertical_speed = bpy.props.FloatProperty(
            name="Vertical Speed", description="Initial vertical speed in blocks per second",
            default=1.0, options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_vertical_acceleration = bpy.props.FloatProperty(
            name="Vertical Acceleration",
            description="Vertical acceleration in blocks per second squared",
            default=0.0, options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_lifetime_mode = bpy.props.EnumProperty(
            name="Particle Lifetime",
            items=((builder.LIFETIME_SOURCE, "Authored", "Use source particle lifetime"),
                   (builder.LIFETIME_CLIP, "Through Clip", "Keep particles alive through the playback clip")),
            default=builder.LIFETIME_SOURCE,
            options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_collision_mode = bpy.props.EnumProperty(
            name="Collisions",
            items=((builder.COLLISION_SOURCE, "Authored", "Use source collision and kill rules"),
                   (builder.COLLISION_IGNORE, "Ignore", "Ignore collision, kill plane, and block expiration")),
            default=builder.COLLISION_SOURCE,
            options=set(), update=mark_particle_emission_dirty,
        )
        bpy.types.Object.mcbedrock_random_seed = bpy.props.IntProperty(
            name="Simulation Seed",
            description="Deterministic emitter random seed; use Reroll for a new Blockbench-style trajectory",
            default=0, min=0, max=2147483647,
            options=set(), update=mark_particle_emission_dirty,
        )


        asset_browser.register()
        pack_manager.register()
        schematic_importer.register()
        custom_ui.register()
        session_state.register()
        from .ui import world_exporter
        world_exporter.register()
        from .ui import mod_import
        mod_import.register()
        # Saved active-pack collections are the source of truth. Restore their
        # index-zero compatibility mirror after every dependent property exists.
        pack_manager.initialize_active_pack_state()

def register():
    """Register transactionally and recover stale state from failed installs."""
    if not bpy:
        return
    unregister()
    try:
        _register_impl()
    except Exception:
        unregister()
        raise


def unregister():
    if bpy:
        from .ui import mod_import, world_exporter
        try:
            mod_import.unregister()
        except Exception:
            pass
        try:
            world_exporter.unregister()
        except Exception:
            pass
        _unregister_model_tool_timer()
        _unregister_model_selection_handler()
        try:
            bpy.types.TOPBAR_MT_file_export.remove(_menu_export_bbmodel)
        except Exception:
            pass
        try:
            custom_ui.unregister()
        except Exception:
            pass
        session_state.unregister()
        try:
            schematic_importer.unregister()
        except Exception:
            pass
        try:
            pack_manager.unregister()
        except Exception:
            pass
        try:
            asset_browser.unregister()
        except Exception:
            pass
        _unregister_model_keymaps()
        for cls in reversed(classes):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                registered_cls = getattr(bpy.types, cls.__name__, None)
                if registered_cls is not None and registered_cls is not cls:
                    try:
                        bpy.utils.unregister_class(registered_cls)
                    except Exception:
                        pass
        if hasattr(bpy.types.Object, "mcbedrock_particle_scale"):
            del bpy.types.Object.mcbedrock_particle_scale
        if hasattr(bpy.types.Object, "mcbedrock_animation_scale"):
            del bpy.types.Object.mcbedrock_animation_scale
        if hasattr(bpy.types.Object, "mcbedrock_speed_scale"):
            del bpy.types.Object.mcbedrock_speed_scale
        if hasattr(bpy.types.Object, "mcbedrock_density"):
            del bpy.types.Object.mcbedrock_density
        if hasattr(bpy.types.Object, "mcbedrock_color_tint"):
            del bpy.types.Object.mcbedrock_color_tint
        for property_name in (
            "mcbedrock_start_frame", "mcbedrock_end_frame",
            "mcbedrock_emission_mode", "mcbedrock_burst_count",
            "mcbedrock_emission_rate", "mcbedrock_max_active",
            "mcbedrock_emission_dirty", "mcbedrock_motion_mode",
            "mcbedrock_vertical_speed", "mcbedrock_vertical_acceleration",
            "mcbedrock_lifetime_mode", "mcbedrock_collision_mode",
            "mcbedrock_random_seed",
        ):
            if hasattr(bpy.types.Object, property_name):
                delattr(bpy.types.Object, property_name)
        if hasattr(bpy.types.Scene, "mcbedrock_resource_pack_path"):
            del bpy.types.Scene.mcbedrock_resource_pack_path
        if hasattr(bpy.types.Scene, "mcbedrock_bake_frames"):
            del bpy.types.Scene.mcbedrock_bake_frames
        if hasattr(bpy.types.Scene, "mcbedrock_asset_advanced_show"):
            del bpy.types.Scene.mcbedrock_asset_advanced_show
        if hasattr(bpy.types.Scene, "mcbedrock_thumb_quality"):
            del bpy.types.Scene.mcbedrock_thumb_quality
        if hasattr(bpy.types.Scene, "mcbedrock_thumb_generating"):
            del bpy.types.Scene.mcbedrock_thumb_generating


        if hasattr(bpy.types.Scene, "mcbedrock_target_armature"):
            del bpy.types.Scene.mcbedrock_target_armature
        if hasattr(bpy.types.Scene, "mcbedrock_target_bone"):
            del bpy.types.Scene.mcbedrock_target_bone


def _native_bedrock_model_source(armature):
    """Return the geometry JSON behind an eligible resource-pack model rig."""
    if armature is None or getattr(armature, "type", "") != "ARMATURE":
        return ""
    if str(armature.get("mcbedrock_asset_type", "")).lower() not in {
        "model", "client_entity",
    }:
        return ""
    if not armature.get("mcbedrock_identifier", ""):
        return ""
    source = str(armature.get("mcbedrock_source_path", "") or "")
    return source if source.lower().endswith(".json") else ""


_original_bbmodel_source = _bbmodel_source
_original_is_bbmodel_armature = _is_bbmodel_armature
_base_bbmodel_source = globals().get(
    "_base_bbmodel_source", _original_bbmodel_source)
_base_is_bbmodel_armature = globals().get(
    "_base_is_bbmodel_armature", _original_is_bbmodel_armature)
_original_bbmodel_source = _base_bbmodel_source
_original_is_bbmodel_armature = _base_is_bbmodel_armature


def _bbmodel_source(armature):
    """Resolve either an original Blockbench project or Bedrock geometry."""
    return (
        _original_bbmodel_source(armature)
        or _native_bedrock_model_source(armature)
    )


def _is_bbmodel_armature(armature):
    """Accept Blockbench imports and native resource-pack model armatures."""
    return (
        _original_is_bbmodel_armature(armature)
        or bool(_native_bedrock_model_source(armature))
    )


def _active_model_export_action(context):
    armature = _active_bbmodel_armature(context)
    animation_data = getattr(armature, "animation_data", None)
    return animation_data.action if animation_data is not None else None


def _animation_loop_mode_get(action):
    value = str(action.get("mcbedrock_loop_mode", "once") or "once").lower()
    if value in {"loop", "true", "repeat", "cycle", "1"}:
        return 0
    if value in {"hold", "hold_on_last_frame"}:
        return 1
    return 2


def _animation_loop_mode_set(action, value):
    modes = ("loop", "hold", "once")
    mode = modes[value] if 0 <= value < len(modes) else "once"
    action["mcbedrock_loop_mode"] = mode

    # Keep Blender's curve preview consistent with the exported policy.
    fcurves = list(getattr(action, "fcurves", ()) or ())
    if not fcurves:
        for layer in getattr(action, "layers", ()) or ():
            for strip in getattr(layer, "strips", ()) or ():
                channelbag = getattr(strip, "channelbag", None)
                if channelbag is not None:
                    fcurves.extend(getattr(channelbag, "fcurves", ()) or ())
                for bag in getattr(strip, "channelbags", ()) or ():
                    fcurves.extend(getattr(bag, "fcurves", ()) or ())
    for fcurve in fcurves:
        for modifier in list(fcurve.modifiers):
            if modifier.type == "CYCLES":
                fcurve.modifiers.remove(modifier)
        if mode == "loop":
            try:
                fcurve.modifiers.new(type="CYCLES")
            except RuntimeError:
                pass


class MCBEDROCK_PT_model_animation_export(bpy.types.Panel):
    bl_label = "Animation Export"
    bl_idname = "MCBEDROCK_PT_model_animation_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Sparkle"
    bl_order = 20

    @classmethod
    def poll(cls, context):
        return custom_ui.legacy_ui_enabled(context) and _active_model_export_action(context) is not None

    def draw(self, context):
        action = _active_model_export_action(context)
        layout = self.layout
        layout.label(text=action.name, icon="ACTION")
        layout.prop(action, "mcbedrock_export_loop_mode", text="Type")
        layout.label(text="Used by Blockbench project export")


class MCBEDROCK_PT_dopesheet_animation_export(bpy.types.Panel):
    bl_label = "Blockbench Playback"
    bl_idname = "MCBEDROCK_PT_dopesheet_animation_export"
    bl_space_type = "DOPESHEET_EDITOR"
    bl_region_type = "UI"
    bl_category = "Sparkle"

    @classmethod
    def poll(cls, context):
        return _active_model_export_action(context) is not None

    def draw(self, context):
        action = _active_model_export_action(context)
        self.layout.prop(action, "mcbedrock_export_loop_mode", text="Type")


classes = classes + (
    MCBEDROCK_PT_model_animation_export,
    MCBEDROCK_PT_dopesheet_animation_export,
)

_register_impl_without_animation_export = _register_impl
_unregister_without_animation_export = unregister


def _register_impl():
    if bpy:
        bpy.types.Action.mcbedrock_export_loop_mode = bpy.props.EnumProperty(
            name="Animation Type",
            description="Blockbench playback behavior for the active animation",
            items=[
                ("LOOP", "Loop", "Restart after the last frame"),
                ("HOLD", "Pause on Last Frame", "Hold the final pose"),
                ("ONCE", "Play Once", "Stop after playing once"),
            ],
            get=_animation_loop_mode_get,
            set=_animation_loop_mode_set,
        )
    _register_impl_without_animation_export()


def unregister():
    _unregister_without_animation_export()
    if bpy and bpy.app.timers.is_registered(_finish_update_check):
        bpy.app.timers.unregister(_finish_update_check)
    if bpy and hasattr(bpy.types.Action, "mcbedrock_export_loop_mode"):
        del bpy.types.Action.mcbedrock_export_loop_mode


if __name__ == "__main__":
    register()

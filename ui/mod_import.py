"""File > Import > Minecraft Mod Model (.jar), and dropping a .jar on the viewport.

The JAR is read on its own: its mobs, blocks and items show in the import
panel over the 3D viewport (``mod_import_panel``; a dialog with Blender's
native interface) and the chosen one is built at the 3D cursor. The JAR
never joins the resource-pack list; active packs still override its
textures, as they do in the game.
"""

import difflib
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

_KIND_LABELS = {"entity": "Mob", "model": "Model", "block": "Block", "item": "Item"}
# Blender keeps only pointers to enum strings; the lists must stay alive.
_ENUM_ITEMS = {}
# Search labels per catalog, built once: (catalog, [(label, kind, lowered)], {label: asset}).
_CHOICES = {}


def _jar_path(operator):
    path = bpy.path.abspath(getattr(operator, "filepath", "") or "")
    if path.lower().endswith(".jar") and os.path.isfile(path):
        return path
    return ""


def _catalog(operator):
    path = _jar_path(operator)
    if not path:
        return None
    from ..importers.mod_jar import mod_catalog
    try:
        return mod_catalog(path)
    except Exception as exc:  # unreadable or not a mod: the dialog says so
        print(f"Sparkle: could not read {path}: {exc}")
        return None


def _choices(catalog):
    """``([(label, kind)], {label: asset})``: unique labels, a name two kinds share gets its kind."""
    if catalog is None:
        return [], {}
    cached = _CHOICES.get(id(catalog))
    if cached is not None and cached[0] is catalog:
        return cached[1], cached[2]
    counts = {}
    for asset in catalog.assets:
        counts[asset.name] = counts.get(asset.name, 0) + 1
    results, by_label = [], {}
    for asset in catalog.assets:
        kind = _KIND_LABELS.get(asset.kind, asset.kind)
        label = asset.name if counts[asset.name] == 1 else f"{asset.name} ({kind})"
        if label in by_label:
            label = f"{label} [{asset.identifier}]"
        results.append((label, kind))
        by_label[label] = asset
    if len(_CHOICES) > 8:
        _CHOICES.clear()
    _CHOICES[id(catalog)] = (catalog, results, by_label)
    return results, by_label


def _search_models(self, context, edit_text):
    # Every label: Blender filters and ranks them as the user types (fuzzy, like
    # its own ID search) and pages through them, drawing only the visible rows.
    return _choices(_catalog(self))[0]


def _best_label(text, labels, fuzzy=False):
    """The label a typed name means: exact, any case, start of the name or of a word,
    part of it (shortest first), then with ``fuzzy`` the closest spelling."""
    if text in labels:
        return text
    lowered = text.strip().lower()
    if not lowered:
        return ""
    tiers = ([], [], [])
    for label in labels:
        name = label.lower()
        if name == lowered:
            return label
        if name.startswith(lowered):
            tiers[0].append(label)
        elif f" {lowered}" in f" {name}":
            tiers[1].append(label)
        elif lowered in name:
            tiers[2].append(label)
    for tier in tiers:
        if tier:
            return min(tier, key=len)
    if fuzzy:
        names = {label.lower(): label for label in labels}
        close = difflib.get_close_matches(lowered, list(names), n=1, cutoff=0.6)
        if close:
            return names[close[0]]
    return ""


def _model_changed(self, context):
    # Enter on a part or a misspelling of a name picks the closest model.
    best = _best_label(self.model, _choices(_catalog(self))[1], fuzzy=True)
    if best and best != self.model:
        self.model = best
        return
    _reset_layers(self)


# (jar path, asset key) -> [(layer key, name, shown by default)], run once per model.
_LAYERS = {}


def _layer_list(operator):
    """The selected model's render layers: glow and overlays a default mob shows,
    then optional ones (saddles, armor, charge auras)."""
    catalog = _catalog(operator)
    asset = _selected_asset(operator, catalog)
    if asset is None or asset.kind not in ("entity", "model") or asset.layer_method:
        return []
    cache_key = (catalog.path, asset.key)
    if cache_key not in _LAYERS:
        from ..builders.mod_builder import geo_layers, layer_key
        from ..importers.mod_models import ModModelError, load_entity_model, vanilla_model_library

        default, optional = [], []
        try:
            if asset.geckolib:
                _renderer, default, optional = geo_layers(catalog.path, asset)
            elif asset.renderer_class:
                entity = load_entity_model(catalog.path, renderer_class=asset.renderer_class,
                                           model_class=asset.model_class, library=vanilla_model_library)
                default, optional = entity.layers, entity.optional_layers
        except (ModModelError, OSError, ValueError) as exc:
            print(f"Sparkle: could not read the layers of {asset.name}: {exc}")
        names = {}
        for layer in default + optional:
            names[layer["name"]] = names.get(layer["name"], 0) + 1
        entries = []
        for layer in default + optional:
            name = layer["name"]
            if names[name] > 1:  # two saddles: say which texture
                name = f"{name} ({layer['texture'].rsplit('/', 1)[-1].rsplit('.', 1)[0]})"
            entries.append((layer_key(layer), name, not layer["optional"]))
        if len(_LAYERS) > 64:
            _LAYERS.clear()
        _LAYERS[cache_key] = entries[:30]
    return _LAYERS[cache_key]


def _layer_items(self, context):
    entries = _layer_list(self)
    items = [(key, name, "Drawn for a default mob" if shown else "Optional: drawn when the mob wears or does it",
              "", 1 << index)
             for index, (key, name, shown) in enumerate(entries)]
    catalog = _catalog(self)
    _ENUM_ITEMS[("layers", catalog.path if catalog else "",
                 getattr(_selected_asset(self, catalog), "key", ""))] = items
    return items


def _reset_layers(operator):
    """The selected model's default layers on, its optional ones off."""
    entries = _layer_list(operator)
    operator.layers = {key for key, _name, shown in entries if shown}
    operator.layers_set = True


def _selected_asset(operator, catalog):
    if catalog is None:
        return None
    if operator.asset_key:
        found = catalog.get(operator.asset_key)
        if found is None:
            found = next((asset for asset in catalog.assets
                          if asset.identifier == operator.asset_key), None)
        return found
    by_label = _choices(catalog)[1]
    return by_label.get(_best_label(operator.model, by_label))


def _texture_items(self, context):
    catalog = _catalog(self)
    asset = _selected_asset(self, catalog)
    items = [("DEFAULT", "In-game Default", "The texture the mod's renderer picks for a default mob")]
    if asset is not None and asset.kind in ("entity", "model"):
        for index, texture in enumerate(asset.textures):
            items.append((str(index), texture.rsplit("/", 1)[-1], texture))
    _ENUM_ITEMS[("textures", catalog.path if catalog else "", getattr(asset, "key", ""))] = items
    return items


class MCBEDROCK_OT_import_mod_jar(bpy.types.Operator, ImportHelper):
    """Import a mob, block or item model from a Minecraft mod (.jar)"""
    bl_idname = "mcbedrock.import_mod_jar"
    bl_label = "Import Mod Model"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".jar"
    filter_glob: StringProperty(default="*.jar", options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    # A search field, not a dropdown: mods list hundreds of models.
    model: StringProperty(
        name="Model",
        description="Mob, block or item to import. Type to search",
        search=_search_models,
        update=_model_changed,
    )
    asset_key: StringProperty(
        name="Asset Key",
        description="Catalog key or identifier to import (scripts); overrides Model",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    texture: EnumProperty(name="Texture", description="Texture variant for mobs", items=_texture_items)
    texture_location: StringProperty(
        name="Texture Location",
        description="Texture to use, as namespace:path (the import panel's texture menu); overrides Texture",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    layers: EnumProperty(
        name="Layers",
        description="Render layers drawn over the mob: glowing eyes, overlays, saddles",
        items=_layer_items,
        options={"ENUM_FLAG"},
    )
    layers_set: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
    layer_keys: StringProperty(
        name="Layer Keys",
        description="Render layers to draw, one key per line, * for all (scripts); overrides Layers",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    interpolation: EnumProperty(
        name="Texture Interpolation",
        description="Choose how model textures are rendered",
        items=[
            ("Closest", "Closest (Pixel Art)", "Minecraft default, sharp pixelated edges"),
            ("Linear", "Linear (Soft)", "Soft texture interpolation"),
        ],
        default="Closest",
    )
    game_size: BoolProperty(
        name="In-game Size",
        description="Scale mobs the way the mod's renderer does in the game",
        default=True,
    )

    def invoke(self, context, event):
        if _jar_path(self) and not self.asset_key:
            # Dropped on the viewport (or picked in the file browser): the
            # import panel over the viewport shows the mod's models. Nothing
            # is imported yet, so no undo step and no redo panel.
            from .custom_ui import legacy_ui_enabled
            from .mod_import_panel import open_panel

            if not legacy_ui_enabled(context) and open_panel(context, _jar_path(self)):
                return {"CANCELLED"}
        if _jar_path(self):
            # The native interface: pick the model in a dialog.
            catalog = _catalog(self)
            if catalog is not None and catalog.assets and _selected_asset(self, catalog) is None:
                self.model = _choices(catalog)[0][0][0]
            _reset_layers(self)
            return context.window_manager.invoke_props_dialog(self, width=420)
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        catalog = _catalog(self)
        if catalog is None:
            layout.label(text="Pick a mod .jar to list its models", icon="INFO")
            return
        counts = {}
        for asset in catalog.assets:
            counts[asset.kind] = counts.get(asset.kind, 0) + 1
        box = layout.box()
        box.label(text=catalog.name, icon="FILE_ARCHIVE")
        box.label(text=(
            f"{counts.get('entity', 0)} mobs, {counts.get('model', 0)} other models, "
            f"{counts.get('block', 0)} blocks, {counts.get('item', 0)} items"))
        drawn_in_code = len(catalog.code_drawn) + len(catalog.skipped.get("custom loader", ()))
        not_listed = [
            (drawn_in_code, "drawn in code"),
            (len(catalog.skipped.get("needs another mod", ())), "need another mod"),
            (len(catalog.skipped.get("generated at runtime", ())), "made at runtime"),
            (len(catalog.skipped.get("part of another mob", ())), "parts of other mobs"),
            (len(catalog.skipped.get("invisible", ())), "invisible"),
        ]
        not_listed = [f"{count} {reason}" for count, reason in not_listed if count]
        if not_listed:
            box.label(text="Not listed: " + ", ".join(not_listed), icon="INFO")
        layout.prop(self, "model", icon="VIEWZOOM")
        asset = _selected_asset(self, catalog)
        if asset is None and self.model.strip():
            layout.label(text="No model by that name; pick one from the search", icon="ERROR")
        if asset is not None and asset.kind in ("entity", "model"):
            layout.prop(self, "texture")
            if _layer_list(self):
                layout.prop(self, "layers")
            layout.prop(self, "game_size")
        layout.prop(self, "interpolation")

    def execute(self, context):
        path = _jar_path(self)
        if not path:
            self.report({"ERROR"}, "Choose a Minecraft mod .jar file")
            return {"CANCELLED"}
        catalog = _catalog(self)
        asset = _selected_asset(self, catalog)
        if asset is None:
            if catalog is None:
                self.report({"ERROR"}, "Could not read the mod's models")
            elif not self.model.strip() and not self.asset_key and not bpy.app.background:
                # Opened from the file browser without a model: ask for one.
                bpy.ops.mcbedrock.import_mod_jar("INVOKE_DEFAULT", filepath=path)
            else:
                self.report({"ERROR"}, f"{catalog.name} has no model named '{self.model or self.asset_key}'")
            return {"CANCELLED"}
        from ..builders.asset_placement import place_new_roots_at_cursor
        from ..builders.mod_builder import import_mod_entity, import_mod_json_model
        from ..importers.mod_models import ModModelError

        scene = context.scene
        previous_objects = set(scene.objects)
        cursor = scene.cursor.location.copy()
        try:
            if asset.kind in ("entity", "model"):
                texture = self.texture_location
                if not texture and self.texture not in ("", "DEFAULT"):
                    index = int(self.texture)
                    texture = asset.textures[index] if 0 <= index < len(asset.textures) else ""
                layers = None  # what a default mob shows
                if self.layer_keys.strip() == "*":
                    layers = "*"
                elif self.layer_keys.strip():
                    layers = {line.strip() for line in self.layer_keys.splitlines() if line.strip()}
                elif self.layers_set:
                    layers = set(self.layers)
                result = import_mod_entity(
                    context, path, asset, texture=texture,
                    interpolation=self.interpolation, use_render_scale=self.game_size,
                    layers=layers)
                warnings = result.get("warnings", [])
            else:
                import_mod_json_model(context, path, asset, interpolation=self.interpolation)
                warnings = []
        except (ModModelError, OSError, ValueError) as exc:
            self.report({"ERROR"}, f"{asset.name}: {exc}")
            return {"CANCELLED"}
        place_new_roots_at_cursor(context, previous_objects, cursor)
        if warnings:
            self.report({"WARNING"}, f"{asset.name}: " + " | ".join(warnings[:3]))
        else:
            self.report({"INFO"}, f"Imported {asset.name} from {catalog.name}")
        return {"FINISHED"}


def _menu_import(self, context):
    self.layout.operator(MCBEDROCK_OT_import_mod_jar.bl_idname, text="Minecraft Mod Model (.jar)")


classes = [MCBEDROCK_OT_import_mod_jar]

if hasattr(bpy.types, "FileHandler"):
    class MCBEDROCK_FH_mod_jar(bpy.types.FileHandler):
        bl_idname = "MCBEDROCK_FH_mod_jar"
        bl_label = "Minecraft mod model"
        bl_import_operator = "mcbedrock.import_mod_jar"
        bl_file_extensions = ".jar"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type == "VIEW_3D"

    classes.append(MCBEDROCK_FH_mod_jar)


def register():
    from . import mod_import_panel

    for cls in classes:
        bpy.utils.register_class(cls)
    mod_import_panel.register()
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    try:
        bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    except Exception:
        pass
    from . import mod_import_panel, mod_thumbnails

    mod_import_panel.unregister()
    mod_thumbnails.cancel()
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

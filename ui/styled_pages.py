"""Page presentation. All operations and asset state belong to the existing addon."""

import os
import bpy

from .gpu_framework import ActionRow, Button, Column, Image, Label, Panel, Rect, Row, ScrollView, Style, TextField
from .gpu_framework.navigation import Dropdown, PixelIcon
from .property_controls import property_control
from .gpu_framework.core import VirtualGrid

# Page builders run several times a second; walks over every object in a large
# world would cost that much each time. Results hold until objects change.
_SCENE_SCANS = {}


def scene_scan(scene, name, compute):
    # The scene's object count catches scripted changes no depsgraph update reported.
    key = (scene.as_pointer(), name, len(scene.objects))
    if key not in _SCENE_SCANS:
        _SCENE_SCANS[key] = compute()
    return _SCENE_SCANS[key]


@bpy.app.handlers.persistent
def forget_scene_scans(*args):
    depsgraph = next((arg for arg in args if isinstance(arg, bpy.types.Depsgraph)), None)
    if depsgraph is None or depsgraph.id_type_updated('OBJECT') or depsgraph.id_type_updated('COLLECTION'):
        _SCENE_SCANS.clear()


SCENE_SCAN_HANDLERS = ('depsgraph_update_post', 'undo_post', 'redo_post', 'load_post')


def invoke(name, *, invoke=True, **properties):
    def action(context):
        namespace, operator = name.split(".", 1)
        op = getattr(getattr(bpy.ops, namespace), operator)
        if not op.poll():
            raise RuntimeError("This action is unavailable in the current Blender context")
        # These calls are user actions from the persistent sidebar modal.
        # Python calls otherwise suppress the operator's own undo entry.
        return op("INVOKE_DEFAULT" if invoke else "EXEC_DEFAULT", True, **properties)
    action.operator_name = name
    action.operator_properties = properties
    return action


def button(key, text, operator, *, primary=False, enabled=True, invoke_default=True, **properties):
    return Button(key, text, invoke(operator, invoke=invoke_default, **properties), primary=primary,
                  style=Style(height=32, enabled=enabled,align='CENTER'), tooltip=text)


def prop(key, owner, name, label=None, **kwargs):
    control = property_control(key, owner, name, label, **kwargs)
    control.rna_name = name
    control.rna_owner = owner
    return control


def text_field(key, owner, name, placeholder):
    control = TextField(key, lambda ctx: getattr(owner, name),
                        lambda ctx, value: setattr(owner, name, value), placeholder=placeholder)
    control.rna_name = name
    control.rna_owner = owner
    return control


def section(key, title, children):
    return Panel(key, [Label(key + ".title", title.upper(), muted=True,
                            style=Style(height=20, font_size=10, font_weight='SemiBold')), *children])


def actions(key, children):
    # Wrap at a minimum usable width, preserving button labels on narrow sidebars.
    return ResponsiveActions(key, children)


class ResponsiveActions(Column):
    def __init__(self, key, children):
        self.buttons = list(children)
        super().__init__(key, self.buttons, style=Style(gap=8))

    def preferred_size(self, available, state, theme):
        minimum = max((max(64,len(child.text)*6+20) for child in self.buttons),default=64)
        self.direction = "ROW" if available.width >= len(self.buttons) * minimum + 5*max(0,len(self.buttons)-1) else "COLUMN"
        for child in self.buttons:
            child.style = child.style.with_updates(flex=1 if self.direction == "ROW" else 0,
                font_size=11 if self.direction == 'ROW' and available.width/max(1,len(self.buttons))<100 else 12)
        return super().preferred_size(available, state, theme)


class Gallery(VirtualGrid):
    pass


def asset_action(item, index, *, dialog=False):
    from . import asset_browser as browser
    name = "mcbedrock.asset_browser_import_dialog" if dialog and browser._item_has_setup(item) else "mcbedrock.asset_browser_import_selected"
    return invoke(name, invoke=dialog and browser._item_has_setup(item), index=index,
                  source_path=item.source_path, asset_key=browser._browser_item_key(item),
                  asset_type=item.asset_type, asset_identifier=item.identifier,
                  geometry_index=item.geometry_index)


def _asset_card(context, index, item):
    from . import asset_browser as browser
    key = f"assets.card.{index}"
    def preview(ctx):
        return browser.asset_thumbnail_path(ctx.scene, item)
    enabled = bool(item.importable) and item.status not in {"Unreadable", "Protected"}
    return Panel(key, [
        AssetPreview(key + ".preview", preview, item, style=Style(height=96)),
        Label(key + ".name", browser._gallery_card_label(item), style=Style(height=20, font_size=11, font_weight='SemiBold')),
        Button(key + ".import", "Import", asset_action(item, index, dialog=True),
               style=Style(height=28, enabled=enabled,align='CENTER')),
    ], style=Style(height=178, padding=(8,8,8,8), gap=5))


class AssetPreview(Image):
    def __init__(self, key, source, item, **kwargs):
        super().__init__(key, source, **kwargs)
        self.item = item

    def draw(self, renderer, state, theme):
        from . import asset_browser as browser
        context = state.get("_runtime_context")
        if not browser._br_metadata(self.item).get('br_available', True):
            renderer.text(self.rect, 'Choose Pack Experience', theme.text_muted, size=10, align='CENTER')
            return
        source = self.source(context) if context else ""
        size = min(self.rect.width, self.rect.height)
        from .gpu_framework import Rect
        image_rect = Rect(self.rect.x + (self.rect.width-size)/2, self.rect.y, size, size)
        texture = self.item.asset_type == 'Texture'
        mod = browser.is_mod_asset(self.item)
        if source and renderer.image(image_rect, source, fit=texture or mod, nearest=texture):
            return
        icon = PixelIcon(self.key + ".fallback", "PARTICLE" if self.item.asset_type == "Particle" else "ASSETS")
        icon.rect = image_rect
        icon.draw(renderer, state, theme)
        # Shown mod cards are always on their way: their renderer takes every card in view.
        generating = mod or (context and context.scene.mcbedrock_thumb_generating)
        # After the render workers gave up, no preview is on its way.
        unavailable = browser.thumbnails_gave_up(self.item) or browser._asset_thumb_failed(self.item)
        status = "Preview unavailable" if unavailable else (
            "Generating preview..." if generating else "Preview not generated")
        renderer.text(Rect(self.rect.x, self.rect.y, self.rect.width, 18), status,
                      theme.text_muted, size=9, align="CENTER")


def build_assets(context, state):
    from . import asset_browser as browser
    from .. import _active_model_export_action
    scene = context.scene
    children = [section("assets.quick", "Quick Import", [
        actions("assets.quick.actions", [
            button("assets.quick.particle", "Particle", "mcbedrock.import_particle", primary=True),
            button("assets.quick.model", "Model / Entity", "mcbedrock.import_asset_json"),
        ]),
    ])]
    path = scene.mcbedrock_resource_pack_path
    meta = browser._active_pack_meta(path) if path else {}
    children.append(section("assets.pack", "Active Resource Pack", [
        ActionRow("assets.pack.card", [
            Image("assets.pack.icon", meta.get("icon_path", ""), style=Style(width=40, height=40)),
            Column("assets.pack.copy", [Label("assets.pack.name", meta.get("name") or (os.path.basename(path) if path else "No active resource pack")),
                                       Label("assets.pack.author", browser._active_pack_subtitle(meta), muted=True)], style=Style(flex=1)),
        ], style=Style(height=56, padding=(8,8,8,8), gap=8))
    ]))
    children.append(Button("assets.advanced.toggle", ("v  " if scene.mcbedrock_asset_advanced_show else ">  ") + "Advanced",
                           lambda ctx: setattr(scene, "mcbedrock_asset_advanced_show", not scene.mcbedrock_asset_advanced_show),
                           style=Style(height=28, align="LEFT")))
    if scene.mcbedrock_asset_advanced_show:
        children.append(section("assets.advanced", "Import Settings", [
            text_field("assets.root", scene, "mcbedrock_browser_root", "Browser folder"),
            actions("assets.folder.actions", [button("assets.folder", "Folder", "mcbedrock.asset_browser_set_root"),
                button("assets.link", "Use as Pack", "mcbedrock.asset_browser_use_as_pack", invoke_default=False)]),
            prop("assets.quality", scene, "mcbedrock_thumb_quality", "Thumbnail Quality", enabled=not scene.mcbedrock_thumb_generating),
            button("assets.thumbnails", "Generate Thumbnails", "mcbedrock.generate_thumbnails", enabled=not scene.mcbedrock_thumb_generating, invoke_default=False),
            prop("assets.texture", scene, "mcbedrock_texture_override_path", "Texture"),
            prop("assets.loop", scene, "mcbedrock_particle_force_loop", "Force Loop"),
            prop("assets.interpolation", scene, "mcbedrock_particle_interpolation", "Interpolation"),
            prop("assets.geometry", scene, "mcbedrock_geometry_index", "Geometry Index"),
            prop("assets.variants", scene, "mcbedrock_import_all_variants", "Import All Variants"),
            prop("assets.pose", scene, "mcbedrock_pose_preset", "Pose Mode"),
            prop("assets.effects", scene, "mcbedrock_entity_effects", "Entity Effects"),
        ]))
    setup = children
    children = [Label('assets.heading','Asset Library',style=Style(height=28,font_size=14,font_weight='SemiBold'))]
    children += [
        text_field("assets.search", scene, "mcbedrock_browser_search", "Search assets"),
        Dropdown("assets.filter", lambda ctx: scene.mcbedrock_browser_filter,
                 lambda ctx, value: setattr(scene, "mcbedrock_browser_filter", value),
                 [(v,n) for v,n,*_ in browser.ASSET_FILTER_ITEMS], style=Style(height=32, align="LEFT")),
        actions("assets.toolbar", [button("assets.set_pack", "Resource Pack", "mcbedrock.asset_browser_set_pack"),
            button("assets.refresh", "Refresh", "mcbedrock.asset_browser_refresh", invoke_default=False)]),
        Dropdown("assets.view", lambda ctx: scene.mcbedrock_browser_view_mode,
                 lambda ctx, value: setattr(scene, "mcbedrock_browser_view_mode", value),
                 [(v,n) for v,n,*_ in browser.VIEW_MODE_ITEMS], style=Style(height=28, align="LEFT")),
    ]
    visible = list(browser._iter_filtered_browser_assets(scene))
    grid = scene.mcbedrock_browser_view_mode == "GRID"
    if grid:
        content = Gallery("assets.gallery", visible,
                          lambda entry: _asset_card(context, *entry))
    else:
        rows = []
        for i,item in visible:
            rows.append(Button(f"assets.item.{i}", item.name + "  /  " + item.asset_type,
                               lambda ctx, index=i: setattr(scene, "mcbedrock_browser_index", index),
                               primary=i == scene.mcbedrock_browser_index,
                               style=Style(height=28, align="LEFT"), tooltip=item.name))
        content = Column("assets.list", rows, style=Style(gap=4))
    children.append(ScrollView("assets.library.scroll", content, style=Style(height=372 if grid else 252)))
    children.append(Label("assets.count", f"{len(visible)} assets" if visible else "No matching assets", muted=True))
    if not grid and 0 <= scene.mcbedrock_browser_index < len(scene.mcbedrock_browser_assets):
        index = scene.mcbedrock_browser_index
        item = scene.mcbedrock_browser_assets[index]
        if item.asset_type == "Entity":
            children.append(prop("assets.animation", scene, "mcbedrock_browser_animation", "Animation"))
        children.append(Button("assets.import_selected", "Import", asset_action(item, index),
                               primary=True, style=Style(height=32, align='CENTER', enabled=bool(item.importable))))
        children.append(section("assets.details", item.name, [
            Label("assets.details." + field, str(getattr(item, field)), muted=True)
            for field in ("asset_type", "identifier", "pack_name", "status", "detail", "source_path") if getattr(item, field)
        ]))
    action = _active_model_export_action(context)
    children += setup
    if action is not None:
        children.append(section("assets.animation_export", "Animation Export", [
            Label("assets.animation_export.name", action.name),
            prop("assets.animation_export.type", action, "mcbedrock_export_loop_mode", "Type"),
            Label("assets.animation_export.help", "Used by Blockbench project export", muted=True),
        ]))
    return Column("assets.page", children, style=Style(gap=12))


class PackCard(ActionRow):
    """Figma pack-card presentation; selection and activation use existing callbacks."""
    def __init__(self, *args, item, edition, subtitle, active=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.item, self.edition, self.subtitle, self.active = item, edition, subtitle, active

    def draw(self, renderer, state, theme):
        super().draw(renderer,state,theme)
        r=self.rect.inset((14,14,14,14) if not self.active else (10,10,10,10))
        size=min(72 if not self.active else 44,r.height)
        icon=Rect(r.x,r.y+(r.height-size)/2,size,size)
        if not self.item.icon_path or not renderer.image(icon,self.item.icon_path,radius=8):
            renderer.rect(icon,theme.surface_raised,radius=8)
            pixel=PixelIcon(self.key+'.placeholder','PACKS');pixel.rect=icon;pixel.draw(renderer,state,theme)
        copy=Rect(r.x+size+14,r.y,max(0,r.width-size-14),r.height)
        if not self.active:
            badge_width=min(copy.width,max(42,len(self.edition)*6+16))
            badge=Rect(copy.x,copy.top-19,badge_width,19)
            renderer.rect(badge,theme.selected,radius=4)
            renderer.text(badge,self.edition,theme.accent,size=10,align='CENTER',weight='SemiBold')
        name_y=copy.y+17 if not self.active else copy.y+20
        renderer.text(Rect(copy.x,name_y,copy.width,26 if not self.active else 22),
                      self.item.name or os.path.basename(self.item.path),theme.text,
                      size=(18 if self.rect.width>=330 else 15) if not self.active else 13,weight='SemiBold')
        renderer.text(Rect(copy.x,copy.y,copy.width,17),self.subtitle,theme.text_muted,size=10)


def pack_card(context, index, item, active=False, cls=PackCard):
    from . import pack_manager as packs
    scene = context.scene
    key = f"packs.{'active' if active else 'discovered'}.{index}"
    selected_prop = "mcbedrock_active_pack_index" if active else "mcbedrock_discovered_pack_index"
    name = item.name or os.path.basename(item.path)
    edition = packs._edition_label(item.kind)
    active_label = "Highest priority" if active and index == 0 else ("Active" if active or packs._is_active_pack(context,item) else edition)
    row = cls(key, [],
                    lambda ctx: setattr(scene, selected_prop, index),
                    selected=lambda ctx: getattr(scene, selected_prop) == index,
                    style=Style(height=64 if active else 100, radius=10), tooltip=name,
                    item=item,edition=edition,subtitle=active_label if active else packs._pack_subtitle(item),active=active)
    if not active:
        row.double_action = invoke("mcbedrock.add_resource_pack_to_active", invoke=False, pack_index=index)
    return row


def build_packs(context, state):
    from . import pack_manager as packs
    scene = context.scene
    active = scene.mcbedrock_active_packs
    discovered = scene.mcbedrock_discovered_packs
    index = scene.mcbedrock_active_pack_index
    active_children = [pack_card(context,i,item,True) for i,item in enumerate(active)]
    if not active_children:
        active_children = [Label("packs.active.empty", "Add packs below; first pack has highest priority", muted=True)]
    active_children.append(actions("packs.priority", [
        button("packs.up", "Move Up", "mcbedrock.move_active_pack_up", enabled=0 < index < len(active), invoke_default=False),
        button("packs.down", "Move Down", "mcbedrock.move_active_pack_down", enabled=0 <= index < len(active)-1, invoke_default=False),
        button("packs.remove", "Remove", "mcbedrock.remove_active_pack", enabled=0 <= index < len(active), invoke_default=False),
    ]))
    search=text_field("packs.search", scene, "mcbedrock_pack_search", "Search resource packs…")
    search.style=search.style.with_updates(flex=1)
    clear=button("packs.clear", "Clear", "mcbedrock.clear_pack_search", invoke_default=False)
    clear.style=Style(width=44,height=38,font_size=10,ghost=True)
    children = [Label('packs.heading','Resource Pack Manager',style=Style(height=28,font_size=14,font_weight='SemiBold')),
        Row('packs.search.row',[search,clear],style=Style(height=38,gap=6)),
        actions("packs.scan.actions", [button("packs.scan", "Scan for Packs", "mcbedrock.scan_resource_packs", invoke_default=False),
            button("packs.add", "Add Folder", "mcbedrock.add_resource_pack_folder")]),
    ]
    rows = [pack_card(context,i,item) for i,item in enumerate(discovered) if packs._pack_matches_search(item, scene.mcbedrock_pack_search)]
    children.append(ScrollView("packs.library.scroll", Column("packs.library", rows or [
        Label('packs.library.empty','No matching resource packs',muted=True)], style=Style(gap=10)),
        style=Style(height=min(430,max(50,len(rows)*110-10)))))
    children.append(section("packs.active", "Active Resource Packs", active_children))
    selected = scene.mcbedrock_discovered_pack_index
    if 0 <= selected < len(discovered):
        item = discovered[selected]
        children.append(section("packs.details", item.name, [
            Label("packs.subtitle", packs._pack_subtitle(item), muted=True),
            Label("packs.edition", packs._edition_label(item.kind), muted=True),
            button("packs.activate", "Add to Active", "mcbedrock.add_resource_pack_to_active", primary=True,
                   enabled=not packs._is_active_pack(context,item), invoke_default=False, pack_index=selected),
        ]))
    else:
        children.append(Label("packs.count", f"{len(discovered)} packs - select one for details", muted=True))
    return Column("packs.page", children, style=Style(gap=14))


def resource_pack_controls(context, state, prefix="schematic"):
    """Edit the same resource stack from schematic and asset imports."""
    scene = context.scene
    active = scene.mcbedrock_active_packs
    index = scene.mcbedrock_active_pack_index
    rows = [pack_card(context, i, item, True) for i, item in enumerate(active)]
    children = [
        Label(f"{prefix}.priority", "Top pack wins; lower packs fill gaps", muted=True),
        Label(f"{prefix}.shared", "Shared with Resource Packs", muted=True),
    ]
    if rows:
        children.append(ScrollView(f"{prefix}.stack.scroll",
            Column(f"{prefix}.stack", rows, style=Style(gap=4)),
            style=Style(height=min(268, len(rows) * 68))))
    else:
        children.append(Label(f"{prefix}.stack.empty", "No active packs; vanilla is the fallback", muted=True))
    children.append(actions(f"{prefix}.stack.actions", [
        button(f"{prefix}.up", "Move Up", "mcbedrock.move_active_pack_up", enabled=0 < index < len(active), invoke_default=False),
        button(f"{prefix}.down", "Move Down", "mcbedrock.move_active_pack_down", enabled=0 <= index < len(active)-1, invoke_default=False),
        button(f"{prefix}.remove", "Remove", "mcbedrock.remove_active_pack", enabled=0 <= index < len(active), invoke_default=False),
    ]))
    children.append(Button(f"{prefix}.manage", "Add / Enable Packs",
        lambda ctx: state.update({"app.active_tab": "PACKS", "_tree_dirty": True}),
        style=Style(height=32, align="CENTER")))
    children.extend([
        prop(f"{prefix}.vanilla", scene, "mcbedrock_vanilla_assets_path", "Vanilla fallback"),
        Label(f"{prefix}.rebuild", ("Build Geometry again after changing the order" if prefix == "schematic" else "Import again after changing the order"), muted=True),
    ])
    if not scene.mcbedrock_vanilla_assets_path:
        children.append(Label(f"{prefix}.auto", "Auto-detects .minecraft jar", muted=True))
    return section(f"{prefix}.assets", "Resource Pack Load Order", children)


def schematic_pack_controls(context, state, ordering_only=False, heading=True):
    from .schematic_drag import SchematicPackList
    scene = context.scene
    active = scene.mcbedrock_active_packs
    children = [Label('schematic.assets.heading', 'Resource Packs',
        style=Style(height=28, font_size=13, font_weight='SemiBold')),
        Label('schematic.priority', 'Top pack wins · drag to reorder' if ordering_only else 'Top pack takes priority', muted=True,
              style=Style(height=20, font_size=11))] if heading else []
    rows = []
    height, gap = 60, 6
    for index, item in enumerate(active):
        card = pack_card(context, index, item, True)
        card.style = card.style.with_updates(flex=1, height=height, min_height=0, radius=4)
        card.tooltip = 'Hold and drag to reorder'
        rows.append(Row(f'schematic.pack.{index}', [card], style=Style(height=height)))
    if rows:
        children.append(SchematicPackList(rows, [p.path for p in active], row_height=height, gap=gap))
    if not active:
        children.append(Label('schematic.stack.empty', 'Enable packs in the Resource Packs panel' if ordering_only else 'Add packs or use vanilla assets', muted=True))
    if ordering_only:
        # Without a heading the host owns removal.
        if heading:
            children.append(button('schematic.remove', 'Remove Selected', 'mcbedrock.remove_active_pack',
                enabled=0 <= scene.mcbedrock_active_pack_index < len(active), invoke_default=False))
        return Column('schematic.assets', children, style=Style(gap=8))
    children.append(actions('schematic.actions', [
        Button('schematic.manage', 'Add Packs',
            lambda ctx: state.update({'app.active_tab':'PACKS', '_tree_dirty':True}),
            style=Style(height=32, align='CENTER')),
        button('schematic.remove', 'Remove Selected', 'mcbedrock.remove_active_pack',
               enabled=0 <= scene.mcbedrock_active_pack_index < len(active), invoke_default=False),
    ]))
    children.append(prop('schematic.vanilla', scene, 'mcbedrock_vanilla_assets_path', 'Vanilla assets'))
    children.append(Label('schematic.auto', 'Vanilla is detected automatically when empty',
                          muted=True, style=Style(height=20, font_size=10)))
    return Column('schematic.assets', children, style=Style(gap=8))


def build_schematic(context, state):
    from . import schematic_importer
    from .pack_manager import active_pack_roots
    scene = context.scene
    world = schematic_importer.get_loaded_world()
    children = [Column("schematic.import", [
        button("schematic.load", "Import Schematic", "mcbedrock.import_litematic", primary=True),
        Label("schematic.formats", "WorldEdit · MCEdit · Litematic · Axiom", muted=True,
              style=Style(height=20, font_size=10))], style=Style(gap=4))]
    if world is None:
        children.append(Label("schematic.empty", "No schematic loaded", muted=True))
        children.append(schematic_pack_controls(context, state))
        return Column("schematic.page", children, style=Style(gap=8))
    stats = [Label('schematic.'+key, f'{label}: {value}', muted=True,
                   style=Style(height=20, flex=1, font_size=11)) for key,label,value in (
        ('regions','Regions',len(world.regions)), ('chunks','Chunks',world.chunk_count),
        ('blocks','Blocks',world.total_blocks), ('states','States',len(world.palette_counts)),
        ('entities','Entities',world.entity_count), ('tile_entities','Block entities',world.tile_entity_count))]
    children.append(Column('schematic.stats', [
        Label('schematic.name', world.name or 'Untitled schematic',
              style=Style(height=28, font_size=13, font_weight='SemiBold')),
        *[Row('schematic.stats.'+str(i), stats[i:i+2], style=Style(height=20, gap=8))
          for i in range(0, len(stats), 2)]], style=Style(gap=4)))
    has_assets = bool(scene.mcbedrock_active_packs or scene.mcbedrock_resource_pack_path or scene.mcbedrock_vanilla_assets_path)
    children.append(button("schematic.build", "Build Geometry", "mcbedrock.build_schematic", primary=True,
                           enabled=has_assets, invoke_default=False))
    children.append(Label("schematic.live", "Reordering updates this schematic",
        muted=True, style=Style(height=20, font_size=10)))
    children.append(schematic_pack_controls(context, state))
    return Column("schematic.page", children, style=Style(gap=8))


def build_particle(context, state):
    from .. import _get_active_emitter, builder
    emitter = _get_active_emitter(context)
    if emitter is None:
        return Column("particle.page", [Label("particle.empty", "Select an imported particle emitter", muted=True)])
    timing = []
    if emitter.get("mcbedrock_timing_is_looping", False):
        cycle = float(emitter.get("mcbedrock_timing_cycle_time_seconds",0))
        frames = int(emitter.get("mcbedrock_timing_cycle_frames",0))
        timing.append(Label("particle.loop", f"Loop: {cycle:g}s ({frames} frames)"))
        sleep = float(emitter.get("mcbedrock_timing_sleep_time_seconds",0))
        if sleep > 0:
            active = float(emitter.get("mcbedrock_timing_active_time_seconds",0))
            timing.append(Label("particle.sleep", f"Active {active:g}s / Sleep {sleep:g}s"))
    else:
        duration = float(emitter.get("mcbedrock_timing_active_time_seconds",0))
        if duration > 0:
            timing.append(Label("particle.duration", f"Emitter duration: {duration:g}s"))
    lifetime = float(emitter.get("mcbedrock_timing_maximum_particle_lifetime_seconds",0))
    if lifetime > 0:
        timing.append(Label("particle.lifetime", f"Particle max life: {lifetime:g}s"))
    playback = []
    for boundary, label in (("START","Start"),("END","End")):
        playback += [prop("particle." + boundary, emitter, "mcbedrock_" + boundary.lower() + "_frame", label),
                     button("particle.current." + boundary, f"Set {label} to Current Frame", "mcbedrock.particle_clip_frame", invoke_default=False, boundary=boundary)]
    emission = [prop("particle.emission.mode", emitter, "mcbedrock_emission_mode", "Mode")]
    if emitter.mcbedrock_emission_mode == builder.EMISSION_BURST:
        emission.append(prop("particle.count",emitter,"mcbedrock_burst_count","Particle Count"))
    elif emitter.mcbedrock_emission_mode == builder.EMISSION_CONTINUOUS:
        emission += [prop("particle.rate",emitter,"mcbedrock_emission_rate","Particles / Second"),
                     prop("particle.max",emitter,"mcbedrock_max_active","Max Active")]
    motion = [prop("particle.motion",emitter,"mcbedrock_motion_mode","Vertical Motion")]
    if emitter.mcbedrock_motion_mode != builder.MOTION_SOURCE:
        motion += [prop("particle.vertical_speed",emitter,"mcbedrock_vertical_speed","Vertical Speed"),
                   prop("particle.vertical_acceleration",emitter,"mcbedrock_vertical_acceleration","Vertical Acceleration")]
    motion += [prop("particle.lifetime_mode",emitter,"mcbedrock_lifetime_mode","Lifetime"),
               prop("particle.collision",emitter,"mcbedrock_collision_mode","Collisions"),
               prop("particle.seed",emitter,"mcbedrock_random_seed","Simulation Seed"),
               button("particle.reroll","Reroll Seed","mcbedrock.randomize_particle_seed",invoke_default=False),
               button("particle.rebuild","Apply & Rebuild","mcbedrock.rebuild_particle_cache",primary=True,invoke_default=False)]
    if emitter.mcbedrock_emission_dirty:
        motion.append(Label("particle.dirty","Rebuild required",style=Style(height=22,text_color=(1,.65,.24,1))))
    appearance = [prop("particle." + key,emitter,"mcbedrock_" + key,label) for key,label in (
        ("animation_scale","Animation Scale"), ("particle_scale","Particle Size"),
        ("speed_scale","Simulation Speed"), ("density","Density"), ("color_tint","Color Tint"))]
    return Column("particle.page", [section("particle.identity",emitter.name,timing),
        section("particle.playback","Playback",playback), section("particle.emission","Emission",emission),
        section("particle.motion_lifetime","Motion & Lifetime",motion), section("particle.appearance","Appearance",appearance)], style=Style(gap=8))

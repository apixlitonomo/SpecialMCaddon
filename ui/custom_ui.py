"""Main Sparkle sidebar application and native recovery host."""

import textwrap
import bpy
from dataclasses import replace
from .gpu_framework import Button, Column, Label, Row, ScrollView, Style
from .gpu_framework.navigation import Application, Dropdown, PixelIcon
from .gpu_framework import runtime
from .property_controls import MCBEDROCK_OT_ui_edit_property
from .styled_pages import build_assets, build_packs, build_particle, build_schematic
from .sky_page import build_sky, MCBEDROCK_OT_sky_value, MCBEDROCK_OT_sky_value_edit, MCBEDROCK_OT_sky_pack, MCBEDROCK_OT_sky_color
from .posterize_page import build_posterize
from .outlines_page import build_outlines

TAB_ITEMS = (("ASSETS", "Asset Browser"), ("PACKS", "RP Manager"),
             ("SCHEMATIC", "Schematics"), ("PARTICLE", "Particles"), ("SKY", "Sky & Lighting"), ("OUTLINES", "Outlines"),
             ("POSTERIZE", "Posterize"))

_refresh_passes=0


def _refresh_registered_panels():
    """Refresh existing regions after live enable exits its restricted context."""
    global _refresh_passes
    _refresh_passes+=1
    manager=getattr(bpy.context,'window_manager',None)
    if manager:
        for window in manager.windows:
            for area in window.screen.areas:
                if area.type!='VIEW_3D':
                    continue
                area.tag_redraw()
                region=next((r for r in area.regions if r.type=='UI'),None)
                if region:
                    region.tag_redraw()
                    if area.spaces.active.show_region_ui and region.active_panel_category=='Sparkle' and not legacy_ui_enabled(bpy.context):
                        with bpy.context.temp_override(window=window,area=area,region=region):
                            runtime.request_mount(bpy.context,build_app)
    return .25 if _refresh_passes<4 else None

def _addon_preferences(context):
    if context is None:
        return None
    addons = getattr(context.preferences, "addons", {})
    candidates = (
        __package__.split(".")[0],
        "Sparkle",
        "MCBedrockParticles",
        "mcbedrockparticles",
    )
    for candidate in candidates:
        addon = addons.get(candidate)
        if addon is not None:
            return addon.preferences
    for key in addons.keys():
        lowered = key.lower()
        if lowered.endswith(".mcbedrockparticles") or lowered == "mcbedrockparticles":
            return addons[key].preferences
    return None



def legacy_ui_enabled(context):
    return bool(getattr(_addon_preferences(context), "use_native_ui", False))


def developer_ui_enabled(context):
    """Compatibility alias for callers of the former beta gate."""
    return not legacy_ui_enabled(context)


def _set_legacy(context, enabled):
    preferences = _addon_preferences(context)
    if preferences is not None:
        preferences.use_native_ui = bool(enabled)
    if enabled:
        from .asset_browser import rebuild_gallery_rows
        rebuild_gallery_rows(context.scene)
        runtime.cancel_all()
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


class PageTab(Button):
    show_focus_outline = False

    def __init__(self, *args, selected=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected = selected
        self.primary = selected

    def draw(self, renderer, state, theme):
        self.style=self.style.with_updates(background=theme.accent if self.selected else theme.surface_raised,
                                          text_color=theme.selected_text if self.selected else theme.text_muted)
        super().draw(renderer,state,replace(theme,radius=self.style.radius))


def sync_particle_page(context, state):
    """Follow emitter selection once; never fight a manual page change."""
    from .. import _get_active_emitter
    scene_key = context.scene.as_pointer()
    if state.get('particle.scene', scene_key) != scene_key:
        if state.get('app.active_tab') == 'PARTICLE':
            state['app.active_tab'] = state.get('particle.return_page', 'PACKS')
        state.pop('particle.emitter', None)
        state.pop('particle.return_page', None)
    state['particle.scene'] = scene_key
    active = getattr(context, 'active_object', None)
    emitter = _get_active_emitter(context) if active and active.select_get() else None
    key = getattr(emitter, 'session_uid', None) or (emitter.as_pointer() if emitter else None)
    if state.pop('particle.restore_page', False):
        # Undo can replace the scene and emitter RNA. Establish the new baseline
        # without treating those replacement objects as a new user selection.
        state['particle.emitter'] = key
        if emitter is None and state.get('app.active_tab') == 'PARTICLE':
            state['app.active_tab'] = state.pop('particle.return_page', 'PACKS')
        return
    previous = state.get('particle.emitter')
    if key == previous:
        return
    state['particle.emitter'] = key
    page = state.get('app.active_tab', 'PACKS')
    if emitter is not None:
        if page != 'PARTICLE':
            state['particle.return_page'] = page
        state['app.active_tab'] = 'PARTICLE'
        state.pop('app.scroll.particle', None)
    elif page == 'PARTICLE':
        state['app.active_tab'] = state.pop('particle.return_page', 'PACKS')
    state.pop('_menu', None)
    state.pop('_runtime_focus', None)


def build_app(context, state, theme):
    from .gpu_framework.theme_adapter import THEME_SETTINGS_ENABLED
    if not THEME_SETTINGS_ENABLED and state.get('app.active_tab') == 'SETTINGS':
        state['app.active_tab'] = state.pop('settings.return_page', 'PACKS')
    from .make_panels import MakeApplication, ModeDropdown, HeaderDivider, build_assets, build_packs, build_quick_import, context_menu
    width=state.get('_content_width',getattr(context.region,'width',360) if context.region else 360)
    narrow=width<220
    control_scale=1.0  # Blender display scaling already applies in the renderer.
    if state.get("app.active_tab") != "SETTINGS":
        sync_particle_page(context, state)
    from .styled_pages import scene_scan
    has_particles = scene_scan(context.scene, 'has_particles', lambda: any(
        obj.name.startswith('Emitter_') or 'mcbedrock_is_emitter' in obj for obj in context.scene.objects))
    if not has_particles and state.get('app.active_tab') == 'PARTICLE':
        state['app.active_tab'] = state.pop('particle.return_page', 'PACKS')
        state.pop('_menu', None)
        state.pop('_runtime_focus', None)
    active = state.get("app.active_tab", "PACKS")
    if active not in {"PACKS", "ASSETS", "QUICK", "SCHEMATIC", "PARTICLE", "SETTINGS", "SKY", "OUTLINES", "POSTERIZE"}:
        active = "PACKS"
    state["app.active_tab"] = active
    def select(ctx, value):
        if value == 'ASSETS' and state.get('app.active_tab') != 'ASSETS' and ctx.scene.mcbedrock_active_packs:
            from .make_panels import Carousel
            from .pack_manager import _normalized_path
            packs = ctx.scene.mcbedrock_active_packs
            if state.get('app.active_tab') == 'PACKS':
                state['assets.carousel.index'] = Carousel('packs.carousel', packs, state).selected(state)
            else:
                state['assets.carousel.index'] = next((i for i,p in enumerate(packs)
                    if _normalized_path(p.path)==_normalized_path(ctx.scene.mcbedrock_browser_root)),0)
            try:
                if not Carousel('assets.carousel', packs, state).load_into_browser(ctx,state):
                    return
            except Exception as exc:
                # A pack whose folder or file is gone still opens the page, which
                # offers Locate / Remove; other errors stay on this page.
                from .make_panels import show_missing_pack
                if not show_missing_pack(state, Carousel('assets.carousel', packs, state)):
                    state['app.status']='Error: '+str(exc)
                    return
        if value == 'PARTICLE' and state.get('app.active_tab') != 'PARTICLE':
            state['particle.return_page'] = state.get('app.active_tab', 'PACKS')
        elif value != 'PARTICLE':
            state.pop('particle.return_page', None)
        state["app.active_tab"] = value
        state.pop("_menu", None)
    tabs = [PageTab("app.tab."+value, 'Packs' if narrow and value=='PACKS' else label, lambda ctx,value=value:select(ctx,value),
                    selected=value==active, style=Style(height=28*control_scale, flex=1, font_size=10.5*control_scale,
                    font_weight='Bold' if value==active else 'Medium', align='CENTER', radius=2,padding=(3,0,3,0)))
            for value,label in (("PACKS","Resource Packs"),("ASSETS","Assets"))]
    navigation = ModeDropdown('app.navigation',
        lambda ctx: state.get('app.active_tab') if state.get('app.active_tab') in {'QUICK', 'SCHEMATIC', 'PARTICLE', 'SKY', 'OUTLINES', 'POSTERIZE'} else 'PACKS',
        select, [('PACKS','Library'),('QUICK','Quick Import'),('SCHEMATIC','Schematics'),('POSTERIZE','Posterize'),('SKY','Sky & Lighting'),('OUTLINES','Outlines')]
        + ([('PARTICLE','Particle Settings')] if has_particles else []),
        icons=True, style=Style(width=min(max(0,width-32),(112 if active in {'PACKS','ASSETS'} else 176 if active=='PARTICLE' else 144)*control_scale),height=24*control_scale,font_size=12*control_scale))
    heading = Row("app.heading", [
        Label("app.title", "", style=Style(height=32,flex=1,font_size=12 if width<280 else 13,font_weight='Medium')),
        navigation],style=Style(height=28*control_scale,gap=8))
    if narrow:
        heading=Row('app.heading',[navigation],style=Style(height=28,gap=0))
    if active == 'SETTINGS':
        from .settings_page import build_settings, toggle_settings
        heading = Row('app.heading', [Button('settings.back','Back',lambda ctx:toggle_settings(state),style=Style(width=58,height=28,ghost=True)), Label('settings.heading','Settings',style=Style(flex=1,height=28,font_size=14,font_weight='SemiBold'))],style=Style(height=28,gap=8))
        page = build_settings(context,state)
    elif active in {'SCHEMATIC', 'PARTICLE', 'SKY', 'OUTLINES', 'POSTERIZE'}:
        content = {'SCHEMATIC':build_schematic,'PARTICLE':build_particle,'SKY':build_sky,'OUTLINES':build_outlines,'POSTERIZE':build_posterize}[active](context,state)
        page = ScrollView('app.scroll.'+active.lower(),content,style=Style(flex=1))
    else:
        page = {'PACKS':build_packs,'ASSETS':build_assets,'QUICK':build_quick_import}[active](context,state)
    # Keep the primary asset action outside the scrolling content so shorter
    # windows and saved panel heights cannot clip it below the gallery.
    asset_actions = []
    if active == 'ASSETS':
        asset_actions = [child for child in page.child.children
                         if child.key == 'assets.import_selected']
        if asset_actions:
            page.child.set_children(child for child in page.child.children
                                    if child.key != 'assets.import_selected')
    from .settings_page import Footer
    app=MakeApplication("app.root", [heading,HeaderDivider(),
        *([Row("app.tabs",tabs,style=Style(height=28*control_scale+4,padding=(2,2,2,2),gap=2,
                                      background=theme.surface_raised,radius=4))] if active in {'PACKS','ASSETS'} else []),
        page,
        *asset_actions, *([Footer(state)] if THEME_SETTINGS_ENABLED else [])],
        style=Style(padding=(16,12,16,8),gap=8,background=theme.canvas,radius=8))
    app.context_source=context_menu(context,state)
    return app


class MCBEDROCK_OT_open_styled_ui(bpy.types.Operator):
    bl_idname = "mcbedrock.open_styled_ui"
    bl_label = "Open Styled Interface"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        runtime.retry_mount(context)
        runtime.request_mount(context, build_app)
        if context.area:
            context.area.tag_redraw()
        return {"FINISHED"}


class MCBEDROCK_OT_restore_native_ui(bpy.types.Operator):
    bl_idname = "mcbedrock.restore_native_ui"
    bl_label = "Use Native Interface"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        _set_legacy(context, True)
        return {"FINISHED"}


class MCBEDROCK_PT_custom_ui_host(bpy.types.Panel):
    bl_label = "Sparkle"
    bl_idname = "MCBEDROCK_PT_custom_ui_host"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Sparkle"
    bl_order = -100
    bl_options = {"HIDE_HEADER"}

    @classmethod
    def poll(cls, context):
        return not legacy_ui_enabled(context)

    def draw(self, context):
        runtime.request_mount(context, build_app)
        state = runtime.mount_state(context)
        if state == "FAILED":
            box = self.layout.box()
            box.label(text="Interface unavailable",icon="ERROR")
            reason = runtime.failure_reason(context)
            if reason:
                # Without the reason, Retry and native-UI were guesses.
                column = box.column(align=True)
                column.scale_y = .8
                for line in textwrap.wrap(reason, 40)[:4]:
                    column.label(text=line)
            box.operator("mcbedrock.open_styled_ui", text="Retry")
            box.operator("mcbedrock.restore_native_ui")



classes = (MCBEDROCK_OT_sky_color, MCBEDROCK_OT_sky_pack, MCBEDROCK_OT_sky_value, MCBEDROCK_OT_sky_value_edit, MCBEDROCK_OT_ui_edit_property, MCBEDROCK_OT_open_styled_ui,
           MCBEDROCK_OT_restore_native_ui, MCBEDROCK_PT_custom_ui_host)


def register():
    global _refresh_passes
    from . import workspace_tab
    from . import posterize_page
    from . import outlines_page, flat_page, masks_page
    from ..builders import sky, outlines, flat_lighting, render_masks, fog_planes
    sky.register()
    outlines.register()
    flat_lighting.register()
    render_masks.register()
    fog_planes.register()
    from ..builders import bedrock_culling, light_mix_cleanup, node_layout
    light_mix_cleanup.register()
    node_layout.register()
    bedrock_culling.register()
    from . import native_panels
    native_panels.register()
    posterize_page.register()
    runtime.register()
    from .styled_pages import SCENE_SCAN_HANDLERS, forget_scene_scans
    for name in SCENE_SCAN_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if forget_scene_scans not in handlers:
            handlers.append(forget_scene_scans)
    for cls in classes + outlines_page.CLASSES + flat_page.CLASSES + masks_page.CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mcbedrock_custom_ui_page = bpy.props.EnumProperty(
        name="Sparkle Page", items=[(v,n,n) for v,n in TAB_ITEMS], default="ASSETS", options={"SKIP_SAVE"})
    workspace_tab.register()
    _refresh_passes=0
    if not bpy.app.background and not bpy.app.timers.is_registered(_refresh_registered_panels):
        bpy.app.timers.register(_refresh_registered_panels,first_interval=0.)


def unregister():
    from . import workspace_tab
    from . import posterize_page
    from . import outlines_page, flat_page, masks_page
    from ..builders import sky, outlines, flat_lighting, render_masks, fog_planes
    if bpy.app.timers.is_registered(_refresh_registered_panels):
        bpy.app.timers.unregister(_refresh_registered_panels)
    from .styled_pages import SCENE_SCAN_HANDLERS, forget_scene_scans
    for name in SCENE_SCAN_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if forget_scene_scans in handlers:
            handlers.remove(forget_scene_scans)
    forget_scene_scans()
    workspace_tab.unregister()
    posterize_page.unregister()
    runtime.unregister()
    if hasattr(bpy.types.Scene, "mcbedrock_custom_ui_page"):
        del bpy.types.Scene.mcbedrock_custom_ui_page
    for cls in reversed(classes + outlines_page.CLASSES + flat_page.CLASSES + masks_page.CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    fog_planes.unregister()
    from . import native_panels
    native_panels.unregister()
    from ..builders import bedrock_culling, light_mix_cleanup, node_layout
    bedrock_culling.unregister()
    node_layout.unregister()
    light_mix_cleanup.unregister()
    render_masks.unregister()
    flat_lighting.unregister()
    outlines.unregister()
    # Partial registration may fail before the Scene pointer is installed.
    sky.unregister()

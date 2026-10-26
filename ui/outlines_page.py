"""Outlines page: add hand-painted style lines to models, rigs and worlds."""
import math

import bpy
from .gpu_framework import Column, Label, Rect, Style, Toggle
from .gpu_framework.motion import tween, blend
from .gpu_framework.navigation import Dropdown
from .styled_pages import button, actions, invoke, scene_scan
from .sky_page import (SkySlider, SkySectionHeader, SkyGroup, Segmented, Segment, SwatchButton, QuietButton,
                       DetailsToggle, FIELD_LABEL, text_width, hint)
from ..builders import outlines

PERCENT = {'world_lines', 'light_falloff', 'ink_opacity', 'ink_depth', 'ink_fade', 'ink_taper', 'ink_gradient', 'rim_opacity', 'rim_light_tint',
           'rim_fade', 'rim_taper', 'rim_gradient', 'edge_highlight', 'edge_shade', 'edge_follow_light', 'bevel_strength', 'bevel_light',
           'gradient', 'weight_variation', 'stroke_opacity', 'stroke_glow_strength'}
PIXELS = {'ink_width', 'rim_width', 'edge_width', 'stroke_size', 'stroke_softness', 'stroke_glow'}
SECTION_KEYS = ('rim', 'ink', 'edges', 'bevel', 'shading', 'stroke', 'distance')
# Fade starts spans 1 to 1000 blocks: on a log track a close-up's few blocks
# get as much room as a world's hundreds.
LOG = {'fade_distance'}
# Everyday sliders that stand for a whole layer read Off while it is off,
# and moving one turns it back on, so none of them sits there doing nothing.
LAYERS = {'rim_opacity': 'rim', 'rim_glow': 'rim', 'ink_opacity': 'ink', 'edge_highlight': 'edges'}
_switched = set()        # (scene, layer) turned on by a drag still in progress


class OutlineSlider(SkySlider):
    """A Sky & Lighting slider bound to the scene's outline settings."""

    def __init__(self, settings, name, label=None):
        super().__init__(settings, name, label or settings.bl_rna.properties[name].name)
        self.key = 'outline.slider.' + name
        self.pending_key = 'outline.pending.' + name
        meta = self.meta
        self.low, self.high = meta.soft_min, meta.soft_max
        self.high = max(self.high, getattr(settings, name))
        self.layer = LAYERS.get(name)

    def fraction_of(self, value):
        if self.name in LOG:
            return max(0, min(1, math.log(max(value, self.low) / self.low) / math.log(self.high / self.low)))
        return super().fraction_of(value)

    def value_at(self, fraction):
        if self.name in LOG:
            return self.low * (self.high / self.low) ** fraction
        return super().value_at(fraction)

    def off(self):
        return self.layer is not None and not getattr(self.settings, self.layer)

    def text_for(self, value):
        if self.name in PERCENT:
            return f'{value * 100:.0f}%'
        if self.name in PIXELS:
            return f'{value:.1f} px'
        if self.name == 'line_size':
            return f'{value:.2f}×'
        if self.name == 'rim_spread':
            return f'{value:+.2f}'
        if self.name == 'bevel_size':
            return f'{value:.3f}'
        if self.name == 'fade_distance':
            return f'{value:.1f} blocks' if value < 10 else f'{value:.0f} blocks'
        return f'{value:.2f}'

    def draw(self, renderer, state, theme):
        r = self.rect
        value = state.get(self.pending_key, getattr(self.settings, self.name))
        off = self.pending_key not in state and self.off()
        fraction = 0. if off else self.fraction_of(value)
        text = 'Off' if off else self.text_for(value)
        dragging = (state.get('_sky_drag') or (None,))[0] == self.key
        keyboard = state.get('_runtime_focus') == self.key and state.get('_focus_visible')
        active = tween(state, self.key + '.active', 1. if dragging else 0., .14)
        pill_width = min(r.width * .6, text_width(renderer, text, 10) + 14)
        pill = Rect(r.right - pill_width, r.y + 24, pill_width, 19)
        renderer.rect(pill, blend(theme.surface_raised, theme.selected, active), radius=4)
        renderer.text(pill, text, theme.text, size=10, align='CENTER', cap_center=True)
        renderer.text(Rect(r.x, r.y + 21, max(0, pill.x - r.x - 6), 25), self.label,
                      blend(theme.text_muted, theme.text, active * .7), size=11, cap_center=True)
        track = Rect(r.x, r.y + 10, r.width, 4)
        x, y = r.x + 5 + (r.width - 10) * fraction, track.y + 2
        renderer.rect(track, blend(theme.surface_raised, theme.surface_hover, active), radius=2)
        renderer.rect(Rect(track.x, track.y, x - track.x, 4), theme.accent, radius=2)
        if active > .6:
            halo = 22 * (active - .6) / .4
            renderer.rect(Rect(x - halo / 2, y - halo / 2, halo, halo), theme.accent[:3] + (.25,), radius=halo / 2)
        knob = 10 + 4 * active
        renderer.rect(Rect(x - knob / 2, y - knob / 2, knob, knob), theme.text, radius=knob / 2)
        if keyboard:
            ring = knob + 6
            renderer.rect(Rect(x - ring / 2, y - ring / 2, ring, ring), (0, 0, 0, 0), radius=ring / 2,
                          border=theme.text, border_width=1.5)

    def preview(self, state):
        from time import monotonic
        value = state.get(self.pending_key, getattr(self.settings, self.name))
        if abs(getattr(self.settings, self.name) - value) <= 1e-6 and not self.off():
            return
        now = monotonic()
        if now - state.get('_sky_preview_time', 0) >= 1 / 30:
            if self.off():
                _switched.add((self.settings.id_data.as_pointer(), self.layer))
                setattr(self.settings, self.layer, True)
            # Integer settings such as fog Layers reject the slider's float.
            setattr(self.settings, self.name, int(round(value)) if self.meta.type == 'INT' else value)
            state['_sky_preview_time'] = now

    def _switch_back(self):
        """Turn off a layer this drag turned on: cancelled, or about to be committed with undo."""
        key = (self.settings.id_data.as_pointer(), self.layer)
        if key in _switched:
            _switched.discard(key)
            setattr(self.settings, self.layer, False)

    def cancel(self, value):
        setattr(self.settings, self.name, value)
        self._switch_back()

    def snapshot(self):
        # Whether this slider's layer was on: a click on an Off slider turns it on.
        return getattr(self.settings, self.layer) if self.layer else None

    def revert(self, value, snapshot):
        self.cancel(value)
        if self.layer and snapshot is False and getattr(self.settings, self.layer):
            setattr(self.settings, self.layer, False)

    def restore(self, context, value):
        scene = context.scene
        outlines.BUSY.add(scene.as_pointer())
        try:
            setattr(self.settings, self.name, value)
            self._switch_back()
        finally:
            outlines.BUSY.discard(scene.as_pointer())

    def commit(self, value):
        bpy.ops.mcbedrock.outline_value(name=self.name, value=value)

    def edit(self):
        bpy.ops.mcbedrock.outline_value_edit('INVOKE_DEFAULT', name=self.name, label=self.label)


class OutlineSwatch(SwatchButton):
    def __init__(self, settings, field, label):
        super().__init__('outline.color.' + field, label, invoke('mcbedrock.outline_color', field=field), settings, field)


def build_outlines(context, state):
    scene = context.scene
    settings = scene.mcbedrock_outlines
    def summary():
        objects = outlines.outlined_objects(scene)
        return len(objects), any(obj.get(outlines.WORLD_FLAG) for obj in objects)
    outlined, world_lines = scene_scan(scene, 'outlined', summary)
    worlds = outlines.world_collections(scene)
    children = []

    def caption(key, text):
        return Label('outline.caption.' + key, text, muted=True, style=Style(height=20, font_size=10.5, font_weight='SemiBold'))

    def note(key, text):
        # Status lines carry the useful part at their end: wrap, never cut.
        return hint('outline.note.' + key, text)

    def act(key, label, operation, primary=False, **extra):
        return button('outline.' + key, label, 'mcbedrock.outlines', primary=primary, action=operation,
                      invoke_default=False, **extra)

    if not outlined:
        # Until something has outlines the page offers one thing: add them.
        children.append(act('add.selected', 'Add outlines to selection', 'ADD_SELECTED', primary=True))
        if worlds:
            children.append(act('add.worlds', 'Add outlines to imported world' + ('s' if len(worlds) > 1 else ''), 'ADD_WORLDS'))
        children.append(note('start', 'Select a model or rig, or import a world.'))
        if scene.mcbedrock_outlines_status:
            children.append(note('status', scene.mcbedrock_outlines_status))
        from .masks_page import masks_section
        children.append(masks_section(context, state))
        return Column('outline.page', children, style=Style(gap=8, padding=(0, 0, 12, 0)))


    def slider(name, label=None):
        return OutlineSlider(settings, name, label)

    def sliders(names):
        return [slider(name) for name in names]

    def toggle(name, label, key=None):
        return Toggle('outline.' + (key or name), label, lambda ctx: getattr(settings, name),
                      lambda ctx, value: bpy.ops.mcbedrock.outline_value(name=name, value=float(value)), style=Style(height=32))

    def choices(name, options):
        value = getattr(settings, name)
        return Segmented('outline.' + name, [
            Segment('outline.' + name + '.' + option.lower(), label,
                    lambda ctx, option=option: bpy.ops.mcbedrock.outline_value(name=name, text=option), value == option)
            for option, label in options])

    def labelled(key, text):
        return Label('outline.label.' + key, text, muted=True, style=FIELD_LABEL)

    def dropdown(name, label):
        items = [(item.identifier, item.name) for item in settings.bl_rna.properties[name].enum_items]
        return [labelled(name, label),
                Dropdown('outline.' + name, lambda ctx: getattr(settings, name),
                         lambda ctx, value: bpy.ops.mcbedrock.outline_value(name=name, text=value),
                         items, style=Style(height=32))]

    def group(key, title, everyday, detail_builder=None, default_open=False):
        heading = SkySectionHeader('outline.' + key, title, state)
        heading.expanded = state.get('sky.section.open.outline.' + key, default_open)
        controls = [heading]
        if not heading.expanded:
            state.pop('outline.section.' + key + '.opened', None)
            children.append(SkyGroup('outline.section.' + key, controls, False, style=Style(padding=(0, 6, 0, 6))))
            return
        controls.extend(everyday)
        if detail_builder is not None:
            expanded = state.get('outline.details.' + key, False)

            def expand(ctx, key=key):
                state['outline.details.' + key] = not state.get('outline.details.' + key, False)
            controls.append(DetailsToggle('outline.details.' + key, expanded, expand))
            if expanded:
                controls.extend(detail_builder())
        children.append(SkyGroup('outline.section.' + key, controls, True, style=Style(padding=(0, 6, 0, 6), gap=8)))

    # Everyday controls: a style, one width for every line and how strong each kind is.
    style = Segmented('outline.style', [
        Segment('outline.style.' + key.lower(), label,
                lambda ctx, key=key: bpy.ops.mcbedrock.outline_value(name='style', text=key), settings.style == key)
        for key, label in (('EDGES', 'Edge lights'), ('BOLD', 'Bold'), ('STICKER', 'Sticker'))])
    everyday = [style, slider('line_size', 'Thickness'), slider('rim_glow', 'Glow'), slider('rim_opacity', 'Rim'),
                slider('ink_opacity', 'Ink'),
                slider('edge_highlight', 'Inner edges'), slider('gradient', 'Face shading')]
    if world_lines:
        everyday.append(slider('world_lines', 'World edges'))
    if settings.rim_color_mode == 'CUSTOM':
        everyday.append(OutlineSwatch(settings, 'rim_color', 'Rim color'))
    children.append(SkyGroup('outline.section.everyday', [Label('outline.everyday.title', 'Lines',
                                                                style=Style(height=32, font_size=12, font_weight='SemiBold'))]
                             + everyday, True, style=Style(padding=(0, 6, 0, 6), gap=8)))

    more = state.get('outline.more', False)

    def toggle_more(ctx):
        state['outline.more'] = not state.get('outline.more', False)
    children.append(DetailsToggle('outline.more', more, toggle_more, label='Hide controls' if more else 'More controls',
                                  tooltip='Every line setting: widths, colors, blend modes, fades and the stroke'))
    if more:
        # Rim: the painted, light-colored line where light reaches the outline.
        rim = [toggle('rim', 'Rim on the lit side')]
        if settings.rim:
            rim += sliders(('rim_width', 'rim_glow', 'light_falloff', 'rim_light_tint'))
            rim += [labelled('rim_color_mode', 'Color'),
                    choices('rim_color_mode', (('CUSTOM', 'Custom'), ('SURFACE', 'Surface'), ('LIGHT', 'Lights')))]
            rim += dropdown('rim_blend', 'Blend')
            rim += sliders(('rim_fade', 'rim_taper', 'rim_spread', 'rim_gradient'))
            if settings.rim_gradient > 0:
                rim.append(OutlineSwatch(settings, 'rim_color_2', 'Inner color'))
        group('rim', 'Rim', rim)

        # Ink: the deeper line on the side turned away from the light.
        ink = [toggle('ink', 'Ink on the shadow side')]
        if settings.ink:
            ink += sliders(('ink_width', 'ink_depth'))
            ink += [labelled('ink_color_mode', 'Color'),
                    choices('ink_color_mode', (('SURFACE', 'From surface'), ('CUSTOM', 'Custom')))]
            if settings.ink_color_mode == 'CUSTOM':
                ink.append(OutlineSwatch(settings, 'ink_color', 'Ink color'))
            ink += [labelled('ink_side', 'Where'), choices('ink_side', (('SHADOW', 'Shadow side'), ('ALL', 'All round')))]
            ink += dropdown('ink_blend', 'Blend')
            ink += sliders(('ink_fade', 'ink_taper', 'weight_variation', 'ink_gradient'))
            if settings.ink_gradient > 0:
                ink.append(OutlineSwatch(settings, 'ink_color_2', 'Inner color'))
        group('ink', 'Ink', ink)

        # Inner edges: lighter and darker lines inside a shape.
        edges = [toggle('edges', 'Inner edge lines')]
        if settings.edges:
            edges += sliders(('edge_width', 'edge_shade', 'edge_follow_light'))
            edges.append(OutlineSwatch(settings, 'edge_color', 'Edge light color'))
            edges += dropdown('edge_highlight_blend', 'Highlight blend') + dropdown('edge_shade_blend', 'Shade blend')
        group('edges', 'Inner edges', edges)

        bevel = [toggle('bevel', 'Rounded, light-catching edges')]
        if settings.bevel:
            bevel += sliders(('bevel_size', 'bevel_strength', 'bevel_light'))
        group('bevel', 'Bevel', bevel)

        # Stroke: the compositor pass that paints along the whole silhouette.
        stroke = [toggle('stroke', 'Stroke in the compositor')]
        if settings.stroke:
            stroke += [labelled('stroke_side', 'Side'),
                       choices('stroke_side', (('LIGHT', 'Lit side'), ('ALL', 'All round'), ('SHADOW', 'Shadow side'))),
                       labelled('stroke_position', 'Position'),
                       choices('stroke_position', (('INSIDE', 'On the edge'), ('CENTER', 'Center'), ('OUTSIDE', 'Outside')))]
            stroke += sliders(('stroke_size', 'stroke_opacity', 'stroke_light', 'stroke_softness', 'stroke_glow'))
            stroke += dropdown('stroke_blend', 'Blend')
            stroke.append(OutlineSwatch(settings, 'stroke_color', 'Shadow side color'))
            stroke.append(note('stroke.render', 'Shows in renders and with viewport compositing.'))
        group('stroke', 'Stroke', stroke)

        group('distance', 'Distance', [slider('fade_distance'), toggle('show_masks', 'Show line masks')])

    # Who has outlines, and changes to that.
    count = outlined
    children.append(caption('targets', f'On {count} object' + ('' if count == 1 else 's')))
    children.append(actions('outline.targets', [act('add.more', 'Add selected', 'ADD_SELECTED'),
                                                 act('remove.selected', 'Remove selected', 'REMOVE_SELECTED')]))
    if worlds and any(not collection.get(outlines.WORLD) for collection in worlds):
        children.append(act('add.worlds', 'Add imported worlds', 'ADD_WORLDS'))
    if scene.mcbedrock_outlines_status:
        children.append(note('status', scene.mcbedrock_outlines_status))
    children.append(QuietButton('outline.refresh', 'Refresh edges', invoke('mcbedrock.outlines', invoke=False, action='REFRESH'),
                                style=Style(height=32), tooltip='Recompute edges after editing outlined geometry'))
    from .masks_page import masks_section
    children.append(masks_section(context, state))
    children.append(QuietButton('outline.reset', 'Reset settings', invoke('mcbedrock.outlines', invoke=False, action='RESET'),
                                style=Style(height=32), tooltip='Put every outline setting back to its default'))
    children.append(QuietButton('outline.remove', 'Remove all outlines', invoke('mcbedrock.outlines', invoke=False, action='REMOVE_ALL'),
                                style=Style(height=32), tooltip='Remove outlines from everything in this scene'))
    return Column('outline.page', children, style=Style(gap=8, padding=(0, 0, 12, 0)))


# ---------------------------------------------------------------- operators

EDITABLE = set()


def _editable():
    if not EDITABLE:
        EDITABLE.update(outlines.OutlineSettings.__annotations__)
    return EDITABLE


class MCBEDROCK_OT_outline_value(bpy.types.Operator):
    bl_idname = 'mcbedrock.outline_value'
    bl_label = 'Adjust outlines'
    bl_options = {'UNDO', 'INTERNAL'}
    name: bpy.props.StringProperty()
    value: bpy.props.FloatProperty()
    text: bpy.props.StringProperty()

    def execute(self, context):
        settings = context.scene.mcbedrock_outlines
        if self.name == 'style':
            # A one-click style: every line setting at once.
            if self.text not in outlines.STYLES:
                return {'CANCELLED'}
            outlines.apply_style(context.scene, self.text)
            return {'FINISHED'}
        if self.name not in _editable():
            return {'CANCELLED'}
        meta = settings.bl_rna.properties[self.name]
        if meta.type == 'ENUM':
            if self.text not in {item.identifier for item in meta.enum_items}:
                return {'CANCELLED'}
            setattr(settings, self.name, self.text)
        elif meta.type == 'BOOLEAN':
            setattr(settings, self.name, bool(self.value))
        elif meta.type == 'FLOAT' and not getattr(meta, 'is_array', False):
            setattr(settings, self.name, max(meta.hard_min, min(meta.hard_max, self.value)))
            layer = LAYERS.get(self.name)
            if layer and self.value > 0 and not getattr(settings, layer):
                setattr(settings, layer, True)
        else:
            return {'CANCELLED'}
        return {'FINISHED'}


class MCBEDROCK_OT_outline_value_edit(bpy.types.Operator):
    bl_idname = 'mcbedrock.outline_value_edit'
    bl_label = 'Set value'
    bl_description = 'Type an exact value for this setting'
    bl_options = {'INTERNAL'}
    name: bpy.props.StringProperty(options={'HIDDEN'})
    label: bpy.props.StringProperty(options={'HIDDEN'})
    value: bpy.props.FloatProperty(name='Value')

    def invoke(self, context, event):
        settings = context.scene.mcbedrock_outlines
        if self.name not in _editable():
            return {'CANCELLED'}
        self.value = getattr(settings, self.name)
        return context.window_manager.invoke_props_dialog(self, width=240, title=self.label or 'Set value')

    def draw(self, context):
        self.layout.prop(self, 'value', text=self.label or 'Value')

    def execute(self, context):
        return bpy.ops.mcbedrock.outline_value(name=self.name, value=self.value)


COLOR_FIELDS = {'rim_color', 'rim_color_2', 'ink_color', 'ink_color_2', 'stroke_color', 'edge_color'}


class MCBEDROCK_OT_outline_color(bpy.types.Operator):
    bl_idname = 'mcbedrock.outline_color'
    bl_label = 'Choose color'
    bl_options = {'UNDO'}
    field: bpy.props.StringProperty()
    color: bpy.props.FloatVectorProperty(subtype='COLOR', size=3, min=0, max=1)

    def invoke(self, context, event):
        if self.field not in COLOR_FIELDS:
            return {'CANCELLED'}
        self.color = getattr(context.scene.mcbedrock_outlines, self.field)
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        self.layout.template_color_picker(self, 'color', value_slider=True)
        self.layout.prop(self, 'color', text='')

    def execute(self, context):
        if self.field not in COLOR_FIELDS:
            return {'CANCELLED'}
        setattr(context.scene.mcbedrock_outlines, self.field, self.color)
        return {'FINISHED'}


CLASSES = (MCBEDROCK_OT_outline_value, MCBEDROCK_OT_outline_value_edit, MCBEDROCK_OT_outline_color)

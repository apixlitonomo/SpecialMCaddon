"""Flat lighting controls, shown on the Sky & Lighting page while the mode is on."""
import bpy
from .gpu_framework import Column, Label, Style, Toggle
from .styled_pages import button, invoke
from .live_color import SettingColor
from .sky_page import SkyGroup, Segmented, Segment, SwatchButton, QuietButton, FIELD_LABEL, hint
from .outlines_page import OutlineSlider
from ..builders import flat_lighting

PERCENT = {'shading', 'shadow_floor', 'ground_shadow', 'ground_fade', 'gradient_position', 'gradient_spread',
           'center_x', 'center_y'}
DEGREES = {'softness', 'gradient_angle'}
COLOR_FIELDS = {'color', 'top_color', 'middle_color', 'shadow_tint', 'light_color'}
FLOATS = {'shading', 'shadow_floor', 'ground_shadow', 'ground_fade', 'softness', 'strength', 'gradient_position',
          'gradient_spread', 'gradient_angle', 'center_x', 'center_y'}
ENUMS = {'background', 'gradient_style'}
BOOLS = {'shadows', 'ground', 'flat_faces', 'use_middle', 'gradient_smooth'}
# What the gradient's ends are called in each style: (top, middle, bottom).
GRADIENT_NAMES = {'LINEAR': ('Top', 'Middle', 'Bottom'), 'RADIAL': ('Outside', 'Middle', 'Center'),
                  'HORIZON': ('Sky', 'Horizon', 'Ground')}


class FlatSlider(OutlineSlider):
    def __init__(self, settings, name, label=None):
        super().__init__(settings, name, label)
        self.key = 'flat.slider.' + name

    def text_for(self, value):
        if self.name in PERCENT:
            return f'{value * 100:.0f}%'
        if self.name in DEGREES:
            return f'{value:.0f}°'
        return f'{value:.2f}'

    def restore(self, context, value):
        setattr(self.settings, self.name, value)

    def commit(self, value):
        bpy.ops.mcbedrock.flat_value(name=self.name, value=value)

    def edit(self):
        bpy.ops.mcbedrock.flat_value_edit('INVOKE_DEFAULT', name=self.name, label=self.label)


class FlatSwatch(SwatchButton):
    def __init__(self, settings, field, label):
        super().__init__('flat.color.' + field, label, invoke('mcbedrock.flat_color', field=field), settings, field)


def build_flat(context, state):
    settings = context.scene.mcbedrock_flat

    def background_choice(key, label):
        return Segment('flat.background.' + key.lower(), label,
                       lambda ctx, key=key: bpy.ops.mcbedrock.flat_value(name='background', text=key),
                       settings.background == key)

    current = flat_lighting.look(settings)

    def look_choice(key, label):
        return Segment('flat.look.' + key.lower(), label,
                       lambda ctx, key=key: bpy.ops.mcbedrock.flat_value(name='look', text=key), current == key)

    lighting = [Label('flat.lighting.title', 'Flat lighting', style=Style(height=32, font_size=12, font_weight='SemiBold')),
                Segmented('flat.look', [look_choice('STUDIO', 'Sharp'), look_choice('FALLOFF', 'Soft shadows'),
                                         look_choice('SOFT', 'Soft light')]),
                FlatSlider(settings, 'shading', 'Shading'),
                FlatSlider(settings, 'shadow_floor', 'Shadow brightness'),
                FlatSlider(settings, 'strength', 'Light strength'),
                FlatSlider(settings, 'softness', 'Light size'),
                Toggle('flat.shadows', 'Shadows', lambda ctx: settings.shadows,
                       lambda ctx, value: bpy.ops.mcbedrock.flat_value(name='shadows', value=float(value)),
                       style=Style(height=32)),
                Toggle('flat.ground', 'Ground shadow', lambda ctx: settings.ground,
                       lambda ctx, value: bpy.ops.mcbedrock.flat_value(name='ground', value=float(value)),
                       style=Style(height=32)),
                *([FlatSlider(settings, 'ground_shadow', 'Ground shadow'), FlatSlider(settings, 'ground_fade', 'Shadow fade')]
                  if settings.ground else []),
                Toggle('flat.faces', 'Flat faces', lambda ctx: settings.flat_faces,
                       lambda ctx, value: bpy.ops.mcbedrock.flat_value(name='flat_faces', value=float(value)),
                       style=Style(height=32)),
                FlatSwatch(settings, 'shadow_tint', 'Shadow tint'),
                FlatSwatch(settings, 'light_color', 'Light color'),
                button('flat.aim', 'Aim light from this view', 'mcbedrock.flat_aim', invoke_default=False),
                hint('flat.light.help', 'Or rotate the Sparkle Flat light to move the shadows.')]
    background = [Label('flat.background.title', 'Background', style=Style(height=32, font_size=12, font_weight='SemiBold')),
                  Segmented('flat.background', [background_choice('SOLID', 'Color'), background_choice('GRADIENT', 'Gradient'),
                                                background_choice('TRANSPARENT', 'None')])]
    if settings.background == 'SOLID':
        background.append(FlatSwatch(settings, 'color', 'Color'))
    elif settings.background == 'GRADIENT':
        background += _gradient(settings)
    else:
        background.append(hint('flat.background.help', 'Renders with a transparent background.'))
    children = [
        SkyGroup('flat.section.lighting', lighting, True, style=Style(padding=(0, 6, 0, 6), gap=8)),
        SkyGroup('flat.section.background', background, True, style=Style(padding=(0, 6, 0, 6), gap=8)),
        hint('flat.help', 'Live in the Rendered viewport. Outlines work on top.'),
        _fog(context, state),
        _masks(context, state),
        QuietButton('flat.remove', 'Turn off flat lighting', invoke('mcbedrock.flat_lighting', invoke=False, enable=False),
                    style=Style(height=32), tooltip='Put back your world, colors and compositor'),
    ]
    return children


def _gradient(settings):
    def style_choice(key, label):
        return Segment('flat.gradient.' + key.lower(), label,
                       lambda ctx, key=key: bpy.ops.mcbedrock.flat_value(name='gradient_style', text=key),
                       settings.gradient_style == key)

    def toggle(name, label):
        return Toggle('flat.' + name, label, lambda ctx: getattr(settings, name),
                      lambda ctx, value: bpy.ops.mcbedrock.flat_value(name=name, value=float(value)),
                      style=Style(height=32))

    style = settings.gradient_style
    top, middle, bottom = GRADIENT_NAMES.get(style, GRADIENT_NAMES['LINEAR'])
    rows = [Segmented('flat.gradient', [style_choice('LINEAR', 'Linear'), style_choice('RADIAL', 'Radial'),
                                        style_choice('HORIZON', 'Horizon')]),
            FlatSwatch(settings, 'top_color', top)]
    if settings.use_middle:
        rows.append(FlatSwatch(settings, 'middle_color', middle))
    rows += [FlatSwatch(settings, 'color', bottom),
             toggle('use_middle', middle + ' color'),
             FlatSlider(settings, 'gradient_position', 'Position'),
             FlatSlider(settings, 'gradient_spread', 'Spread')]
    if style == 'LINEAR':
        rows.append(FlatSlider(settings, 'gradient_angle', 'Angle'))
    elif style == 'RADIAL':
        rows += [FlatSlider(settings, 'center_x', 'Center X'), FlatSlider(settings, 'center_y', 'Center Y')]
    else:
        rows.append(hint('flat.gradient.help', 'Follows the camera: tilt up for more sky.'))
    rows.append(toggle('gradient_smooth', 'Smooth blend'))
    return rows


def _fog(context, state):
    from .masks_page import fog_planes_section
    return fog_planes_section(context, state)


def _masks(context, state):
    from .masks_page import masks_section
    return masks_section(context, state)


class MCBEDROCK_OT_flat_value(bpy.types.Operator):
    bl_idname = 'mcbedrock.flat_value'
    bl_label = 'Adjust flat lighting'
    bl_options = {'UNDO', 'INTERNAL'}
    name: bpy.props.StringProperty()
    value: bpy.props.FloatProperty()
    text: bpy.props.StringProperty()

    def execute(self, context):
        settings = context.scene.mcbedrock_flat
        if self.name == 'look':
            # Sharp, Soft shadows or Soft light (hexioq's 180-degree sun): both values at once.
            if self.text not in flat_lighting.LOOKS:
                return {'CANCELLED'}
            settings.softness, settings.shadow_floor = flat_lighting.LOOKS[self.text]
            return {'FINISHED'}
        meta = settings.bl_rna.properties.get(self.name)
        if meta is None:
            return {'CANCELLED'}
        if self.name in ENUMS:
            if self.text not in {item.identifier for item in meta.enum_items}:
                return {'CANCELLED'}
            setattr(settings, self.name, self.text)
        elif self.name in BOOLS:
            setattr(settings, self.name, bool(self.value))
        elif self.name in FLOATS:
            setattr(settings, self.name, max(meta.hard_min, min(meta.hard_max, self.value)))
        else:
            return {'CANCELLED'}
        return {'FINISHED'}


class MCBEDROCK_OT_flat_value_edit(bpy.types.Operator):
    bl_idname = 'mcbedrock.flat_value_edit'
    bl_label = 'Set value'
    bl_description = 'Type an exact value for this setting'
    bl_options = {'INTERNAL'}
    name: bpy.props.StringProperty(options={'HIDDEN'})
    label: bpy.props.StringProperty(options={'HIDDEN'})
    value: bpy.props.FloatProperty(name='Value')

    def invoke(self, context, event):
        if self.name not in FLOATS:
            return {'CANCELLED'}
        self.value = getattr(context.scene.mcbedrock_flat, self.name)
        return context.window_manager.invoke_props_dialog(self, width=240, title=self.label or 'Set value')

    def draw(self, context):
        self.layout.prop(self, 'value', text=self.label or 'Value')

    def execute(self, context):
        return bpy.ops.mcbedrock.flat_value(name=self.name, value=self.value)


class MCBEDROCK_OT_flat_color(SettingColor, bpy.types.Operator):
    # The scene follows the picker while it moves (live_color.py).
    bl_idname = 'mcbedrock.flat_color'
    bl_label = 'Choose color'
    bl_options = {'UNDO'}
    settings = 'mcbedrock_flat'
    fields = COLOR_FIELDS


CLASSES = (MCBEDROCK_OT_flat_value, MCBEDROCK_OT_flat_value_edit, MCBEDROCK_OT_flat_color)

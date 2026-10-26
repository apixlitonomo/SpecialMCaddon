"""Photoshop masks section, shared by the Outlines and Flat lighting pages."""
import bpy
from .gpu_framework import Label, Style, Toggle
from .styled_pages import button, invoke
from .sky_page import SkyGroup, QuietButton, hint, PathLabel
from .outlines_page import OutlineSlider
from ..builders import render_masks


def open_value_editor(operator, **properties):
    """Blender's own number dialog, for typing an exact value (double-click)."""
    namespace, name = operator.split('.', 1)
    getattr(getattr(bpy.ops, namespace), name)('INVOKE_DEFAULT', **properties)


class MaskSlider(OutlineSlider):
    def __init__(self, settings, name, label=None):
        super().__init__(settings, name, label)
        self.key = 'masks.slider.' + name
        self.pending_key = 'masks.pending.' + name

    def text_for(self, value):
        return f'{value * 100:.0f}%'

    def restore(self, context, value):
        setattr(self.settings, self.name, value)

    def commit(self, value):
        bpy.ops.mcbedrock.render_masks_value(name=self.name, value=value)

    def edit(self):
        open_value_editor('mcbedrock.render_masks_value_edit', name=self.name, label=self.label)


def masks_section(context, state):
    scene = context.scene
    settings = getattr(scene, 'mcbedrock_masks', None)
    if settings is None:
        return Label('masks.unavailable', '', style=Style(height=0))
    controls = [Label('masks.title', 'Photoshop masks', style=Style(height=32, font_size=12, font_weight='SemiBold')),
                Toggle('masks.enabled', 'Save masks with each render', lambda ctx: settings.active,
                       lambda ctx, value: bpy.ops.mcbedrock.render_masks(enable=bool(value)), style=Style(height=32))]
    if settings.active:
        controls += [
            # A long folder loses its middle, never the folder's own name.
            PathLabel('masks.folder', 'Folder: ', settings.folder or '//masks/', muted=True),
            QuietButton('masks.choose', 'Choose folder…', invoke('mcbedrock.render_masks_folder'), style=Style(height=32)),
            Toggle('masks.ids', 'Character masks', lambda ctx: settings.ids,
                   lambda ctx, value: bpy.ops.mcbedrock.render_masks_value(name='ids', value=float(value)),
                   style=Style(height=32)),
            MaskSlider(settings, 'light_threshold', 'Light threshold'),
            MaskSlider(settings, 'edge_softness', 'Mask edge softness'),
            hint('masks.help', 'Light, Shadow, Normals, Depth, outline lines and one mask per character.'),
        ]
    else:
        controls.append(hint('masks.off', 'Clipping masks for painting light and shadow, selecting characters and more.'))
    return SkyGroup('masks.section', controls, True, style=Style(padding=(0, 8, 0, 8), gap=8))


class MCBEDROCK_OT_render_masks_value(bpy.types.Operator):
    bl_idname = 'mcbedrock.render_masks_value'
    bl_label = 'Adjust masks'
    bl_options = {'UNDO', 'INTERNAL'}
    name: bpy.props.StringProperty()
    value: bpy.props.FloatProperty()

    def execute(self, context):
        settings = context.scene.mcbedrock_masks
        if self.name == 'ids':
            settings.ids = bool(self.value)
        elif self.name in {'light_threshold', 'edge_softness'}:
            meta = settings.bl_rna.properties[self.name]
            setattr(settings, self.name, max(meta.hard_min, min(meta.hard_max, self.value)))
        else:
            return {'CANCELLED'}
        return {'FINISHED'}


class MCBEDROCK_OT_render_masks_value_edit(bpy.types.Operator):
    bl_idname = 'mcbedrock.render_masks_value_edit'
    bl_label = 'Set value'
    bl_description = 'Type an exact value for this setting'
    bl_options = {'INTERNAL'}
    name: bpy.props.StringProperty(options={'HIDDEN'})
    label: bpy.props.StringProperty(options={'HIDDEN'})
    value: bpy.props.FloatProperty(name='Value')

    def invoke(self, context, event):
        if self.name not in {'light_threshold', 'edge_softness'}:
            return {'CANCELLED'}
        self.value = getattr(context.scene.mcbedrock_masks, self.name)
        return context.window_manager.invoke_props_dialog(self, width=240, title=self.label or 'Set value')

    def draw(self, context):
        self.layout.prop(self, 'value', text=self.label or 'Value')

    def execute(self, context):
        return bpy.ops.mcbedrock.render_masks_value(name=self.name, value=self.value)


class MCBEDROCK_OT_render_masks_folder(bpy.types.Operator):
    bl_idname = 'mcbedrock.render_masks_folder'
    bl_label = 'Choose Mask Folder'
    bl_description = 'Where the Photoshop masks are saved'
    bl_options = {'INTERNAL'}
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        context.scene.mcbedrock_masks.folder = self.directory
        return {'FINISHED'}


CLASSES = (MCBEDROCK_OT_render_masks_value, MCBEDROCK_OT_render_masks_value_edit, MCBEDROCK_OT_render_masks_folder)


# ---------------------------------------------------------------- fog planes

class FogSlider(OutlineSlider):
    def __init__(self, settings, name, label=None):
        super().__init__(settings, name, label)
        self.key = 'fogplanes.slider.' + name
        self.pending_key = 'fogplanes.pending.' + name

    def text_for(self, value):
        if self.name in {'density', 'glow'}:
            return f'{value * 100:.0f}%'
        if self.name == 'count':
            return f'{value:.0f}'
        return f'{value:.0f} blocks'

    def restore(self, context, value):
        setattr(self.settings, self.name, int(round(value)) if self.name == 'count' else value)

    def commit(self, value):
        bpy.ops.mcbedrock.fog_planes_value(name=self.name, value=value)

    def edit(self):
        open_value_editor('mcbedrock.fog_planes_value_edit', name=self.name, label=self.label)


def fog_planes_section(context, state):
    """Painted-style fog layers: shared by the Sky & Lighting and Flat lighting pages.

    A standard section: it opens by default, keeps Starts/Ends/Density in
    view and the shaping controls behind Show details."""
    from .sky_page import SwatchButton, Segmented, Segment, SkySectionHeader, DetailsToggle
    from ..builders.fog_planes import PENDING
    scene = context.scene
    settings = getattr(scene, 'mcbedrock_fog_planes', None)
    if settings is None:
        return Label('fogplanes.unavailable', '', style=Style(height=0))
    state.setdefault('sky.section.open.distance_fog', True)
    heading = SkySectionHeader('distance_fog', 'Distance fog', state)
    if not heading.expanded:
        state.pop('fogplanes.section.opened', None)
        return SkyGroup('fogplanes.section', [heading], False, style=Style(padding=(0, 6, 0, 6)))
    controls = [heading,
                Toggle('fogplanes.enabled', 'Fog over far land', lambda ctx: settings.active or bool(scene.get(PENDING)),
                       lambda ctx, value: bpy.ops.mcbedrock.fog_planes(enable=bool(value)), style=Style(height=32))]
    if not settings.active and scene.get(PENDING):
        controls.append(hint('fogplanes.pending', 'Appears once the scene has a camera.'))
    if settings.active:
        controls += [FogSlider(settings, name, label) for name, label in (
            ('near', 'Starts'), ('far', 'Ends'), ('density', 'Density'))]
        controls.append(Segmented('fogplanes.color', [
            Segment('fogplanes.color.' + key.lower(), label,
                    lambda ctx, key=key: bpy.ops.mcbedrock.fog_planes_value(name='color_mode', text=key),
                    settings.color_mode == key)
            for key, label in (('AUTO', 'Match sky'), ('CUSTOM', 'Custom'))]))
        if settings.color_mode == 'CUSTOM':
            controls.append(SwatchButton('fogplanes.swatch', 'Fog color', invoke('mcbedrock.fog_planes_color'),
                                         settings, 'color'))
        expanded = state.get('sky.details.distance_fog', False)

        def expand(ctx):
            state['sky.details.distance_fog'] = not state.get('sky.details.distance_fog', False)
        controls.append(DetailsToggle('sky.details.distance_fog', expanded, expand))
        if expanded:
            controls += [FogSlider(settings, name, label) for name, label in (
                ('top', 'Height'), ('fade', 'Softness'), ('soft', 'Soft edges'), ('count', 'Layers'), ('glow', 'Sun glow'))]
            controls.append(Toggle('fogplanes.edge', 'End of world', lambda ctx: settings.edge,
                                   lambda ctx, value: bpy.ops.mcbedrock.fog_planes_value(name='edge', value=float(value)),
                                   style=Style(height=32)))
        controls.append(hint('fogplanes.view', 'Shows in Material and Rendered view.'))
    else:
        controls.append(hint('fogplanes.off', 'Haze over far land. The sky stays clear.'))
    return SkyGroup('fogplanes.section', controls, True, style=Style(padding=(0, 8, 0, 8), gap=8))


class MCBEDROCK_OT_fog_planes_value(bpy.types.Operator):
    bl_idname = 'mcbedrock.fog_planes_value'
    bl_label = 'Adjust fog layers'
    bl_options = {'UNDO', 'INTERNAL'}
    name: bpy.props.StringProperty()
    value: bpy.props.FloatProperty()
    text: bpy.props.StringProperty()

    def execute(self, context):
        settings = context.scene.mcbedrock_fog_planes
        meta = settings.bl_rna.properties.get(self.name)
        if self.name == 'color_mode':
            if self.text not in {'AUTO', 'CUSTOM'}:
                return {'CANCELLED'}
            settings.color_mode = self.text
        elif self.name == 'count':
            settings.count = max(meta.hard_min, min(meta.hard_max, int(round(self.value))))
        elif self.name in {'density', 'near', 'far', 'top', 'fade', 'soft', 'glow'}:
            setattr(settings, self.name, max(meta.hard_min, min(meta.hard_max, self.value)))
        elif self.name == 'edge':
            settings.edge = bool(self.value)
        else:
            return {'CANCELLED'}
        return {'FINISHED'}


FOG_VALUES = {'density', 'near', 'far', 'top', 'fade', 'soft', 'glow', 'count'}


class MCBEDROCK_OT_fog_planes_value_edit(bpy.types.Operator):
    bl_idname = 'mcbedrock.fog_planes_value_edit'
    bl_label = 'Set value'
    bl_description = 'Type an exact value for this setting'
    bl_options = {'INTERNAL'}
    name: bpy.props.StringProperty(options={'HIDDEN'})
    label: bpy.props.StringProperty(options={'HIDDEN'})
    value: bpy.props.FloatProperty(name='Value')

    def invoke(self, context, event):
        if self.name not in FOG_VALUES:
            return {'CANCELLED'}
        self.value = getattr(context.scene.mcbedrock_fog_planes, self.name)
        return context.window_manager.invoke_props_dialog(self, width=240, title=self.label or 'Set value')

    def draw(self, context):
        self.layout.prop(self, 'value', text=self.label or 'Value')

    def execute(self, context):
        if self.name not in FOG_VALUES:
            return {'CANCELLED'}
        return bpy.ops.mcbedrock.fog_planes_value(name=self.name, value=self.value)


class MCBEDROCK_OT_fog_planes_color(bpy.types.Operator):
    bl_idname = 'mcbedrock.fog_planes_color'
    bl_label = 'Choose fog color'
    bl_options = {'UNDO'}
    color: bpy.props.FloatVectorProperty(subtype='COLOR', size=3, min=0, max=1)

    def invoke(self, context, event):
        self.color = context.scene.mcbedrock_fog_planes.color
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        self.layout.template_color_picker(self, 'color', value_slider=True)
        self.layout.prop(self, 'color', text='')

    def execute(self, context):
        context.scene.mcbedrock_fog_planes.color = self.color
        return {'FINISHED'}


CLASSES = CLASSES + (MCBEDROCK_OT_fog_planes_value, MCBEDROCK_OT_fog_planes_value_edit, MCBEDROCK_OT_fog_planes_color)

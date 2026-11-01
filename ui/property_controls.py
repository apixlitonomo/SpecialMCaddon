"""Styled RNA bindings with Blender's own precise property editor."""

import bpy

from .gpu_framework import Button, Rect, Style, Toggle
from .gpu_framework.motion import blend, tween
from .gpu_framework.navigation import Dropdown
from .gpu_framework import runtime


def _target(collection, pointer):
    return next((item for item in getattr(bpy.data, collection, ())
                 if str(item.as_pointer()) == pointer), None)


def swatch_color(value, subtype):
    """A color property as the panel draws it. COLOR values are scene-linear;
    drawn as they are, swatches looked darker than Blender's own color field."""
    def display(channel):
        channel = max(0., min(1., channel))
        return channel * 12.92 if channel <= .0031308 else 1.055 * channel ** (1 / 2.4) - .055
    rgb = tuple(value[:3])
    if subtype == 'COLOR':
        rgb = tuple(display(channel) for channel in rgb)
    return rgb + (1.,)


class MCBEDROCK_OT_ui_edit_property(bpy.types.Operator):
    bl_idname = "mcbedrock.ui_edit_property"
    bl_label = "Edit Setting"
    bl_options = {"INTERNAL"}

    collection: bpy.props.StringProperty()
    pointer: bpy.props.StringProperty()
    property_name: bpy.props.StringProperty()

    def invoke(self, context, event):
        owner = _target(self.collection, self.pointer)
        if not self.property_name.startswith("mcbedrock_") or _resolve(owner, self.property_name)[0] is None:
            return {"CANCELLED"}
        self._owner = owner
        runtime._NATIVE_DIALOG_OPEN = True
        return context.window_manager.invoke_props_dialog(self, width=320)

    def _live_owner(self):
        # The dialog redraws constantly; scanning every object each time lags big scenes.
        owner = getattr(self, "_owner", None)
        try:
            if owner is not None and str(owner.as_pointer()) == self.pointer:
                return owner
        except ReferenceError:
            pass
        return _target(self.collection, self.pointer)

    def draw(self, context):
        owner, name = _resolve(self._live_owner(), self.property_name)
        if owner is not None:
            self.layout.use_property_split = True
            self.layout.use_property_decorate = True
            self.layout.prop(owner, name)

    def execute(self, context):
        runtime._NATIVE_DIALOG_OPEN = False
        return {"FINISHED"}

    def cancel(self, context):
        runtime._NATIVE_DIALOG_OPEN = False


def _resolve(datablock, path):
    """The struct and property name ``path`` names under ``datablock``, or (None, None)."""
    if datablock is None:
        return None, None
    parent, _, name = path.rpartition(".")
    try:
        owner = datablock.path_resolve(parent) if parent else datablock
    except ValueError:
        return None, None
    return (owner, name) if hasattr(owner, name) else (None, None)


def edit_property(owner, name):
    # Settings in a property group (Posterize's live on the window manager)
    # are edited through their datablock and the path to them.
    datablock = owner.id_data
    collection = next((collection for typ, collection in (
        (bpy.types.Scene, "scenes"), (bpy.types.Object, "objects"),
        (bpy.types.Action, "actions"), (bpy.types.WindowManager, "window_managers"))
        if isinstance(datablock, typ)), None)
    path = name if owner == datablock else f"{owner.path_from_id()}.{name}"
    if collection:
        bpy.ops.mcbedrock.ui_edit_property("INVOKE_DEFAULT", collection=collection,
                                         pointer=str(datablock.as_pointer()), property_name=path)


class PropertyField(Button):
    def __init__(self, key, owner, name, label=None, *, enabled=True):
        self.owner, self.name = owner, name
        self.metadata = owner.bl_rna.properties[name]
        self.label = label if label is not None else self.metadata.name
        super().__init__(key, "", lambda context: edit_property(owner, name),
                         style=Style(height=32, align="LEFT", enabled=enabled),
                         tooltip=self.metadata.description)

    def draw(self, renderer, state, theme):
        value = getattr(self.owner, self.name)
        hovered = False
        focused = state.get('_runtime_focus') == self.key and state.get('_focus_visible', False)
        label_rect = Rect(self.rect.x,self.rect.y,max(0,self.rect.width*.52-8),self.rect.height)
        renderer.text(label_rect,self.label,theme.text_muted,size=12)
        field = Rect(self.rect.x+self.rect.width*.52,self.rect.y+3,self.rect.width*.48,self.rect.height-6)
        if self.metadata.type == 'BOOLEAN':
            progress = tween(state,self.key+'.switch',float(bool(value)),.16)
            track = Rect(self.rect.right-36,self.rect.y+(self.rect.height-20)/2,36,20)
            renderer.rect(track,blend(theme.surface_hover,theme.accent,progress),radius=10,
                          border=theme.accent if focused else None)
            renderer.rect(Rect(track.x+3+16*progress,track.y+3,14,14),theme.text,radius=7)
            return
        hover = tween(state,self.key+'.hover',float(hovered))
        renderer.rect(field,blend(theme.surface_raised,theme.surface_hover,hover),radius=6,
                      border=theme.accent if focused else None)
        subtype = getattr(self.metadata,'subtype','')
        if subtype in {'COLOR','COLOR_GAMMA'}:
            renderer.rect(field.inset((4,4,4,4)),swatch_color(value,subtype),radius=3)
            return
        if getattr(self.metadata, "is_array", False):
            value = ", ".join(f"{v:.4g}" for v in value)
        elif self.metadata.type == "FLOAT":
            value = f"{value:.6g}"
        elif self.metadata.type == "ENUM":
            value = next((i.name for i in self.metadata.enum_items if i.identifier == value), value)
        renderer.text(field.inset((8,0,8,0)),str(value),theme.text if self.style.enabled else theme.text_muted,
                      size=11,align='RIGHT')


def property_control(key, owner, name, label=None, *, enabled=True):
    metadata = owner.bl_rna.properties[name]
    label = metadata.name if label is None else label
    # Keyframeable properties retain Blender's decorated RNA editor.
    if metadata.type == "BOOLEAN" and not metadata.is_animatable:
        return Toggle(key, label, lambda ctx: getattr(owner, name),
                      lambda ctx, value: setattr(owner, name, value),
                      style=Style(height=32, enabled=enabled, align="LEFT"))
    if metadata.type == "ENUM" and not metadata.is_animatable:
        choices = [(i.identifier, i.name) for i in metadata.enum_items if i.identifier]
        if choices:
            return Dropdown(key, lambda ctx: getattr(owner, name),
                            lambda ctx, value: setattr(owner, name, value),
                            [(v, f"{label}: {n}" if label else n) for v, n in choices],
                            style=Style(height=32, enabled=enabled, align="LEFT"))
    return PropertyField(key, owner, name, label, enabled=enabled)

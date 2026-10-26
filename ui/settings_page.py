"""Application preferences and the persistent panel footer."""
import math
import textwrap
import bpy
from .gpu_framework import Button, Column, Label, Row, ScrollView, Style, Rect, Toggle
from .gpu_framework.core import Widget, Size, Divider
from .gpu_framework.navigation import Dropdown


def preferences(context):
    from .custom_ui import _addon_preferences
    return _addon_preferences(context)


def toggle_settings(state):
    if state.get('app.active_tab') == 'SETTINGS':
        state['app.active_tab'] = state.pop('settings.return_page', 'PACKS')
    else:
        state['settings.return_page'] = state.get('app.active_tab', 'PACKS')
        state['app.active_tab'] = 'SETTINGS'
    for key in ('_menu', '_runtime_focus', '_runtime_pressed', '_runtime_hover'):
        state.pop(key, None)
    state['_tree_dirty'] = True


class SettingsButton(Button):
    def draw(self, renderer, state, theme):
        selected = state.get('app.active_tab') == 'SETTINGS'
        self.primary = selected
        super().draw(renderer, state, theme)
        tint = theme.selected_text if selected else theme.text
        x, y = self.rect.x + 14, self.rect.y + 14
        for i in range(8):
            angle = i * math.pi / 4
            renderer.rect(Rect(x + math.cos(angle)*6-1.5, y+math.sin(angle)*6-1.5, 3, 3), tint, radius=1)
        renderer.rect(Rect(x-5,y-5,10,10), tint, radius=5)
        renderer.rect(Rect(x-2,y-2,4,4), theme.accent if selected else theme.surface_raised, radius=2)


class Footer(Widget):
    def __init__(self, state):
        super().__init__('app.footer', style=Style(height=32))
        self.set_children([SettingsButton('app.settings', '', lambda ctx: toggle_settings(state),
            style=Style(width=28,height=28,ghost=True), tooltip='Settings')])

    def layout(self, bounds, state, theme):
        super().layout(bounds,state,theme)
        self.children[0].layout(Rect(bounds.right-28,bounds.y+2,28,28),state,theme)

    def draw(self, renderer, state, theme):
        renderer.rect(self.rect, theme.canvas)
        renderer.rect(Rect(self.rect.x+self.rect.width/2-18,self.rect.y+15,36,2),
                      theme.accent if state.get('_panel_resize_y') else theme.border, radius=1)
        self.children[0].draw(renderer,state,theme)


class WrappedLabel(Label):
    def lines(self, width):
        return textwrap.wrap(self.text, max(1, int(width / ((self.style.font_size or 12)*.62)))) or ['']

    def preferred_size(self, available, state, theme):
        width = self.style.width if self.style.width is not None else available.width
        return Size(width, len(self.lines(width))*18)

    def draw(self, renderer, state, theme):
        for i, line in enumerate(self.lines(self.rect.width)):
            renderer.text(Rect(self.rect.x,self.rect.top-(i+1)*18,self.rect.width,18),line,
                          theme.text_muted if self.muted else theme.text,size=self.style.font_size or 12)


def reset_size(context, state):
    prefs = preferences(context)
    prefs.custom_ui_scale = 1.0
    prefs.adaptive_ui_scale = True
    for key in ('_panel_width','_panel_height','_panel_page_heights','_panel_height_page'):
        state.pop(key,None)
    from .gpu_framework.panel_storage import save_size
    save_size(state)
    state['_tree_dirty'] = True


def open_preferences(context):
    bpy.ops.preferences.addon_show(module=preferences(context).bl_idname)


def build_settings(context, state):
    prefs = preferences(context)
    if prefs is None:
        return ScrollView('app.scroll.settings', Column('settings.body', [
            WrappedLabel('settings.unavailable','Enable the add-on in Blender Preferences to edit settings.')]),style=Style(flex=1))
    wide = state.get('_content_width',320)-32 >= 420
    children=[]
    def section(key,title):
        if children:
            children.append(Divider(key+'.divider'))
        children.append(Label(key+'.title',title,style=Style(height=28,font_size=14,font_weight='SemiBold')))
    def setting(key,label,control,help_text):
        control.style=control.style.with_updates(flex=1 if wide else 0)
        heading=WrappedLabel(key+'.label',label,style=Style(width=160 if wide else None))
        # The fixed label column leaves at least 248 pixels for wide controls.
        if wide:
            heading.style=heading.style.with_updates(width=160)
        children.append((Row if wide else Column)(key,[heading,control],style=Style(gap=8)))
        children.append(WrappedLabel(key+'.help',help_text,muted=True,style=Style(font_size=11)))
    section('settings.appearance','Appearance')
    setting('settings.theme','Theme',Dropdown('settings.theme.control',lambda ctx:prefs.ui_theme,
        lambda ctx,value:setattr(prefs,'ui_theme',value), [('BLENDER','Follow Blender'),('ORIGINAL_DARK','Original Dark')],
        style=Style(height=32)), 'Follow your Blender theme, or keep the original dark appearance.')
    section('settings.size','Interface size')
    scale=Row('settings.scale.control',[
        Button('settings.scale.minus','-',lambda ctx:setattr(prefs,'custom_ui_scale',max(.75,round(prefs.custom_ui_scale-.05,2))),style=Style(width=30,height=32)),
        Label('settings.scale.value',f'{prefs.custom_ui_scale:.0%}',style=Style(flex=1,height=32,align='CENTER')),
        Button('settings.scale.plus','+',lambda ctx:setattr(prefs,'custom_ui_scale',min(1.5,round(prefs.custom_ui_scale+.05,2))),style=Style(width=30,height=32))],style=Style(height=32,gap=6))
    setting('settings.scale','Interface Scale',scale,'Adjust text, controls and spacing together, from 75% to 150%.')
    setting('settings.fit','Fit Interface to Viewport',Toggle('settings.fit.control','',lambda ctx:prefs.adaptive_ui_scale,
        lambda ctx,value:setattr(prefs,'adaptive_ui_scale',value),style=Style(height=32)), 'Use a more compact interface in shorter viewports.')
    children.append(Button('settings.reset','Reset Interface Size',lambda ctx:reset_size(ctx,state)))
    section('settings.compatibility','Compatibility')
    children.append(Button('settings.preferences','Open Blender Add-on Preferences',open_preferences,style=Style(height=32,font_size=11)))
    children.append(WrappedLabel('settings.compatibility.help','Access the native interface fallback and Blender preference controls.',muted=True,style=Style(font_size=11)))
    section('settings.about','About & updates')
    from .. import ADDON_VERSION
    children.append(Label('settings.brand','Sparkle'))
    children.append(WrappedLabel('settings.creators','Created by MrRobot and TylerBits',muted=True))
    children.append(Label('settings.version','Installed version: '+'.'.join(map(str,ADDON_VERSION))))
    children.append(Button('settings.update','Check for Updates',lambda ctx:bpy.ops.mcbedrock.check_update('INVOKE_DEFAULT')))
    if not context.preferences.use_preferences_save or getattr(bpy.app, 'use_userpref_skip_save_on_exit', False):
        children.append(Button('settings.save','Save Preferences',lambda ctx:bpy.ops.wm.save_userpref()))
        children.append(WrappedLabel('settings.save.help','Save to keep these changes after restarting Blender.',muted=True,style=Style(font_size=11)))
    return ScrollView('app.scroll.settings',Column('settings.body',children,style=Style(gap=10)),style=Style(flex=1))

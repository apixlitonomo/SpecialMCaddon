"""Figma Make RP Manager / Assets presentation; existing operators own all data.

Reference: design/make-source/App.tsx, version 58. Demo categories and counts are
replaced by actual browser asset types. Selecting a carousel item never reorders
the resource pack stack; priority changes are explicit context-menu operations.
"""
import os
import time
import math
from pathlib import Path
from dataclasses import replace

from .gpu_framework import Button, Column, Label, Row, ScrollView, Style, Rect
from .gpu_framework.core import Widget, Size, VirtualGrid
from .gpu_framework.navigation import Application, Dropdown, PageIcon
from .gpu_framework.motion import tween, blend, make_ease, resize_transition
from .styled_pages import button, text_field, asset_action, AssetPreview, actions, invoke, build_assets as legacy_asset_controls


def color(value):
    return tuple(int(value[i:i+2], 16)/255 for i in (0, 2, 4)) + (1.,)

PANEL, CARD, BLUE, MUTED = map(color, ('1c1c1c', '242424', '5294ff', '999999'))
ICONS=Path(__file__).resolve().parents[1]/'assets/ui/icons'


class IconButton(Button):
    def __init__(self,key,icon,action,*,icon_size=10,**kwargs):
        super().__init__(key,'',action,**kwargs)
        self.icon=icon
        self.icon_size=icon_size

    def draw(self,renderer,state,theme):
        r=self.rect
        size=self.icon_size
        renderer.image(Rect(r.x+(r.width-size)/2,r.y+(r.height-size)/2,size,size),str(ICONS/(self.icon+'.png')))


class ModeDropdown(Dropdown):
    def draw(self,renderer,state,theme):
        r=self.rect
        if state.get('_menu',{}).get('key')==self.key:
            renderer.rect(r,theme.surface_hover,radius=4)
        value=self.getter(state.get('_runtime_context'))
        icon=PageIcon(self.key+'.icon',value,style=Style(text_color=theme.text))
        icon.rect=Rect(r.x+9,r.y+(r.height-15)/2,15,15)
        icon.draw(renderer,state,theme)
        label=next((label for key,label in self.choices if key==value),'Library')
        renderer.text(Rect(r.x+31,r.y,r.width-52,r.height),label,theme.text,size=self.style.font_size or 12,weight='Bold',cap_center=True)
        renderer.image(Rect(r.right-16,r.y+(r.height-10)/2,10,10),str(ICONS/'chevron.png'))


class HeaderDivider(Widget):
    # No height of its own: the hairline sits in the gap, so what follows stays on the 4 px grid.
    def __init__(self):
        super().__init__('app.header.divider',style=Style(height=0))

    def draw(self,renderer,state,theme):
        renderer.rect(Rect(self.rect.x-16,self.rect.y,self.rect.width+32,1),theme.border)


class PackHeading(Widget):
    def __init__(self,packs):
        super().__init__('packs.heading',style=Style(height=24))
        self.packs=list(packs)

    def draw(self,renderer,state,theme):
        r=self.rect
        count=len(self.packs)
        thumbs=min(3,count,max(0,int((r.width-195)/25)))
        remaining=count-thumbs
        counter=('+' if thumbs else '')+str(remaining) if remaining else ''
        reserve=thumbs*25+(max(24,len(counter)*7+4) if counter else 0)
        renderer.text(Rect(r.x,r.y,max(0,r.width-reserve-4),r.height),'Resource Pack Manager',theme.text,size=13,weight='SemiBold')
        x=r.right-reserve
        for item in self.packs[:thumbs]:
            pack_image(renderer,Rect(x,r.y+1,22,22),item,theme)
            x+=25
        if counter:
            renderer.text(Rect(x,r.y,r.right-x,r.height),counter,color('f5a623'),size=12,weight='SemiBold',align='RIGHT')


def make_theme(theme):
    return theme


def active_index(scene, item):
    from .pack_manager import _normalized_path
    wanted = _normalized_path(item.path)
    return next((i for i, p in enumerate(scene.mcbedrock_active_packs)
                 if _normalized_path(p.source_path or p.path) == wanted), -1)


def toggle_pack(context, index):
    from .styled_pages import invoke
    from .pack_manager import archive_preparing
    scene = context.scene
    item = scene.mcbedrock_discovered_packs[index]
    if archive_preparing(item.path):
        return False  # being extracted: it switches on by itself when ready
    active = active_index(scene, item)
    if active >= 0:
        scene.mcbedrock_active_pack_index = active
        return invoke('mcbedrock.remove_active_pack', invoke=False)(context)
    # An archive that still has to be extracted is extracted on a helper
    # thread; its row says "Preparing…" meanwhile.
    return invoke('mcbedrock.add_resource_pack_to_active', invoke=False, pack_index=index,
                  background=True)(context)


# Badge colors for pack states, beside the Java (green) and Bedrock (blue) ones.
MISSING_BG, MISSING_TEXT = color('3d2e14'), color('f5a623')


def pack_image(renderer, rect, item, theme):
    from .pack_manager import is_generated_icon, is_mod_kind, mod_pack_icon
    icon = item.icon_path
    mod = is_mod_kind(getattr(item, 'kind', ''))
    if mod and (not icon or is_generated_icon(icon)):
        icon = mod_pack_icon(item)  # one of the mod's own models, once rendered
    # Logos keep their shape (some are wide banners); renders are smoothed, pixel icons stay crisp.
    rendered = os.path.basename(icon or '').startswith('modrender_')
    if not icon or not renderer.image(rect, icon, radius=4, fit=mod, nearest=not rendered):
        renderer.rect(rect, theme.surface_raised, radius=4)
        renderer.text(rect, 'Mod' if mod else 'RP', theme.text_muted, size=11 if mod else 13, align='CENTER')


def pack_name(renderer,rect,text,theme,size=18):
    """Prefer a word break, but also accommodate hash-like pack filenames."""
    if renderer.text_width(text,size,'Bold')<=rect.width:
        renderer.text(rect,text,theme.text,size=size,weight='Bold')
        return
    size=min(size,14)
    cut=len(text)
    while cut>1 and renderer.text_width(text[:cut],size,'Bold')>rect.width:
        cut-=1
    space=text.rfind(' ',0,cut+1)
    if space>cut//2:
        cut=space
    renderer.text(Rect(rect.x,rect.y+rect.height/2,rect.width,rect.height/2),text[:cut],theme.text,size=size,weight='Bold')
    renderer.text(Rect(rect.x,rect.y,rect.width,rect.height/2),text[cut:].lstrip(),theme.text,size=size,weight='Bold')


class PackSwitch(Button):
    def __init__(self, key, index, scene):
        super().__init__(key, '', lambda ctx: toggle_pack(ctx, index),
                         style=Style(width=40, height=72), tooltip='Enable / disable resource pack')
        self.index, self.scene = index, scene

    def draw(self, renderer, state, theme):
        from .pack_manager import archive_preparing
        packs = self.scene.mcbedrock_discovered_packs
        # A finished scan can shorten the list before this row is rebuilt.
        if self.index >= len(packs):
            return
        item = packs[self.index]
        enabled = active_index(self.scene, item) >= 0
        # An archive being prepared shows the knob half way: on its way to on.
        target = .5 if not enabled and archive_preparing(item.path) else float(enabled)
        progress = tween(state, self.key+'.switch', target, .18)
        r = Rect(self.rect.x, self.rect.y+(self.rect.height-22)/2, 40, 22)
        selected_track = blend(theme.accent,theme.canvas,.3) if theme.follows_blender else theme.accent
        renderer.rect(r, blend(theme.surface_hover, selected_track, progress), radius=11)
        renderer.rect(Rect(r.x+3+18*progress, r.y+3, 16, 16), theme.selected_text, radius=8)


class PackRow(Row):
    interactive = True

    def __init__(self, index, item, scene, identity=''):
        self.index, self.item, self.scene, self.identity = index, item, scene, identity
        super().__init__(f'packs.discovered.{index}', [
            Widget(f'packs.copy.{index}', style=Style(flex=1)),
            PackSwitch(f'packs.toggle.{index}', index, scene)],
            style=Style(height=72, padding=(16,0,16,0), gap=8))
        self.double_action = self.toggle_from_row
        self.detect_double_press = True
        self.tooltip = item.name + (f' · {identity}' if identity else '') + ' — double-click to enable / disable; right-click for pack actions'

    def toggle_from_row(self, context):
        context.scene.mcbedrock_discovered_pack_index = self.index
        return toggle_pack(context, self.index)

    def activate(self, context, state):
        context.scene.mcbedrock_discovered_pack_index = self.index
        return True

    def layout(self,bounds,state,theme):
        inset=max(12,min(16,12+(bounds.width-208)/18))
        self.style=self.style.with_updates(padding=(inset,0,inset,0))
        super().layout(bounds,state,theme)

    def draw(self, renderer, state, theme):
        from .pack_manager import _edition_label, archive_preparing, path_missing
        hover = False
        p = tween(state, self.key+'.hover', float(hover), .15)
        enabled = active_index(self.scene, self.item) >= 0
        background = blend(theme.selected if theme.follows_blender else color('243957'), theme.accent_hover if theme.follows_blender else color('2b4568'), p) if enabled else blend(theme.surface, theme.surface_hover, p)
        renderer.rect(self.rect, background, radius=4)
        if enabled and theme.follows_blender:
            theme = replace(theme, text=theme.selected_text)
        # Keep the same icon / badge / name arrangement throughout a resize.
        size=max(24,min(44,24+(self.rect.width-208)*20/122))
        icon=Rect(self.rect.x+self.style.padding[0],self.rect.y+(self.rect.height-size)/2,size,size)
        x=icon.right+8
        width=max(0,self.children[-1].rect.x-8-x)
        pack_image(renderer,icon,self.item,theme)
        edition = _edition_label(self.item.kind)
        badge = Rect(x,self.rect.top-25,min(width, max(54,len(edition)*6+30)),19)
        java = edition in {'Java', 'Mod'}  # a mod .jar is Java Edition content
        badge_bg = color('1e3d14') if java else color('1c304d')
        badge_text = color('6dcf4a') if java else color('85b4ff')
        # A pack being extracted, or whose folder or file is gone, says so in a
        # second badge beside the edition (and a missing pack's name is dimmed).
        state_badge = (('Preparing…', color('1c304d'), color('85b4ff'))
                       if archive_preparing(self.item.path) else
                       ('Missing', MISSING_BG, MISSING_TEXT) if path_missing(self.item.path) else None)
        if state_badge:
            if state_badge[0] == 'Missing':
                theme = replace(theme, text=theme.text_muted)
            extra = Rect(badge.right+4, badge.y, len(state_badge[0])*6+18, badge.height)
            if extra.right <= x+width:
                renderer.rect(extra, state_badge[1], radius=4)
                renderer.text(extra, state_badge[0], state_badge[2], size=10, weight='SemiBold', align='CENTER')
        renderer.rect(badge,badge_bg,radius=4)
        if edition in {'Java', 'Mod', 'Bedrock'} and badge.width>=50:
            renderer.image(Rect(badge.x+6,badge.y+4,11,11),str(ICONS/'java.png'),tint=badge_text)
            renderer.text(Rect(badge.x+22,badge.y,badge.width-26,badge.height),edition,badge_text,size=10,weight='SemiBold')
        else:
            renderer.text(badge,edition,badge_text,size=10,weight='SemiBold',align='CENTER')
        font_size=14 if self.rect.width>=330 else 12 if self.rect.width>=280 else 11
        pack_name(renderer,Rect(x,self.rect.y+(17 if self.identity else 7),width,28 if self.identity else 32),self.item.name,theme,font_size)
        if self.identity:
            renderer.text(Rect(x,self.rect.y+3,width,12),self.identity,theme.text_muted,size=9 if self.rect.width<280 else 10)
        for child in self.children:
            child.draw(renderer,state,theme)


class Carousel(Widget):
    interactive = True

    def __init__(self, key, packs, state, dock=False):
        self.packs, self.dock = list(packs), dock
        collapsed = state.get(key+'.collapsed', False)
        super().__init__(key, style=Style(height=(70 if collapsed else 110) if dock else 96))
        self.tooltip = ''
        self.buttons = []
        self.dots = []

    def load_into_browser(self, context, state, open_tab=True, background=True):
        import bpy
        if not self.packs:
            return False
        item = self.packs[self.selected(state)]
        from . import pack_manager as packs
        if background and packs.needs_preparation(item):
            # The archive's extracted copy is gone: rebuild it on a helper thread
            # and show "Preparing pack…" until it can load.
            identity = packs.pack_identity(item)
            packs.prepare_active_pack(item, lambda root, error: pack_relocated(identity, '', error))
            if open_tab:
                state['app.active_tab'] = 'ASSETS'
            state['assets.carousel.index'] = self.selected(state)
            # Keep this pack selected while the browser still shows the previous one.
            state['assets.browser.root'] = context.scene.mcbedrock_browser_root
            state['assets.preparing'] = identity
            state.pop('assets.missing', None)
            state['_tree_dirty'] = True
            return True
        root = packs.resolve_active_browser_root(item)
        previous_root = context.scene.mcbedrock_browser_root
        from . import asset_browser as browser
        # The gallery's rows can be ready before the scene list is filled.
        loaded = (browser._browser_rows_for(context.scene) is not None
                  or bool(context.scene.mcbedrock_browser_assets))
        if (root == previous_root and context.scene.get('_mcbedrock_loaded_browser_root') == root
                and loaded and not browser.browser_loading(context.scene) and not browser._index_error):
            if open_tab:
                state['app.active_tab'] = 'ASSETS'
            state['assets.carousel.index'] = self.selected(state)
            state.pop('assets.missing', None)
            state.pop('assets.preparing', None)
            state['_tree_dirty'] = True
            return True
        context.scene.mcbedrock_browser_root = root
        try:
            result = bpy.ops.mcbedrock.asset_browser_refresh('EXEC_DEFAULT', use_cached=True, background=background)
        except Exception:
            context.scene.mcbedrock_browser_root = previous_root
            raise
        if 'FINISHED' not in result:
            context.scene.mcbedrock_browser_root = previous_root
            return False
        if open_tab:
            state['app.active_tab'] = 'ASSETS'
        state['assets.carousel.index'] = self.selected(state)
        state['assets.collapsed'] = False
        state.pop('assets.selection', None)
        state.pop('app.scroll.assets', None)
        state.pop('assets.library.scroll', None)
        state.pop('app.status', None)
        state.pop('assets.missing', None)
        state.pop('assets.preparing', None)
        state['_tree_dirty'] = True
        return True

    def preferred_size(self,available,state,theme):
        # Measure the expanded dock; the shell and dock share one collapse track.
        height = 110 if self.dock else 96
        self.style = self.style.with_updates(height=height)
        return Size(available.width,height)

    def selected(self, state):
        return max(0,min(len(self.packs)-1,int(state.get(self.key+'.index',0))))

    @property
    def icon_spacing(self):
        return getattr(self,'_icon_spacing',40) if self.dock else 54

    def draw(self, renderer, state, theme):
        if not self.packs:
            return
        from .pack_manager import active_pack_missing
        r, selected = self.rect, self.selected(state)
        renderer.rect(r,theme.surface,radius=4)
        if not self.dock:
            renderer.text(Rect(r.x+12,r.top-25,r.width-45,18),'ACTIVE PACKS',theme.text_muted,size=10.5,weight='SemiBold')
            renderer.text(Rect(r.right-32,r.top-25,20,18),str(len(self.packs)),theme.text_muted,size=10.5,align='RIGHT')
        center = tween(state,self.key+'.position',float(selected),.28,easing=make_ease)
        center = max(0., min(len(self.packs)-1., center + state.get(self.key+'.drag',0.)))
        y = r.top-(62 if self.dock else 72)
        icon_size = tween(state,self.key+'.compact_icon',
                          30. if state.get(self.key+'.collapsed') else 36.,.12) if self.dock else 44
        self._icon_spacing=icon_size+4 if self.dock else 54
        self.buttons = []
        renderer.push_clip(r)
        for i,p in enumerate(self.packs):
            dist = abs(i-center)
            if abs((i-center)*self.icon_spacing)>r.width/2+50:
                continue
            selection_distance=abs(i-selected)
            scale=tween(state,self.key+f'.size.{i}',1. if i==selected else .86 if selection_distance==1 else .74,.28,easing=make_ease)
            size = icon_size*scale
            thumb = Rect(r.x+r.width/2-23+(i-center)*self.icon_spacing+(46-size)/2,y+(46-size)/2+(4 if i==selected else 0),size,size)
            target_width=min(52,self.icon_spacing)
            self.buttons.append((Rect(thumb.x+thumb.width/2-target_width/2, y-3, target_width, 54).intersect(r),i))
            opacity=renderer.opacity
            renderer.opacity *= 1 if i==selected else .6 if abs(i-selected)==1 else .3
            if i==selected:
                renderer.rect(thumb,theme.accent,radius=4)
            pack_image(renderer,thumb.inset((4,4,4,4)) if i==selected else thumb,p,theme)
            renderer.opacity=opacity
            if i==selected:
                renderer.rect(thumb,(0,0,0,0),radius=4,border=theme.accent,border_width=2)
            if active_pack_missing(p):
                # Its folder or file is gone: an amber dot, like the list's "Missing" badge.
                renderer.rect(Rect(thumb.right-7,thumb.top-7,6,6),MISSING_TEXT,radius=3)
        renderer.pop_clip()
        item=self.packs[selected]
        missing=active_pack_missing(item)
        if self.dock:
            self.collapse_rect=Rect(r.right-32,r.top-29,22,22)
            renderer.rect(self.collapse_rect,theme.surface_raised,radius=4)
            renderer.image(self.collapse_rect.inset((6,6,6,6)),str(ICONS/('chevron-up.png' if state.get(self.key+'.collapsed') else 'chevron.png')))
            # Details follow the actual animated space, on both open and close.
            # Keep them below the carousel rather than popping over its icons.
            detail_progress = min(1., max(0., (r.height-70)/40))
            self.dots = []
            if detail_progress > 0:
                detail_opacity = renderer.opacity
                renderer.opacity *= detail_progress
                renderer.push_clip(Rect(r.x, r.y, r.width, max(0., r.height-64)))
                pack_image(renderer,Rect(r.x+12,r.y+16,28,28),item,theme)
                badge=Rect(r.x+33,r.y+14,12,12)
                renderer.rect(badge,theme.accent,radius=8)
                renderer.image(badge.inset((2,2,2,2)),str(ICONS/'check.png'))
                renderer.text(Rect(r.x+56,r.y+28,r.width-70,20),item.name,theme.text,size=14,weight='SemiBold')
                from .pack_manager import _pack_identity_hint
                hint=_pack_identity_hint(item,self.packs)
                detail=('Missing · ' if missing else '')+(hint+' · ' if hint else '')+f'Pack {selected+1} of {len(self.packs)}'
                renderer.text(Rect(r.x+56,r.y+10,max(0,r.width-108),18),detail,
                              MISSING_TEXT if missing else theme.text_muted,size=11)
                self.dots=[]
                for i in range(len(self.packs)):
                    dot=Rect(r.right-12,r.y+15+i*8,5,7 if i==selected else 4)
                    if dot.top>r.y+65:
                        break
                    renderer.rect(dot,theme.accent if i==selected else theme.border,radius=2)
                    if detail_progress >= .95:
                        self.dots.append((Rect(dot.x-4,dot.y-2,13,8),i))
                renderer.pop_clip()
                renderer.opacity = detail_opacity
        else:
            renderer.text(Rect(r.x+12,r.y+4,r.width-24,20),('Missing: ' if missing else '')+item.name,
                          MISSING_TEXT if missing else theme.text,size=11,weight='SemiBold',align='CENTER')

    def pointer(self, event, x, y, state):
        key=self.key
        if event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'}:
            state[key+'.index']=max(0,min(len(self.packs)-1,self.selected(state)+(1 if event.type=='WHEELDOWNMOUSE' else -1)))
            return True
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            dot=next((i for r,i in self.dots if r.contains(x,y)),None)
            if dot is not None:
                state[key+'.index']=dot
                return True
            if self.dock and getattr(self,'collapse_rect',Rect(0,0,0,0)).contains(x,y):
                state[key+'.collapsed']=not state.get(key+'.collapsed',False)
                return True
            target = next((i for r,i in self.buttons if r.contains(x,y)), None)
            state['_carousel_drag']=(key,x,self.selected(state),target)
            return True
        drag=state.get('_carousel_drag')
        if drag and drag[0]==key:
            dx=x-drag[1]
            if event.type=='MOUSEMOVE':
                state[key+'.drag']=-dx/self.icon_spacing if abs(dx)>8 else 0.
                return True
            if event.type=='LEFTMOUSE' and event.value=='RELEASE':
                state.pop('_carousel_drag',None)
                state.pop(key+'.drag',None)
                index=drag[2]-round(dx/self.icon_spacing) if abs(dx)>8 else (drag[3] if len(drag)>3 and drag[3] is not None else drag[2])
                state[key+'.index']=max(0,min(len(self.packs)-1,index))
                return True
        return False


def show_missing_pack(state, carousel):
    """If the carousel's selected pack is gone from disk, have the Assets page offer Locate / Remove."""
    import bpy
    from . import pack_manager as packs
    if not carousel.packs:
        return False
    item = carousel.packs[carousel.selected(state)]
    if not packs.active_pack_missing(item, fresh=True):
        return False
    state['assets.missing'] = packs.pack_identity(item)
    state['assets.carousel.index'] = carousel.selected(state)
    # Keep this pack selected while the browser still shows the previous one.
    state['assets.browser.root'] = bpy.context.scene.mcbedrock_browser_root
    state.pop('assets.preparing', None)
    state['_tree_dirty'] = True
    return True


def load_selected_pack(state):
    """Load the Assets carousel's selected pack from a timer or an operator (no sidebar event)."""
    import bpy
    context = bpy.context
    packs = getattr(context.scene, 'mcbedrock_active_packs', None)
    if not packs:
        return False
    carousel = Carousel('assets.carousel', packs, state)
    try:
        return carousel.load_into_browser(context, state, open_tab=False)
    except Exception as exc:
        if not show_missing_pack(state, carousel):
            state['app.status'] = 'Error: ' + str(exc)
        return False
    finally:
        state['_tree_dirty'] = True


def pack_relocated(identity, preparing='', error=None):
    """A missing or re-extracted pack can be read again (or failed): update open Assets pages."""
    from .gpu_framework import runtime
    from .asset_browser import refresh_browser_pages
    for session in tuple(runtime._ACTIVE.values()):
        state = getattr(session, 'state', None)
        if not state or identity not in (state.get('assets.missing'), state.get('assets.preparing')):
            continue
        state.pop('assets.missing', None)
        state.pop('assets.preparing', None)
        if error is not None:
            state['app.status'] = 'Error: Could not open the pack: %s' % error
        elif preparing:
            state['assets.preparing'] = preparing
        elif state.get('app.active_tab') == 'ASSETS':
            load_selected_pack(state)
        state['_tree_dirty'] = True
    if error is not None and not runtime._ACTIVE:
        print('[Sparkle] Could not open the pack: %s' % error)
    refresh_browser_pages()


# Wheel turns over the Assets carousel: the first loads at once, the rest load
# where the wheel stops instead of every pack it passes (each load reads a pack).
_WHEEL_SETTLE = .35
_pending_pack_loads = {}


def _schedule_pack_load(state, delay=_WHEEL_SETTLE):
    import bpy
    state['assets.carousel.load_at'] = time.monotonic() + delay
    _pending_pack_loads[id(state)] = state
    if not bpy.app.timers.is_registered(_run_pending_pack_loads):
        bpy.app.timers.register(_run_pending_pack_loads, first_interval=delay)


def _run_pending_pack_loads():
    now = time.monotonic()
    wait = None
    for key, state in list(_pending_pack_loads.items()):
        due = state.get('assets.carousel.load_at', 0.)
        if due > now:
            wait = min(wait, due - now) if wait is not None else due - now
            continue
        del _pending_pack_loads[key]
        state.pop('assets.carousel.load_at', None)
        try:
            load_selected_pack(state)
        except Exception as exc:  # the add-on may be turning off
            print('[Sparkle] Could not load the pack: %s' % exc)
    if wait is None:
        from .asset_browser import refresh_browser_pages
        refresh_browser_pages()
    return wait


def _load_carousel_pack(context, state, carousel, previous, wheel=False):
    """Load the pack the Assets carousel now shows (a missing one gets Locate / Remove)."""
    now = time.monotonic()
    if wheel:
        recent = now - state.get('assets.carousel.wheel_at', -1.) < _WHEEL_SETTLE
        state['assets.carousel.wheel_at'] = now
        if recent:
            _schedule_pack_load(state)
            return
    try:
        carousel.load_into_browser(context, state, open_tab=not carousel.dock)
    except Exception as exc:
        if not show_missing_pack(state, carousel):
            state[carousel.key+'.index'] = previous
            state['app.status'] = 'Error: ' + str(exc)


class MakePreview(AssetPreview):
    def draw(self,renderer,state,theme):
        from .asset_browser import is_mod_asset
        context=state.get('_runtime_context')
        path=self.source(context) if context else ''
        texture = self.item.asset_type == 'Texture'
        # Mod thumbnails are wider than tall: fitted, as the import panel shows them.
        if path and renderer.image(self.rect,path,radius=4,nearest=texture,fit=is_mod_asset(self.item)):
            return
        super().draw(renderer,state,theme)


class AssetCard(Button):
    def __init__(self, context, index, item, state):
        from .asset_browser import _browser_item_key
        self.item,self.index,self.identity=item,index,_browser_item_key(item)
        def select(ctx):
            # The sidebar keeps its own selection. Writing the scene's index here
            # filled the whole scene list (thousands of rows) on the first click
            # and redrew every editor; imports pass the index themselves.
            state['assets.selection']=None if state.get('assets.selection')==self.identity else self.identity
        action_hint = 'import as a flat plane' if item.asset_type == 'Texture' else 'import'
        super().__init__(f'assets.card.{index}',item.name,select,style=Style(flex=1),
                         tooltip=item.name+' — double-click to '+action_hint)
        self.double_action=asset_action(item,index,dialog=True) if item.importable and item.status not in {'Unreadable','Protected'} else None
        self.detect_double_press = self.double_action is not None
        self.double_click_identity = self.identity
        if self.double_action is None:
            self.tooltip = item.name
        from . import asset_browser as browser
        self.preview=MakePreview(self.key+'.preview',lambda ctx: browser.asset_thumbnail_path(ctx.scene,item),item)

    def draw(self, renderer, state, theme):
        selected=state.get('assets.selection')==self.identity
        hover=False
        p=tween(state,self.key+'.hover',float(hover),.15)
        renderer.rect(self.rect,theme.accent if selected else blend(theme.surface,theme.surface_hover,p),radius=4,
                      border=theme.accent if selected else None,border_width=1.5)
        size=max(0,min(self.rect.width-16,self.rect.height-48))
        self.preview.rect=Rect(self.rect.x+(self.rect.width-size)/2,self.rect.top-size-8,size,size)
        # On the blue selected card the accent glyph and muted status would vanish.
        on_card=(theme.selected_text if theme.follows_blender else theme.canvas) if selected else None
        self.preview.draw(renderer,state,replace(theme,accent=on_card,text_muted=on_card) if selected else theme)
        renderer.text(Rect(self.rect.x+8,self.rect.y+24,max(0,self.rect.width-16),19),self.item.name,
                      (theme.selected_text if theme.follows_blender else theme.canvas) if selected else theme.text,size=11.5,weight='SemiBold')
        asset_label = 'Texture (plane)' if self.item.asset_type == 'Texture' else self.item.asset_type
        renderer.text(Rect(self.rect.x+8,self.rect.y+7,max(0,self.rect.width-16),16),asset_label,(theme.selected_text if theme.follows_blender else theme.canvas) if selected else theme.text_muted,size=10)


def draw_revealing_children(children, renderer, state, theme, key, revision, stagger=0., animate=True):
    """Fade and settle cards upward, optionally revealing packs in sequence.

    Without ``animate`` a new revision shows its cards at once.
    """
    now = state.get('_motion_time', time.monotonic())
    if state.get(key+'.revision') != revision:
        state[key+'.revision'] = revision
        state[key+'.start'] = now
        motion = state.setdefault('_motion', {})
        motion.pop(key+'.progress', None)
        if not animate:
            motion[key+'.progress'] = [1., 1., now, .5]
    shared = tween(state, key+'.progress', 1., .5, initial=0., easing=make_ease)
    opacity, offset = renderer.opacity, renderer.offset_y
    rank = 0
    try:
        for child in children:
            progress = shared
            if stagger and isinstance(child, PackRow):
                elapsed = now - state.get(key+'.start', now) - rank*stagger
                linear = min(1., max(0., elapsed/.4))
                progress = 1. if state.get('_motion_disabled') else (make_ease(linear) if 0 < linear < 1 else linear)
                rank += 1
                if progress < 1.:
                    state['_motion_active'] = True
            renderer.opacity = opacity * progress
            renderer.offset_y = offset - 12*(1-progress)
            child.draw(renderer, state, theme)
    finally:
        renderer.opacity, renderer.offset_y = opacity, offset


class AssetGrid(VirtualGrid):
    def __init__(self, cards, factory=None, state=None):
        super().__init__('assets.gallery', cards, factory,
                         column_breakpoint=220, row_height=None)
        self.view_state = state if state is not None else {}

    def _metrics(self, width):
        # Fill the search row's width evenly; the shell owns the scrollbar gutter.
        # Keep row/preview height stable as columns expand before reflowing.
        gap = self.style.gap
        columns = max(1, int((width + gap) // (96 + gap)))
        previous = self.view_state.get('assets.grid.columns')
        if previous is not None and abs(columns-previous) == 1:
            boundary = max(columns, previous)*(96+gap)-gap
            if abs(width-boundary) < 12:
                columns = previous
        self.view_state['assets.grid.columns'] = columns
        card_width = max(0, (width - (columns - 1) * gap) / columns)
        rows = (len(self.items) + columns - 1) // columns
        return columns, card_width, 112, rows

    def layout(self, bounds, state, theme):
        super().layout(bounds, state, theme)
        columns, width, _, _ = self._metrics(bounds.width)
        inset = max(0, (bounds.width - columns*width - (columns-1)*self.style.gap)/2)
        for card in self.children:
            r = card.rect
            card.layout(Rect(r.x+inset,r.y,r.width,r.height),state,theme)
        context = state.get('_runtime_context')
        viewport = self.ancestor(ScrollView)
        if context is None or viewport is None:
            return
        visible = [card for card in self.children if card.rect.intersect(viewport.rect).height > 0]
        nearby = [card for card in self.children if card not in visible]
        from .asset_browser import request_visible_thumbnails
        request_visible_thumbnails(context, [card.item for card in visible + nearby])  # the visible ones first

    def draw(self, renderer, state, theme):
        context = state.get('_runtime_context')
        scene = context.scene if context else None
        revision = (getattr(scene, 'mcbedrock_browser_root', ''),
                    scene.get('_mcbedrock_gallery_revision', 0) if scene is not None else 0)
        # Cards fade in after "Loading assets…" only. Switching between loaded
        # packs swaps them at once: a fade from blank reads as a flicker there.
        animate = state.get('assets.reveal.armed', False)
        if state.get('assets.reveal.revision') != revision:
            state.pop('assets.reveal.armed', None)
        draw_revealing_children(self.children, renderer, state, theme, 'assets.reveal', revision,
                                animate=animate)


class AssetGalleryScroll(ScrollView):
    """Keep two rows visible by default, with more columns in wider panels."""
    def __init__(self, grid):
        super().__init__('assets.library.scroll', grid, style=Style())

    def preferred_size(self, available, state, theme):
        columns, _, height, rows = self.child._metrics(available.width)
        visible_rows = min(rows, 2)
        preferred = (visible_rows * height
                    + max(0, visible_rows - 1) * self.child.style.gap
                    + state.get('_panel_asset_extra',0))
        limit = getattr(self, 'height_limit', None)
        return Size(available.width, min(preferred, limit) if limit is not None else preferred)

    def layout(self, bounds, state, theme):
        columns, _, height, _ = self.child._metrics(bounds.width)
        stride = height + self.child.style.gap
        entry = state.setdefault(self.key, {})
        previous = entry.get('grid_layout')
        if previous and previous[0] != bounds.width:
            _, old_columns, old_stride = previous
            offset = max(0.0, entry.get('scroll', 0.0))
            row = int(offset // old_stride)
            fraction = (offset % old_stride) / old_stride
            entry['scroll'] = ((row * old_columns // columns) + fraction) * stride
        super().layout(bounds, state, theme)
        entry['grid_layout'] = (bounds.width, columns, stride)


class LibraryHeading(Button):
    def __init__(self,scene,state,action):
        super().__init__('assets.heading','Asset Library',action,style=Style(height=40))
        self.tooltip = 'Ctrl+Alt+Backspace: reset the asset browser. Keeps files, scene objects and resource packs.'

    def draw(self,renderer,state,theme):
        from pathlib import Path
        r=self.rect
        renderer.image(Rect(r.x+2,r.y+14,10,10),str(ICONS/('chevron-right.png' if state.get('assets.collapsed') else 'chevron.png')))
        renderer.text(Rect(r.x+22,r.y,r.width-22,r.height),self.text,theme.text,size=13,weight='SemiBold')


class StickyScroll(ScrollView):
    """Pin the existing input row while the content beneath it scrolls."""
    def __init__(self,key,child,sticky):
        super().__init__(key,child,style=Style(flex=1))
        self.sticky=sticky

    def layout(self,bounds,state,theme):
        # Shrink the gallery before allowing the whole page to overflow. Keeping
        # its natural two-row height here made the thumb jump between two scroll
        # ranges while the bottom edge was dragged.
        gallery = next((w for w in self.child.children if isinstance(w, AssetGalleryScroll)), None)
        if gallery:
            gallery.height_limit = None
            available = Size(bounds.width, max(bounds.height, 100000.0))
            fixed = (self.child.preferred_size(available, state, theme).height
                     - gallery.preferred_size(available, state, theme).height)
            gallery.height_limit = max(40.0, bounds.height - fixed)
        super().layout(bounds,state,theme)
        if gallery:
            gallery.height_limit = None
        r=self.sticky.rect
        if r.top>bounds.top:
            self.sticky.layout(Rect(r.x,bounds.top-r.height,r.width,r.height),state,theme)

    def draw(self,renderer,state,theme):
        super().draw(renderer,state,theme)
        renderer.push_clip(self.rect)
        renderer.rect(self.sticky.rect,theme.canvas)
        self.sticky.draw(renderer,state,theme)
        renderer.pop_clip()

    def hit_test(self,x,y):
        if self.rect.contains(x,y):
            hit=self.sticky.hit_test(x,y)
            if hit:
                return hit
        return super().hit_test(x,y)


class AssetFilterDropdown(Dropdown):
    """Compact, flush category segment of the Make search bar."""
    def label(self,state):
        value=self.getter(state.get('_runtime_context'))
        return next((label for key,label in self.choices if key==value),str(value))

    def preferred_size(self,available,state,theme):
        width=math.ceil((37+len(self.label(state))*7)/8)*8  # whole grid steps
        # Up to half the bar, on the 4 px grid: at the default 240 px panel the
        # default "Importable" filter needs 61 px of text and used to read "Import...".
        return Size(min(width,max(40,math.floor(available.width*.5/4)*4)),32)

    def draw(self,renderer,state,theme):
        r=self.rect
        if state.get('_menu',{}).get('key')==self.key:
            renderer.rect(r,theme.surface_hover,radius=4)
            renderer.rect(Rect(r.right-4,r.y,4,r.height),theme.surface_hover)
        renderer.text(Rect(r.x+12,r.y,max(0,r.width-37),r.height),
                      self.label(state),theme.text,size=12,weight='Medium')
        renderer.image(Rect(r.right-20,r.y+(r.height-10)/2,10,10),str(ICONS/'chevron.png'))
        renderer.rect(Rect(r.right-1,r.y,1,r.height),theme.border)


def search_row(key,scene,prop,placeholder,category=None,trailing=()):
    from .gpu_framework.theme_adapter import resolve_theme
    from .custom_ui import _addon_preferences
    import bpy
    prefs = _addon_preferences(bpy.context)
    theme = resolve_theme(bpy.context.preferences,getattr(prefs,'ui_theme','BLENDER'))
    search=text_field(key,scene,prop,placeholder)
    height=32 if category else 36
    search.style=Style(height=height,flex=1,font_size=12,ghost=bool(category))
    children=([category] if category else [])+[search]
    if getattr(scene,prop):
        children.append(IconButton(key+'.clear','close',lambda ctx:setattr(scene,prop,''),style=Style(width=24,height=32,ghost=True),tooltip='Clear search'))
    if category or trailing:
        children.extend(trailing)
        children=[Row(key+'.bar',children,style=Style(height=height,flex=1,
            background=theme.surface,radius=4,padding=(0,0,4,0)))]
    inset=8 if category else 4
    return Row(key+'.sticky',children,style=Style(height=48 if category else 44,padding=(0,inset,0,inset),gap=0,background=theme.canvas))


class PackGroupHeading(Button):
    def __init__(self, group, label, state, searching=False):
        self.group = group
        self.collapsed = state.get('packs.group.' + group, False) and not searching
        def toggle(context):
            state['packs.group.' + group] = not state.get('packs.group.' + group, False)
            state.pop('packs.library.scroll', None)
        super().__init__('packs.group.' + group, label, toggle, style=Style(height=32))

    def draw(self, renderer, state, theme):
        renderer.image(Rect(self.rect.x+4,self.rect.y+10,10,10),
                       str(ICONS/('chevron-right.png' if self.collapsed else 'chevron.png')))
        renderer.text(Rect(self.rect.x+24,self.rect.y,self.rect.width-24,self.rect.height),
                      self.text,theme.text,size=12,weight='SemiBold')


class PackList(Widget):
    """Lay out the full list but instantiate and draw only nearby rows."""
    def __init__(self, entries, scene, identities=None):
        super().__init__('packs.content')
        self.entries, self.scene, self.identities = entries, scene, identities or {}
        self.signature = tuple(entry.key if isinstance(entry, PackGroupHeading) else (entry[0], entry[1].path) for entry in entries)
        self.cached = {}

    def preferred_size(self, available, state, theme):
        height = sum(32 if isinstance(entry, PackGroupHeading) else 72
                     for entry in self.entries) + max(0, len(self.entries)-1)*8
        return Size(available.width, height)

    def layout(self, bounds, state, theme):
        self.rect = bounds
        viewport = self.ancestor(ScrollView)
        clip = viewport.rect if viewport else bounds
        top = bounds.top
        visible = {}
        for index, entry in enumerate(self.entries):
            height = 32 if isinstance(entry, PackGroupHeading) else 72
            rect = Rect(bounds.x, top-height, bounds.width, height)
            top -= height+8
            if rect.top < clip.y-108 or rect.y > clip.top+108:
                continue
            widget = self.cached.get(index)
            if widget is None:
                widget = entry if isinstance(entry, PackGroupHeading) else PackRow(*entry, self.scene,
                    self.identities.get(entry[1].path,''))
            visible[index] = widget
            widget.layout(rect, state, theme)
        self.cached = visible
        self.set_children(visible.values())

    def draw(self, renderer, state, theme):
        draw_revealing_children(self.children, renderer, state, theme,
                               'packs.reveal', self.scene.get('_mcbedrock_pack_scan_revision', 0), stagger=.09)


class PackPage(Column):
    """Keep controls outside the list and reserve at most three card rows."""
    def __init__(self,children,scroll,row_count,heading_count=0):
        super().__init__('app.scroll.packs',children,style=Style(flex=1,gap=8))
        self.scroll=scroll
        self.list_height=max(36,min(3,row_count)*72+heading_count*32+max(0,min(3,row_count)+heading_count-1)*8)

    def preferred_size(self,available,state,theme):
        fixed=sum(c.preferred_size(available,state,theme).height for c in self.children if c is not self.scroll)
        return Size(available.width,fixed+self.list_height+self.style.gap*max(0,len(self.children)-1))

    def layout(self,bounds,state,theme):
        fixed=sum(c.preferred_size(Size(bounds.width,bounds.height),state,theme).height
                  for c in self.children if c is not self.scroll)
        fixed+=self.style.gap*max(0,len(self.children)-1)
        list_height = max(0,bounds.height-fixed) if '_panel_height' in state else min(self.list_height,max(0,bounds.height-fixed))
        self.scroll.style=Style(height=list_height)
        super().layout(bounds,state,theme)


class PackPageScroll(ScrollView):
    """Keep the pack controls reachable when the shell is shorter than them."""
    def __init__(self, child):
        super().__init__('app.scroll.packs', child, style=Style(flex=1))
        child.key = 'packs.page'

    def preferred_size(self, available, state, theme):
        return self.child.preferred_size(available, state, theme)

    def layout(self, bounds, state, theme):
        # Shrink the list viewport first. Three preferred rows are not a
        # minimum: using them here makes the gutter jump to the outer page
        # for tiny height changes, even while the list has real overflow.
        natural = self.child.preferred_size(Size(bounds.width, bounds.height), state, theme).height
        minimum = natural - self.child.list_height + min(72, self.child.list_height)
        if bounds.height < minimum - .5:
            super().layout(bounds, state, theme)
        else:
            self.rect = bounds
            self.content_height = bounds.height
            state.setdefault(self.key, {})['scroll'] = 0.0
            self.child.layout(bounds, state, theme)
            self.clip_rect = bounds
            self._set_clip(self.child, bounds)


class PackPageWithDock(Column):
    """The active pack stays outside the scrollable library viewport."""
    def __init__(self, page, dock):
        super().__init__('packs.with_dock',[page,dock],style=Style(flex=1,gap=8))
        self.page,self.dock=page,dock

    def layout(self,bounds,state,theme):
        self.rect=bounds
        reduction = state.get('_pack_dock_reduction', 0.)
        height=min(110-reduction,bounds.height)
        self.dock.layout(Rect(bounds.x,bounds.y,bounds.width,height),state,theme)
        self.page.layout(Rect(bounds.x,bounds.y+height+8,bounds.width,max(0,bounds.height-height-8)),state,theme)


def build_packs(context,state):
    from . import pack_manager as packs
    from .pack_filter_ui import PackFilterDropdown, query_for
    packs.ensure_startup_scan()  # once per session, now that the library is shown
    scene=context.scene
    discovered=scene.mcbedrock_discovered_packs
    active_paths={packs._normalized_path(p.source_path or p.path)
                  for p in scene.mcbedrock_active_packs}
    head=(len(discovered), discovered[0].path if discovered else '', frozenset(active_paths))
    if state.get('packs.library.head') != head:
        state.pop('packs.library.scroll',None)
        state.pop('packs.context',None)
        state['packs.library.head']=head
    def refresh(ctx):
        state['packs.group.active'] = False
        state['packs.group.inactive'] = False
        state['_tree_dirty'] = True
        return invoke('mcbedrock.scan_resource_packs',invoke=False,background=True)(ctx)
    search=search_row('packs.search',scene,'mcbedrock_pack_search','Search resource packs...',trailing=(
        *([PackFilterDropdown(scene, state)] if packs.PACK_CONTENT_FILTERS_ENABLED else []),
        IconButton('packs.add','folder',invoke('mcbedrock.add_resource_pack_folder',background=True),
                   icon_size=16,style=Style(width=28,height=36,ghost=True),
                   tooltip='Add a resource pack, a mod .jar or a folder of packs'),
        IconButton('packs.scan','refresh',refresh,
                   icon_size=16,style=Style(width=28,height=36,ghost=True),
                   tooltip='Scan for resource packs. Ctrl+Alt+Backspace over this panel resets the library; keeps files and enabled packs.'),
    ))
    pending = 0
    ordered = list(enumerate(discovered))
    if packs.PACK_CONTENT_FILTERS_ENABLED:
        features, pending = packs.pack_feature_snapshot(scene)
        query = query_for(scene)
        ordered = packs.pack_features.order_entries(ordered, features, query)
    matched=[(i,p) for i,p in ordered if packs._pack_matches_search(p,scene.mcbedrock_pack_search)]
    rows=[]
    visible_count=0
    heading_count=0
    for group,label,enabled in (('active','Active',True),('inactive','Inactive',False)):
        entries=[(i,p) for i,p in matched if (packs._normalized_path(p.path) in active_paths)==enabled]
        if not entries:
            continue
        heading=PackGroupHeading(group,label,state,bool(scene.mcbedrock_pack_search.strip()))
        rows.append(heading)
        heading_count+=1
        if not heading.collapsed:
            rows.extend(entries)
            visible_count+=len(entries)
    state['packs.visible_count'] = visible_count
    identities=packs._pack_identity_hints([p for _,p in matched],discovered)
    # The first scan of a new install takes a while; say so instead of "none found".
    status=scene.mcbedrock_pack_manager_status
    activity=packs.library_activity()
    empty=('Inspecting pack contents…' if pending else 'No matching packs' if discovered
           else activity or (status if status.startswith(('Could not','Pack scan failed','No resource packs found'))
                             else 'No resource packs found'))
    content=PackList(rows,scene,identities) if rows else Column('packs.content',[
        Label('packs.empty',empty,muted=True)],style=Style(gap=8))
    scroll=ScrollView('packs.library.scroll',content,style=Style(flex=1))
    children=[search,scroll]
    if pending:
        children.insert(1, Label('packs.inspecting', f'Inspecting {pending} packs…', muted=True, style=Style(height=20)))
    page=PackPageScroll(PackPage(children,scroll,visible_count,heading_count))
    if len(scene.mcbedrock_active_packs):
        return PackPageWithDock(page,Carousel('packs.carousel',scene.mcbedrock_active_packs,state,dock=True))
    return page


def _unavailable_pack(context,state):
    """Widgets for a selected pack that cannot be shown (gone from disk, or being extracted), else None."""
    import bpy
    from . import pack_manager as packs
    missing,preparing=state.get('assets.missing'),state.get('assets.preparing')
    if not (missing or preparing):
        return None
    active=context.scene.mcbedrock_active_packs
    if not active:
        state.pop('assets.missing',None)
        state.pop('assets.preparing',None)
        return None
    index=max(0,min(len(active)-1,int(state.get('assets.carousel.index',0))))
    item=active[index]
    identity=packs.pack_identity(item)
    if preparing:
        if preparing==identity and packs.archive_preparing(bpy.path.abspath(item.source_path)):
            return [Label('assets.empty','Preparing pack…',muted=True,style=Style(height=24))]
        state.pop('assets.preparing',None)
        return None
    if missing!=identity or not packs.active_pack_missing(item):
        state.pop('assets.missing',None)
        if missing==identity:
            _schedule_pack_load(state,0.)  # it is back where it was: load it
        return None
    source=item.source_path or item.path
    message=Label('assets.empty',"This pack's file is missing" if item.is_archive
                  else "This pack's folder is missing",muted=True)
    where=Label('assets.missing.path',source,muted=True,style=Style(height=20,font_size=10))
    where.tooltip=source
    def remove(ctx):
        ctx.scene.mcbedrock_active_pack_index=index
        invoke('mcbedrock.remove_active_pack',invoke=False)(ctx)
        state.pop('assets.missing',None)
        load_selected_pack(state)
    return [message,where,actions('assets.missing.actions',[
        Button('assets.locate','Locate…',invoke('mcbedrock.relocate_active_pack',pack_index=index),
               primary=True,style=Style(height=32,align='CENTER'),tooltip='Choose where this pack is now'),
        Button('assets.remove_missing','Remove',remove,style=Style(height=32,align='CENTER'),
               tooltip='Turn this pack off; nothing is deleted'),
    ])]


def build_assets(context,state):
    from . import asset_browser as browser
    from .pack_manager import _normalized_path
    scene=context.scene
    root=scene.mcbedrock_browser_root
    if state.get('assets.browser.root') != root:
        state['assets.browser.root']=root
        state['assets.carousel.index']=next((i for i,p in enumerate(scene.mcbedrock_active_packs)
            if _normalized_path(p.path)==_normalized_path(root)),0)
        state.pop('assets.selection',None)
        state.pop('assets.library.scroll',None)
    category=AssetFilterDropdown('assets.filter',lambda ctx:scene.mcbedrock_browser_filter,
        lambda ctx,v:setattr(scene,'mcbedrock_browser_filter',v),
        [(v,n) for v,n,*_ in browser.ASSET_FILTER_ITEMS],style=Style(height=32,font_size=12))
    search=search_row('assets.search',scene,'mcbedrock_browser_search','Search...',category)
    children=[]
    if len(scene.mcbedrock_active_packs):
        children.append(Carousel('assets.carousel',scene.mcbedrock_active_packs,state))
    # Loaded packs draw their prepared rows at once; only a pack still being
    # indexed has nothing to show yet.
    pending=browser.browser_gallery_pending(scene)
    state['assets.loading']=bool(pending)
    def collapse(ctx):
        state['assets.collapsed']=not state.get('assets.collapsed',False)
    children.append(LibraryHeading(scene,state,collapse))
    unavailable=_unavailable_pack(context,state)
    if unavailable is not None:
        # The selected pack is gone from disk or still being extracted: say so
        # (with Locate / Remove) instead of showing another pack's cards.
        if not state.get('assets.collapsed'):
            children.extend(unavailable)
        return ScrollView('app.scroll.assets',Column('assets.content',children,
            style=Style(gap=8)),style=Style(flex=1))
    if pending:
        # Keep the page and the panel as they are; the grid fades in below.
        state['assets.reveal.armed']=True
        if not state.get('assets.collapsed'):
            children.extend([search, Label('assets.empty','Loading assets…',muted=True,
                                           style=Style(height=24))])
        return ScrollView('app.scroll.assets',Column('assets.content',children,
            style=Style(gap=8)),style=Style(flex=1))
    if not state.get('assets.collapsed'):
        visible=browser.filtered_browser_view(scene)
        children.extend([search, AssetGalleryScroll(AssetGrid(visible,
            lambda entry: AssetCard(context, entry[0], entry[1], state), state=state))])
        if not visible:
            filtered=bool(scene.mcbedrock_browser_search.strip()) or scene.mcbedrock_browser_filter != 'ALL'
            if not root:
                label='Enable a resource pack to begin'
            elif browser._index_error:
                label='Could not load assets'
            elif filtered:
                label='No assets match these filters'
            else:
                label='No supported assets in this pack'
            children.append(Label('assets.empty',label,muted=True))
            if not root:
                children.append(Button('assets.open_packs','Resource Packs',
                    lambda ctx:state.update({'app.active_tab':'PACKS'}),
                    primary=True,style=Style(height=32,align='CENTER')))
            elif browser._index_error:
                error=Label('assets.error',str(browser._index_error),muted=True)
                error.tooltip=str(browser._index_error)
                children.append(error)
                retry=button('assets.retry','Retry Loading','mcbedrock.asset_browser_refresh',
                    invoke_default=False,background=True)
                retry.tooltip=str(browser._index_error)
                children.append(retry)
            elif filtered:
                def clear_filters(ctx):
                    scene.mcbedrock_browser_search=''
                    scene.mcbedrock_browser_filter='ALL'
                children.append(Button('assets.clear_filters','Clear Filters',clear_filters,
                    style=Style(height=32,align='CENTER')))
        selected=state.get('assets.selection')
        match=visible.selected(selected) if selected else None
        if match:
            i,item=match
            if item.asset_type == 'Texture':
                children.append(button('assets.posterize','Posterize','mcbedrock.posterize_open',
                    filepath=item.source_path,invoke_default=False,
                    enabled=bool(item.importable) and item.status not in {'Unreadable','Protected'}))
            label = 'Import Texture as Plane' if item.asset_type == 'Texture' else 'Import Selected'
            children.append(Button('assets.import_selected',label,asset_action(item,i,dialog=True),
                primary=True,style=Style(height=32,align='CENTER',enabled=bool(item.importable) and item.status not in {'Unreadable','Protected'})))
    content=Column('assets.content',children,style=Style(gap=8))
    if state.get('assets.collapsed'):
        return ScrollView('app.scroll.assets',content,style=Style(flex=1))
    scroll=StickyScroll('app.scroll.assets',content,search)
    return scroll


def build_quick_import(context,state):
    content=Column('quick.content',[
        Label('quick.heading','Quick Import',style=Style(height=32,font_size=14,font_weight='SemiBold')),
        actions('quick.actions',[
            button('quick.particle','Particle','mcbedrock.import_particle',primary=True),
            button('quick.model','Model / Entity','mcbedrock.import_asset_json'),
        ]),
    ],style=Style(gap=12))
    return ScrollView('app.scroll.quick',content,style=Style(flex=1))


class MakeApplication(Application):
    def walk(self,node=None):
        yield from super().walk(node)
        if node is None and getattr(self,'context_source',None):
            yield self.context_source

    def layout(self,bounds,state,theme):
        self.overlay_bounds = bounds
        state['_pack_dock_reduction'] = 0.
        state['_pack_shell_reduction'] = 0.
        # Manual dimensions belong to the panel, not its current page.
        active=state.get('app.active_tab')
        state['_panel_asset_extra']=0
        if state.get('app.active_tab') in {'PACKS', 'ASSETS', 'QUICK', 'SCHEMATIC', 'PARTICLE', 'SETTINGS', 'SKY', 'POSTERIZE'}:
            available=Size(max(0,bounds.width-32),bounds.height)
            height=sum((c.child if c.key in {'app.scroll.assets','app.scroll.quick','app.scroll.schematic','app.scroll.particle','app.scroll.settings','app.scroll.sky','app.scroll.posterize'} else c).preferred_size(
                available,state,theme).height for c in self.children)
            height+=self.style.padding[1]+self.style.padding[3]+self.style.gap*max(0,len(self.children)-1)
            natural_height=height
            loading_assets = active == 'ASSETS' and state.get('assets.loading', False)
            saved_height = state.get('_panel_height')
            if saved_height is not None:
                requested_height = saved_height
            elif loading_assets and state.get('_assets_shell_height') is not None:
                # A pack that is still being indexed keeps the shell it had:
                # shrinking to the message and growing back reads as a flicker.
                requested_height = state['_assets_shell_height']
            else:
                # First open: fit the page (three pack rows on the library) and
                # keep a quarter of the viewport visible below the panel.
                requested_height = min(natural_height, max(180, bounds.height*.75))
            # Saved height is a useful viewport size, not mandatory blank space
            # on pages with no expandable list. Preserve it for returning pages.
            fits_content = active in {'QUICK', 'SCHEMATIC', 'PARTICLE', 'SETTINGS', 'SKY', 'POSTERIZE'} or (
                    active == 'ASSETS' and state.get('assets.collapsed')) or (
                    active == 'PACKS' and state.get('packs.visible_count') == 0)
            if fits_content:
                requested_height = min(requested_height, natural_height)
            if active == 'PACKS':
                count = state.get('packs.visible_count', 0)
                # Natural size reserves three rows; larger saved panels can use
                # every remaining row, but must not reserve hidden rows.
                content_limit = natural_height + max(0, count-3)*80
                requested_height = min(requested_height, content_limit)
            # Short pages (Sky before a sky exists) shrink below the usual
            # 180 px floor instead of padding the shell with empty space.
            minimum=min(180,natural_height) if fits_content else 180
            if active == 'ASSETS' and any(c.key == 'assets.import_selected' for c in self.children):
                chrome=sum(c.preferred_size(available,state,theme).height
                           for c in self.children if c.key != 'app.scroll.assets')
                minimum=chrome+self.style.padding[1]+self.style.padding[3]+self.style.gap*max(0,len(self.children)-1)+40
            if active == 'PACKS' and any(isinstance(c,PackPageWithDock) for c in self.children):
                chrome=sum(c.preferred_size(available,state,theme).height for c in self.children if not isinstance(c,PackPageWithDock))
                minimum=chrome+self.style.padding[1]+self.style.padding[3]+self.style.gap*max(0,len(self.children)-1)+158
            height=min(bounds.height,max(minimum,requested_height))
            if active == 'ASSETS' and not loading_assets:
                state['_assets_shell_height'] = height
            # Animate automatic size changes, but never lag a resize gesture.
            height=min(bounds.height,max(minimum,resize_transition(state,'app.auto_height',height)))
            if active == 'PACKS':
                group = next((c for c in self.children if isinstance(c,PackPageWithDock)), None)
                if group:
                    # Remove the same space from shell and dock on every frame.
                    # Their top edges (and the arrow hit target) stay stationary.
                    reduction = tween(state, group.dock.key+'.collapse',
                        40. if state.get(group.dock.key+'.collapsed') else 0., .12)
                    state['_pack_dock_reduction'] = reduction
                    height -= reduction
                    state['_pack_shell_reduction'] = reduction
            if state.get('app.active_tab')=='ASSETS':
                state['_panel_asset_extra']=max(0,height-natural_height)
            bounds=Rect(bounds.x,bounds.top-height,bounds.width,height)
        if getattr(self,'context_source',None):
            self.context_source.layout(bounds,state,theme)
        super().layout(bounds,state,make_theme(theme))
        # Even when fixed page controls exceed an unusually short viewport,
        # keep Settings reachable at the bottom of the visible shell.
        footer = next((c for c in self.children if c.key == 'app.footer'), None)
        if footer:
            footer.layout(Rect(self.rect.x+16,self.rect.y+8,max(0,self.rect.width-32),32),state,theme)
        # The page owns scrolling, but its thumb belongs to the shell edge.
        scrolls=[w for w in self.walk() if isinstance(w, ScrollView)]
        self.edge_scroll = next((w for w in scrolls
                                 if w.key in {'packs.library.scroll', 'assets.library.scroll'}),
                                next((w for w in scrolls if w.key=='app.scroll.assets'),None))
        compact_packs = next((w for w in scrolls if isinstance(w, PackPageScroll) and w.content_height > w.rect.height + .5), None)
        if compact_packs:
            self.edge_scroll = compact_packs
        # Every scrolling page uses the shell gutter, including particle and
        # posterize controls. An internal thumb would overlap their cards.
        if self.edge_scroll is None:
            self.edge_scroll = next((w for w in self.children if isinstance(w, ScrollView)), None)
        # A short asset panel can overflow both the page and its gallery.
        # Share one gutter: scroll the page while its controls overflow, then
        # the gallery when the page fits. Suppress the other thumb and hit area.
        asset_scrolls = [w for w in scrolls if w.key in {
            'app.scroll.assets', 'assets.library.scroll'}]
        outer_assets = next((w for w in asset_scrolls
                             if w.key == 'app.scroll.assets'), None)
        if outer_assets and outer_assets.content_height > outer_assets.rect.height + .5:
            self.edge_scroll = outer_assets
        # Nested pack scrolling shares the same single shell gutter as assets.
        shared_scrolls = asset_scrolls + [w for w in scrolls if w.key in {
            'app.scroll.packs', 'packs.library.scroll'}]
        for scroll in shared_scrolls:
            scroll.external_scrollbar = True
            scroll.scrollbar_track_rect = Rect(0, 0, 0, 0)
        if self.edge_scroll:
            scroll = self.edge_scroll
            scroll.external_scrollbar = True
            track=scroll.rect.intersect(self.rect)
            scroll.scrollbar_track_rect = Rect(self.rect.right-11, track.y, 8, track.height)

    def hit_test(self,x,y):
        if not self.menu:
            scroll = getattr(self,'edge_scroll',None)
            if scroll and scroll.scrollbar_contains(x,y):
                return scroll
        return super().hit_test(x,y)

    def draw(self,renderer,state,theme):
        active=state.get('app.active_tab')
        if state.get('_make_previous_tab')!=active:
            state['_make_previous_tab']=active
            state.setdefault('_motion',{}).pop('make.page.enter',None)
        # The runtime owns the single opening/closing fade.
        # Fade content only; navigation and hit targets stay stationary.
        theme=make_theme(theme)
        renderer.icon_tint = theme.text if theme.follows_blender else None
        renderer.use_blender_font = theme.follows_blender
        renderer.rect(self.rect,theme.canvas,radius=self.style.radius)
        renderer.push_clip(self.rect)
        for child in self.children:
            opacity,offset=renderer.opacity,renderer.offset_y
            child.draw(renderer,state,theme)
            renderer.opacity,renderer.offset_y=opacity,offset
        # The gallery is nested inside a page ScrollView. Draw its shell-edge
        # thumb after page clips are popped so it remains visible in the gutter.
        if getattr(self,'edge_scroll',None):
            self.edge_scroll.draw_scrollbar(renderer,state)
        renderer.pop_clip()
        if self.menu:
            p=tween(state,'app.menu.enter',1.,.14,initial=0.,easing=make_ease)
            self.menu_offset=6*(1-p)
            opacity,offset=renderer.opacity,renderer.offset_y
            renderer.opacity*=p
            renderer.offset_y+=self.menu_offset
            renderer.rect(self.menu_rect,theme.surface,radius=4,border=theme.border)
            self.menu.draw(renderer,state,theme)
            renderer.opacity,renderer.offset_y=opacity,offset

    def handle_menu_event(self,context,event,state,x,y):
        if state.get('app.active_tab') == 'POSTERIZE' or state.get('_posterize_drag'):
            from .posterize_page import handle_event
            if handle_event(context,event,state,x,y,self):
                return True
        if state.get('app.active_tab') in ('SKY', 'OUTLINES', 'LIGHTS') or state.get('_sky_drag'):
            from .sky_page import handle_sky_event
            if handle_sky_event(context,event,state,x,y,self):
                return True
        if self.menu:
            return super().handle_menu_event(context,event,state,x,y)
        from .schematic_drag import SchematicPackList
        stack = next((w for w in self.walk() if isinstance(w, SchematicPackList)), None)
        if stack and stack.pointer(context, event, x, y, state):
            return True
        drag=state.get('_carousel_drag')
        carousel=next((w for w in self.walk() if isinstance(w,Carousel) and
                       ((drag and w.key==drag[0]) or (not drag and w.rect.contains(x,y) and
                        (w.clip_rect is None or w.clip_rect.contains(x,y))))),None)
        previous = carousel.selected(state) if carousel else None
        if carousel and carousel.pointer(event,x,y,state):
            if not carousel.dock and carousel.selected(state) != previous:
                _load_carousel_pack(context,state,carousel,previous,
                                    wheel=event.type in {'WHEELUPMOUSE','WHEELDOWNMOUSE'})
            return True
        if event.type=='RIGHTMOUSE' and event.value=='PRESS':
            hit=self.hit_test(x,y)
            row=hit.ancestor(PackRow) if hit else None
            if row:
                scene=context.scene
                choices=[('folder','Open pack location')]
                state['packs.context']=(row.index,choices)
                state['packs.context.path']=scene.mcbedrock_discovered_packs[row.index].path
                state['packs.context.position']=(x,y)
                state['_menu']={'key':'packs.context.menu','index':0,'scroll':0}
                return True
        return False


class ContextMenuSource(Dropdown):
    def layout(self,bounds,state,theme):
        x,y=state.get('packs.context.position',(bounds.x,bounds.top))
        self.rect=Rect(x,y,0,0)

    def draw(self,renderer,state,theme):
        pass


def context_menu(context,state):
    entry=state.get('packs.context')
    if not entry:
        return None
    index,_choices=entry
    from .pack_manager import _normalized_path
    packs=context.scene.mcbedrock_discovered_packs
    path=state.get('packs.context.path')
    if path is None and 0<=index<len(packs):
        path=packs[index].path
    item=next((item for item in packs if _normalized_path(item.path)==_normalized_path(path or '')),None)
    if item is None:
        return None
    choices=[('folder','Open pack location'),
             ('exclude','Remove from list' if item.launcher=='Manual' else 'Exclude from list')]
    def choose(ctx,value):
        from .styled_pages import invoke
        scene=ctx.scene
        current=next((i for i,item in enumerate(scene.mcbedrock_discovered_packs)
                      if _normalized_path(item.path)==_normalized_path(path)),None)
        if current is None:
            return
        if value=='folder':
            invoke('mcbedrock.open_pack_location',invoke=False,index=current)(ctx)
        elif value=='exclude':
            invoke('mcbedrock.exclude_pack',invoke=False,filepath=path)(ctx)
            state.pop('packs.context',None)
            state.pop('packs.context.path',None)
    return ContextMenuSource('packs.context.menu',lambda ctx:'',choose,choices,style=Style(height=0,width=0))

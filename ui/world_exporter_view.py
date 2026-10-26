"""World Exporter frame: the top bar and the sidebar around the real map."""
from pathlib import Path
import time
from .gpu_framework import Rect
from .world_exporter_layout import (geometry, draw_inspector, sweep, text_width, fit_text, edition_tag, wrap, friendly,
                                    T, PAD, GUTTER, PANEL, EDGE)
from .world_exporter_canvas import ICONS
from .gpu_framework.motion import tween, make_ease
from ..exporters.miex.worlds import dimension_key

# The three vanilla dimensions get tabs, in the game's order.
DIMENSION_TABS = (('overworld','Overworld'),('the_nether','Nether'),('the_end','The End'))
SEGMENT = 100


def render(context, s, paint, draw_map):
    r = paint.r
    settings = context.scene.mcbedrock_world_exporter
    from .custom_ui import _addon_preferences
    import bpy
    still = settings.reduce_motion or bool(getattr(_addon_preferences(bpy.context), 'reduce_motion', False))
    s.motion['_motion_disabled'] = still
    # Motion clock first: panel and selection tweens read it while drawing.
    s.motion['_motion_time'] = time.monotonic()
    s.motion['_motion_active'] = False
    s.motion['_motion_sweep'] = False
    w, h = s.size
    # The #141414 canvas shows through the 8 px gutters.
    r.rect(Rect(0,0,w,h),T.canvas)
    s.map_rect = geometry(w, h, s.ui_ready and s.inspector_open, s.ui_ready)[0]
    draw_map(context, s, paint, settings, s.map_rect)
    now = time.monotonic()
    # A save may have no generated chunks at (0, 0). Finish the empty preview
    # too, so navigation and Go to player remain available instead of loading forever.
    if s.loaded and not s.ui_ready and (s.textures or (s.tiles and all(key in s.tiles for key in s.visible()))):
        s.ui_ready = True
        s.reveal_started = now
    opened = s.ui_ready and s.inspector_open
    # The first ready frame gains the top bar and sidebar; hit tests use the new map rect.
    s.map_rect = geometry(w, h, opened, s.ui_ready)[0]
    # A hidden sidebar slides out and fades instead of vanishing (N, or the map menu).
    shown = tween(s.motion, 'miex.panels', 1. if opened else 0., .2, initial=0.)
    drawn = opened or (s.ui_ready and shown > .01)
    _, side, _ = geometry(w, h, drawn, s.ui_ready)
    bar = geometry(w, h, opened, s.ui_ready)[2]
    progress = 1 if s.reveal_started is None or s.motion.get('_motion_disabled') else min(1, max(0, (now-s.reveal_started)/.2))
    ease = 1-(1-progress)**3
    if progress < 1:s.motion['_motion_active'] = True
    ease = min(ease, shown)
    # The sidebar slides in from its edge as the map finishes loading.
    side = Rect(side.x+12*(1-ease), side.y, side.width, side.height)
    s.inspector_rect = side if opened else Rect(0,0,0,0)
    s.toolbar_rect = bar
    opacity = r.opacity
    if drawn:
        try:
            r.opacity = opacity*ease
            draw_inspector(paint, settings, s, side, context)
        finally:
            r.opacity = opacity

    from . import world_exporter as ui
    loading = s.pending == 'LOAD' or (ui.busy() and ui._job.get('action') == 'LOAD') or (s.loaded and not s.ui_ready and not s.failed and not s.paused)
    s.world_picker_landing = not s.ui_ready and not loading
    if s.world_picker_landing:
        draw_landing(paint, s, settings, w, h)
        return
    s.motion.get('_motion',{}).pop('miex.landing',None)
    if not loading:s.motion.get('_motion',{}).pop('miex.loading',None)
    if loading and s.menu=='worlds':s.menu=None
    if loading:
        draw_loading(paint, s, settings)
    elif s.ui_ready:
        appear = min(1, max(0, (now-s.reveal_started)/.2)) if s.reveal_started is not None and not s.motion.get('_motion_disabled') else 1
        try:
            r.opacity = opacity*(1-(1-appear)**3)
            draw_topbar(paint, s, settings, bar, s.map_rect)
            # The one control floating on the map: the Flat | 3D switch, top-left under the bar.
            from .world_exporter_preview import draw_switch
            s.preview_button = draw_switch(paint, s)
        finally:
            r.opacity = opacity
        s.overlay_rects = [s.preview_button]


def card_frame(r, rect):
    """Floating card: the #141414 ground, 8 px corners and a hairline edge, no shadow."""
    r.rect(rect, T.canvas, radius=8, border=T.border)


def world_folder(s, path):
    """The saved world folder if it exists, checked every 2 s at most: a sleeping or
    unplugged drive must not stall each frame of the start card."""
    now = time.monotonic()
    cached = getattr(s,'world_folder_check',None)
    if not cached or cached[0] != path or now-cached[1] >= 2:
        try:found = bool(path) and Path(path).is_dir()
        except OSError:found = False
        cached = s.world_folder_check = (path,now,found)
    return path if cached[2] else ''


def failure_text(status):
    """A failed status in the same plain words the sidebar uses."""
    return friendly(status.removeprefix('Failed: ')) if status.startswith('Failed') else status


def draw_landing(paint, s, settings, w, h):
    """Start card: a title and the choose button; reopening the last world is the quiet second choice."""
    r = paint.r
    s.island_rect = Rect(w/2,h,0,0)
    failed = settings.status.startswith(('Failed','Cancelled'))
    recent = world_folder(s,settings.world_path)
    width = min(360,max(0,w-32))
    # Errors explain what to do next, so show them in full rather than cutting them to one line.
    error = wrap(failure_text(settings.status),width-48,11,lines=4) if failed else []
    height = 24+24+20+36+24+(10+16*len(error) if error else 0)+(38 if recent else 0)
    appear = tween(s.motion,'miex.landing',1.0,.32,initial=0.0,easing=make_ease)
    card = Rect((w-width)/2,(h-height)/2-10*(1-appear),width,height)
    s.overlay_rects=[card]
    opacity = r.opacity
    r.opacity = opacity*appear
    try:
        card_frame(r, card)
        x = card.x+24;inner = width-48
        r.text(Rect(x,card.top-48,inner,24),'Import a Minecraft world',T.text,size=17,weight='SemiBold')
        for i,line in enumerate(error):
            r.text(Rect(x,card.top-72-16*i,inner,18),line,T.danger,size=11)
        y = card.y+24
        if recent:
            name = settings.world_name or Path(settings.world_path).name
            paint.button(Rect(x,y,inner,32),fit_text(r,'Reopen '+name,inner-20,12),('reopen_world',),size=12)
            y += 38
        paint.button(Rect(x,y,inner,36),'Choose world…',('menu','worlds'),active=True,size=12.5,weight='SemiBold')
    finally:
        r.opacity = opacity

def draw_loading(paint, s, settings):
    """Loading card, centred over the map while a world opens."""
    r = paint.r
    appear = tween(s.motion,'miex.loading',1.0,.24,initial=0.0,easing=make_ease)
    opacity = r.opacity
    r.opacity = opacity*appear
    try:_draw_loading(paint, s, settings, 8*(1-appear))
    finally:r.opacity = opacity


def _draw_loading(paint, s, settings, lift):
    r = paint.r
    available = s.map_rect.width
    width = min(320,max(0,available-32))
    island = Rect(s.map_rect.x+(available-width)/2,(s.map_rect.height-132)/2-lift,width,132)
    s.island_rect = island;s.overlay_rects = [island]
    card_frame(r, island)
    x = island.x+PAD;inner = width-2*PAD
    name = settings.world_name or 'world'
    r.text(Rect(x,island.top-36,inner,20),fit_text(r,'Opening '+name+'…',inner,12.5,'SemiBold'),T.text,size=12.5,weight='SemiBold')
    track = Rect(x,island.top-48,inner,2)
    r.rect(track,T.surface_raised,radius=1)
    sweep(r,s,track)
    r.text(Rect(x,island.top-74,inner,18),fit_text(r,settings.status,inner,11),T.text_muted,size=11)
    paint.button(Rect(x,island.y+PAD,inner,32),'Cancel',('cancel',),size=12)


def current_world(s, settings):
    """Name, edition, version and icon of the open world.

    Discovery already read them for listed worlds; a world opened by folder is
    read once from its level.dat and remembered.
    """
    path = settings.world_path
    if not path:return {}
    cached = getattr(s,'world_info',None)
    if cached and cached[0] == (path,len(settings.worlds)):return cached[1]
    info = {}
    try:
        wanted = Path(path).resolve()
        world = next((w for w in settings.worlds if w.path and Path(w.path).resolve() == wanted),None)
        if world is not None:
            info = dict(display_name=world.display_name or world.name,edition=world.edition,version=world.version,icon_path=world.icon_path)
        elif wanted.is_dir():
            from ..exporters.miex.worlds import world_details
            info = world_details({'path':str(wanted),'name':wanted.name})
    except OSError:pass
    s.world_info = ((path,len(settings.worlds)),info)
    return info


def dimension_tabs(settings):
    """(label, dimension) tabs: the vanilla three this world has, plus the open one.

    Returns the tabs and whether more dimensions wait behind a menu.
    """
    names = [d.name for d in settings.dimensions]
    keys = {dimension_key(name):name for name in names}
    tabs = [(label,keys[key]) for key,label in DIMENSION_TABS if key in keys]
    current = settings.dimension
    if current and all(dimension_key(name) != dimension_key(current) for _,name in tabs):
        from .world_exporter_canvas import dimension_label
        tabs.append((dimension_label(current),current))
    if not tabs:tabs = [('Overworld','minecraft:overworld')]
    shown = {dimension_key(name) for _,name in tabs}
    return tabs, any(dimension_key(name) not in shown for name in names)


def draw_topbar(paint, s, settings, bar, column):
    """Full-width panel bar; the world picker and dimension tabs centre over the map."""
    r = paint.r
    if bar.height <= 0:return
    r.rect(bar,PANEL)
    r.rect(Rect(bar.x,bar.y-1,bar.width,1),EDGE)
    if s.inspector_rect.width:r.rect(Rect(s.inspector_rect.x-1,0,1,bar.y-1),EDGE)
    bar = Rect(bar.x,bar.y,column.width,bar.height)
    world = current_world(s,settings)
    name = settings.world_name or world.get('display_name') or 'Choose world'
    edition = world.get('edition','')
    version = world.get('version','')
    tabs, more = dimension_tabs(settings)
    segment = SEGMENT
    room = bar.width-24
    name_width = text_width(r,name,13,'SemiBold')
    tag_width = (22+text_width(r,edition,11,'SemiBold')+7) if edition else 0
    version_width = text_width(r,version,11) if version else 0

    def widths():
        picker = 6+28+10+name_width+(10+tag_width if tag_width else 0)+(10+version_width if version_width else 0)+10+10+10
        tabs_width = segment*len(tabs)+(32 if more else 0)
        return picker, tabs_width, picker+16+1+16+tabs_width

    picker, tabs_width, total = widths()
    # Narrow windows drop the version, then the edition tag, then shorten the name and tabs.
    if total > room:version_width = 0;picker, tabs_width, total = widths()
    if total > room:tag_width = 0;picker, tabs_width, total = widths()
    if total > room:name_width = max(48,name_width-(total-room));picker, tabs_width, total = widths()
    if total > room:segment = max(64,segment-(total-room)/max(1,len(tabs)));picker, tabs_width, total = widths()
    x = round(bar.x+max(12,(bar.width-total)/2))
    mid = bar.y+bar.height/2
    world_rect = Rect(x,mid-20,picker,40)
    icon = Rect(x+6,mid-14,28,28)
    if not (world.get('icon_path') and r.image(icon,world['icon_path'],radius=3,cover=True)):
        r.rect(icon,T.surface_raised,radius=3)
        r.image(Rect(icon.x+6,icon.y+6,16,16),str(ICONS/'miex-globe.png'),tint=T.text_muted)
    cursor = icon.right+10
    r.text(Rect(cursor,world_rect.y,name_width+2,world_rect.height),fit_text(r,name,name_width+2,13,'SemiBold'),T.text,size=13,weight='SemiBold')
    cursor += name_width+10
    if tag_width:
        edition_tag(r,cursor,mid-9.5,edition)
        cursor += tag_width+10
    if version_width:
        r.text(Rect(cursor,world_rect.y,version_width+2,world_rect.height),version,T.text_muted,size=11)
        cursor += version_width+10
    # The arrow turns up while the world list is open.
    paint.icon(Rect(cursor,world_rect.y,10,world_rect.height),'chevron',T.text,10,
               rotation=180*paint.ease('worlds.arrow',s.menu=='worlds',.2))
    paint.ring(world_rect,('menu','worlds'))
    paint.target(world_rect,('menu','worlds'));paint.tip(world_rect,'Switch world')
    s.island_rect = world_rect
    divider = world_rect.right+16
    r.rect(Rect(divider,mid-10,1,20),T.border)
    draw_dimension_tabs(paint,s,settings,Rect(divider+17,mid-15,tabs_width,30),tabs,more,segment)


def draw_dimension_tabs(paint, s, settings, rect, tabs, more, segment):
    """Segmented dimension switch: the open one on a sliding Sparkle Blue pill."""
    r = paint.r
    r.rect(rect,T.canvas,radius=4)
    selected = next((i for i,(_,name) in enumerate(tabs) if dimension_key(name)==dimension_key(settings.dimension)),-1)
    if selected >= 0:
        position = paint.ease('dimension.tab',selected,.22)
        r.rect(Rect(rect.x+position*segment+2,rect.y+2,segment-4,rect.height-4),T.accent,radius=3)
    for i,(label,name) in enumerate(tabs):
        cell = Rect(rect.x+i*segment,rect.y,segment,rect.height)
        cmd = ('dimension',name)
        active = i == selected
        r.text(cell.inset((6,0,6,0)),fit_text(r,label,segment-12,11.5,'SemiBold' if active else 'Medium'),
               (1,1,1,1) if active else T.text_muted,size=11.5,weight='SemiBold' if active else 'Medium',align='CENTER')
        if paint.focused(cmd):r.rect(cell.inset((2,2,2,2)),(0,0,0,0),radius=3,border=T.text,border_width=1.5)
        paint.target(cell,cmd);paint.tip(cell,'Show '+label)
    if more:
        cell = Rect(rect.x+len(tabs)*segment,rect.y,32,rect.height)
        paint.icon(cell,'chevron',T.text_muted,10,rotation=180*paint.ease('dimensions.arrow',s.menu=='dimensions',.2))
        paint.ring(cell,('menu','dimensions'))
        paint.target(cell,('menu','dimensions'));paint.tip(cell,'More dimensions')
        s.anchors['dimensions'] = (cell.right-200,rect.y-GUTTER)


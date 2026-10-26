"""World chooser: the world list with each world's icon, dropping from the breadcrumb."""
from pathlib import Path
import time
from .gpu_framework import Rect
from .gpu_framework.theme_adapter import miex_theme
from .gpu_framework.motion import tween, blend, make_ease

T = miex_theme()
ICONS = Path(__file__).resolve().parents[1]/'assets/ui/icons'


def draw_worlds(context, paint, s, closing=False):
    """Keep closing artwork drawable without leaving its controls interactive."""
    r = paint.r
    progress = tween(s.motion, 'worlds.reveal', 0.0 if closing else 1.0,
                     .14 if closing else .20, initial=0.0, easing=make_ease)
    s.world_picker_visible = progress
    opacity, targets = r.opacity, s.targets
    r.opacity = opacity * progress
    try:
        _draw_worlds(context, paint, s, progress, closing)
    finally:
        r.opacity = opacity
        if closing:
            s.targets = targets
            if progress > .001:s.overlay_rects.append(s.menu_rect)


def _draw_worlds(context, paint, s, progress, closing):
    from . import world_exporter as ui
    from .world_exporter_canvas import menu_items, action_locked, search_caret_visible
    settings = context.scene.mcbedrock_world_exporter
    r = paint.r
    w, h = s.size
    if closing:
        items = [item for item in getattr(s,'world_picker_items',[]) if item[1][1]<len(settings.worlds)]
    else:
        items = [item for item in menu_items(context,s) if s.search.casefold() in item[0].casefold()]
        s.world_picker_items = items
    scanning = s.pending == 'WORLDS' or (ui.busy() and ui._job.get('action') == 'WORLDS')
    landing = getattr(s,'world_picker_was_landing',False) if closing else getattr(s,'world_picker_landing',False)
    host_width = w
    width = min(480, host_width-24)
    if not closing:s.world_picker_was_landing = landing
    row_height, reserved = 80, 144
    # The picker drops from the toolbar breadcrumb, or centres on the landing card.
    available = max(0, h-64 if landing else min(h-24, s.island_rect.y-16))
    # Search keeps the panel anchored and its height stable.
    capacity = max(0, min(6, int((available-reserved)/row_height)))
    # A short window still lists one world, taking more of the window if it must.
    if len(settings.worlds) and capacity < 1:
        capacity = 1;available = max(available, min(h-24, reserved+row_height))
    count = min(capacity, max(1,len(settings.worlds)))
    height = min(available, reserved+count*row_height)
    top = min(h-12,s.island_rect.y-8)
    left=(host_width-width)/2 if landing else max(12,min(s.island_rect.x+s.island_rect.width/2-width/2,host_width-width-12))
    box = Rect(left, max(12,top-height), width,height)
    if landing:box = Rect(box.x,(h-height)/2,width,height)
    if closing:box = getattr(s,'world_picker_box',box)
    else:s.world_picker_box = box
    box = Rect(round(box.x*s.scale)/s.scale,round((box.y+4*(1-progress))*s.scale)/s.scale,box.width,box.height)
    s.menu_rect = box
    s.menu_visible_count = max(1,count)
    s.menu_scroll = max(0,min(s.menu_scroll,max(0,len(items)-count)))
    s.menu_index = max(0,min(s.menu_index,len(items)-1))
    s.targets = []
    r.rect(Rect(box.x,box.y-3,box.width,box.height),(0,0,0,.3),radius=10)
    r.rect(box,T.canvas,radius=8,border=T.border)
    r.push_clip(box)
    r.text(Rect(box.x+16,box.top-40,width-150,24),'Choose a world' if landing else 'Your worlds',T.text,size=14,weight='SemiBold')
    count_text = 'Scanning…' if scanning else f'{len(items)} world'+('' if len(items)==1 else 's')
    r.text(Rect(box.right-140,box.top-40,88,24),count_text,T.text_muted,size=11,align='RIGHT')
    icon_button(paint,s,Rect(box.right-40,box.top-40,24,24),'close',('close_menu',),12)
    search = Rect(box.x+16,box.top-88,width-32,36)
    r.rect(search,T.input_surface,radius=T.radius,border=T.accent if s.search_focused else None)
    s.targets.append((Rect(search.x,search.y,search.width-38,search.height),('search_focus',)))
    r.image(Rect(search.x+12,search.y+11,14,14),str(ICONS/'search.png'))
    text_box=Rect(search.x+36,search.y,search.width-80,36)
    visible_text=s.search
    if s.search_focused:
        while visible_text and r.text_width(visible_text,12)>text_box.width-5:visible_text=visible_text[1:]
        if s.search_select_all and visible_text:
            r.rect(Rect(text_box.x,text_box.y+8,min(text_box.width,r.text_width(visible_text,12)),20),(*T.accent[:3],.25))
    r.text(text_box,visible_text or ('Search worlds...' if not s.search_focused else ''),
           T.input_text if s.search else T.text_muted,size=12)
    if search_caret_visible(s,s.motion.get('_motion_time',time.monotonic())):
        caret=text_box.x+(r.text_width(visible_text,12) if visible_text else 0)
        r.rect(Rect(caret+1,search.y+10,1,16),T.input_text)
    icon_button(paint,s,Rect(search.right-34,search.y+2,32,32),'refresh',('job','WORLDS'),16)
    content = Rect(box.x+16,box.y+44,width-32,max(0,box.height-144))
    r.push_clip(content)
    loaded_path=s.signature[0] if s.loaded and s.signature else settings.world_path if s.loaded else ''
    for offset, (_,cmd) in enumerate(items[s.menu_scroll:s.menu_scroll+count]):
        world = settings.worlds[cmd[1]]
        row = Rect(content.x,box.top-100-(offset+1)*row_height+8,content.width,72)
        started=s.worlds_refresh_started
        appear=1.0
        if started is not None and not s.motion.get('_motion_disabled'):
            elapsed=s.motion.get('_motion_time',time.monotonic())-started-offset*.04
            appear=0.0 if elapsed<=0 else make_ease(elapsed/.08) if elapsed<.08 else 1.0
            if appear<1:s.motion['_motion_active']=True
        row_opacity=r.opacity
        r.opacity *= appear
        # Stable card positions keep text and thumbnails aligned during fades.
        selected = bool(loaded_path and Path(world.path)==Path(loaded_path))
        focused = s.worlds_keyboard_focus and s.menu_index == s.menu_scroll+offset
        enabled = not action_locked(s,cmd)
        hovered = row.contains(*s.hover) and content.contains(*s.hover) and enabled and not closing
        key = 'worlds.card.'+world.path
        hover = 0.
        selection = tween(s.motion,key+'.selected',float(selected),.14)
        background = blend(blend(T.surface,T.selected,selection),T.surface_hover,hover*.35)
        r.rect(row,background,radius=T.radius,border=T.accent if focused else None)
        narrow = width < 420
        size = 36 if narrow else 44
        icon = Rect(row.x+12,row.top-46 if narrow else row.y+14,size,size)
        if not world.icon_path or not r.image(icon,world.icon_path,radius=T.radius,cover=True):
            r.rect(icon,T.surface_raised,radius=T.radius)
            r.image(Rect(icon.x+(size-20)/2,icon.y+(size-20)/2,20,20),str(ICONS/'miex-globe.png'))
        name = world.display_name or world.name
        source = world.launcher
        if world.instance and world.instance != world.version:source += ' / '+world.instance
        edition = world.edition or 'Minecraft'
        version = world.version or 'Version unavailable'
        badge_width = 74 if edition == 'Bedrock' else 58
        if narrow:
            r.text(Rect(row.x+12,row.y+4,row.width-24,22),name,T.text,size=12,weight='SemiBold')
            x = icon.right+12
            badge = Rect(x,row.top-28,badge_width,19)
            edition_badge(r,badge,edition)
            r.text(Rect(badge.right+8,badge.y,row.right-badge.right-20,19),version,T.text_muted,size=11)
            r.text(Rect(icon.right+12,row.top-46,row.right-icon.right-24,18),source,T.text_muted,size=10)
        else:
            x = icon.right+12
            r.text(Rect(x,row.y+38,row.right-x-12,24),name,T.text,size=13,weight='SemiBold')
            badge = Rect(x,row.y+19,badge_width,19)
            edition_badge(r,badge,edition)
            r.text(Rect(badge.right+8,badge.y,row.right-badge.right-20,19),version,T.text_muted,size=11)
            r.text(Rect(x,row.y+4,row.right-x-12,17),source,T.text_muted,size=10)
        hit = row.intersect(content)
        if enabled and hit.height>0:s.targets.append((hit,cmd))
        r.opacity=row_opacity
    if not items:
        message = 'Looking for installed worlds…' if scanning else 'No worlds match your search.' if s.search else 'No worlds found. Browse to a world folder.'
        r.text(content.inset((12,0,12,0)),message,T.text_muted,size=11)
    r.pop_clip()
    if len(items)>count and count:
        thumb = max(16,content.height*count/len(items))
        y = content.top-thumb-(content.height-thumb)*s.menu_scroll/max(1,len(items)-count)
        r.rect(Rect(box.right-7,y,2,thumb),T.text_muted,radius=1)
    paint.button(Rect(box.right-148,box.y+8,132,28),'Browse folder…',('browse','world_path'),surface=T.surface,radius=T.radius)
    if landing and settings.status.startswith(('Failed','Cancelled')):
        # Two short lines beside Browse, so the way out at the end of the message stays.
        from .world_exporter_layout import wrap
        from .world_exporter_view import failure_text
        lines = wrap(failure_text(settings.status),width-180,10,lines=2)
        for i,line in enumerate(lines):
            line_box = Rect(box.x+16,box.y+8,width-180,28) if len(lines) == 1 else Rect(box.x+16,box.y+22-14*i,width-180,14)
            r.text(line_box,line,T.text_muted,size=10)
    r.pop_clip()


def icon_button(paint,s,rect,icon,cmd,size):
    from .world_exporter_canvas import action_locked
    enabled = not action_locked(s,cmd)
    hover = 0.
    if hover:paint.r.rect(rect,(*T.surface_hover[:3],hover),radius=T.radius)
    paint.r.image(Rect(rect.x+(rect.width-size)/2,rect.y+(rect.height-size)/2,size,size),str(ICONS/(icon+'.png')))
    if enabled:s.targets.append((rect,cmd))


def edition_badge(renderer,rect,edition):
    # Resource Pack Manager's edition tag colors, dimensions and font treatment.
    java = edition == 'Java'
    background = (30/255,61/255,20/255,1) if java else (28/255,48/255,77/255,1)
    text = (109/255,207/255,74/255,1) if java else (133/255,180/255,1,1)
    renderer.rect(rect,background,radius=T.radius)
    renderer.image(Rect(rect.x+6,rect.y+4,11,11),str(ICONS/'java.png'),tint=text)
    renderer.text(Rect(rect.x+22,rect.y,rect.width-26,rect.height),edition,text,size=10,weight='SemiBold')

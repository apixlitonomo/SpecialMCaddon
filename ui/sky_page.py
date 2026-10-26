"""Plain-language sky controls in the existing Sparkle interface."""
from pathlib import Path
from time import monotonic
import bpy
from ..builders.compatibility import SKY_REQUIREMENT, SkyOperator, supports_sky
from .gpu_framework import Button, Column, Row, Label, Rect, Style, Toggle
from .gpu_framework.core import Size
from .gpu_framework.motion import tween, blend
from .gpu_framework.navigation import Dropdown
from .styled_pages import button, actions, invoke, scene_scan
from ..builders.sky import PRESETS, current_time, is_preset_current
# The page offers the four time-of-day looks.
TIME_PRESETS = ('DAY','GOLDEN','SUNSET','NIGHT')
from ..builders.sky_textures import PHASES, resolve, load_images
from ..builders import native_color
from ..builders.clouds import PRESETS as CLOUD_PRESETS
from bpy_extras.io_utils import ImportHelper


ICONS=Path(__file__).resolve().parents[1]/'assets/ui/icons'


def text_width(renderer,text,size,weight='Regular'):
    measured=renderer.text_width(text,size,weight) if hasattr(renderer,'text_width') else None
    return measured if isinstance(measured,(int,float)) else len(text)*size*.56


def focus_ring(renderer,rect,state,key,theme,radius=4):
    if state.get('_runtime_focus')==key and state.get('_focus_visible'):
        renderer.rect(Rect(rect.x-2,rect.y-2,rect.width+4,rect.height+4),(0,0,0,0),radius=radius+2,border=theme.text,border_width=1.5)


class WrapLabel(Label):
    """Hint, status and error text that wraps onto more lines instead of being
    cut off at narrow panel widths (the useful part of a message is often its
    end). A text that fits draws exactly like a Label."""
    LINE=16.

    def __init__(self,key,text,**kwargs):
        kwargs.setdefault('style',Style(min_height=20.))
        super().__init__(key,text,**kwargs)

    def _font_size(self,theme):
        return self.style.font_size or theme.font_size

    def lines(self,width,measure):
        lines,line=[],''
        for word in self.text.split():
            candidate=f'{line} {word}' if line else word
            if line and measure(candidate)>width:
                lines.append(line)
                line=word
            else:
                line=candidate
        return lines+[line] if line else (lines or [''])

    def preferred_size(self,available,state,theme):
        width=self._clamp_width(self.style.width if self.style.width is not None else available.width)
        # Drawing measures the real text and remembers how many lines it took
        # at this width; until then, a slightly generous guess per character.
        known=state.get('_wrap_lines',{}).get(self.key)
        if known is not None and abs(known[0]-width)<.5 and known[1]==self.text:
            count=known[2]
        else:
            size=self._font_size(theme)
            count=len(self.lines(max(1.,width),lambda text:len(text)*size*.58))
        return Size(width,self._clamp_height(20. if count<=1 else count*self.LINE+4.))

    def draw(self,renderer,state,theme):
        size=self._font_size(theme)
        lines=self.lines(self.rect.width,lambda text:text_width(renderer,text,size,self.style.font_weight))
        state.setdefault('_wrap_lines',{})[self.key]=(self.rect.width,self.text,len(lines))
        if len(lines)<=1 or self.rect.height<2*self.LINE:
            super().draw(renderer,state,theme)     # one line, or until the next layout makes room
            return
        rows=max(1,int((self.rect.height-4.)//self.LINE))
        if len(lines)>rows:
            lines=lines[:rows-1]+[' '.join(lines[rows-1:])]     # the last row ends in "..."
        color=self.style.text_color or (theme.text_muted if self.muted else theme.text)
        for index,line in enumerate(lines):
            renderer.text(Rect(self.rect.x,self.rect.top-2.-(index+1)*self.LINE,self.rect.width,self.LINE),
                          line,color,size=size,weight=self.style.font_weight,cap_center=True)


def hint(key,text):
    """A muted help or status line that wraps instead of being cut off."""
    return WrapLabel(key,text,muted=True)


def middle_ellipsis(text,width,measure):
    """text shortened in its middle to fit width, keeping its start and its
    end: a folder path keeps its drive and its last folders."""
    if measure(text)<=width:
        return text
    mark='…'
    best=mark if measure(mark)<=width else ''
    low,high=1,len(text)-1
    while low<=high:
        keep=(low+high)//2
        head=keep//3
        candidate=text[:head]+mark+text[len(text)-(keep-head):]
        if measure(candidate)<=width:
            best,low=candidate,keep+1
        else:
            high=keep-1
    return best


class PathLabel(Label):
    """A label ending in a path: its middle gives way so the folder name stays."""
    def __init__(self,key,prefix,path,**kwargs):
        super().__init__(key,prefix+path,**kwargs)
        self.prefix,self.path=prefix,path

    def draw(self,renderer,state,theme):
        size=self.style.font_size or theme.font_size
        def measure(text):
            return text_width(renderer,text,size,self.style.font_weight)
        room=self.rect.inset(self.style.padding).width-measure(self.prefix)-1
        self.text=self.prefix+middle_ellipsis(self.path,max(0.,room),measure)
        super().draw(renderer,state,theme)


def look_state(settings,state,preset):
    """'current' when the look is untouched, 'edited' when it is the base but changed."""
    if not (settings.active and settings.preset==preset):return ''
    return 'current' if is_preset_current(settings,state) else 'edited'


def look_tooltip(label,status):
    if status=='current':return label+' is active'
    if status=='edited':return 'Reset to '+label+'. Discards your changes.'
    return 'Switch to '+label+'. Replaces your current look.'


def look_badge(renderer,badge,theme,status,pick):
    """A check for the active look; a reset arrow once it has been edited."""
    if status=='edited':
        renderer.rect(badge,theme.surface_raised[:3]+(pick,),radius=8,border=theme.accent[:3]+(pick,))
        renderer.image(badge.inset((3,3,3,3)),str(ICONS/'arrow-counter-clockwise.png'),tint=theme.text[:3]+(pick,))
    else:
        renderer.rect(badge,theme.accent[:3]+(pick,),radius=8)
        renderer.image(badge.inset((3,3,3,3)),str(ICONS/'check.png'),tint=theme.selected_text[:3]+(pick,))


# Labels above dropdowns match the slider labels (11 px, muted).
FIELD_LABEL=Style(height=20,font_size=11)


def hover_mix(state,key,duration=.14):
    """Eased 0..1 press emphasis for one widget; hover changes nothing."""
    active=state.get('_runtime_pressed')==key
    return tween(state,key+'.emphasis',float(active),duration)


class SkySectionHeader(Button):
    def __init__(self,key,title,state):
        self.expanded=state.get('sky.section.open.'+key,key=='lighting')
        def toggle(context):
            state['sky.section.open.'+key]=not self.expanded
        super().__init__('sky.section.'+key+'.title',title,toggle,
                         style=Style(height=32,align='LEFT',padding=(20,0,6,0),font_weight='SemiBold'),
                         tooltip=('Collapse ' if self.expanded else 'Expand ')+title)

    def draw(self,renderer,state,theme):
        hover=0.
        focused=state.get('_runtime_focus')==self.key and state.get('_focus_visible',False)
        opened=tween(state,self.key+'.open',float(self.expanded),.18)
        icon=ICONS/('chevron.png' if opened>.5 else 'chevron-right.png')
        renderer.image(Rect(self.rect.x+1,self.rect.y+(self.rect.height-10)/2,10,10),str(icon),
                       tint=blend(theme.text_muted,theme.text,max(hover,opened)))
        title=self.rect.inset(self.style.padding)
        renderer.text(title,self.text,theme.accent if focused else blend(blend(theme.text,theme.text_muted,.15),theme.text,hover),
                      size=12,weight='SemiBold',align='LEFT',cap_center=True)


class SkyGroup(Column):
    """Flat section: a hairline above, and its controls cascade in when it opens."""
    def __init__(self,key,children,expanded,style):
        super().__init__(key,children,style=style)
        self.expanded=expanded

    def draw(self,renderer,state,theme):
        renderer.rect(Rect(self.rect.x,self.rect.top-1,self.rect.width,1),theme.border)
        header,*content=self.children
        header.draw(renderer,state,theme)
        if not content:
            return
        now=state.get('_motion_time',monotonic())
        started=state.setdefault(self.key+'.opened',now)
        still=state.get('_motion_disabled')
        opacity,offset=renderer.opacity,getattr(renderer,'offset_y',0.)
        try:
            for index,child in enumerate(content):
                if child.clip_rect is not None and child.rect.intersect(child.clip_rect).height<=0:
                    continue
                linear=1. if still else min(1.,max(0.,(now-started-min(index,8)*.025)/.24))
                if linear<1:
                    state['_motion_active']=True
                appear=1-(1-linear)**3
                renderer.opacity=opacity*appear
                if hasattr(renderer,'offset_y'):
                    renderer.offset_y=offset-8*(1-appear)
                child.draw(renderer,state,theme)
        finally:
            renderer.opacity=opacity
            if hasattr(renderer,'offset_y'):
                renderer.offset_y=offset


class QuietButton(Button):
    """Low-emphasis text action for rare, page-level commands."""
    def draw(self,renderer,state,theme):
        hover=hover_mix(state,self.key)
        if hover>.01:
            renderer.rect(self.rect,theme.surface_hover[:3]+(hover,),radius=4)
        renderer.text(self.rect,self.text,blend(theme.text_muted,theme.text,hover),size=11.5,weight='Medium',align='CENTER',cap_center=True)
        if state.get('_runtime_focus')==self.key and state.get('_focus_visible'):
            focus_ring(renderer,self.rect,state,self.key,theme)


class DetailsToggle(Button):
    def __init__(self,key,expanded,action,label=None,tooltip=None):
        self.expanded=expanded
        super().__init__(key,label or ('Hide details' if expanded else 'Show details'),action,
                         style=Style(height=28,align='LEFT'),
                         tooltip=tooltip or ('Hide extra controls' if expanded else 'Show every control in this section'))

    def draw(self,renderer,state,theme):
        hover=hover_mix(state,self.key)
        color=blend(theme.text_muted,theme.text,hover)
        icon=ICONS/('chevron.png' if self.expanded else 'chevron-right.png')
        renderer.image(Rect(self.rect.x+1,self.rect.y+(self.rect.height-9)/2,9,9),str(icon),tint=color)
        renderer.text(Rect(self.rect.x+20,self.rect.y,self.rect.width-20,self.rect.height),self.text,color,size=11,cap_center=True)
        focus_ring(renderer,self.rect,state,self.key,theme)


class Segment(Button):
    show_focus_outline=False

    def __init__(self,key,text,action,selected,tooltip=''):
        super().__init__(key,text,action,style=Style(height=28,flex=1,align='CENTER'),tooltip=tooltip)
        self.selected=selected
        self.primary=selected

    def draw(self,renderer,state,theme):
        hover=hover_mix(state,self.key)
        color=(1,1,1,1) if self.selected else blend(theme.text_muted,theme.text,hover)
        size=11 if self.rect.width<90 else 11.5
        if text_width(renderer,self.text,size)>self.rect.width-8:size=9.5
        renderer.text(self.rect,self.text,color,size=size,weight='SemiBold' if self.selected else 'Regular',align='CENTER',cap_center=True)
        if state.get('_runtime_focus')==self.key and state.get('_focus_visible'):
            # An accent ring vanished on the selected option's accent pill.
            ring=self.rect.inset((1,1,1,1)) if self.selected else self.rect
            renderer.rect(ring,(0,0,0,0),radius=3,border=theme.selected_text if self.selected else theme.accent)


class Segmented(Row):
    """Segmented choice on a recessed track; the selected option sits on a sliding Sparkle Blue pill."""
    def __init__(self,key,segments):
        super().__init__(key,segments,style=Style(height=32,gap=0,padding=(2,2,2,2)))
        self.selected=next((i for i,segment in enumerate(segments) if segment.selected),-1)

    def draw(self,renderer,state,theme):
        renderer.rect(self.rect,theme.canvas,radius=4)
        if 0<=self.selected<len(self.children):
            position=tween(state,self.key+'.indicator',float(self.selected),.22)
            low=max(0,min(len(self.children)-1,int(position)))
            high=min(len(self.children)-1,low+1)
            fraction=position-low
            a,b=self.children[low].rect,self.children[high].rect
            x=a.x+(b.x-a.x)*fraction;width=a.width+(b.width-a.width)*fraction
            renderer.rect(Rect(x,a.y,width,a.height),theme.accent,radius=3)
        for child in self.children:
            child.draw(renderer,state,theme)


def srgb(value):
    value=max(0.,min(1.,value))
    return value*12.92 if value<=.0031308 else 1.055*value**(1/2.4)-.055


class SwatchButton(Button):
    def __init__(self,key,text,action,settings,field):
        super().__init__(key,text,action,style=Style(height=32,align='LEFT',padding=(12,0,36,0)),tooltip='Choose '+text.lower())
        self.settings,self.field=settings,field

    def draw(self,renderer,state,theme):
        super().draw(renderer,state,theme)
        value=tuple(getattr(self.settings,self.field,(1,1,1)))[:3]
        swatch=Rect(self.rect.right-28,self.rect.y+(self.rect.height-16)/2,16,16)
        renderer.rect(swatch,tuple(srgb(c) for c in value)+(1.,),radius=4,border=theme.border)


_PRESET_PICTURES = {}


def _preset_picture(preset):
    """Bundled preview picture, resolved once: path lookups cost on every frame."""
    if preset not in _PRESET_PICTURES:
        path = Path(__file__).resolve().parents[1]/'assets/sky'/f'{preset.lower()}.png'
        _PRESET_PICTURES[preset] = str(path) if path.exists() else ''
    return _PRESET_PICTURES[preset]


class PresetCard(Button):
    def __init__(self, preset, settings, state=None):
        self.preset, self.settings = preset, settings
        label = PRESETS[preset]['label']
        super().__init__('sky.preset.'+preset,label,
                         lambda ctx:bpy.ops.mcbedrock.sky(preset=preset),
                         style=Style(height=116,flex=1),
                         tooltip=look_tooltip(label,look_state(settings,state or {},preset)))

    def draw(self, renderer, state, theme):
        status = look_state(self.settings,state,self.preset)
        selected = bool(status)
        hover = hover_mix(state,self.key,.16)
        pick = tween(state,self.key+'.selected',float(selected),.2)
        r = self.rect
        edge = blend(blend(theme.border,theme.text_muted,.35*hover),theme.accent,pick)
        renderer.rect(r,blend(theme.surface,theme.surface_hover,hover),radius=4,border=edge,border_width=1+pick*.5)
        image = Rect(r.x+5,r.y+29,r.width-10,r.height-34)
        path = _preset_picture(self.preset)
        if path:
            renderer.image(image,path,radius=3,cover=True)
        if pick>.01:
            look_badge(renderer,Rect(image.right-20,image.top-20,16,16),theme,status,pick)
        renderer.text(Rect(r.x+4,r.y+3,r.width-8,24),self.text,blend(theme.text_muted,theme.text,max(pick,hover)),
                      size=11,weight='SemiBold' if selected else 'Regular',align='CENTER',cap_center=True)
        focus_ring(renderer,r,state,self.key,theme)


def time_label(value):
    hour = value%24
    label = 'Night' if hour<5 or hour>=20 else 'Sunrise' if hour<8 else 'Morning' if hour<11 else 'Noon' if hour<14 else 'Afternoon' if hour<16 else 'Golden Hour' if hour<17.75 else 'Sunset'
    return f'{int(hour):02d}:{int((hour%1)*60):02d} · {label}'


# Sliders whose 0..1 range reads best as a percentage.
PERCENT={'clouds','look_strength','warmth','glow','glow_spread','glow_softness','bloom','bloom_size','fog','star_amount',
         'emissive_glow','light_glow_size','light_glow_wide','light_glow_warmth','sky_glow','sun_rays','sun_rays_length'}
GLOW_SLIDERS=('emissive_glow','light_glow_size','light_glow_wide','light_glow_warmth','sky_glow','sun_rays','sun_rays_length','light_block_brightness')
# Their soft range fills the track; typed values beyond it stay reachable.
SOFT_RANGE={'fog_light_strength','cloud_height','sun_world_haze_height','sun_world_haze_height_falloff','fog_start','fog_distance',
            'emissive_glow','light_block_brightness'}


class SkySlider(Button):
    def __init__(self, settings, name, label=None):
        self.settings, self.name = settings, name
        self.meta = settings.bl_rna.properties[name]
        self.pending_key = 'sky.pending.' + name
        self.low,self.high = (self.meta.soft_min,self.meta.soft_max) if name in SOFT_RANGE else (self.meta.hard_min,self.meta.hard_max)
        if name in {'fog_start','fog_distance','emissive_glow','light_block_brightness'}:
            self.high=max(self.high,getattr(settings,name))
        self.label = label or {
            'brightness':'Scene lighting', 'clouds':'Cloud amount',
            'warmth':'Sun warmth', 'glow':'Sun halo', 'glow_spread':'Halo size',
            'horizon_height':'Haze spread',
            'glow_softness':'Halo softness', 'bloom':'Bloom', 'bloom_size':'Bloom size',
        }.get(name,self.meta.name)
        super().__init__('sky.slider.'+name,'',lambda ctx:None,style=Style(height=48))

    def fraction_of(self, value):
        """Where value sits along the track, 0 to 1."""
        return max(0,min(1,(value-self.low)/max(self.high-self.low,1e-9)))

    def value_at(self, fraction):
        return self.low+fraction*(self.high-self.low)

    def draw(self, renderer, state, theme):
        r = self.rect
        value = state.get(self.pending_key,getattr(self.settings,self.name))
        fraction = self.fraction_of(value)
        text = time_label(value) if self.name=='time' else f'{value:.1f}°' if self.name in {'azimuth','cloud_direction','star_rotation'} else f'{value:.1f}' if self.name not in PERCENT else f'{value*100:.0f}%'
        if self.name in {'sun_haze_radius','horizon_position'}:
            text = f'{value:.1f}°'
        if self.name in {'exposure','contrast','saturation','temperature','tint','gradient_start','gradient_end','gradient_power'}:
            text = f'{value:.2f}'
        if self.name in {'fog_start','fog_distance'}:
            text = f'{value:.0f} m'
        if self.name=='light_block_brightness':
            text = f'{value:.1f}×'
        if self.name=='dungeons_cloud_scale':
            text = f'{value:.2f}×'
        dragging = (state.get('_sky_drag') or (None,))[0]==self.key
        hovered = state.get('_runtime_hover')==self.key
        keyboard = state.get('_runtime_focus')==self.key and state.get('_focus_visible')
        active = tween(state,self.key+'.active',1. if dragging else 0.,.14)
        pill_width = min(r.width*.6,text_width(renderer,text,10)+14)
        pill = Rect(r.right-pill_width,r.y+24,pill_width,19)
        renderer.rect(pill,blend(theme.surface_raised,theme.selected,active),radius=4)
        renderer.text(pill,text,theme.text,size=10,align='CENTER',cap_center=True)
        renderer.text(Rect(r.x,r.y+21,max(0,pill.x-r.x-6),25),self.label,blend(theme.text_muted,theme.text,active*.7),size=11,cap_center=True)
        track = Rect(r.x,r.y+10,r.width,4)
        x, y = r.x+5+(r.width-10)*fraction, track.y+2
        renderer.rect(track,blend(theme.surface_raised,theme.surface_hover,active),radius=2)
        renderer.rect(Rect(track.x,track.y,x-track.x,4),theme.accent,radius=2)
        if active>.6:
            halo = 22*(active-.6)/.4
            renderer.rect(Rect(x-halo/2,y-halo/2,halo,halo),theme.accent[:3]+(.25,),radius=halo/2)
        knob = 10+4*active
        renderer.rect(Rect(x-knob/2,y-knob/2,knob,knob),theme.text,radius=knob/2)
        if keyboard:
            ring = knob+6
            renderer.rect(Rect(x-ring/2,y-ring/2,ring,ring),(0,0,0,0),radius=ring/2,border=theme.text,border_width=1.5)

    def set_from_pointer(self,x,state):
        fraction = min(1,max(0,(x-self.rect.x-5)/max(1,self.rect.width-10)))
        value = round(self.value_at(fraction),2)
        state[self.pending_key]=value
        self.preview(state)

    def preview(self, state):
        # Keep the thumb responsive without queuing a shader/lighting rebuild
        # for every pointer event. Release always commits the latest value.
        # Generated coverage/size rebuild geometry on release. Dungeons opacity
        # only changes its material, so it can preview while dragging.
        if self.name=='clouds' and self.settings.cloud_preset!='DUNGEONS':return
        if self.name=='dungeons_cloud_scale':return
        value=state.get(self.pending_key,getattr(self.settings,self.name))
        if abs(getattr(self.settings,self.name)-value)<=1e-6:return
        from ..builders import preview_quality
        preview_quality.interaction(self.settings.id_data)
        now=monotonic()
        interval=1/30
        if now-state.get('_sky_preview_time',0)>=interval:
            setattr(self.settings,self.name,value)
            state['_sky_preview_time']=now

    # The drag handler below works for any slider built on this class; the
    # Outlines page overrides these to write its own settings.
    def cancel(self, value):
        """Esc or right-click mid-drag: back to the value the drag started from."""
        setattr(self.settings,self.name,value)

    def snapshot(self):
        """Anything besides the value that a click can change, for revert()."""
        return None

    def revert(self, value, snapshot):
        """Undo a click's jump (live, without an undo step): a double-click's
        first click moved the value to the pointer before the dialog opened."""
        self.cancel(value)

    def restore(self, context, value):
        from ..builders.sky import BUSY
        BUSY.add(context.scene.as_pointer())
        try:
            setattr(self.settings,self.name,value)
        finally:
            BUSY.discard(context.scene.as_pointer())

    def commit(self, value):
        bpy.ops.mcbedrock.sky_value(name=self.name,value=value)

    def edit(self):
        bpy.ops.mcbedrock.sky_value_edit('INVOKE_DEFAULT',name=self.name,label=self.label)


class PhaseCard(Button):
    def __init__(self, phase, settings):
        self.phase, self.settings = phase, settings
        label=next(label for key,label,_ in PHASES if key==phase)
        super().__init__('sky.phase.'+phase,label,lambda ctx:self.select(),style=Style(height=62,flex=1))

    def select(self):
        bpy.ops.mcbedrock.sky_value(name='phase',text=self.phase)

    def draw(self,renderer,state,theme):
        r=self.rect
        renderer.rect(r,theme.surface_raised,radius=4,border=theme.accent if self.settings.phase==self.phase else None)
        size=22
        x,y=r.x+(r.width-size)/2,r.y+29
        renderer.rect(Rect(x,y,size,size),(.08,.11,.18,1))
        index=next(index for key,_,index in PHASES if key==self.phase)
        world=getattr(self.settings,'world',None)
        node=world.node_tree.nodes.get('Moon artwork') if world and world.node_tree else None
        if node is not None and node.image is not None:
            col,row=index%4,1-index//4
            renderer.image(Rect(x,y,size,size),node.image,nearest=True,uv_rect=((col+.375)/4,(row+.375)/2,(col+.625)/4,(row+.625)/2))
        renderer.text(Rect(r.x,r.y,r.width,24),self.text,theme.text,size=10,align='CENTER',cap_center=True)
        focus_ring(renderer,r,state,self.key,theme)


def handle_sky_event(context,event,state,x,y,tree):
    drag=state.get('_sky_drag')
    if drag:
        widget=next((w for w in tree.walk() if w.key==drag[0]),None)
        if widget is None:
            state.pop('_sky_drag',None)
            return False
        if event.type=='MOUSEMOVE':
            widget.set_from_pointer(x,state)
        elif event.type=='TIMER':
            # Flush the last coalesced value even if the pointer stops moving.
            widget.preview(state)
        elif event.type in {'ESC','RIGHTMOUSE'}:
            widget.cancel(drag[1])
            state.pop(widget.pending_key,None)
            state.pop('_sky_drag',None)
        # Leaving Blender mid-drag (Alt+Tab) never delivers the release: keep the value.
        elif (event.type=='LEFTMOUSE' and event.value=='RELEASE') or event.type=='WINDOW_DEACTIVATE':
            value=state.pop(widget.pending_key,getattr(widget.settings,widget.name))
            # Restore the initial RNA value before the undoable commit.
            widget.restore(context,drag[1])
            widget.commit(value)
            state.pop('_sky_drag',None)
        return True
    if state.get('_menu'):
        return False
    if event.type in {'LEFT_ARROW','RIGHT_ARROW','HOME','END'} and event.value=='PRESS':
        focused=next((w for w in tree.walk() if w.key==state.get('_runtime_focus')),None)
        if isinstance(focused,SkySlider):
            low,high=focused.low,focused.high
            step=.01 if event.type=='RIGHT_ARROW' else -.01
            value=focused.value_at(max(0,min(1,focused.fraction_of(getattr(focused.settings,focused.name))+step)))
            if event.type in {'HOME','END'}:
                value=low if event.type=='HOME' else high
            focused.commit(max(low,min(high,value)))
            return True
    if event.type=='LEFTMOUSE' and event.value=='DOUBLE_CLICK':
        hit=tree.hit_test(x,y)
        if isinstance(hit,SkySlider):
            state.pop('_sky_drag',None);state.pop(hit.pending_key,None)
            press=state.pop('_sky_press',None)
            # The double-click's first click already moved the value to the
            # pointer: put back what was there before typing an exact value.
            if press and press[0]==hit.key:
                hit.revert(press[1],press[2])
            hit.edit()
            return True
    if event.type=='LEFTMOUSE' and event.value=='PRESS':
        hit=tree.hit_test(x,y)
        if isinstance(hit,SkySlider):
            state['_runtime_focus']=hit.key
            original=getattr(hit.settings,hit.name)
            state['_sky_drag']=(hit.key,original)
            state['_sky_press']=(hit.key,original,hit.snapshot())
            state.pop('_sky_preview_time',None)
            hit.set_from_pointer(x,state)
            return True
    return False


class MCBEDROCK_OT_sky_value(SkyOperator, bpy.types.Operator):
    bl_idname='mcbedrock.sky_value'
    bl_label='Adjust sky'
    bl_options={'UNDO','INTERNAL'}
    name:bpy.props.StringProperty()
    value:bpy.props.FloatProperty()
    text:bpy.props.StringProperty()

    def execute(self,context):
        settings=context.scene.mcbedrock_sky
        if self.name=='loocs_enabled' and self.value and not native_color.available(context.scene):
            self.report({'ERROR'},'Set up LOOCS colors and restart Blender first')
            return {'CANCELLED'}
        allowed={'time','brightness','warmth','clouds','azimuth','sun_size','moon_size','phase','glow','softness','fill','cloud_height','cloud_thickness','moving','cloud_speed','cloud_direction'}
        allowed.update({'dungeons_cloud_scale','cloud_streak','cloud_streak_length'})
        allowed.update({'atmosphere_match_lighting','volume_height_fog','volume_anisotropy','edge_highlights'})
        allowed.update({'glow_spread','glow_softness','bloom','bloom_size','fog','fog_distance'})
        allowed.update({'sun_haze','sun_haze_radius','horizon_haze','horizon_position'})
        allowed.update({'exposure','contrast','saturation','temperature','tint','shadow_lift','midtone_light','sky_brightness','horizon_height','sky_color_mix'})
        allowed.update({'sunlight','moonlight','ambient_light','contact_shadows','key_height_offset','daylight_balance','light_direction_mode'})
        allowed.update({'fog_start','terrain_fog','sun_haze_mode','flat_fog','flat_fog_start','flat_fog_distance'})
        allowed.update({'sun_light_warmth','sun_heading_offset','sun_world_haze','sun_world_haze_start','sun_world_haze_distance','sun_world_haze_spread','sun_world_haze_radiance','sun_world_haze_height','sun_world_haze_height_falloff'})
        allowed.update({'stars','star_brightness','star_amount','star_rotation'})
        allowed.update({'gradient_mapping','gradient_start','gradient_end','gradient_power'})
        allowed.update({'loocs_enabled','texture_compensation','emissive_glow','blue_noise_opacity'})
        allowed.update({f'grade_{i}_{key}' for i in range(5) for key in ('exposure','contrast','saturation','temperature','tint')})
        allowed.add('cycle_seconds')
        allowed.update({'fog_light','fog_light_strength','film_look'})
        allowed.add('look_strength')
        allowed.add('grass_vibrance')
        allowed.update({'preview_quality','atmosphere_height_enabled','atmosphere_auto_color'})
        allowed.update({'texture_vibrance','texture_brightness','foliage_color','surface_sheen','surface_matte','leaf_scattering','vegetation_translucency','penumbra'})
        allowed.update({'light_blocks','block_lights','plain_background',*GLOW_SLIDERS})
        if self.name not in allowed or not settings.active:
            return {'CANCELLED'}
        if self.name=='sun_haze_mode' and self.text not in {'MIST','SUN'}:
            return {'CANCELLED'}
        if self.name=='preview_quality' and self.text not in {'AUTO','FULL','FAST'}:
            return {'CANCELLED'}
        if self.name=='light_direction_mode' and self.text not in {'TRAILER','SUN'}:
            return {'CANCELLED'}
        # On/off settings by their type: a toggle passes 0 or 1 as a float,
        # which an on/off property refuses.
        boolean=settings.bl_rna.properties[self.name].type=='BOOLEAN'
        setattr(settings,self.name,self.text if self.name in {'phase','sun_haze_mode','preview_quality','light_direction_mode'} else bool(self.value) if boolean else self.value)
        return {'FINISHED'}


class MCBEDROCK_OT_sky_value_edit(SkyOperator, bpy.types.Operator):
    bl_idname='mcbedrock.sky_value_edit'
    bl_label='Set value'
    bl_description='Type an exact value for this setting'
    bl_options={'INTERNAL'}
    name:bpy.props.StringProperty(options={'HIDDEN'})
    label:bpy.props.StringProperty(options={'HIDDEN'})
    value:bpy.props.FloatProperty(name='Value')

    def invoke(self,context,event):
        settings=context.scene.mcbedrock_sky
        if not hasattr(settings,self.name):
            return {'CANCELLED'}
        self.value=getattr(settings,self.name)
        return context.window_manager.invoke_props_dialog(self,width=240,title=self.label or 'Set value')

    def draw(self,context):
        self.layout.prop(self,'value',text=self.label or 'Value')

    def execute(self,context):
        return bpy.ops.mcbedrock.sky_value(name=self.name,value=self.value)


COLOR_FIELDS={'fog_light_color','flat_fog_color','horizon_color','zenith_color','sun_color','moon_color','cloud_color','fog_color','surface_tint','shadow_color','sun_light_color','sun_world_haze_color','plain_background_color'}


class MCBEDROCK_OT_sky_color(SkyOperator, bpy.types.Operator):
    bl_idname='mcbedrock.sky_color'
    bl_label='Choose color'
    bl_options={'UNDO'}
    field:bpy.props.StringProperty()
    color:bpy.props.FloatVectorProperty(subtype='COLOR',size=3,min=0,max=1)

    def invoke(self,context,event):
        if self.field not in COLOR_FIELDS:
            return {'CANCELLED'}
        self.color=getattr(context.scene.mcbedrock_sky,self.field)
        return context.window_manager.invoke_props_dialog(self,width=280)

    def draw(self,context):
        self.layout.template_color_picker(self,'color',value_slider=True)
        self.layout.prop(self,'color',text='')

    def execute(self,context):
        if self.field not in COLOR_FIELDS:
            return {'CANCELLED'}
        s=context.scene.mcbedrock_sky
        setattr(s,self.field,self.color)
        if self.field in {'horizon_color','zenith_color'}:
            s.sky_color_mix=1
        return {'FINISHED'}


class MCBEDROCK_OT_sky_pack(SkyOperator, bpy.types.Operator,ImportHelper):
    bl_idname='mcbedrock.sky_pack'
    bl_label='Choose optional sky resource pack'
    bl_options={'UNDO'}
    filter_glob:bpy.props.StringProperty(default='*.zip;*.mcpack;pack.mcmeta;manifest.json',options={'HIDDEN'})

    def execute(self,context):
        try:
            source=resolve(self.filepath)
            s=context.scene.mcbedrock_sky
            images=load_images(source,s.owner) if source and s.active else None
        except Exception as exc:
            self.report({'ERROR'},'Could not read this sky pack: '+str(exc))
            return {'CANCELLED'}
        if source is None:
            self.report({'ERROR'},'This pack needs sun.png and moon_phases.png')
            return {'CANCELLED'}
        if s.active:
            tree=s.world.node_tree
            for name,image in zip(('Sun','Moon'),images):
                old=tree.nodes[name+' artwork'].image
                tree.nodes[name+' artwork'].image=image
                if old and not old.users:
                    bpy.data.images.remove(old)
        s.source_pack=source[0]
        return {'FINISHED'}


def build_sky(context,state):
    if not supports_sky():
        return Column('sky.page',[
            Label('sky.title','Sky & Lighting',style=Style(height=28,font_size=17,font_weight='SemiBold')),
            hint('sky.compatibility','Requires Blender 5.2 or newer.'),
            hint('sky.compatibility.detail','Uses newer lighting and compositor APIs.'),
            *[Button('sky.preset.'+preset,data['label'],lambda ctx:None,
                     style=Style(height=36,enabled=False),tooltip=SKY_REQUIREMENT)
              for preset,data in PRESETS.items() if preset in TIME_PRESETS],
            hint('sky.compatibility.imports','Import tools remain available.'),
        ],style=Style(gap=8,padding=(0,0,12,0)))
    settings=context.scene.mcbedrock_sky
    flat=getattr(context.scene,'mcbedrock_flat',None)
    if flat is not None and flat.active:
        # Flat lighting replaces the sky; its own controls take the page.
        from .flat_page import build_flat
        return Column('sky.page',build_flat(context,state),style=Style(gap=8,padding=(0,0,12,0)))
    def caption(key,text):
        return Label('sky.caption.'+key,text,muted=True,style=Style(height=20,font_size=10.5,font_weight='SemiBold'))

    children=[]
    if settings.active:
        children=[caption('presets','Looks'),
                  Row('sky.presets.a',[PresetCard('DAY',settings,state),PresetCard('GOLDEN',settings,state)],style=Style(gap=8)),
                  Row('sky.presets.b',[PresetCard('SUNSET',settings,state),PresetCard('NIGHT',settings,state)],style=Style(gap=8))]
    else:
        # Until a sky exists the page offers one thing: create it (as Daylight).
        children.append(button('sky.create','Create sky','mcbedrock.sky',primary=True,action='APPLY',preset='DAY',invoke_default=False))
        children.append(button('sky.flat','Flat lighting','mcbedrock.flat_lighting',enable=True,invoke_default=False))
        children.append(hint('sky.flat.help','No sky: flat, fast key-art lighting on a plain background.'))
    if not native_color.available(context.scene):
        if settings.native_color_pending:
            children.append(hint('sky.profile.restart','Save your project and restart Blender.'))
        else:
            children.append(button('sky.profile.setup','Set up LOOCS colors','mcbedrock.sky_color_profile'))
            children.append(hint('sky.profile.help','One-time setup. Requires a restart.'))
    if not settings.active:
        return Column('sky.page',children,style=Style(gap=8,padding=(0,0,12,0)))
    from ..builders.sky import world_status
    world=world_status(context.scene)
    if world=='MISSING':
        # Without its world the sky cannot update: offer to rebuild it instead
        # of a page of controls that change nothing.
        return Column('sky.page',[
            hint('sky.world.missing',"This sky's world was deleted, so its controls have nothing to change."),
            button('sky.world.repair','Rebuild sky world','mcbedrock.sky',primary=True,action='REPAIR_WORLD',invoke_default=False),
            QuietButton('sky.restore','Remove sky',invoke('mcbedrock.sky',invoke=False,action='RESTORE'),
                        style=Style(height=32),tooltip='Remove the sky and bring back the lighting you had before')],
            style=Style(gap=8,padding=(0,0,12,0)))
    if world=='REPLACED':
        children[0:0]=[hint('sky.world.replaced','The scene shows another world, so the Sparkle sky is hidden.'),
                       button('sky.world.use','Show Sparkle sky','mcbedrock.sky',action='REPAIR_WORLD',invoke_default=False)]

    updated=settings.atmosphere_version>=1

    def action(key,label,operation):
        return button('sky.'+key,label,'mcbedrock.sky',action=operation,invoke_default=False)

    def sliders(names):
        return [SkySlider(settings,name) for name in names]

    def color(key,label,field):
        return SwatchButton('sky.'+key,label,invoke('mcbedrock.sky_color',field=field),settings,field)

    def toggle(name,label,key=None):
        return Toggle('sky.'+(key or name),label,lambda ctx:getattr(settings,name),
            lambda ctx,value:bpy.ops.mcbedrock.sky_value(name=name,value=float(value)),style=Style(height=32))

    def choices(key,name,options,value,tooltip=''):
        return Segmented('sky.'+key,[
            Segment('sky.'+key+'.'+str(option).lower(),label,
                    lambda ctx,option=option:bpy.ops.mcbedrock.sky_value(
                        name=name,**({'text':option} if isinstance(option,str) else {'value':float(option)})),
                    value==option,tooltip)
            for option,label in options])

    def group(key,title,everyday,detail_builder,details=True):
        heading=SkySectionHeader(key,title,state)
        controls=[heading]
        if not heading.expanded:
            state.pop('sky.section.'+key+'.opened',None)
            children.append(SkyGroup('sky.section.'+key,controls,False,style=Style(padding=(0,6,0,6))))
            return
        controls.extend(everyday)
        if not details:
            children.append(SkyGroup('sky.section.'+key,controls,True,style=Style(padding=(0,6,0,6),gap=8)))
            return
        expanded=state.get('sky.details.'+key,False)
        def expand(ctx):
            state['sky.details.'+key]=not state.get('sky.details.'+key,False)
        controls.append(DetailsToggle('sky.details.'+key,expanded,expand))
        if expanded:
            controls.extend(detail_builder())
            controls.append(action('reset.'+key,'Reset '+title.lower(),'RESET_'+key.upper()))
        children.append(SkyGroup('sky.section.'+key,controls,True,style=Style(padding=(0,6,0,6),gap=8)))

    if context.scene.render.engine=='CYCLES' and settings.terrain_fog:
        children.append(hint('sky.mist.cycles','Cycles fog: final renders only.'))
        children.append(action('mist.preview','Use EEVEE for live atmosphere','PREVIEW_EEVEE'))
    elif settings.preview_previous_engine:
        children.append(action('mist.return','Return to '+settings.preview_previous_engine.title(),'PREVIEW_RETURN'))
    if not updated:
        children.append(hint('sky.legacy','Saved look preserved.'))
        children.append(action('update_look','Apply updated look','UPDATE_LOOK'))

    def lighting_details():
        controls=sliders(('brightness','look_strength','warmth','sunlight' if moon_selected else 'moonlight','ambient_light','softness','penumbra',
                          'daylight_balance','sun_light_warmth','key_height_offset','sun_heading_offset'))
        if context.scene.render.engine=='BLENDER_EEVEE' and settings.trailer_lighting:
            controls.append(SkySlider(settings,'contact_shadows'))
        if context.scene.render.engine=='BLENDER_EEVEE':
            controls += [hint('sky.bounce.title','Local bounce lighting'),
                         button('sky.bounce.add','Add volume around selection','mcbedrock.bounce_lighting',action='ADD'),
                         actions('sky.bounce.actions',[button('sky.bounce.bake','Bake bounce lighting','mcbedrock.bounce_lighting',action='BAKE'),
                                                      button('sky.bounce.remove','Remove volume','mcbedrock.bounce_lighting',action='REMOVE')]),
                         hint('sky.bounce.help','Rebake after changing lights or geometry.')]
        controls.append(hint('sky.materials.title','Scene materials'))
        controls += [SkySlider(settings,'texture_compensation'),
                     toggle('blue_noise_opacity','Blue-noise opacity (experimental)','blue_noise'),
                     action('surfaces','Refresh scene materials','SURFACES')]
        if settings.finish_active:
            controls += sliders(('surface_matte','surface_sheen','grass_vibrance','leaf_scattering',
                                 'vegetation_translucency','texture_vibrance','texture_brightness','foliage_color'))
            controls += [color('surface.tint','Surface tint','surface_tint'),
                         action('surfaces.restore','Restore original materials','RESTORE_SURFACES')]
        if settings.finish_status:
            controls.append(hint('sky.surfaces.status',settings.finish_status))
        if settings.finish_skipped:
            controls.append(hint('sky.surfaces.skipped',f'{settings.finish_skipped} slots unchanged'))
        return controls

    from ..builders.sun_placement import selected_body
    moon_selected = selected_body(context) == 'Moon'
    lighting=[SkySlider(settings,'time','Starting time' if settings.cycle_enabled else 'Time of day'),
              choices('light.direction','light_direction_mode',(('TRAILER','Trailer lighting'),('SUN','Follow sun')),settings.light_direction_mode),
              *([action('key.aim','Aim sun from this view','AIM_KEY'),
                 hint('key.aim.help','Or rotate MC Sun to move the shadows.')]
                if settings.light_direction_mode=='TRAILER' else []),
              SkySlider(settings,'azimuth','Sky direction'),
              SkySlider(settings,'moonlight' if moon_selected else 'sunlight','Moon strength' if moon_selected else 'Sun strength'),
              SkySlider(settings,'fill','Shadow fill'),
              SkySlider(settings,'character_fill','Character fill'),
              action('cycle','Stop day/night' if settings.cycle_enabled else 'Animate day/night','CYCLE')]
    if settings.cycle_enabled:
        lighting += [hint('sky.clock',time_label(current_time(context.scene))),
                     SkySlider(settings,'cycle_seconds'),hint('sky.cycle.help','Play or scrub the timeline to preview.')]
    group('lighting','Lighting',lighting,lighting_details)


    def glow_details():
        controls=sliders(('light_glow_wide','light_glow_warmth'))
        controls.append(toggle('light_blocks','Light up torches and lanterns'))
        if settings.light_blocks:
            controls += [SkySlider(settings,'light_block_brightness'),toggle('block_lights','Torches light their surroundings')]
        # The viewport preview runs without the compositor, which draws the glow.
        controls.append(hint('sky.glow.help','The glow shows in renders.'))
        return controls

    group('glow','Glowing blocks',sliders(('emissive_glow','light_glow_size','sky_glow')),glow_details)

    def atmosphere_details():
        if not settings.terrain_fog:
            return [color('fog.color','Volume color','fog_color'),
                    SkySlider(settings,'volume_anisotropy'),
                    toggle('volume_height_fog','Height fog'),
                    * (sliders(('sun_world_haze_height','sun_world_haze_height_falloff')) if settings.volume_height_fog else []),
                    hint('sky.volume.help','Face the sun behind trees to see light shafts.'),
                    Label('sky.atmosphere.mode.label','Fog method',muted=True,style=FIELD_LABEL),
                    choices('atmosphere.mode','terrain_fog',((True,'Lightweight'),(False,'Volumetric')),False),
                    hint('sky.atmosphere.method.help','Shadowed volume. Costs more to preview.')]
        controls=[]
        if updated:
            controls += [toggle('atmosphere_auto_color','Automatic fog colors'),
                         * ([toggle('atmosphere_match_lighting','Match sky and sun colors')] if settings.atmosphere_auto_color else []),
                         toggle('atmosphere_height_enabled','Height fog')]
            if settings.atmosphere_height_enabled:
                controls += [SkySlider(settings,'sun_world_haze_height','Ground height'),
                             SkySlider(settings,'sun_world_haze_height_falloff','Height falloff')]
            controls += [SkySlider(settings,'sun_world_haze','Sun scattering')]
            if settings.sun_world_haze>0:
                controls += sliders(('sun_world_haze_spread','sun_world_haze_radiance'))
                if not (settings.atmosphere_auto_color and settings.atmosphere_match_lighting):
                    controls.append(color('sun.haze_tint','Sun scattering tint','sun_world_haze_color'))
            if not settings.atmosphere_auto_color:
                controls.append(color('fog.color','Fog color','fog_color'))
        else:
            controls.append(color('fog.color','Fog color','fog_color'))
            controls.append(hint('sky.legacy.haze','Legacy sun haze'))
            controls.append(choices('haze','sun_haze_mode',(('MIST','Native Mist'),('SUN','Sunlit haze')),settings.sun_haze_mode))
            controls += sliders(('sun_world_haze','sun_world_haze_start','sun_world_haze_distance'))
            if settings.sun_haze_mode=='SUN':
                controls += sliders(('sun_world_haze_height','sun_world_haze_height_falloff','sun_world_haze_spread'))
            controls += [SkySlider(settings,'sun_world_haze_radiance'),
                         color('sun.haze_tint','Sunlit haze tint','sun_world_haze_color')]
        controls.append(Label('sky.atmosphere.mode.label','Fog method',muted=True,style=FIELD_LABEL))
        controls.append(choices('atmosphere.mode','terrain_fog',((True,'Lightweight'),(False,'Volumetric')),settings.terrain_fog))
        controls.append(hint('sky.atmosphere.method.help','Volumetric fog costs more to preview.' if not settings.terrain_fog
                              else 'Fast distance and height fog.'))
        return controls

    # Flat fog: Element-style post fog in the horizon color, off on a new sky.
    atmosphere_controls=[SkySlider(settings,'flat_fog','Flat fog')]
    if settings.flat_fog>0:
        atmosphere_controls += [SkySlider(settings,'flat_fog_start','Flat fog starts'),
                                SkySlider(settings,'flat_fog_distance','Flat fog distance'),
                                color('flat.fog.tint','Flat fog tint','flat_fog_color'),
                                hint('sky.flat.fog.help','Shows in renders.')]
    atmosphere_controls.append(SkySlider(settings,'fog','Atmosphere amount'))
    if settings.terrain_fog:
        atmosphere_controls += [SkySlider(settings,'fog_start','Fog starts'),
                                SkySlider(settings,'fog_distance','Fade distance')]
    else:
        atmosphere_controls.append(SkySlider(settings,'fog_distance','Distance'))
    if settings.terrain_fog and not settings.auto_materials:
        atmosphere_controls += [hint('sky.atmosphere.materials','Refresh materials to enable surface fog.'),
                                action('atmosphere.refresh','Refresh scene materials','SURFACES')]
    elif settings.terrain_fog and settings.finish_skipped:
        atmosphere_controls.append(hint('sky.atmosphere.skipped',f'{settings.finish_skipped} shared or unsupported slots unchanged.'))
    # God rays: optional, off on a new sky (Mojang rarely uses them).
    atmosphere_controls.append(Toggle('sky.godrays','God rays',lambda ctx:settings.sun_rays>0,
        lambda ctx,value:bpy.ops.mcbedrock.sky_value(name='sun_rays',value=.5 if value else 0.),style=Style(height=32)))
    if settings.sun_rays>0:
        atmosphere_controls += [SkySlider(settings,'sun_rays','God ray strength'),SkySlider(settings,'sun_rays_length','Ray length'),
                                hint('sky.godrays.help','Shafts from the sun through trees. Shows in renders.')]
    atmosphere_controls.append(toggle('fog_light','Fog light'))
    if settings.fog_light:
        atmosphere_controls += [SkySlider(settings,'fog_light_strength','Fog light strength'),
                                color('fog.light.color','Fog light color','fog_light_color'),
                                hint('sky.fog.light.help','Lights the volume fog only.')]
    group('atmosphere','Atmosphere',atmosphere_controls,atmosphere_details)
    from .masks_page import fog_planes_section
    children.append(fog_planes_section(context,state))

    def sky_details():
        controls=[]
        if settings.cloud_preset=='DUNGEONS':
            controls += [hint('sky.clouds.title','Cloud color and motion'),
                         color('cloud.color','Cloud color','cloud_color'),toggle('moving','Moving clouds')]
            if settings.moving:
                controls += sliders(('cloud_speed','cloud_direction'))
                controls.append(hint('sky.play','Play the timeline to see clouds move.'))
            controls.append(toggle('cloud_streak','Motion streaks'))
            if settings.cloud_streak:
                controls += [SkySlider(settings,'cloud_streak_length'),
                             *([] if settings.moving else [SkySlider(settings,'cloud_direction')])]
        controls += sliders(('sky_brightness','horizon_haze','horizon_position','horizon_height','sky_color_mix'))
        controls += [actions('sky.palette',[action('palette.meadow','Meadow','MEADOW'),action('palette.adventure','Adventure','ADVENTURE')]),
                     actions('sky.colors',[color('horizon.color','Horizon color','horizon_color'),
                                            color('zenith.color','Upper sky color','zenith_color')]),
                     toggle('gradient_mapping','Shape sky gradient')]
        if settings.gradient_mapping:
            controls += sliders(('gradient_start','gradient_end','gradient_power'))
            controls.append(hint('sky.gradient.help','0.50 = horizon · 1.00 = overhead'))
        controls += sliders(('sun_size','moon_size','glow'))
        if settings.glow>0:
            controls += sliders(('glow_spread','glow_softness'))
        controls += sliders(('sun_haze','sun_haze_radius'))
        controls += [actions('sky.sun.colors',[color('sun.color','Sun tint','sun_color'),color('moon.color','Moon tint','moon_color')]),
                     hint('sky.phase.label','Moon phase')]
        for row in range(2):
            controls.append(Row('sky.phases.'+str(row),[PhaseCard(key,settings) for key,_,_ in PHASES[row*4:row*4+4]],style=Style(gap=4)))
        controls.append(toggle('stars','Show stars'))
        if settings.stars:
            controls += sliders(('star_brightness','star_amount','star_rotation'))
            controls.append(hint('sky.stars.help','Fade in at night, fade out at dawn.'))
        if settings.cloud_preset != 'DUNGEONS':
            controls += [hint('sky.clouds.title','Cloud shape and motion')]
            controls += sliders(('cloud_height','cloud_thickness'))
            controls += [action('cloud.move','Move clouds','MOVE_CLOUDS')]
            controls += [color('cloud.color','Cloud color','cloud_color'),toggle('moving','Moving clouds')]
            if settings.moving:
                controls += sliders(('cloud_speed','cloud_direction'))
                controls.append(hint('sky.play','Play the timeline to see clouds move.'))
            controls.append(toggle('cloud_streak','Motion streaks'))
            if settings.cloud_streak:
                controls += [SkySlider(settings,'cloud_streak_length'),
                             *([] if settings.moving else [SkySlider(settings,'cloud_direction')])]
        return controls

    cloud_controls=[Label('sky.cloud.preset.label','Cloud preset',muted=True,style=FIELD_LABEL),
                    Dropdown('sky.cloud.preset',lambda ctx:settings.cloud_preset,
                             lambda ctx,value:bpy.ops.mcbedrock.sky(action='CLOUD_PRESET',preset=value),
                             [(key,data['label']) for key,data in CLOUD_PRESETS.items()],style=Style(height=32)),
                    SkySlider(settings,'clouds','Cloud opacity' if settings.cloud_preset=='DUNGEONS' else 'Cloud coverage')]
    if settings.cloud_preset == 'DUNGEONS':
        cloud_controls[-1:-1]=[
            Label('sky.cloud.density.label','Cloud density',muted=True,style=FIELD_LABEL),
            Dropdown('sky.cloud.density',lambda ctx:settings.dungeons_cloud_density,
                     lambda ctx,value:bpy.ops.mcbedrock.sky(action='CLOUD_DENSITY',preset=value),
                     [('SPARSE','Sparse'),('DENSE','Dense')],style=Style(height=32))]
        cloud_controls += [SkySlider(settings,'dungeons_cloud_scale','Cloud size'),
                           *sliders(('cloud_height','cloud_thickness')),
                           action('cloud.move','Move clouds','MOVE_CLOUDS')]
    elif settings.cloud_preset != 'CLASSIC':
        cloud_controls.append(hint('sky.cloud.preset.help','Square clouds. Shape controls are in Show details.'))
    cloud_controls.append(toggle('plain_background','Plain background'))
    if settings.plain_background:
        cloud_controls += [color('sky.plain.color','Background color','plain_background_color'),
                           hint('sky.plain.help','Lighting still follows the sky and time of day.')]
    group('sky','Sky & Clouds',cloud_controls,sky_details)

    def color_details():
        controls=sliders(('temperature','tint','shadow_lift','midtone_light','bloom','edge_highlights'))
        if settings.bloom>0:
            controls.append(SkySlider(settings,'bloom_size'))
        controls.append(hint('sky.effects.help','Glow and grading require compositing.'))
        for i in range(5):
            opened=state.get('sky.grade_group',-1)==i
            def choose_group(ctx,i=i):
                state['sky.grade_group']=-1 if state.get('sky.grade_group',-1)==i else i
            controls.append(DetailsToggle(f'sky.group.open.{i}',opened,choose_group,
                                          label='Background grade' if i==0 else f'Grade group {i}',
                                          tooltip='Grade the background' if i==0 else f'Grade the objects in group {i}'))
            if opened:
                if i:
                    controls.append(button(f'sky.group.{i}',f'Assign selected to group {i}','mcbedrock.lighting_group',group=i,invoke_default=False))
                controls.extend(SkySlider(settings,f'grade_{i}_{key}') for key in ('exposure','contrast','saturation','temperature','tint'))
        controls.append(button('sky.group.clear','Clear selected group','mcbedrock.lighting_group',group=0,invoke_default=False))
        return controls

    if native_color.available(context.scene):
        colors=[choices('transform','loocs_enabled',((True,native_color.RENDERING_LABELS[settings.loocs_rendering]),(False,'Blender')),native_color.active(context.scene),
                        tooltip='LOOCS color rendering; pick others on the Color page')]
    else:
        colors=[hint('sky.transform.current','Using Blender colors ('+context.scene.view_settings.view_transform+')')]
    colors += sliders(('exposure','contrast','saturation'))
    colors.append(toggle('film_look','Film look'))
    if settings.film_look:
        colors.append(hint('sky.film.help','Halation around lights, grain and gate weave. Shows in renders.'))
    group('color','Color',colors,color_details)
    scene=context.scene
    shared=scene_scan(scene,'shared_lights',lambda:any(o.type=='LIGHT' and not o.get('mc_sky_owner') and (o.library or sum(o.name in sc.objects for sc in bpy.data.scenes)>1) for o in scene.objects))
    if shared:
        children.append(hint('sky.shared','Shared or linked lights stay on.'))
    # Clicking the selected preset re-applies it, so there is no separate reset.
    children.append(QuietButton('sky.restore','Remove sky',
                                invoke('mcbedrock.sky',invoke=False,action='RESTORE'),
                                style=Style(height=32),tooltip='Remove the sky and bring back the lighting you had before'))
    return Column('sky.page',children,style=Style(gap=8,padding=(0,0,12,0)))

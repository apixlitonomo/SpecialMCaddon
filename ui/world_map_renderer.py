"""MiEx top-down colour and height shading, drawn in large GPU pages."""
import os
import time
import numpy as np
import gpu
from gpu_extras.batch import batch_for_shader
from .gpu_framework import Rect
from ..exporters.miex.map_atlas import PAGE_BLOCKS, PAGE_CHUNKS, SIZE

_shader = None
_quad = None
_dash = None
_dash_quad = None
_boxes = None
_icons = None
_textures = {}
MAX_TEXTURE_PAGES = 324  # 205 MiB; fits the worst-case bounded viewport without thrashing.
MAX_PREPARED_PAGES = 4
UNIT = [(0,0),(1,0),(1,1),(0,1)]
CORNERS = np.array([(0,0),(1,0),(1,1),(0,0),(1,1),(0,1)],dtype=np.float32)


def page_arrays(tiles, page):
    """One page and its one-chunk halo as page_pixels lays them out (colours 0..1, heights),
    built with numpy so a page costs about a millisecond instead of five."""
    colors=np.zeros((SIZE,SIZE,4),np.uint8);heights=np.zeros((SIZE,SIZE),np.float32)
    x0,z0=page[0]*PAGE_CHUNKS-1,page[1]*PAGE_CHUNKS-1
    for cz in range(PAGE_CHUNKS+2):
        for cx in range(PAGE_CHUNKS+2):
            tile=tiles.get((x0+cx,z0+cz))
            if not tile:continue
            pixels,heightmap=tile
            rows=slice(cz*16,cz*16+16);cols=slice(cx*16,cx*16+16)
            colors[rows,cols]=np.frombuffer(pixels,np.uint8).reshape(16,16,4)
            if heightmap is not None:heights[rows,cols]=np.asarray(heightmap,np.float32).reshape(16,16)
    normalized=colors.reshape(-1).astype(np.float32)
    normalized/=np.float32(255)
    return normalized,heights.reshape(-1)


def prepare_pages(state, budget=.004):
    """Prepare a bounded CPU queue from the timer, never from the draw callback."""
    rect=state.map_rect
    visible=state.view.page_keys((rect.x,rect.y,rect.width,rect.height)) & state.map_pages
    for key in list(state.prepared_pages):
        if key not in visible:state.prepared_pages.pop(key,None)
    needed=(state.dirty_pages | (visible-set(state.textures))) & visible
    needed-=set(state.prepared_pages)
    deadline=time.perf_counter()+budget
    changed=False
    for key in sorted(needed,key=lambda p:(p[0]*256+128-state.view.x)**2+(p[1]*256+128-state.view.z)**2):
        if len(state.prepared_pages)>=MAX_PREPARED_PAGES:break
        state.prepared_pages[key]=page_arrays(state.tiles,key)
        state.dirty_pages.discard(key)
        changed=True
        if time.perf_counter()>=deadline:break
    return changed


def shader():
    global _shader
    if _shader is not None:return _shader
    info=gpu.types.GPUShaderCreateInfo()
    # Vectors first keeps the push constants inside 128 bytes.
    info.push_constant('MAT4','ModelViewProjectionMatrix')
    info.push_constant('VEC4','selection')
    info.push_constant('VEC4','bounds')
    info.push_constant('VEC2','yRange')
    info.push_constant('VEC2','pageOrigin')
    info.push_constant('FLOAT','pixelStep')
    info.push_constant('FLOAT','cutStrength')
    info.vertex_in(0,'VEC2','pos')
    interface=gpu.types.GPUStageInterfaceInfo('mcaddon_map_uv')
    interface.smooth('VEC2','uv');info.vertex_out(interface)
    info.sampler(0,'FLOAT_2D','colors');info.sampler(1,'FLOAT_2D','heights')
    info.fragment_out(0,'VEC4','fragColor')
    # One unit quad serves every page; `bounds` places it.
    info.vertex_source('void main(){uv=vec2(pos.x,1.0-pos.y);gl_Position=ModelViewProjectionMatrix*vec4(bounds.xy+pos*bounds.zw,0,1);}')
    # Same directional height formula as MiEx Renderer2D (BSD-3-Clause,
    # Bram Stout Productions). Height neighbours are screen-pixel samples.
    info.fragment_source('''
        float heightAt(vec2 p, float center) {
            ivec2 at=clamp(ivec2(floor(p)),ivec2(0),textureSize(heights,0)-ivec2(1));
            return texelFetch(colors,at,0).a>0.0 ? texelFetch(heights,at,0).r : center;
        }
        void main(){
            vec2 p=vec2(16.0)+uv*256.0;
            ivec2 at=ivec2(floor(p));
            vec4 color=texelFetch(colors,at,0);
            float center=texelFetch(heights,at,0).r;
            float left=center-heightAt(p-vec2(pixelStep,0),center);
            float right=center-heightAt(p+vec2(pixelStep,0),center);
            float south=center-heightAt(p+vec2(0,pixelStep),center);
            float north=center-heightAt(p-vec2(0,pixelStep),center);
            float total=min(0.0,left+4.0*right+south+4.0*north);
            float shade=0.5+1.0/(1.0+exp(min(80.0,-0.25*total)));
            vec3 srgb=floor(color.rgb*255.0*shade+0.0001)/255.0;
            // Inside the selection, show what the height range removes: surfaces
            // above the top are trimmed (amber stripes), columns below the bottom
            // export nothing (darkened).
            vec2 world=pageOrigin+floor(p)-vec2(16.0);
            bool inside=world.x>=selection.x&&world.y>=selection.y&&world.x<selection.z&&world.y<selection.w;
            if(cutStrength>0.0&&inside&&color.a>0.0){
                if(center>yRange.y){
                    float stripe=step(0.5,fract((world.x+world.y)*0.25));
                    srgb=mix(srgb,vec3(0.96,0.65,0.14),cutStrength*(0.22+0.2*stripe));
                }else if(center<yRange.x){
                    srgb=mix(srgb,vec3(0.06),cutStrength*0.62);
                }
            }
            vec3 linear=mix(srgb/12.92,pow((srgb+0.055)/1.055,vec3(2.4)),step(vec3(0.04045),srgb));
            fragColor=vec4(linear,color.a);
        }
    ''')
    _shader=gpu.shader.create_from_info(info)
    return _shader


def draw_pages(renderer,state,rect,settings=None):
    global _quad
    program=shader()
    if _quad is None:_quad=batch_for_shader(program,'TRI_FAN',{'pos':UNIT})
    program.bind();program.uniform_float('ModelViewProjectionMatrix',renderer._matrix)
    program.uniform_float('pixelStep',1/(state.view.scale*renderer.ui_scale))
    if settings is not None:
        x0,x1=sorted((settings.min_x,settings.max_x));z0,z1=sorted((settings.min_z,settings.max_z))
        program.uniform_float('selection',(x0,z0,x1+1,z1+1))
        program.uniform_float('yRange',(min(settings.min_y,settings.max_y),max(settings.min_y,settings.max_y)))
        program.uniform_float('cutStrength',1.)
    else:
        program.uniform_float('selection',(0,0,0,0))
        program.uniform_float('yRange',(0,0))
        program.uniform_float('cutStrength',0.)
    viewrect=rect.x,rect.y,rect.width,rect.height
    visible=sorted(state.view.page_keys(viewrect) & state.map_pages)
    uploads=0
    deadline=time.perf_counter()+.003
    for key in visible:
        textures=state.textures.get(key)
        prepared=state.prepared_pages.get(key)
        if prepared is not None and uploads<2 and time.perf_counter()<deadline:
            normalized,heights=prepared
            textures=(gpu.types.GPUTexture((SIZE,SIZE),format='RGBA8',data=gpu.types.Buffer('FLOAT',len(normalized),normalized)),
                      gpu.types.GPUTexture((SIZE,SIZE),format='R32F',data=gpu.types.Buffer('FLOAT',len(heights),heights)))
            state.textures[key]=textures
            state.prepared_pages.pop(key,None)
            uploads+=1
            while len(state.textures)>MAX_TEXTURE_PAGES:state.textures.popitem(last=False)
        if textures is None:continue
        state.textures.move_to_end(key)
        x,y=state.view.screen(key[0]*PAGE_BLOCKS,(key[1]+1)*PAGE_BLOCKS,viewrect)
        box=renderer._physical_rect(Rect(x,y,PAGE_BLOCKS*state.view.scale,PAGE_BLOCKS*state.view.scale))
        program.uniform_float('pageOrigin',(key[0]*PAGE_BLOCKS,key[1]*PAGE_BLOCKS))
        program.uniform_float('bounds',(box.x,box.y,box.width,box.height))
        program.uniform_sampler('colors',textures[0]);program.uniform_sampler('heights',textures[1])
        _quad.draw(program)


def batched(renderer):
    """Batched overlays need the real GPU renderer; test doubles take the per-item path."""
    return getattr(renderer,'record_only',True) is False and not getattr(renderer,'capture',False)


def dashes(renderer, rect, color, dash, gap, vertical=False):
    """A dashed hairline in one draw call, dashes restarting at the rect's start, with the
    same soft edges as renderer.rect."""
    global _dash, _dash_quad
    if _dash is None:
        info=gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4','ModelViewProjectionMatrix')
        info.push_constant('VEC4','bounds')
        info.push_constant('VEC4','color')
        info.push_constant('FLOAT','period')
        info.push_constant('FLOAT','dash')
        info.push_constant('FLOAT','vertical')
        info.vertex_in(0,'VEC2','pos')
        info.fragment_out(0,'VEC4','fragColor')
        info.vertex_source('void main(){vec2 p=bounds.xy-vec2(1.0)+pos*(bounds.zw+vec2(2.0));gl_Position=ModelViewProjectionMatrix*vec4(p,0.0,1.0);}')
        # Each dash is the rect renderer.rect drew before: the nearest dash's distance field.
        info.fragment_source('''
            void main(){
                bool up=vertical>0.5;
                vec2 local=gl_FragCoord.xy-bounds.xy;
                float along=up?local.y:local.x;
                float across=up?local.x:local.y;
                float length_=up?bounds.w:bounds.z;
                float thick=up?bounds.z:bounds.w;
                float first=floor(along/period);
                float d=1e9;
                for(int i=-1;i<=1;i++){
                    float start=(first+float(i))*period;
                    float end=min(start+dash,length_);
                    if(start>=length_||end<=0.0)continue;
                    start=max(start,0.0);
                    vec2 q=abs(vec2(along,across)-vec2((start+end)*0.5,thick*0.5))-vec2((end-start)*0.5,thick*0.5);
                    d=min(d,length(max(q,0.0))+min(max(q.x,q.y),0.0));
                }
                float aa=max(fwidth(along),0.75);
                fragColor=vec4(color.rgb,color.a*(1.0-smoothstep(-aa*0.5,aa*0.5,d)));
            }
        ''')
        _dash=gpu.shader.create_from_info(info)
        _dash_quad=batch_for_shader(_dash,'TRI_FAN',{'pos':UNIT})
    box=renderer._physical_rect(rect)
    if box.width<=0 or box.height<=0:return
    color=renderer._color(color)
    _dash.bind()
    _dash.uniform_float('ModelViewProjectionMatrix',renderer._matrix)
    _dash.uniform_float('bounds',(box.x,box.y,box.width,box.height))
    _dash.uniform_float('color',(*color[:3],color[3]*renderer.opacity))
    _dash.uniform_float('period',(dash+gap)*renderer.ui_scale)
    _dash.uniform_float('dash',dash*renderer.ui_scale)
    _dash.uniform_float('vertical',1. if vertical else 0.)
    gpu.state.blend_set('ALPHA')
    _dash_quad.draw(_dash)


def _box_shader():
    global _boxes
    if _boxes is None:
        info=gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4','ModelViewProjectionMatrix')
        info.push_constant('VEC4','fillColor')
        info.push_constant('VEC4','borderColor')
        info.push_constant('FLOAT','borderWidth')
        info.vertex_in(0,'VEC2','pos');info.vertex_in(1,'VEC4','box')
        interface=gpu.types.GPUStageInterfaceInfo('mcaddon_map_box')
        interface.flat('VEC4','rect');info.vertex_out(interface)
        info.fragment_out(0,'VEC4','fragColor')
        info.vertex_source('void main(){rect=box;vec2 p=box.xy-vec2(1.0)+pos*(box.zw+vec2(2.0));gl_Position=ModelViewProjectionMatrix*vec4(p,0.0,1.0);}')
        # renderer.rect's square-corner case, one quad per box.
        info.fragment_source('''
            void main(){
                vec2 halfSize=rect.zw*0.5;
                vec2 q=abs(gl_FragCoord.xy-rect.xy-halfSize)-halfSize;
                float d=length(max(q,0.0))+min(max(q.x,q.y),0.0);
                float aa=max(fwidth(d),0.75);
                float coverage=1.0-smoothstep(-aa*0.5,aa*0.5,d);
                float edge=borderWidth>0.0?smoothstep(-aa*0.5,aa*0.5,d+borderWidth):0.0;
                fragColor=mix(fillColor,borderColor,edge);
                fragColor.a*=coverage;
            }
        ''')
        _boxes=gpu.shader.create_from_info(info)
    return _boxes


def _icon_shader():
    global _icons
    if _icons is None:
        info=gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4','ModelViewProjectionMatrix')
        info.push_constant('VEC4','tint')
        info.vertex_in(0,'VEC2','pos');info.vertex_in(1,'VEC2','texCoord')
        interface=gpu.types.GPUStageInterfaceInfo('mcaddon_map_icon')
        interface.smooth('VEC2','uv');info.vertex_out(interface)
        info.sampler(0,'FLOAT_2D','image')
        info.fragment_out(0,'VEC4','fragColor')
        info.vertex_source('void main(){uv=texCoord;gl_Position=ModelViewProjectionMatrix*vec4(pos,0.0,1.0);}')
        # renderer.image's tinted-icon case.
        info.fragment_source('void main(){fragColor=vec4(tint.rgb,texture(image,uv).a*tint.a);}')
        _icons=gpu.shader.create_from_info(info)
    return _icons


def _texture(path):
    """An icon file as a GPU texture, decoded like the renderer's (no Image datablock, no
    mipmaps, so batched icons stay as crisp as single ones). None for non-8-bit files."""
    if path not in _textures:
        import imbuf
        buffer=imbuf.load(path)
        try:
            if buffer.buffer_type!='BYTE' or buffer.channels!=4:_textures[path]=None
            else:
                width,height=buffer.size
                with buffer.with_buffer() as view:
                    pixels=np.asarray(view).reshape(height,width,4).astype(np.float32)/np.float32(255)
                _textures[path]=gpu.types.GPUTexture((width,height),format='SRGB8_A8',
                                                     data=gpu.types.Buffer('FLOAT',pixels.size,pixels.ravel()))
        finally:buffer.free()
    return _textures[path]


def _physical(renderer, rects):
    return np.array([(r.x,r.y,r.width,r.height) for r in map(renderer._physical_rect,rects)],dtype=np.float32)


def marks(renderer, boxes, fill, border, icons=(), icon='', tint=(1,1,1,1)):
    """Many bordered boxes, then one tinted icon in each icon rect, in two draw calls."""
    if boxes:
        rects=_physical(renderer,boxes)
        fill,border=renderer._color(fill),renderer._color(border)
        program=_box_shader();program.bind()
        program.uniform_float('ModelViewProjectionMatrix',renderer._matrix)
        program.uniform_float('fillColor',(*fill[:3],fill[3]*renderer.opacity))
        program.uniform_float('borderColor',(*border[:3],border[3]*renderer.opacity))
        program.uniform_float('borderWidth',renderer.ui_scale)
        gpu.state.blend_set('ALPHA')
        batch_for_shader(program,'TRIS',{'pos':np.tile(CORNERS,(len(rects),1)),'box':np.repeat(rects,6,axis=0)}).draw(program)
    if icons and icon:
        try:texture=_texture(os.path.abspath(icon))
        except (OSError,RuntimeError,ValueError):texture=None
        if texture is None:
            for box in icons:renderer.image(box,icon,tint=tint)
            return
        rects=_physical(renderer,icons)
        corners=np.tile(CORNERS,(len(rects),1));placed=np.repeat(rects,6,axis=0)
        positions=placed[:,:2]+corners*placed[:,2:]
        program=_icon_shader();program.bind()
        program.uniform_float('ModelViewProjectionMatrix',renderer._matrix)
        program.uniform_float('tint',(*tint[:3],tint[3]*renderer.opacity))
        program.uniform_sampler('image',texture)
        gpu.state.blend_set('ALPHA')
        batch_for_shader(program,'TRIS',{'pos':positions,'texCoord':corners}).draw(program)


def reset():
    global _shader,_quad,_dash,_dash_quad,_boxes,_icons
    _shader=_quad=_dash=_dash_quad=_boxes=_icons=None
    _textures.clear()

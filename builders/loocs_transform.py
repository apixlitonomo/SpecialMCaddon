"""LOOCS 0.11 ChromaLuma + SDR display mapping, expressed as scalar operations.

Adapted from Bram Stout Productions' chroma_luma.py and display_transform.py.
Copyright 2024 Bram Stout Productions. See assets/loocs/LICENSE.txt.
Input/output: linear Rec.709. The native OCIO view supplies display encoding.
The expression is evaluated with NumPy to generate the display and inverse LUTs.
"""
import json
import math
from pathlib import Path

DATA = json.loads((Path(__file__).resolve().parents[1]/'assets/loocs/matrices.json').read_text())


def transform(rgb, op):
    add=lambda a,b:op('ADD',a,b)
    sub=lambda a,b:op('SUBTRACT',a,b)
    mul=lambda a,b:op('MULTIPLY',a,b)
    div=lambda a,b:op('DIVIDE',a,op('MAXIMUM',b,1e-12))
    power=lambda a,b:op('POWER',op('MAXIMUM',a,0),b)
    lo=lambda a,b:op('MINIMUM',a,b)
    hi=lambda a,b:op('MAXIMUM',a,b)
    clamp=lambda a:lo(hi(a,0),1)
    select=lambda condition,a,b:add(mul(condition,a),mul(sub(1,condition),b))
    gt=lambda a,b:op('GREATER_THAN',a,b)
    vmax=lambda v:hi(hi(v[0],v[1]),v[2])
    vmin=lambda v:lo(lo(v[0],v[1]),v[2])
    dot=lambda v,w:add(add(mul(v[0],w[0]),mul(v[1],w[1])),mul(v[2],w[2]))
    matrix=lambda v,m:[dot(v,row) for row in m]

    def sigmoid(x,gamma):
        # Only the positive branch is evaluated; bound exp to avoid GPU overflow.
        e=op('EXPONENT',hi(mul(x,-2/gamma),-80))
        return sub(1,power(div(mul(2,e),add(1,e)),gamma))

    def tone(x,chroma=False):
        x=op('LOGARITHM',hi(x,1e-7),10)
        slope=1 if chroma else 1.3
        hx=-.6 if chroma else -.716
        linear=lambda x:add(mul(sub(x,math.log10(.18)),slope),-.95)
        highlight=add(mul(sigmoid(mul(hi(sub(x,hx),0),slope/(.8165 if chroma else .94)),1.3 if chroma else 10),.8165 if chroma else .94),linear(hx))
        y=select(gt(x,hx),highlight,linear(x))
        if not chroma:
            shadow=sub(linear(-1.35),mul(sigmoid(hi(sub(-1.35,x),0),1),1.3))
            y=select(gt(x,-1.35),y,shadow)
            y=hi(y,-3)
        y=power(10,lo(y,0))
        return y if chroma else div(sub(y,.001),.999)

    # Rec.709 -> G1 -> G2 collapses to this precomputed matrix.
    v=matrix(rgb,DATA['input'])
    old_luma=dot(v,DATA['luma_render'])
    mn=lo(vmin(v),0)
    mx=vmax(v)
    v=[mul(sub(c,mn),div(mx,hi(sub(mx,mn),1e-7))) for c in v]
    ratio=select(gt(dot(v,DATA['luma_render']),1e-8),div(old_luma,dot(v,DATA['luma_render'])),1)
    v=[hi(mul(c,ratio),0) for c in v]
    mx=vmax(v)
    m=power(div(mx,500),.5)
    v=[mul(power(div(c,mx),add(1,mul(m,1.3))),mx) for c in v]
    # desaturate() uses the maximum after resaturation (unchanged).
    v=[add(mul(c,sub(1,mul(m,.15))),mul(mx,mul(m,.15))) for c in v]
    v=[clamp(tone(c,True)) for c in v]
    cm=vmax(v)
    v=[mul(select(gt(cm,.00001),div(c,cm),c),tone(mx)) for c in v]
    v=[clamp(c) for c in v]

    def hsv(v):
        mx=vmax(v); mn=vmin(v); delta=sub(mx,mn)
        # Signed division is required for hue.
        r=op('FLOORED_MODULO',op('DIVIDE',sub(v[1],v[2]),hi(delta,1e-12)),6)
        g=add(op('DIVIDE',sub(v[2],v[0]),hi(delta,1e-12)),2)
        b=add(op('DIVIDE',sub(v[0],v[1]),hi(delta,1e-12)),4)
        h=select(op('COMPARE',mx,v[0],1e-9),r,select(op('COMPARE',mx,v[1],1e-9),g,b))
        h=select(gt(delta,1e-12),mul(h,60),0)
        return [h,select(gt(mx,1e-7),div(delta,mx),0),mx]

    def rgb_from_hsv(h,s,v):
        h=div(op('FLOORED_MODULO',h,360),60)
        c=mul(v,s); x=mul(c,sub(1,op('ABSOLUTE',sub(op('FLOORED_MODULO',h,2),1))))
        def interval(a,b):return mul(op('LESS_THAN',h,b),sub(1,op('LESS_THAN',h,a)))
        r=add(mul(add(interval(0,1),interval(5,6)),c),mul(add(interval(1,2),interval(4,5)),x))
        g=add(mul(interval(1,3),c),mul(add(interval(0,1),interval(3,4)),x))
        b=add(mul(interval(3,5),c),mul(add(interval(2,3),interval(5,6)),x))
        return [add(k,sub(v,c)) for k in (r,g,b)]

    orig_luma=dot(v,DATA['luma_render'])
    h,s,value=hsv(matrix(v,DATA['display']))
    delta=sub(vmax(v),vmin(v))
    sat_rgb=[select(gt(delta,1e-7),div(sub(c,vmin(v)),delta),c) for c in v]
    maxsat=hi(hsv(matrix(sat_rgb,DATA['display']))[1],1)
    sat=lo(s,maxsat)
    rolled=add(mul(sub(1,power(sub(1,div(sub(sat,.9),sub(maxsat,.9))),div(sub(maxsat,.9),.1))),.1),.9)
    s=select(gt(sat,.9),rolled,sat)
    maxval=vmax(matrix([select(gt(vmax(v),1e-7),div(c,vmax(v)),c) for c in v],DATA['display']))
    value=select(gt(maxval,1),sub(1,power(sub(1,div(lo(value,maxval),maxval)),maxval)),lo(value,1))
    for source,dest in zip(DATA['primary_hues'],(0,240)):
        source=(source-(dest-180))%360+(dest-180)-dest
        delta=-source
        if abs(delta)<.1:continue
        x=op('DIVIDE',sub(add(op('FLOORED_MODULO',sub(h,dest-180),360),dest-180),dest),delta)
        p1=sub(sub(sub(mul(power(hi(op('ABSOLUTE',x),0),3),2),mul(mul(x,x),9)),mul(x,11)),4)
        # x is negative in the p1 branch, so -2*x^3 = +2*abs(x)^3.
        p2=add(add(mul(mul(mul(x,x),x),.25),mul(x,.25)),.5)
        mapped=select(op('LESS_THAN',x,-1),p1,p2)
        inside=mul(gt(x,-2),op('LESS_THAN',x,1))
        h=op('FLOORED_MODULO',add(mul(select(inside,mapped,x),delta),dest),360)
    lum=dot(rgb_from_hsv(h,s,value),DATA['luma_display'])
    scaling=div(power(orig_luma,1/2.2),power(lum,1/2.2))
    gamma=sub(1,op('LOGARITHM',sub(2,lo(scaling,1.75)),2))
    mapped=power(sub(1,power(sub(1,power(value,1/2.2)),gamma)),2.2)
    value=select(gt(orig_luma,1e-6),mapped,value)
    return [clamp(c) for c in rgb_from_hsv(h,s,value)]


def numpy_op(name,*args):
    import numpy as np
    a=args[0]; b=args[1] if len(args)>1 else None
    with np.errstate(all='ignore'):
        if name=='ADD':return a+b
        if name=='SUBTRACT':return a-b
        if name=='MULTIPLY':return a*b
        if name=='DIVIDE':return np.divide(a,b)
        if name=='POWER':return np.power(a,b)
        if name=='MINIMUM':return np.minimum(a,b)
        if name=='MAXIMUM':return np.maximum(a,b)
        if name=='GREATER_THAN':return np.asarray(a>b,dtype=float)
        if name=='LESS_THAN':return np.asarray(a<b,dtype=float)
        if name=='COMPARE':return np.asarray(np.abs(a-b)<=args[2],dtype=float)
        if name=='EXPONENT':return np.exp(a)
        if name=='LOGARITHM':return np.log(a)/np.log(b)
        if name=='ABSOLUTE':return np.abs(a)
        if name=='FLOORED_MODULO':return np.mod(a,b)
    raise ValueError(name)


def evaluate(values):
    import numpy as np
    a=np.asarray(values,dtype=float)
    return np.stack(transform([a[...,i] for i in range(3)],numpy_op),axis=-1)


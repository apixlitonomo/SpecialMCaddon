"""Chunky cloud generators with stable variation across the world."""
import json
from pathlib import Path
import random

CELL_SIZE = 32
TILE_CELLS = 16
TILE_SIZE = CELL_SIZE*TILE_CELLS
# Classic and Vibrant clouds at Sparkle's own scale: 32-block cells, so a tile
# is 512 blocks and the field reaches CLASSIC_REACH tiles each way, past the
# distant-cloud fade (1450 blocks). Minecraft's literal 12-block cells (layout
# 21, 2026-10-15) filled the sky with small clouds; the user preferred these.
CLASSIC_CELL = 32
CLASSIC_TILE = CLASSIC_CELL*TILE_CELLS
CLASSIC_REACH = 3
# Dungeons II clouds use the game's shapes: one game block per cell, scaled
# up so the 17-30 block shapes span roughly 140-240 blocks of sky.
DUNGEONS_UNIT = 8
DUNGEONS_TILE_UNITS = TILE_SIZE//DUNGEONS_UNIT
_SHAPES = None

# Cloud-only recipes. Time, scene lighting and atmosphere stay editable.
PRESETS = {
    'CLASSIC': dict(label='Classic', clouds=.25, cloud_height=256., cloud_thickness=5.,
                    cloud_color=(.92,.95,1.)),
    'DUNGEONS': dict(label='MC Dungeons II', clouds=1., dungeons_cloud_density='SPARSE',
                     dungeons_cloud_scale=1., cloud_height=256., cloud_thickness=8.,
                     cloud_color=(.92,.95,1.)),
    'VIBRANT': dict(label='Vibrant Visuals', clouds=.3, cloud_height=192., cloud_thickness=3.,
                    cloud_color=(1.,.98,.96)),
}


def _game_shapes():
    """The 16 SM_Clouds_Shape footprints decoded from Minecraft Dungeons II."""
    global _SHAPES
    if _SHAPES is None:
        data=json.loads((Path(__file__).resolve().parents[1]/'assets'/'dungeons_ii'/'cloud_shapes.json').read_text())
        shapes=[]
        for rows in data['shapes'].values():
            shapes.append(frozenset((x,y) for y,row in enumerate(rows) for x,c in enumerate(row) if c=='#'))
        _SHAPES=([s for s in shapes if len(s)>=100],[s for s in shapes if len(s)<100])
    return _SHAPES


def _placed(shape,turns,mirror):
    width=max(x for x,_ in shape)+1
    cells=((width-1-x,y) if mirror else (x,y) for x,y in shape)
    for _ in range(turns):
        cells=[(-y,x) for x,y in cells]
    cells=list(cells)
    mx,my=min(x for x,_ in cells),min(y for _,y in cells)
    return [(x-mx,y-my) for x,y in cells]


def dungeons_cells(tile_origin, density='SPARSE'):
    """Place the game's own flat cloud shapes, stable across camera tiles.

    Keys are local cells of DUNGEONS_UNIT blocks (one game block each), offset
    by DUNGEONS_TILE_UNITS per world tile; values are relative top heights.
    The game meshes are flat one-block slabs, so every height is 1. Dense adds
    shapes after the sparse ones from the same sequence, never reshuffling.
    """
    large,small=_game_shapes()
    occupied={}
    ox,oy=tile_origin
    span=DUNGEONS_TILE_UNITS
    low,high=-3*span,4*span
    picks=(('large',1),('small',1))+((('large',2),('small',1)) if density=='DENSE' else ())
    for tx in range(ox-4,ox+4):
        for ty in range(oy-4,oy+4):
            rng=random.Random(2197 ^ ((tx&0xffffffff)*73856093) ^ ((ty&0xffffffff)*19349663))
            for kind,count in picks:
                for _ in range(count):
                    pool=large if kind=='large' else small
                    shape=_placed(pool[rng.randrange(len(pool))],rng.randrange(4),rng.random()<.5)
                    cx,cy=rng.randrange(span),rng.randrange(span)
                    for x,y in shape:
                        px,py=(tx-ox)*span+cx+x,(ty-oy)*span+cy+y
                        if low<=px<high and low<=py<high:
                            occupied[px,py]=1.
    return occupied


def window_cells(amount, tile_origin, preset='CLASSIC', density='SPARSE'):
    if preset=='DUNGEONS':
        # Dungeons amount controls opacity; density alone changes the footprint.
        return set(dungeons_cells(tile_origin,density))
    occupied=set()
    if amount <= 0:
        return occupied
    ox,oy=tile_origin
    low,high=-CLASSIC_REACH*TILE_CELLS,(CLASSIC_REACH+1)*TILE_CELLS
    # Keep the original seed and shape-building sequence. Only the world-tile
    # seed changes, instead of arraying the same 512-block patch everywhere.
    # Include neighbors so clouds crossing a tile edge stay connected.
    for tx in range(ox-CLASSIC_REACH-1,ox+CLASSIC_REACH+2):
        for ty in range(oy-CLASSIC_REACH-1,oy+CLASSIC_REACH+2):
            rng=random.Random(418 ^ ((tx&0xffffffff)*73856093) ^ ((ty&0xffffffff)*19349663))
            for _ in range(round(amount*14)):
                cx,cy=rng.randrange(16),rng.randrange(16)
                # VV's official plains reference has long, very thin square
                # plates. Preserve the exact classic RNG sequence otherwise.
                for x in range(rng.randrange(3,8) if preset=='VIBRANT' else rng.randrange(2,5)):
                    for y in range(rng.randrange(1,3) if preset=='VIBRANT' else rng.randrange(2,5)):
                        px,py=(tx-ox)*TILE_CELLS+cx+x,(ty-oy)*TILE_CELLS+cy+y
                        if low <= px < high and low <= py < high:
                            occupied.add((px,py))
    return occupied

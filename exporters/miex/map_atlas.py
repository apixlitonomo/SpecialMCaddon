"""Map pages for bounded GPU draw calls, including neighbouring terrain heights."""
from array import array

PAGE_CHUNKS = 16
PAGE_BLOCKS = PAGE_CHUNKS * 16
PADDING = 16
SIZE = PAGE_BLOCKS + PADDING * 2


def page_pixels(tiles, page):
    colors = bytearray(SIZE * SIZE * 4)
    heights = array('f', [0]) * (SIZE * SIZE)
    x0, z0 = page[0]*PAGE_CHUNKS-1, page[1]*PAGE_CHUNKS-1
    for cz in range(PAGE_CHUNKS+2):
        for cx in range(PAGE_CHUNKS+2):
            tile = tiles.get((x0+cx,z0+cz))
            if not tile:continue
            pixels, heightmap = tile
            for z in range(16):
                dest=(cz*16+z)*SIZE+cx*16
                colors[dest*4:(dest+16)*4]=pixels[z*64:(z+1)*64]
                if heightmap is not None:
                    heights[dest:dest+16]=array('f',heightmap[z*16:(z+1)*16])
    return colors, heights


def affected_pages(chunk):
    x,z=chunk
    # A page includes a one-chunk halo, so update adjacent page seams too.
    return {(xx//PAGE_CHUNKS,zz//PAGE_CHUNKS) for xx in (x-1,x,x+1) for zz in (z-1,z,z+1)}

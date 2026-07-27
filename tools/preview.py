#!/usr/bin/env python3
"""
Host-side validator: reads the generated .hpak and renders what the 32X will
draw for a given level, using the same palette/compositing rules as the SH-2
renderer.  Emits a PNG so the conversion can be eyeballed and, more
importantly, checked programmatically (non-black, correct colour counts).
"""
import struct
import sys
import zlib

MAGIC = b'HPAK'
MAP_W, MAP_H = 240, 60
TILE = 16
SCREEN_W, SCREEN_H = 320, 200
HUD_H = 40


class Pak:
    def __init__(self, path):
        self.d = open(path, 'rb').read()
        assert self.d[:4] == MAGIC, 'bad magic'
        ver, count, hs = struct.unpack('>HHI', self.d[4:12])
        self.dir = {}
        p = 12
        for _ in range(count):
            name = self.d[p:p + 16].split(b'\x00')[0].decode()
            off, ln = struct.unpack('>II', self.d[p + 16:p + 24])
            self.dir[name] = (off, ln)
            p += 24

    def get(self, name):
        if name not in self.dir:
            return None
        o, l = self.dir[name]
        return self.d[o:o + l]


def write_png(path, w, h, rgb):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]
    def chunk(t, data):
        c = struct.pack('>I', len(data)) + t + data
        return c + struct.pack('>I', zlib.crc32(t + data) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(bytes(raw), 6))
    png += chunk(b'IEND', b'')
    open(path, 'wb').write(png)


def main():
    pak = Pak(sys.argv[1] if len(sys.argv) > 1 else 'build/hocus.hpak')
    level = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    out = sys.argv[3] if len(sys.argv) > 3 else 'build/preview.png'

    levels = pak.get('LEVELS')
    px, py, shoot, limit, tsid, bgid = struct.unpack('>HHHHHH', levels[level * 12:(level + 1) * 12])
    print('level %d: player=(%d,%d) limit=%d tileset=%d bg=%d' % (level, px, py, limit, tsid, bgid))

    # build 256-colour CRAM: 0..127 game palette, 128..255 background palette
    pal = bytearray(256 * 3)
    gp = pak.get('PAL_GAME')
    pal[0:384] = gp
    bp = pak.get('BGPAL%d' % bgid)
    pal[384:768] = bp

    tiles = pak.get('TILES%d' % tsid)
    bg = pak.get('BG%d' % bgid)
    fg = pak.get('LVFG%d' % level)
    bgl = pak.get('LVBG%d' % level)
    adl = pak.get('LVAD%d' % level)

    # camera centred on player, clamped
    cam_x = max(0, min(px * TILE - SCREEN_W // 2, MAP_W * TILE - SCREEN_W))
    cam_y = max(0, min(py * TILE - (SCREEN_H - HUD_H) // 2, MAP_H * TILE - (SCREEN_H - HUD_H)))

    view_h = SCREEN_H - HUD_H
    idx = bytearray(SCREEN_W * SCREEN_H)

    # parallax background (bg scrolls at half speed, wraps)
    for y in range(view_h):
        sy = (cam_y // 2 + y) % 200
        for x in range(SCREEN_W):
            sx = (cam_x // 2 + x) % 320
            idx[y * SCREEN_W + x] = bg[sy * 320 + sx]

    def blit_layer(layer, transparent_ff=True):
        for ty in range(cam_y // TILE, min(MAP_H, (cam_y + view_h) // TILE + 1)):
            for tx in range(cam_x // TILE, min(MAP_W, (cam_x + SCREEN_W) // TILE + 1)):
                t = layer[ty * MAP_W + tx]
                if t == 0xFF:
                    continue
                sx = tx * TILE - cam_x
                sy = ty * TILE - cam_y
                for row in range(TILE):
                    dy = sy + row
                    if dy < 0 or dy >= view_h:
                        continue
                    src = t * 256 + row * TILE
                    for col in range(TILE):
                        dx = sx + col
                        if dx < 0 or dx >= SCREEN_W:
                            continue
                        v = tiles[src + col]
                        if v:
                            idx[dy * SCREEN_W + dx] = v

    blit_layer(bgl)
    blit_layer(fg)
    blit_layer(adl)

    # player sprite (sprite 0, stand frame, east)
    meta = pak.get('SPRMETA')
    sdata = pak.get('SPRDATA')
    nspr = struct.unpack('>H', meta[:2])[0]
    rec = 20 + 30 * 4
    o = 2
    sw, sh, stand, wb, we, jf, ff, sb, se, pf = struct.unpack('>10H', meta[o:o + 20])
    offs = struct.unpack('>30I', meta[o + 20:o + 20 + 120])
    fo = offs[stand]
    if fo != 0xFFFFFFFF:
        sxp = px * TILE - cam_x
        syp = py * TILE - cam_y
        for row in range(sh):
            dy = syp + row
            if dy < 0 or dy >= view_h:
                continue
            for col in range(sw):
                dx = sxp + col
                if dx < 0 or dx >= SCREEN_W:
                    continue
                v = sdata[fo + row * sw + col]
                if v:
                    idx[dy * SCREEN_W + dx] = v

    # HUD strip at the bottom
    hud = pak.get('HUD')
    hw, hh = struct.unpack('>HH', hud[:4])
    hp = hud[4:]
    for row in range(min(hh, HUD_H)):
        for col in range(min(hw, SCREEN_W)):
            idx[(view_h + row) * SCREEN_W + col] = hp[row * hw + col]

    rgb = bytearray(SCREEN_W * SCREEN_H * 3)
    for i, v in enumerate(idx):
        rgb[i * 3] = pal[v * 3]
        rgb[i * 3 + 1] = pal[v * 3 + 1]
        rgb[i * 3 + 2] = pal[v * 3 + 2]

    write_png(out, SCREEN_W, SCREEN_H, rgb)
    nonzero = sum(1 for v in idx if v)
    uniq = len(set(idx))
    print('wrote %s  nonzero=%d/%d unique_colours=%d' % (out, nonzero, len(idx), uniq))
    return 0


if __name__ == '__main__':
    sys.exit(main())

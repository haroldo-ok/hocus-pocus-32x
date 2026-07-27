#!/usr/bin/env python3
"""
Hocus Pocus 32X - asset converter.

Reads the original shareware HOCUS.DAT / HOCUS.EXE (v1.1) and produces a single
big-endian "HPAK" blob that is appended to the 32X ROM.  All decoding of the
original DOS formats (PCX/RLE, 4-plane "blocks" images, layout-compressed
sprites, 1bpp font, VGA 6-bit palettes) happens here on the host, so the SH-2
only ever sees flat, ready-to-blit 8bpp data.

Palette design (single 256-entry 32X CRAM):
  indices   0..127 -> tileset / sprite / HUD palette  (DAT file 6,  <<2)
  indices 128..255 -> per-level background palette    (DAT file 62+, <<2)
That split is exactly how the original game uses VGA palette space, so both
halves coexist without any remapping.  Index 0 is the transparent colour for
sprites and for foreground tiles.
"""

import struct
import sys
import os

# ---------------------------------------------------------------- DAT indices
DATFILE_FONT_MAIN = 0
DATFILE_PALETTE_GAME = 6
DATFILE_IMAGE_STUFF = 10
DATFILE_IMAGE_HUD = 11
DATFILE_PALETTE_BACKGROUND_01 = 62
DATFILE_IMAGE_BACKGROUND_01 = 66
DATFILE_TILESET_01 = 70
DATFILE_SPRITE_SET = 83
DATFILE_LEVELS_START = 84

EXEFILE_LIMIT_TIME = 0
EXEFILE_ITEMS = 1
EXEFILE_TILESETS = 2
EXEFILE_BACKGROUNDS = 3

EPISODES = 1
STAGES = 9
LEVELS = EPISODES * STAGES

MAP_W = 240
MAP_H = 60
TILE = 16
TILES_PER_SET = 240          # 20 columns x 12 rows of 16x16 in a 320x200 PCX

SCREEN_W = 320
SCREEN_H = 200

MAGIC = b'HPAK'
VERSION = 3


# ------------------------------------------------------------------ utilities
def load_fat(path):
    d = open(path, 'rb').read()
    n = struct.unpack('<I', d[:4])[0]
    return [struct.unpack('<II', d[4 + i * 8:12 + i * 8]) for i in range(n)]


class Archive:
    def __init__(self, blob, fat):
        self.blob = blob
        self.fat = fat

    def get(self, i):
        o, l = self.fat[i]
        return self.blob[o:o + l]


def pcx_decode(d):
    """Decode an 8bpp RLE PCX. Returns (w, h, pixels, palette or None)."""
    if d[0] != 0x0A:
        raise ValueError('not a PCX')
    xmin, ymin, xmax, ymax = struct.unpack('<HHHH', d[4:12])
    w = xmax - xmin + 1
    h = ymax - ymin + 1
    bpl = struct.unpack('<H', d[66:68])[0]
    total = bpl * h
    out = bytearray(total)
    i = 128
    p = 0
    n = len(d)
    while p < total and i < n:
        b = d[i]
        i += 1
        if (b & 0xC0) == 0xC0:
            cnt = b & 0x3F
            if i >= n:
                break
            v = d[i]
            i += 1
            end = min(p + cnt, total)
            for k in range(p, end):
                out[k] = v
            p = end
        else:
            out[p] = b
            p += 1
    pal = None
    if len(d) >= 769 and d[-769] == 0x0C:
        pal = d[-768:]
    # crop bpl -> w
    if bpl != w:
        rows = [out[y * bpl:y * bpl + w] for y in range(h)]
        out = bytearray(b''.join(rows))
    return w, h, bytes(out), pal


def image_decode(d):
    """
    Decode the Apogee 4-plane "blocks" image used for HUD/stuff bitmaps.
    Header: uint16 width/4, uint16 height, then 4 planes of columns.
    """
    w = struct.unpack('<H', d[0:2])[0] * 4
    h = struct.unpack('<H', d[2:4])[0]
    if w * h + 4 != len(d):
        raise ValueError('bad image size w=%d h=%d len=%d' % (w, h, len(d)))
    px = bytearray(w * h)
    p = 4
    for plane in range(4):
        for y in range(h):
            row = y * w
            for x in range(plane, w, 4):
                px[row + x] = d[p]
                p += 1
    return w, h, bytes(px)


def font_decode(d):
    """90 glyphs, 8x8, 1bpp, row-major bit x = bit index."""
    glyphs = []
    for c in range(90):
        g = bytearray(64)
        for y in range(8):
            row = d[c * 8 + y]
            for x in range(8):
                g[y * 8 + x] = 1 if (row >> x) & 1 else 0
        glyphs.append(bytes(g))
    return glyphs


FONT_WIDTHS = [3, 6, 6, 6, 6, 6, 3, 4, 4, 6, 6, 3, 6, 2, 7, 6, 4, 6,
               6, 7, 6, 6, 7, 6, 6, 2, 2, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 3, 6, 6, 6, 7,
               6, 6, 6, 7, 6, 6, 7, 6, 6, 7, 6, 7, 6, 5, 7, 5, 5, 7, 3, 6, 6, 6, 6, 6, 6, 6, 6,
               3, 5, 6, 3, 7, 6, 6, 6, 6, 6, 6, 6, 6, 6, 7, 6, 6, 6]


def sprite_headers(d):
    """Parse the sprite set: N fixed-size headers followed by pixel data."""
    first = struct.unpack('<I', d[:4])[0]
    hdr_size = 220
    n = first // hdr_size
    sprites = []
    for i in range(n):
        h = d[i * hdr_size:(i + 1) * hdr_size]
        off = struct.unpack('<I', h[0:4])[0]
        name = h[4:26].split(b'\x00')[0].decode('latin-1', 'replace')
        v = struct.unpack('<17H', h[26:60])
        s = {
            'offset': off,
            'name': name,
            'width': v[0] * 4,
            'height': v[1],
            'standFrame': v[2],
            'standFrame2': v[3],
            'walkBegin': v[4],
            'walkEnd': v[5],
            'jumpFrame': v[6],
            'fallFrame': v[7],
            'shootBegin': v[8],
            'shootEnd': v[9],
            'projWidth': v[10] * 4,
            'projHeight': v[11],
            'projY': v[12],
            'projFrame': v[13],
            'pixelsOffset': v[15],
            'pixelsSize': v[16],
        }
        p = 60
        s['layoutE'] = struct.unpack('<20H', h[p:p + 40]); p += 40
        s['layoutW'] = struct.unpack('<20H', h[p:p + 40]); p += 40
        s['pixelsE'] = struct.unpack('<20H', h[p:p + 40]); p += 40
        s['pixelsW'] = struct.unpack('<20H', h[p:p + 40]); p += 40
        sprites.append(s)
    return sprites


SPRITE_STRIDE = 320     # OpenPocus decodes every frame into a 320px-wide buffer


def sprite_frame(blob, s, frame, west):
    """
    Rebuild one sprite frame into an 8bpp buffer (0 = transparent).
    Mirrors OpenPocus' Sprite::createAsTexture layout interpreter.

    The layout stream addresses pixels in a scratch buffer that is always
    SPRITE_STRIDE pixels wide (not the sprite's own width), so we decode at
    that stride and then crop the sprite's WxH rectangle out of it.
    """
    w, h = s['width'], s['height']
    scratch = bytearray(SPRITE_STRIDE * h)
    layout_off = (s['layoutW'] if west else s['layoutE'])[frame]
    pixel_off = (s['pixelsW'] if west else s['pixelsE'])[frame] * 4
    if pixel_off >= s['pixelsSize'] or layout_off >= s['pixelsOffset']:
        return None
    base = s['offset']
    lp = base + layout_off
    pp = base + s['pixelsOffset'] + pixel_off
    img = 0
    trans = 0
    limit = s['pixelsOffset']
    i = 0
    while i < limit:
        if lp >= len(blob):
            break
        flag = blob[lp]; lp += 1
        if flag == 0:
            if lp >= len(blob):
                break
            trans = blob[lp]; lp += 1
            img = 0
        elif flag == 1:
            if lp + 1 >= len(blob):
                break
            mv = blob[lp] | (blob[lp + 1] << 8); lp += 2
            img += mv * 4
        elif flag == 2:
            for j in range(4):
                if (trans >> j) & 1:
                    idx = img + j
                    if 0 <= idx < len(scratch) and pp + j < len(blob):
                        scratch[idx] = blob[pp + j]
            img += 4
            pp += 4
        elif flag == 3:
            break
        else:
            break
        i += 1

    # crop the sprite rectangle out of the 320-wide scratch buffer
    out = bytearray(w * h)
    for y in range(h):
        src = y * SPRITE_STRIDE
        out[y * w:(y + 1) * w] = scratch[src:src + w]
    return bytes(out)


# ------------------------------------------------------------------- pack out
class Packer:
    """Collects binary lumps and emits a directory-indexed big-endian blob."""

    def __init__(self):
        self.lumps = []      # (name, bytes)

    def add(self, name, data):
        assert len(name) <= 15, name
        self.lumps.append((name, bytes(data)))
        return len(self.lumps) - 1

    def build(self):
        # header: magic, version, count, then dir entries (16b name, u32 off, u32 len)
        count = len(self.lumps)
        dir_size = count * 24
        head_size = 4 + 2 + 2 + 4 + dir_size
        head_size = (head_size + 15) & ~15
        body = bytearray()
        entries = []
        for name, data in self.lumps:
            off = head_size + len(body)
            entries.append((name, off, len(data)))
            body += data
            while len(body) & 15:
                body.append(0)
        out = bytearray()
        out += MAGIC
        out += struct.pack('>HHI', VERSION, count, head_size)
        for name, off, ln in entries:
            nb = name.encode('ascii')
            nb += b'\x00' * (16 - len(nb))
            out += nb
            out += struct.pack('>II', off, ln)
        while len(out) < head_size:
            out.append(0)
        out += body
        return bytes(out)


def vga6_to_rgb8(pal384):
    """VGA 6-bit palette (128 colours x3) -> 8-bit RGB triples."""
    out = bytearray(128 * 3)
    for i in range(128 * 3):
        v = pal384[i] if i < len(pal384) else 0
        out[i] = min(255, v << 2)
    return bytes(out)


def main():
    if len(sys.argv) < 4:
        print('usage: mkassets.py HOCUS.DAT HOCUS.EXE out.hpak [fatdir]')
        return 1
    dat_path, exe_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    fatdir = sys.argv[4] if len(sys.argv) > 4 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '..', 'openpocus', 'data')

    dat = Archive(open(dat_path, 'rb').read(), load_fat(os.path.join(fatdir, 'shareware.fat')))
    exe = Archive(open(exe_path, 'rb').read(), load_fat(os.path.join(fatdir, 'shareware_exe.fat')))

    pk = Packer()

    # ---- palettes -----------------------------------------------------------
    game_pal = vga6_to_rgb8(dat.get(DATFILE_PALETTE_GAME))
    pk.add('PAL_GAME', game_pal)

    # per-level tables from the executable
    limit_raw = exe.get(EXEFILE_LIMIT_TIME)
    limits = struct.unpack('<%dH' % (len(limit_raw) // 2), limit_raw)
    ts_raw = exe.get(EXEFILE_TILESETS)
    tilesets = struct.unpack('<%dH' % (len(ts_raw) // 2), ts_raw)
    bg_raw = exe.get(EXEFILE_BACKGROUNDS)
    bgs = struct.unpack('<%dH' % (len(bg_raw) // 2), bg_raw)

    # Item info.  The record is 42 bytes, not 44: 36 bytes of name followed by
    # score(u16), heal, firePower, type, pad.  Using 44 shifts every field and
    # yields garbage scores (Ruby=100 is the giveaway that 42 is right).
    items = exe.get(EXEFILE_ITEMS)
    ITEM_REC = 42
    nitems = len(items) // ITEM_REC
    it = bytearray()
    for i in range(nitems):
        e = items[i * ITEM_REC:(i + 1) * ITEM_REC]
        if len(e) < ITEM_REC:
            e = e + b'\x00' * (ITEM_REC - len(e))
        score, = struct.unpack('<H', e[36:38])
        heal, fire, typ, pad = e[38], e[39], e[40], e[41]
        it += struct.pack('>HBBBB', score, heal, fire, typ, pad)
    pk.add('ITEMS', it)

    # ---- tilesets -----------------------------------------------------------
    # Each tileset PCX is 320x200 = 20x12 tiles of 16x16 (240 tiles).
    used_ts = sorted(set(tilesets[:LEVELS]))
    used_bg = sorted(set(bgs[:LEVELS]))

    for tid in used_ts:
        w, h, px, pal = pcx_decode(dat.get(DATFILE_TILESET_01 + tid))
        tiles = bytearray(TILES_PER_SET * TILE * TILE)
        for t in range(TILES_PER_SET):
            cx = (t % 20) * TILE
            cy = (t // 20) * TILE
            for y in range(TILE):
                src = (cy + y) * w + cx
                dst = t * 256 + y * TILE
                tiles[dst:dst + TILE] = px[src:src + TILE]
        pk.add('TILES%d' % tid, tiles)

        # Per-tile opacity class, so the renderer can pick the fastest blit:
        #   0 = fully transparent (skip entirely)
        #   1 = fully opaque      (straight 32-bit copy, no masking needed)
        #   2 = mixed             (needs the VDP overwrite region)
        cls = bytearray(TILES_PER_SET)
        for t in range(TILES_PER_SET):
            blk = tiles[t * 256:(t + 1) * 256]
            z = blk.count(0)
            cls[t] = 0 if z == 256 else (1 if z == 0 else 2)
        pk.add('TCLS%d' % tid, cls)

    for bid in used_bg:
        w, h, px, pal = pcx_decode(dat.get(DATFILE_IMAGE_BACKGROUND_01 + bid))
        # backgrounds are already 320x200 with indices >=128
        if (w, h) != (SCREEN_W, SCREEN_H):
            buf = bytearray(SCREEN_W * SCREEN_H)
            for y in range(min(h, SCREEN_H)):
                buf[y * SCREEN_W:y * SCREEN_W + min(w, SCREEN_W)] = px[y * w:y * w + min(w, SCREEN_W)]
            px = bytes(buf)
        pk.add('BG%d' % bid, px)
        # matching background palette occupies CRAM 128..255
        bpal = vga6_to_rgb8(dat.get(DATFILE_PALETTE_BACKGROUND_01 + bid))
        pk.add('BGPAL%d' % bid, bpal)

    # ---- HUD + stuff --------------------------------------------------------
    w, h, hud = image_decode(dat.get(DATFILE_IMAGE_HUD))
    pk.add('HUD', struct.pack('>HH', w, h) + hud)
    w2, h2, stuff = image_decode(dat.get(DATFILE_IMAGE_STUFF))
    pk.add('STUFF', struct.pack('>HH', w2, h2) + stuff)

    # ---- font ---------------------------------------------------------------
    glyphs = font_decode(dat.get(DATFILE_FONT_MAIN))
    fb = bytearray()
    for g in glyphs:
        fb += g
    pk.add('FONT', bytes(FONT_WIDTHS) + bytes(fb))

    # ---- sprites ------------------------------------------------------------
    sblob = dat.get(DATFILE_SPRITE_SET)
    sprites = sprite_headers(sblob)
    # Export every sprite that has usable frames: player + enemies + score tags.
    # Frame table: for each sprite, up to 15 frames x 2 directions.
    smeta = bytearray()
    sdata = bytearray()
    exported = []
    for si, s in enumerate(sprites):
        frames = []
        for west in (0, 1):
            for f in range(15):
                fr = sprite_frame(sblob, s, f, west)
                frames.append(fr)
        # count valid
        valid = sum(1 for f in frames if f is not None)
        if valid == 0 or s['width'] == 0 or s['height'] == 0:
            exported.append(None)
            continue
        base = len(sdata)
        offs = []
        for fr in frames:
            if fr is None:
                offs.append(0xFFFFFFFF)
            else:
                offs.append(len(sdata))
                sdata += fr
        exported.append((si, base, offs, s))

    # metadata blob: count, then per sprite record
    nspr = len(sprites)
    smeta += struct.pack('>H', nspr)
    for e in exported:
        if e is None:
            smeta += struct.pack('>HHHHHHHHHH', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            smeta += b'\x00' * (30 * 4)
            continue
        si, base, offs, s = e
        smeta += struct.pack('>HHHHHHHHHH',
                             s['width'], s['height'],
                             s['standFrame'], s['walkBegin'], s['walkEnd'],
                             s['jumpFrame'], s['fallFrame'],
                             s['shootBegin'], s['shootEnd'], s['projFrame'])
        for o in offs:
            smeta += struct.pack('>I', o if o != 0xFFFFFFFF else 0xFFFFFFFF)
    pk.add('SPRMETA', smeta)
    pk.add('SPRDATA', sdata)

    # ---- levels -------------------------------------------------------------
    lvl_index = bytearray()
    for lv in range(LEVELS):
        def lump(kind):
            return dat.get(DATFILE_LEVELS_START + kind * LEVELS + lv)

        pc = lump(0)
        _, px_, py_, shoot = struct.unpack('<4H', pc[:8])
        tanim = lump(1)
        bglayer = lump(9)
        maplayer = lump(10)
        addlayer = lump(11)
        evraw = lump(12)
        ev = struct.unpack('<%dH' % (len(evraw) // 2), evraw)
        ev8 = bytearray(MAP_W * MAP_H)
        for i, v in enumerate(ev[:MAP_W * MAP_H]):
            # 30000 == empty; real events are 0..0x10
            ev8[i] = 0xFF if v >= 0x100 else (v & 0xFF)

        # tile animation settings -> flat 240*3 + 4 control bytes
        tinfo = bytes(tanim[:4]) + bytes(tanim[4:4 + 240 * 3])

        # teleports / switches / toggles kept raw but byte-swapped to BE
        def be16(raw, count):
            vals = struct.unpack('<%dH' % count, raw[:count * 2])
            return struct.pack('>%dH' % count, *vals)

        tel = be16(lump(3), 20)
        swi_raw = lump(4)
        # 23 switches x 22 bytes: type,4 offsets,4 desiredTile(bytes),ulx,uly,lrx,lry
        swi = bytearray()
        for i in range(23):
            e = swi_raw[i * 22:(i + 1) * 22]
            if len(e) < 22:
                e += b'\x00' * (22 - len(e))
            typ, = struct.unpack('<H', e[0:2])
            offs = struct.unpack('<4H', e[2:10])
            desired = e[10:14]
            ulx, uly, lrx, lry = struct.unpack('<4H', e[14:22])
            swi += struct.pack('>H4H4sHHHH', typ, *offs, bytes(desired), ulx, uly, lrx, lry)

        def toggles(raw):
            out = bytearray()
            for i in range(25):
                e = raw[i * 12:(i + 1) * 12]
                if len(e) < 12:
                    e += b'\x00' * (12 - len(e))
                v = struct.unpack('<6H', e)
                out += struct.pack('>6H', *v)
            return bytes(out)

        ins = toggles(lump(5))
        kh = toggles(lump(6))

        # tile properties: 10 x 12 uint16
        tp_raw = lump(7)
        tp = bytearray()
        for i in range(10):
            v = struct.unpack('<12H', tp_raw[i * 24:(i + 1) * 24])
            tp += struct.pack('>12H', *v)

        # enemy triggers: 250 entries x (8 type + 8 offset) uint16
        et_raw = lump(8)
        et = bytearray()
        for i in range(250):
            e = et_raw[i * 32:(i + 1) * 32]
            v = struct.unpack('<16H', e)
            et += struct.pack('>16H', *v)

        # ------------------------------------------------------------------
        # Flattened enemy spawn list.
        #
        # The original stores 250 "trigger groups", each holding up to 8
        # (type, tile-offset) pairs.  `type` indexes the level's tile-property
        # table, which supplies the sprite set, hit points and behaviour.  We
        # resolve all of that here so the SH-2 just walks a flat array.
        #
        # Record (12 bytes, big endian):
        #   u16 tx, u16 ty      spawn tile
        #   u8  spriteset       index into the sprite set
        #   u8  health
        #   u8  behaviour       0 walker, 1 hopper, 2 turret
        #   u8  shoots          non-zero if it fires projectiles
        #   u8  projh, u8 projv projectile speeds
        #   u8  targetplayer, u8 group
        # ------------------------------------------------------------------
        tp_vals = [struct.unpack('<12H', tp_raw[i * 24:(i + 1) * 24])
                   for i in range(10)]
        spawns = bytearray()
        nspawn = 0
        for i in range(250):
            e = et_raw[i * 32:(i + 1) * 32]
            types = struct.unpack('<8H', e[0:16])
            offs = struct.unpack('<8H', e[16:32])
            for k in range(8):
                t = types[k]
                if t == 0xFFFF or t >= 10:
                    continue
                off = offs[k]
                if off == 0 or off == 0xFFFF:
                    continue
                p = tp_vals[t]
                sset = p[0]
                if sset == 0xFFFF:
                    continue
                tx = off % MAP_W
                ty = off // MAP_W
                if ty >= MAP_H:
                    continue
                spawns += struct.pack('>HHBBBBBBBB',
                                      tx, ty,
                                      sset & 0xFF,
                                      p[1] & 0xFF,          # health
                                      p[11] & 0xFF,         # behaviour
                                      1 if p[7] else 0,     # shootProjectiles
                                      p[2] & 0xFF,          # projectile h speed
                                      p[3] & 0xFF,          # projectile v speed
                                      p[5] & 0xFF,          # targetPlayer
                                      i & 0xFF)
                nspawn += 1
        pk.add('LVEN%d' % lv, bytes(spawns))

        # messages: 10 x (x,y,10 lines x 50 chars)
        ms_raw = lump(2)
        ms = bytearray()
        for i in range(10):
            e = ms_raw[i * 504:(i + 1) * 504]
            x, y = struct.unpack('<2H', e[0:4])
            ms += struct.pack('>HH', x, y) + e[4:504]

        pk.add('LVBG%d' % lv, bglayer)
        pk.add('LVFG%d' % lv, maplayer)
        pk.add('LVAD%d' % lv, addlayer)
        pk.add('LVEV%d' % lv, bytes(ev8))
        pk.add('LVTA%d' % lv, tinfo)
        pk.add('LVTEL%d' % lv, tel)
        pk.add('LVSW%d' % lv, bytes(swi))
        pk.add('LVIN%d' % lv, ins)
        pk.add('LVKH%d' % lv, kh)
        pk.add('LVTP%d' % lv, bytes(tp))
        pk.add('LVET%d' % lv, bytes(et))
        pk.add('LVMS%d' % lv, bytes(ms))

        lvl_index += struct.pack('>HHHHHH',
                                 px_, py_, shoot,
                                 limits[lv] if lv < len(limits) else 300,
                                 tilesets[lv], bgs[lv])
    pk.add('LEVELS', bytes(lvl_index))

    blob = pk.build()
    with open(out_path, 'wb') as f:
        f.write(blob)
    print('wrote %s: %d bytes, %d lumps' % (out_path, len(blob), len(pk.lumps)))
    print('  tilesets used: %s' % (used_ts,))
    print('  backgrounds used: %s' % (used_bg,))
    return 0


if __name__ == '__main__':
    sys.exit(main())

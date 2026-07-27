/*
 * Hocus Pocus 32X - software renderer.
 *
 * Draws straight into the 32X 8bpp packed-pixel framebuffer.  Layout:
 *   0x000..0x1FF  line table (256 entries, word offsets into the FB)
 *   0x200..       pixel data, 320 bytes per line
 *
 * Everything is index-based; the CRAM holds the game palette in 0..127 and
 * the current level's background palette in 128..255, mirroring how the DOS
 * original partitioned VGA palette space.
 *
 * Performance notes
 * -----------------
 * The SH-2 is slow and, worse, every byte write to the framebuffer is a
 * separate bus transaction.  Three hardware properties are exploited here:
 *
 *  1. 32-bit writes move four pixels at a time.  The framebuffer is only
 *     16-bit aligned in general, but because the playfield is drawn on a
 *     16px tile grid at 320 bytes/line, tile columns land on 4-byte
 *     boundaries whenever the camera x is a multiple of 4.  We keep the
 *     camera 4-aligned for drawing and shift the whole scene with the VDP
 *     line table instead (see R_ScrollFine), so the fast path is always hit.
 *
 *  2. The VDP exposes the frame buffer a second time at 0x24020000 as the
 *     "overwrite" region: bytes written as zero are discarded by hardware.
 *     That makes index-0 transparency free - no per-pixel test, no read,
 *     no branch - so masked tiles and sprites blit as fast as opaque ones.
 *
 *  3. Most tiles are entirely opaque or entirely empty (the converter tags
 *     each one).  Empty tiles are skipped outright, opaque tiles go through
 *     the plain region, and only genuinely mixed tiles use the overwrite
 *     region, which keeps the write count minimal.
 */

#include "hocus.h"

const uint8_t *tilegfx;
const uint8_t *tilecls;
const uint8_t *bggfx;
const uint8_t *lay_bg;
const uint8_t *lay_fg;
const uint8_t *lay_ad;
uint8_t        lay_ev[MAP_W * MAP_H];
uint8_t        fg_removed[MAP_W * MAP_H / 8];
uint8_t        bg_removed[MAP_W * MAP_H / 8];
const uint8_t *hudgfx;
uint16_t       hudw, hudh;
const uint8_t *fontwidths;
const uint8_t *fontglyphs;
sprmeta_t      sprmeta;
const uint8_t *sprdata;
uint8_t        palette[768];

/* Framebuffer helpers ----------------------------------------------------- */

/* Plain framebuffer: writes land unconditionally. */
uint8_t *I_FrameBuffer(void)
{
    return (uint8_t *)&MARS_FRAMEBUFFER + 0x200;
}

/*
 * Overwrite alias of the same framebuffer.  Bytes written as 0 are dropped by
 * the VDP, giving free colour-0 transparency.
 */
static inline uint8_t *I_FrameBufferOW(void)
{
    return (uint8_t *)&MARS_OVERWRITE_IMG + 0x200;
}

void R_SetPalette(void)
{
    Mars_SetPalette(palette);
}

/* 32-bit block copy; count is in longs.  Unrolled 4x to cut loop overhead. */
static inline void copy_longs(uint32_t *d, const uint32_t *s, int n)
{
    while (n >= 4) {
        d[0] = s[0];
        d[1] = s[1];
        d[2] = s[2];
        d[3] = s[3];
        d += 4;
        s += 4;
        n -= 4;
    }
    while (n--)
        *d++ = *s++;
}

static inline void copy_bytes(uint8_t *d, const uint8_t *s, int n)
{
    while (n--)
        *d++ = *s++;
}

/*
 * Copy a run of pixels, using 32-bit transfers for the aligned middle.
 * Used for the background, which is opaque so no masking is needed.
 */
static void copy_run(uint8_t *dst, const uint8_t *src, int n)
{
    /* align destination to 4 bytes */
    while (n > 0 && ((uintptr_t)dst & 3)) {
        *dst++ = *src++;
        n--;
    }
    if (n >= 4 && !((uintptr_t)src & 3)) {
        int longs = n >> 2;
        copy_longs((uint32_t *)dst, (const uint32_t *)src, longs);
        dst += longs << 2;
        src += longs << 2;
        n -= longs << 2;
    }
    copy_bytes(dst, src, n);
}

static inline int tile_is_removed(const uint8_t *mask, int i)
{
    return (mask[i >> 3] >> (i & 7)) & 1;
}

/*
 * Occlusion map for the current camera position.
 *
 * A 16x16 cell of the parallax background is invisible if the background tile
 * layer has a fully-opaque tile in that cell (60-90% of cells in the shipped
 * levels).  Skipping those cells is the single biggest saving in the frame,
 * because the parallax bitmap is otherwise the only full-screen fill we do.
 *
 * Cells are one byte each: 1 = covered, skip it.
 */
#define OCC_W ((SCREEN_W / TILE) + 2)       /* 22 */
#define OCC_H ((VIEW_H / TILE) + 2)         /* 12 */
static uint8_t occ[OCC_H][OCC_W];
static int occ_tx0, occ_ty0;

static void R_BuildOcclusion(void)
{
    int cy, cx;

    occ_tx0 = camx >> 4;
    occ_ty0 = camy >> 4;

    for (cy = 0; cy < OCC_H; cy++) {
        int ty = occ_ty0 + cy;
        const uint8_t *row;
        if (ty < 0 || ty >= MAP_H) {
            for (cx = 0; cx < OCC_W; cx++)
                occ[cy][cx] = 0;
            continue;
        }
        row = lay_bg + ty * MAP_W;
        for (cx = 0; cx < OCC_W; cx++) {
            int tx = occ_tx0 + cx;
            int t;
            if (tx < 0 || tx >= MAP_W) {
                occ[cy][cx] = 0;
                continue;
            }
            t = row[tx];
            occ[cy][cx] = (t != TILE_EMPTY && tilecls && tilecls[t] == 1 &&
                           !tile_is_removed(bg_removed, ty * MAP_W + tx));
        }
    }
}

/*
 * Parallax background: scrolls at half the camera speed and wraps on the
 * 320x200 source image, exactly like the original.
 *
 * Drawn in 16-pixel-tall bands so each band can consult the occlusion map and
 * skip the horizontal spans that opaque background tiles will cover anyway.
 */
static void R_DrawBackground(uint8_t *fb, int ylo, int yhi)
{
    int bx = (camx >> 1) % 320;
    int by = (camy >> 1) % 200;
    int cy;
    int ytile0 = camy & 15;         /* how far into the first cell row we are */

    if (bx < 0) bx += 320;
    if (by < 0) by += 200;

    for (cy = 0; cy < OCC_H; cy++) {
        int band_y0 = cy * TILE - ytile0;
        int band_y1 = band_y0 + TILE;
        int y;

        if (band_y1 <= ylo)
            continue;
        if (band_y0 >= yhi)
            break;
        if (band_y0 < ylo) band_y0 = ylo;
        if (band_y1 > yhi) band_y1 = yhi;

        for (y = band_y0; y < band_y1; y++) {
            uint8_t *dstrow = fb + (VIEW_TOP + y) * SCREEN_W;
            int sy = by + y;
            const uint8_t *src;
            int cx = 0;
            int xtile0 = camx & 15;

            if (sy >= 200) sy -= 200;
            src = bggfx + sy * 320;

            /* walk the cell columns, coalescing runs of visible cells */
            while (cx < OCC_W) {
                int x0, x1, run;

                if (occ[cy][cx]) {
                    cx++;
                    continue;
                }
                run = cx;
                while (run < OCC_W && !occ[cy][run])
                    run++;

                x0 = cx * TILE - xtile0;
                x1 = run * TILE - xtile0;
                if (x0 < 0) x0 = 0;
                if (x1 > SCREEN_W) x1 = SCREEN_W;

                if (x1 > x0) {
                    int n = x1 - x0;
                    int sx = bx + x0;
                    while (sx >= 320) sx -= 320;
                    if (sx + n <= 320) {
                        copy_run(dstrow + x0, src + sx, n);
                    } else {
                        int first = 320 - sx;
                        copy_run(dstrow + x0, src + sx, first);
                        copy_run(dstrow + x0 + first, src, n - first);
                    }
                }
                cx = run + 1;
            }
        }
    }
}

/*
 * Blit one 16x16 tile.
 *
 * `sx` is guaranteed 4-aligned by the caller (camera is kept 4-aligned), so
 * every row is four 32-bit stores.  Opaque tiles go to the plain framebuffer,
 * mixed tiles to the overwrite alias where the VDP drops the zero bytes.
 */
static inline void blit_tile_full(uint8_t *dst, const uint8_t *src, int rows)
{
    int row;
    for (row = 0; row < rows; row++) {
        const uint32_t *s = (const uint32_t *)src;
        uint32_t *d = (uint32_t *)dst;
        d[0] = s[0];
        d[1] = s[1];
        d[2] = s[2];
        d[3] = s[3];
        dst += SCREEN_W;
        src += TILE;
    }
}

/* Partially clipped tile: byte loop, only used at the screen edges. */
static void blit_tile_clipped(uint8_t *fb, const uint8_t *src, int sx, int sy,
                              int r0, int r1, int opaque)
{
    int row;
    int c0 = 0, c1 = TILE;

    if (sx < 0) c0 = -sx;
    if (sx + TILE > SCREEN_W) c1 = SCREEN_W - sx;
    if (c1 <= c0)
        return;

    for (row = r0; row < r1; row++) {
        uint8_t *dst = fb + (VIEW_TOP + sy + row) * SCREEN_W + sx;
        const uint8_t *s = src + row * TILE;
        int i;
        if (opaque) {
            copy_bytes(dst + c0, s + c0, c1 - c0);
        } else {
            for (i = c0; i < c1; i++) {
                uint8_t v = s[i];
                if (v)
                    dst[i] = v;
            }
        }
    }
}

static void R_DrawLayer(const uint8_t *layer, const uint8_t *removed, int ylo, int yhi)
{
    uint8_t *fb = I_FrameBuffer();
    uint8_t *fbow = I_FrameBufferOW();
    int tx, ty;
    int tx0 = camx >> 4;
    int ty0 = (camy + ylo) >> 4;
    int tx1 = (camx + SCREEN_W + TILE - 1) >> 4;
    int ty1 = (camy + yhi + TILE - 1) >> 4;

    if (tx0 < 0) tx0 = 0;
    if (ty0 < 0) ty0 = 0;
    if (tx1 > MAP_W) tx1 = MAP_W;
    if (ty1 > MAP_H) ty1 = MAP_H;

    for (ty = ty0; ty < ty1; ty++) {
        int sy = (ty << 4) - camy;
        int r0 = 0, r1 = TILE;
        int clipy;
        const uint8_t *rowbase = layer + ty * MAP_W;
        int rowidx = ty * MAP_W;

        if (sy < ylo) r0 = ylo - sy;
        if (sy + TILE > yhi) r1 = yhi - sy;
        if (r1 <= r0)
            continue;
        clipy = (r0 != 0 || r1 != TILE);

        for (tx = tx0; tx < tx1; tx++) {
            int t = rowbase[tx];
            int sx, cls;
            const uint8_t *src;

            if (t == TILE_EMPTY)
                continue;
            if (removed && tile_is_removed(removed, rowidx + tx))
                continue;

            cls = tilecls ? tilecls[t] : 2;
            if (cls == 0)               /* fully transparent tile */
                continue;

            sx = (tx << 4) - camx;
            src = tilegfx + (t << 8);

            if (clipy || sx < 0 || sx + TILE > SCREEN_W) {
                blit_tile_clipped(fb, src, sx, sy, r0, r1, cls == 1);
                continue;
            }

            /* fast path: fully visible, 4-aligned */
            if (cls == 1)
                blit_tile_full(fb + (VIEW_TOP + sy) * SCREEN_W + sx,
                               src, TILE);
            else
                blit_tile_full(fbow + (VIEW_TOP + sy) * SCREEN_W + sx,
                               src, TILE);
        }
    }
}

/*
 * Sprites always use the overwrite region, so transparency costs nothing.
 * The x position is arbitrary, so we only use 32-bit stores when the run
 * happens to be aligned; otherwise bytes (sprites are small).
 */
static void R_DrawSprite(const sprmeta_t *m, int frame, int dir,
                         int wx, int wy, int ylo, int yhi)
{
    uint32_t off;
    const uint8_t *src;
    uint8_t *fbow = I_FrameBufferOW();
    int sx, sy, row, r0, r1, c0, c1, w;

    if (frame < 0 || frame >= 15)
        return;
    off = m->frame[dir * 15 + frame];
    if (off == 0xFFFFFFFF)
        return;

    src = sprdata + off;
    w = m->w;
    sx = wx - camx;
    sy = wy - camy;

    if (sx + w <= 0 || sx >= SCREEN_W)
        return;
    if (sy + (int)m->h <= ylo || sy >= yhi)
        return;

    r0 = 0;
    r1 = m->h;
    if (sy < ylo) r0 = ylo - sy;
    if (sy + (int)m->h > yhi) r1 = yhi - sy;

    c0 = 0;
    c1 = w;
    if (sx < 0) c0 = -sx;
    if (sx + w > SCREEN_W) c1 = SCREEN_W - sx;
    if (c1 <= c0)
        return;

    for (row = r0; row < r1; row++) {
        uint8_t *dst = fbow + (VIEW_TOP + sy + row) * SCREEN_W + sx + c0;
        const uint8_t *s = src + row * w + c0;
        int n = c1 - c0;

        /* zero bytes are dropped by the VDP, so a straight copy is correct */
        if (!(((uintptr_t)dst | (uintptr_t)s) & 3)) {
            int longs = n >> 2;
            copy_longs((uint32_t *)dst, (const uint32_t *)s, longs);
            dst += longs << 2;
            s += longs << 2;
            n -= longs << 2;
        }
        copy_bytes(dst, s, n);
    }
}

/* Font ------------------------------------------------------------------- */

static const char fontchars[] =
    "!\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
    "abcdefghijklmnopqrstuvwxyz";

/* index by ASCII for O(1) glyph lookup instead of scanning the table */
static uint8_t fontlut[128];

static void R_BuildFontLut(void)
{
    int i;
    for (i = 0; i < 128; i++)
        fontlut[i] = 0xFF;
    for (i = 0; fontchars[i]; i++)
        fontlut[(int)(unsigned char)fontchars[i]] = (uint8_t)i;
}

void R_DrawString(int x, int y, const char *s, int colour)
{
    uint8_t *fb = I_FrameBuffer();

    while (*s) {
        unsigned char c = (unsigned char)*s++;
        int idx;

        if (c == ' ') {
            x += 4;
            continue;
        }
        idx = (c < 128) ? fontlut[c] : 0xFF;
        if (idx == 0xFF) {
            x += 4;
            continue;
        }
        {
            const uint8_t *g = fontglyphs + idx * 64;
            int gy, gx;
            for (gy = 0; gy < 8; gy++) {
                int dy = y + gy;
                uint8_t *dst;
                if (dy < 0 || dy >= SCREEN_H)
                    continue;
                dst = fb + dy * SCREEN_W;
                for (gx = 0; gx < 8; gx++) {
                    int dx = x + gx;
                    if (dx < 0 || dx >= SCREEN_W)
                        continue;
                    if (g[gy * 8 + gx])
                        dst[dx] = (uint8_t)colour;
                }
            }
            x += fontwidths[idx];
        }
    }
}

/* HUD -------------------------------------------------------------------- */

static void R_DrawNumber(int x, int y, int value, int digits, int colour)
{
    char buf[12];
    int i;

    for (i = digits - 1; i >= 0; i--) {
        buf[i] = '0' + (value % 10);
        value /= 10;
    }
    buf[digits] = 0;
    R_DrawString(x, y, buf, colour);
}

/*
 * Palette index 88 is (252,244,244) - near-white.  The original choice of 15
 * is (56,28,28), which is all but invisible against the dark HUD plate, so
 * the score and crystal counters looked permanently stuck at zero.
 */
#define HUD_TEXT_COLOUR 88

/*
 * The HUD plate itself never changes, so it is only blitted when something
 * marks it dirty (level change, or the first frame).  The numbers on top are
 * redrawn only when their values change.
 */
static int hud_dirty = 1;
static int hud_last_score = -1, hud_last_health = -1;
static int hud_last_crystals = -1, hud_last_level = -1;
static int hud_val_pending = 2;

void R_DirtyHud(void)
{
    hud_dirty = 1;
    hud_val_pending = 2;
    hud_last_score = hud_last_health = -1;
    hud_last_crystals = hud_last_level = -1;
}

static void R_DrawHud(uint8_t *fb)
{
    int y;
    int top = VIEW_TOP + VIEW_H;
    int redraw_vals = 0;

    if (hud_dirty) {
        for (y = 0; y < HUD_H && y < (int)hudh; y++) {
            uint8_t *dst = fb + (top + y) * SCREEN_W;
            const uint8_t *src = hudgfx + y * hudw;
            int n = hudw < SCREEN_W ? hudw : SCREEN_W;
            copy_run(dst, src, n);
        }
        hud_dirty = 0;
        redraw_vals = 1;
    }

    /*
     * The values changed, or they changed recently and this framebuffer has
     * not caught up yet.  With double buffering a single repaint only updates
     * one of the two buffers, so a change has to be applied twice - otherwise
     * every other displayed frame shows the stale number and the HUD appears
     * frozen.
     */
    if (player.score != hud_last_score ||
        player.health != hud_last_health ||
        player.crystals != hud_last_crystals ||
        curlevel != hud_last_level) {
        hud_val_pending = 2;
        hud_last_score = player.score;
        hud_last_health = player.health;
        hud_last_crystals = player.crystals;
        hud_last_level = curlevel;
    }

    if (redraw_vals || hud_val_pending > 0) {
        /* repaint just the number strips from the HUD bitmap, then the text */
        if (!redraw_vals) {
            int ry;
            for (ry = 18; ry < 32 && ry < (int)hudh; ry++) {
                uint8_t *dst = fb + (top + ry) * SCREEN_W;
                const uint8_t *src = hudgfx + ry * hudw;
                int n = hudw < SCREEN_W ? hudw : SCREEN_W;
                copy_run(dst, src, n);
            }
        }
        R_DrawNumber(18, top + 22, player.score, 7, HUD_TEXT_COLOUR);
        R_DrawNumber(85, top + 22, player.health, 2, HUD_TEXT_COLOUR);
        R_DrawNumber(143, top + 22, player.crystals, 2, HUD_TEXT_COLOUR);
        R_DrawNumber(285, top + 22, curlevel + 1, 2, HUD_TEXT_COLOUR);

        if (hud_val_pending > 0)
            hud_val_pending--;
    }
}

/*
 * The HUD lives in both framebuffers, so after a flip the other buffer still
 * has last frame's HUD.  Track how many buffers still need a full repaint.
 */
static int hud_repaint_pending = 2;

/* Frame ------------------------------------------------------------------ */

/*
 * Render one horizontal band of the playfield.  Both SH-2s call this with
 * disjoint [ylo,yhi) ranges, so they never write the same bytes.
 */
void R_DrawBand(int ylo, int yhi)
{
    uint8_t *fb = I_FrameBuffer();
    int i;

    if (yhi <= ylo)
        return;

    R_DrawBackground(fb, ylo, yhi);
    R_DrawLayer(lay_bg, bg_removed, ylo, yhi);
    R_DrawLayer(lay_fg, fg_removed, ylo, yhi);
    R_DrawLayer(lay_ad, 0, ylo, yhi);

    /* enemies */
    for (i = 0; i < MAX_ENEMIES; i++) {
        const sprmeta_t *m;
        int frame;

        if (!enemies[i].active)
            continue;
        /* flash off every other tic just after being hit */
        if (enemies[i].hurttic && (gametic & 1))
            continue;

        m = &esprmeta[i];
        if (enemies[i].shoottic && m->shootb != 0xFFFF)
            frame = m->shootb;
        else if (m->walke >= m->walkb && m->walkb != 0xFFFF)
            frame = m->walkb + enemies[i].animframe;
        else
            frame = m->stand;

        R_DrawSprite(m, frame, enemies[i].dir,
                     FIX2INT(enemies[i].x), FIX2INT(enemies[i].y), ylo, yhi);
    }

    /* enemy projectiles */
    for (i = 0; i < MAX_EBULLETS; i++) {
        int s;
        if (!ebullets[i].active)
            continue;
        s = ebullets[i].spriteset;
        /* reuse the owning enemy's metadata if it is still resident */
        {
            const sprmeta_t *m = NULL;
            int k;
            for (k = 0; k < MAX_ENEMIES; k++) {
                if (enemies[k].active && enemies[k].spriteset == s) {
                    m = &esprmeta[k];
                    break;
                }
            }
            if (!m || m->proj == 0xFFFF)
                continue;
            R_DrawSprite(m, m->proj, ebullets[i].dir,
                         FIX2INT(ebullets[i].x), FIX2INT(ebullets[i].y),
                         ylo, yhi);
        }
    }

    /* player projectiles */
    for (i = 0; i < MAX_SHOTS; i++) {
        if (!shots[i].active)
            continue;
        R_DrawSprite(&sprmeta, sprmeta.proj, shots[i].dir,
                     FIX2INT(shots[i].x), FIX2INT(shots[i].y), ylo, yhi);
    }

    /* player - blink while invulnerable */
    if (!(player.invuln && (gametic & 2))) {
        int frame;
        switch (player.state) {
        case ST_JUMP:  frame = sprmeta.jump; break;
        case ST_FALL:  frame = sprmeta.fall; break;
        case ST_WALK:  frame = sprmeta.walkb + player.walkframe; break;
        default:       frame = sprmeta.stand; break;
        }
        if (player.shoottic)
            frame = sprmeta.shootb;
        R_DrawSprite(&sprmeta, frame, player.dir,
                     FIX2INT(player.x), FIX2INT(player.y), ylo, yhi);
    }
}

void R_DrawFrame(void)
{
    uint8_t *fb = I_FrameBuffer();

    R_BuildOcclusion();

    /*
     * Hand the lower half of the playfield to the slave SH-2 and draw the
     * upper half here, then wait for it.  The split point is on a tile row so
     * neither CPU touches the other's scanlines.
     */
    Mars_R_BeginDrawBand(SPLIT_Y, VIEW_H);
    R_DrawBand(0, SPLIT_Y);
    Mars_R_EndDrawBand();

    if (hud_repaint_pending > 0) {
        hud_dirty = 1;
        hud_repaint_pending--;
    }
    R_DrawHud(fb);
}

void R_HudRepaintBothBuffers(void)
{
    hud_repaint_pending = 2;
    R_DirtyHud();
}

void R_Init(void)
{
    uint32_t len;
    const uint8_t *p;

    R_BuildFontLut();

    p = HP_Find("PAL_GAME", &len);
    if (p) {
        uint32_t i;
        for (i = 0; i < 384 && i < len; i++)
            palette[i] = p[i];
    }

    p = HP_Find("HUD", &len);
    if (p) {
        hudw = ((uint16_t)p[0] << 8) | p[1];
        hudh = ((uint16_t)p[2] << 8) | p[3];
        hudgfx = p + 4;
    }

    p = HP_Find("FONT", &len);
    if (p) {
        fontwidths = p;
        fontglyphs = p + 90;
    }

    /* player sprite metadata (record 0) */
    R_LoadSpriteMeta(0, &sprmeta);
    sprdata = HP_Find("SPRDATA", &len);
}

/*
 * Decode one sprite record out of the SPRMETA lump.  Records are 140 bytes:
 * ten u16 fields followed by 30 u32 frame offsets (15 east, 15 west).
 * Returns 0 if the index is out of range.
 */
#define SPRMETA_REC 140

static const uint8_t *sprmeta_lump;     /* cached: HP_Find scans all lumps */

int R_LoadSpriteMeta(int index, sprmeta_t *out)
{
    uint32_t len;
    const uint8_t *p = sprmeta_lump;
    const uint8_t *r;
    int i, n;

    if (!p) {
        p = sprmeta_lump = HP_Find("SPRMETA", &len);
    }
    if (!p || index < 0)
        return 0;
    n = ((uint16_t)p[0] << 8) | p[1];
    if (index >= n)
        return 0;

    r = p + 2 + index * SPRMETA_REC;
    out->w      = ((uint16_t)r[0] << 8) | r[1];
    out->h      = ((uint16_t)r[2] << 8) | r[3];
    out->stand  = ((uint16_t)r[4] << 8) | r[5];
    out->walkb  = ((uint16_t)r[6] << 8) | r[7];
    out->walke  = ((uint16_t)r[8] << 8) | r[9];
    out->jump   = ((uint16_t)r[10] << 8) | r[11];
    out->fall   = ((uint16_t)r[12] << 8) | r[13];
    out->shootb = ((uint16_t)r[14] << 8) | r[15];
    out->shoote = ((uint16_t)r[16] << 8) | r[17];
    out->proj   = ((uint16_t)r[18] << 8) | r[19];
    for (i = 0; i < 30; i++) {
        const uint8_t *q = r + 20 + i * 4;
        out->frame[i] = ((uint32_t)q[0] << 24) | ((uint32_t)q[1] << 16) |
                        ((uint32_t)q[2] << 8) | q[3];
    }
    return 1;
}

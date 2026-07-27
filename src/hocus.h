/*
 * Hocus Pocus 32X - shared definitions
 *
 * Port of Hocus Pocus (Apogee, 1994) to the Sega 32X.
 * Engine logic derived from OpenPocus (GPLv2, A. Roldan);
 * 32X hardware layer derived from Doom 32X Resurrection (MIT, V. Luchits).
 */

#ifndef _HOCUS_H
#define _HOCUS_H

#include <stdint.h>
#include <stddef.h>

#include "32x.h"
#include "marshw.h"

/* ------------------------------------------------------------------ types */
typedef uint8_t byte;
typedef int32_t fixed_t;      /* 16.16 fixed point */

#define FRACBITS 16
#define FRACUNIT (1 << FRACBITS)
#define FIXED(x) ((fixed_t)((x) * FRACUNIT))
#define FIX2INT(x) ((int)((x) >> FRACBITS))

/* ------------------------------------------------------------------ screen */
#define SCREEN_W    320
#define SCREEN_H    224     /* 32X framebuffer height we request */
#define GAME_W      320
#define GAME_H      200     /* original game resolution */
#define HUD_H       40
#define VIEW_H      (GAME_H - HUD_H)    /* 160 playfield lines */
#define VIEW_TOP    ((SCREEN_H - GAME_H) / 2)   /* letterbox offset = 12 */

/* ------------------------------------------------------------------ world */
#define MAP_W       240
#define MAP_H       60
#define TILE        16
#define TILES_PER_SET 240
#define TILE_EMPTY  0xFF

#define LEVELS      9

/* ------------------------------------------------------------------ input */
#define BTN_UP      0x0001
#define BTN_DOWN    0x0002
#define BTN_LEFT    0x0004
#define BTN_RIGHT   0x0008
#define BTN_A       0x0010
#define BTN_B       0x0020
#define BTN_C       0x0040
#define BTN_START   0x0080
#define BTN_X       0x0100
#define BTN_Y       0x0200
#define BTN_Z       0x0400
#define BTN_MODE    0x0800

/* ------------------------------------------------------------- asset pack */
typedef struct {
    const uint8_t *base;
    uint16_t version;
    uint16_t count;
    uint32_t datastart;
} hpak_t;

typedef struct {
    uint16_t x, y, shootdelay, limit, tileset, background;
} levelinfo_t;

typedef struct {
    uint16_t w, h;
    uint16_t stand, walkb, walke, jump, fall, shootb, shoote, proj;
    uint32_t frame[30];      /* 0..14 east, 15..29 west; 0xFFFFFFFF = absent */
} sprmeta_t;

/* events found in the event layer */
enum {
    EV_RUBY = 0x00, EV_DIAMOND, EV_GLOBET, EV_CROWN, EV_HEALPOTION,
    EV_CRYSTAL, EV_SPIKES, EV_ZAPPER, EV_INVISIBILITY, EV_GOLDENPOTION,
    EV_WHITEPOTION, EV_UNUSED1, EV_SILVERKEY, EV_GOLDKEY, EV_LAVA,
    EV_WIZARD, EV_GREYPOTION,
    EV_NONE = 0xFF
};

/* player states */
typedef enum { ST_STAND, ST_WALK, ST_JUMP, ST_FALL } pstate_t;

/*
 * Collision box.  The 24x32 sprite cell is wider than the wizard actually is
 * (there is transparent padding either side, and the hat overhangs), so we
 * collide with a narrower box inset into the cell.  Without this the player
 * snags on 16px tiles in the narrow shafts the original levels are full of.
 */
#define BOX_X0  6       /* inset from the left of the sprite cell */
#define BOX_X1  18      /* exclusive right edge  -> 12px wide */
#define BOX_Y0  4       /* top inset (hat is decorative)        */
#define BOX_Y1  32      /* exclusive bottom edge -> 28px tall   */
#define BOX_W   (BOX_X1 - BOX_X0)
#define BOX_H   (BOX_Y1 - BOX_Y0)

typedef struct {
    fixed_t x, y;
    fixed_t vy;
    int8_t  dir;            /* 0 = east/right, 1 = west/left */
    pstate_t state;
    int     health;
    int     score;
    int     crystals;
    int     keys;           /* bit0 silver, bit1 gold */
    int     walkframe;
    int     walktic;
    int     invuln;
    int     shoottic;
} player_t;

typedef struct {
    int      active;
    fixed_t  x, y;
    fixed_t  vx;
    int      dir;
    int      life;
} shot_t;

#define MAX_SHOTS 4

/* ------------------------------------------------------------- enemies --- */
/*
 * Enemies are spawned from the level's flattened LVEN lump when they come
 * near the camera, and retired again when they drift far off screen, so only
 * a handful are ever live at once.
 */
#define MAX_ENEMIES     12
#define MAX_EBULLETS    8

/* behaviour values taken from the level tile-property table */
enum {
    EB_WALKER = 0,      /* paces back and forth on its ledge      */
    EB_HOPPER = 1,      /* walks and hops periodically            */
    EB_TURRET = 2       /* stays put, fires downward/at the player */
};

typedef struct {
    uint16_t tx, ty;
    uint8_t  spriteset;
    uint8_t  health;
    uint8_t  behaviour;
    uint8_t  shoots;
    uint8_t  projh, projv;
    uint8_t  target;
    uint8_t  group;
} espawn_t;

typedef struct {
    int      active;
    int      spawnidx;      /* index into the level spawn list, -1 if none */
    fixed_t  x, y;
    fixed_t  vx, vy;
    int      dir;
    int      health;
    uint8_t  behaviour;
    uint8_t  shoots;
    uint8_t  spriteset;
    int      animtic;
    int      animframe;
    int      shoottic;
    int      hurttic;       /* flashes when damaged */
    int      onground;
} enemy_t;

typedef struct {
    int      active;
    fixed_t  x, y;
    fixed_t  vx, vy;
    int      dir;
    int      life;
    uint8_t  spriteset;
} ebullet_t;

/* --------------------------------------------------------------- globals */
extern hpak_t       hpak;
extern player_t     player;
extern shot_t       shots[MAX_SHOTS];
extern enemy_t      enemies[MAX_ENEMIES];
extern ebullet_t    ebullets[MAX_EBULLETS];
extern const uint8_t *enemyspawns;
extern int          numspawns;
extern uint8_t      spawn_dead[512];
extern int          curlevel;
extern int          camx, camy;
extern int          gametic;
extern levelinfo_t  levelinfo;
extern uint8_t      palette[768];

extern const uint8_t *tilegfx;      /* 240 * 256 bytes */
extern const uint8_t *tilecls;      /* per-tile opacity class: 0 empty, 1 opaque, 2 mixed */
extern const uint8_t *bggfx;        /* 320 * 200 */
extern const uint8_t *lay_bg;
extern const uint8_t *lay_fg;
extern const uint8_t *lay_ad;
extern uint8_t        lay_ev[MAP_W * MAP_H];
extern uint8_t        fg_removed[MAP_W * MAP_H / 8];
extern uint8_t        bg_removed[MAP_W * MAP_H / 8];
extern const uint8_t *hudgfx;
extern uint16_t       hudw, hudh;
extern const uint8_t *fontwidths;
extern const uint8_t *fontglyphs;
extern sprmeta_t      sprmeta;      /* player sprite */
extern sprmeta_t      esprmeta[MAX_ENEMIES];  /* metadata for live enemies */
extern const uint8_t *sprdata;

/* ------------------------------------------------------- dual-SH2 renderer */
/*
 * The playfield is split between the two SH-2s on a tile-row boundary.
 * VIEW_H is 160, so 80 gives each CPU five 16px tile rows.
 */
#define SPLIT_Y  80

/* commands passed to the slave through MARS_SYS_COMM4 */
enum {
    SECCMD_NONE = 0,
    SECCMD_DRAW_BAND,
    SECCMD_CLEAR_CACHE
};

void  R_DrawBand(int ylo, int yhi);
int   R_LoadSpriteMeta(int index, sprmeta_t *out);
void  Mars_R_BeginDrawBand(int ylo, int yhi);
void  Mars_R_EndDrawBand(void);

/* --------------------------------------------------------------- hpak API */
void  HP_Init(const uint8_t *base);
const uint8_t *HP_Find(const char *name, uint32_t *len);

/* --------------------------------------------------------------- game API */
void  G_LoadLevel(int level);
void  G_Tick(int buttons);
int   G_TileSolid(int tx, int ty);

/* ------------------------------------------------------------- render API */
void  R_Init(void);
void  R_DrawFrame(void);
void  R_SetPalette(void);
void  R_DrawString(int x, int y, const char *s, int colour);
void  R_DirtyHud(void);
void  R_HudRepaintBothBuffers(void);

/* ------------------------------------------------------------ system glue */
void  I_Init(void);
int   I_ReadPad(void);
void  I_Flip(void);
uint8_t *I_FrameBuffer(void);

#endif /* _HOCUS_H */

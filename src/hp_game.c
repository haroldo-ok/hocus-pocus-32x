/*
 * Hocus Pocus 32X - game logic.
 *
 * Movement, collision, camera and item pickup follow OpenPocus' rules, but
 * reworked into integer/fixed-point form (the SH-2 has no FPU worth using
 * here) and driven by a fixed 1/60 s tick instead of a delta time.
 */

#include "hocus.h"

player_t    player;
shot_t      shots[MAX_SHOTS];
enemy_t     enemies[MAX_ENEMIES];
ebullet_t   ebullets[MAX_EBULLETS];
sprmeta_t   esprmeta[MAX_ENEMIES];
const uint8_t *enemyspawns;
int         numspawns;
uint8_t     spawn_dead[512];
int         curlevel;
int         camx, camy;
int         gametic;
levelinfo_t levelinfo;

static const uint8_t *itemtab;

/* Speeds are per tick in 16.16.  ~2 px/tick walk, gravity ramps to 8 px. */
#define WALK_SPEED   FIXED(2)
#define GRAVITY      (FRACUNIT / 4)
#define MAX_FALL     FIXED(8)
#define JUMP_VEL     (-FIXED(5))
#define SHOT_SPEED   FIXED(5)

/* enemy tuning */
#define ENEMY_SPEED     (FRACUNIT / 2)      /* half a pixel per tick     */
#define ENEMY_HOP_VEL   (-FIXED(4))
#define EBULLET_LIFE    150
#define ENEMY_SHOT_MIN  70                  /* ticks between shots       */
#define SPAWN_MARGIN    64                  /* px beyond the view edge   */
#define DESPAWN_MARGIN  200
#define ENEMY_TOUCH_DMG 10
#define ENEMY_HURT_TICS 8

static char namebuf[16];

static const char *lumpname(const char *prefix, int n)
{
    int i = 0;
    while (prefix[i]) {
        namebuf[i] = prefix[i];
        i++;
    }
    if (n >= 10) {
        namebuf[i++] = '0' + (n / 10);
        namebuf[i++] = '0' + (n % 10);
    } else {
        namebuf[i++] = '0' + n;
    }
    namebuf[i] = 0;
    return namebuf;
}

static uint16_t rd16(const uint8_t *p)
{
    return ((uint16_t)p[0] << 8) | p[1];
}

/*
 * A tile blocks movement when the foreground layer has a tile there that has
 * not been shot away.
 */
int G_TileSolid(int tx, int ty)
{
    int i;

    if (tx < 0 || tx >= MAP_W)
        return 1;
    if (ty < 0)
        return 0;
    if (ty >= MAP_H)
        return 1;

    i = ty * MAP_W + tx;
    if (lay_fg[i] == TILE_EMPTY)
        return 0;
    if ((fg_removed[i >> 3] >> (i & 7)) & 1)
        return 0;
    return 1;
}

static void clear_removed(void)
{
    int i;
    for (i = 0; i < (MAP_W * MAP_H) / 8; i++) {
        fg_removed[i] = 0;
        bg_removed[i] = 0;
    }
}

void G_LoadLevel(int level)
{
    uint32_t len;
    const uint8_t *p;
    int i;

    curlevel = level;

    p = HP_Find("LEVELS", &len);
    if (p) {
        const uint8_t *r = p + level * 12;
        levelinfo.x = rd16(r);
        levelinfo.y = rd16(r + 2);
        levelinfo.shootdelay = rd16(r + 4);
        levelinfo.limit = rd16(r + 6);
        levelinfo.tileset = rd16(r + 8);
        levelinfo.background = rd16(r + 10);
    }

    tilegfx = HP_Find(lumpname("TILES", levelinfo.tileset), &len);
    tilecls = HP_Find(lumpname("TCLS", levelinfo.tileset), &len);
    bggfx   = HP_Find(lumpname("BG", levelinfo.background), &len);
    lay_bg  = HP_Find(lumpname("LVBG", level), &len);
    lay_fg  = HP_Find(lumpname("LVFG", level), &len);
    lay_ad  = HP_Find(lumpname("LVAD", level), &len);

    p = HP_Find(lumpname("LVEV", level), &len);
    for (i = 0; i < MAP_W * MAP_H; i++)
        lay_ev[i] = p ? p[i] : EV_NONE;

    /* background palette occupies CRAM entries 128..255 */
    p = HP_Find(lumpname("BGPAL", levelinfo.background), &len);
    if (p) {
        uint32_t k;
        for (k = 0; k < 384 && k < len; k++)
            palette[384 + k] = p[k];
    }

    itemtab = HP_Find("ITEMS", &len);

    /* enemy spawn list for this level */
    enemyspawns = HP_Find(lumpname("LVEN", level), &len);
    numspawns = enemyspawns ? (int)(len / 12) : 0;
    if (numspawns > (int)sizeof(spawn_dead))
        numspawns = (int)sizeof(spawn_dead);
    for (i = 0; i < (int)sizeof(spawn_dead); i++)
        spawn_dead[i] = 0;
    for (i = 0; i < MAX_ENEMIES; i++)
        enemies[i].active = 0;
    for (i = 0; i < MAX_EBULLETS; i++)
        ebullets[i].active = 0;

    clear_removed();

    player.x = FIXED(levelinfo.x * TILE);
    player.y = FIXED(levelinfo.y * TILE);
    player.vy = 0;
    player.dir = 0;
    player.state = ST_FALL;
    player.health = 100;
    player.crystals = 0;
    player.keys = 0;
    player.walkframe = 0;
    player.walktic = 0;
    player.invuln = 0;
    player.shoottic = 0;

    for (i = 0; i < MAX_SHOTS; i++)
        shots[i].active = 0;

    camx = camy = 0;
    R_HudRepaintBothBuffers();
    R_SetPalette();
}

static void centre_camera(void)
{
    int px = FIX2INT(player.x);
    int py = FIX2INT(player.y);

    camx = px + BOX_X0 + BOX_W / 2 - SCREEN_W / 2;
    camy = py + BOX_Y0 + BOX_H / 2 - VIEW_H / 2;

    if (camx < 0) camx = 0;
    if (camy < 0) camy = 0;
    if (camx > MAP_W * TILE - SCREEN_W) camx = MAP_W * TILE - SCREEN_W;
    if (camy > MAP_H * TILE - VIEW_H) camy = MAP_H * TILE - VIEW_H;
}

/* Does the player's collision box overlap a solid tile at this position? */
static int box_blocked(fixed_t nx, fixed_t ny)
{
    int x0 = FIX2INT(nx) + BOX_X0;
    int y0 = FIX2INT(ny) + BOX_Y0;
    int x1 = x0 + BOX_W - 1;
    int y1 = y0 + BOX_H - 1;
    int tx, ty;

    if (x0 < 0)
        return 1;

    for (ty = y0 / TILE; ty <= y1 / TILE; ty++)
        for (tx = x0 / TILE; tx <= x1 / TILE; tx++)
            if (G_TileSolid(tx, ty))
                return 1;
    return 0;
}

/* Item table lookups: 6-byte records of score(u16), heal, fire, type, pad. */
static int item_score(int ev)
{
    if (!itemtab)
        return 100;
    return rd16(itemtab + ev * 6);
}

static int item_heal(int ev)
{
    if (!itemtab)
        return 10;
    return itemtab[ev * 6 + 2];
}

/*
 * Collect the item in map cell `i`: clear the event and erase the background
 * tile that draws it.  The occlusion map is rebuilt every frame and consults
 * bg_removed, so the parallax behind the gem reappears automatically.
 */
static void collect_at(int i)
{
    lay_ev[i] = EV_NONE;
    bg_removed[i >> 3] |= (uint8_t)(1 << (i & 7));
}

static void pickup_items(void)
{
    int x0 = FIX2INT(player.x) + BOX_X0;
    int y0 = FIX2INT(player.y) + BOX_Y0;
    int x1 = x0 + BOX_W - 1;
    int y1 = y0 + BOX_H - 1;
    int tx, ty;

    for (ty = y0 / TILE; ty <= y1 / TILE; ty++) {
        if (ty < 0 || ty >= MAP_H)
            continue;
        for (tx = x0 / TILE; tx <= x1 / TILE; tx++) {
            int i;
            uint8_t ev;
            if (tx < 0 || tx >= MAP_W)
                continue;
            i = ty * MAP_W + tx;
            ev = lay_ev[i];
            if (ev == EV_NONE)
                continue;

            /*
             * Collectibles are drawn as tiles in the *background* layer, so
             * picking one up has to erase that tile as well as clearing the
             * event - otherwise the gem stays on screen forever.  This is
             * what OpenPocus' map.removeTile(0, pos) does.
             */
            switch (ev) {
            case EV_CRYSTAL:
                player.crystals++;
                player.score += item_score(ev);
                collect_at(i);
                break;

            case EV_SPIKES:
            case EV_ZAPPER:
            case EV_LAVA:
                /* hazards stay put - they just hurt */
                if (!player.invuln) {
                    player.health -= 10;
                    player.invuln = 60;
                    if (player.health < 0)
                        player.health = 0;
                }
                break;

            case EV_HEALPOTION:
                player.health += item_heal(ev);
                if (player.health > 100)
                    player.health = 100;
                collect_at(i);
                break;

            case EV_SILVERKEY:
                player.keys |= 1;
                collect_at(i);
                break;

            case EV_GOLDKEY:
                player.keys |= 2;
                collect_at(i);
                break;

            case EV_WIZARD:
                /* the wizard note simply vanishes when touched */
                collect_at(i);
                break;

            default:
                if (ev <= EV_GREYPOTION) {
                    player.score += item_score(ev);
                    collect_at(i);
                }
                break;
            }
        }
    }
}

static void fire_shot(void)
{
    int i;

    if (player.shoottic)
        return;
    for (i = 0; i < MAX_SHOTS; i++) {
        if (shots[i].active)
            continue;
        shots[i].active = 1;
        shots[i].dir = player.dir;
        shots[i].x = player.x + FIXED((int)sprmeta.w / 2);
        shots[i].y = player.y + FIXED((int)sprmeta.h / 3);
        shots[i].vx = player.dir ? -SHOT_SPEED : SHOT_SPEED;
        shots[i].life = 60;
        player.shoottic = 12;
        return;
    }
}

static void move_shots(void)
{
    int i;

    for (i = 0; i < MAX_SHOTS; i++) {
        int tx, ty;
        if (!shots[i].active)
            continue;
        shots[i].x += shots[i].vx;
        if (--shots[i].life <= 0) {
            shots[i].active = 0;
            continue;
        }
        tx = FIX2INT(shots[i].x) / TILE;
        ty = FIX2INT(shots[i].y) / TILE;
        if (tx < 0 || tx >= MAP_W) {
            shots[i].active = 0;
            continue;
        }
        if (G_TileSolid(tx, ty)) {
            /* shootable tiles vanish */
            int k = ty * MAP_W + tx;
            fg_removed[k >> 3] |= (uint8_t)(1 << (k & 7));
            shots[i].active = 0;
        }
    }
}

/* ======================================================================= */
/*                                enemies                                  */
/* ======================================================================= */

static void read_spawn(int idx, espawn_t *e)
{
    const uint8_t *r = enemyspawns + idx * 12;

    e->tx        = ((uint16_t)r[0] << 8) | r[1];
    e->ty        = ((uint16_t)r[2] << 8) | r[3];
    e->spriteset = r[4];
    e->health    = r[5];
    e->behaviour = r[6];
    e->shoots    = r[7];
    e->projh     = r[8];
    e->projv     = r[9];
    e->target    = r[10];
    e->group     = r[11];
}

static int enemy_solid_at(int px, int py, int w, int h)
{
    int tx, ty;
    int x1 = px + w - 1;
    int y1 = py + h - 1;

    if (px < 0)
        return 1;
    for (ty = py / TILE; ty <= y1 / TILE; ty++)
        for (tx = px / TILE; tx <= x1 / TILE; tx++)
            if (G_TileSolid(tx, ty))
                return 1;
    return 0;
}

/* enemies use a slim box so they sit on ledges the way the originals do */
#define EBOX_INSET 8
#define EBOX_TOP   8

static int enemy_blocked(const enemy_t *en, fixed_t nx, fixed_t ny)
{
    const sprmeta_t *m = &esprmeta[en - enemies];
    int w = (int)m->w - EBOX_INSET * 2;
    int h = (int)m->h - EBOX_TOP;

    if (w < 4) w = 4;
    if (h < 4) h = 4;
    return enemy_solid_at(FIX2INT(nx) + EBOX_INSET, FIX2INT(ny) + EBOX_TOP,
                          w, h);
}

static void spawn_enemy(int spawnidx, const espawn_t *sp)
{
    int i;

    for (i = 0; i < MAX_ENEMIES; i++) {
        enemy_t *en;

        if (enemies[i].active)
            continue;

        en = &enemies[i];
        if (!R_LoadSpriteMeta(sp->spriteset, &esprmeta[i]))
            return;

        en->active    = 1;
        en->spawnidx  = spawnidx;
        en->x         = FIXED(sp->tx * TILE);
        en->y         = FIXED(sp->ty * TILE);
        /* the sprite cell is taller than a tile: sit its feet on the floor */
        en->y        -= FIXED((int)esprmeta[i].h - TILE);
        en->vx        = 0;
        en->vy        = 0;
        en->dir       = (sp->tx & 1) ? 1 : 0;
        en->health    = sp->health ? sp->health : 1;
        en->behaviour = sp->behaviour;
        en->shoots    = sp->shoots;
        en->spriteset = sp->spriteset;
        en->animtic   = 0;
        en->animframe = 0;
        en->shoottic  = ENEMY_SHOT_MIN + (spawnidx * 13) % 90;
        en->hurttic   = 0;
        en->onground  = 0;
        return;
    }
}

/* Bring nearby spawns to life and retire ones that have drifted away. */
static void update_spawns(void)
{
    int i;
    int vx0 = camx - SPAWN_MARGIN;
    int vx1 = camx + SCREEN_W + SPAWN_MARGIN;
    int vy0 = camy - SPAWN_MARGIN;
    int vy1 = camy + VIEW_H + SPAWN_MARGIN;

    for (i = 0; i < numspawns && i < (int)sizeof(spawn_dead); i++) {
        espawn_t sp;
        int px, py, k, live = 0;

        if (spawn_dead[i])
            continue;

        read_spawn(i, &sp);
        px = sp.tx * TILE;
        py = sp.ty * TILE;
        if (px < vx0 || px > vx1 || py < vy0 || py > vy1)
            continue;

        for (k = 0; k < MAX_ENEMIES; k++) {
            if (enemies[k].active && enemies[k].spawnidx == i) {
                live = 1;
                break;
            }
        }
        if (!live)
            spawn_enemy(i, &sp);
    }

    /* retire anything far off screen so its slot can be reused */
    for (i = 0; i < MAX_ENEMIES; i++) {
        int px, py;
        if (!enemies[i].active)
            continue;
        px = FIX2INT(enemies[i].x);
        py = FIX2INT(enemies[i].y);
        if (px < camx - DESPAWN_MARGIN || px > camx + SCREEN_W + DESPAWN_MARGIN ||
            py < camy - DESPAWN_MARGIN || py > camy + VIEW_H + DESPAWN_MARGIN)
            enemies[i].active = 0;
    }
}

static void fire_ebullet(enemy_t *en, const sprmeta_t *m)
{
    int i;

    if (m->proj == 0xFFFF)
        return;

    for (i = 0; i < MAX_EBULLETS; i++) {
        if (ebullets[i].active)
            continue;
        ebullets[i].active = 1;
        ebullets[i].dir = en->dir;
        ebullets[i].spriteset = en->spriteset;
        ebullets[i].x = en->x + FIXED((int)m->w / 2);
        ebullets[i].y = en->y + FIXED((int)m->h / 2);
        if (en->behaviour == EB_TURRET) {
            ebullets[i].vx = 0;
            ebullets[i].vy = FIXED(2);
        } else {
            ebullets[i].vx = en->dir ? -FIXED(2) : FIXED(2);
            ebullets[i].vy = 0;
        }
        ebullets[i].life = EBULLET_LIFE;
        return;
    }
}

static void hurt_player(int dmg)
{
    if (player.invuln)
        return;
    player.health -= dmg;
    if (player.health < 0)
        player.health = 0;
    player.invuln = 60;
}

static int boxes_overlap(int ax, int ay, int aw, int ah,
                         int bx, int by, int bw, int bh)
{
    if (ax + aw <= bx || bx + bw <= ax)
        return 0;
    if (ay + ah <= by || by + bh <= ay)
        return 0;
    return 1;
}

static void move_enemies(void)
{
    int i;

    for (i = 0; i < MAX_ENEMIES; i++) {
        enemy_t *en = &enemies[i];
        const sprmeta_t *m = &esprmeta[i];
        fixed_t nx;

        if (!en->active)
            continue;

        if (en->hurttic)
            en->hurttic--;

        /* --- horizontal movement --- */
        if (en->behaviour != EB_TURRET) {
            nx = en->x + (en->dir ? -ENEMY_SPEED : ENEMY_SPEED);
            if (enemy_blocked(en, nx, en->y)) {
                en->dir ^= 1;               /* bumped a wall, turn round */
            } else {
                /* refuse to walk off the edge of the ledge */
                int footx = FIX2INT(nx) + (en->dir ? EBOX_INSET
                                                   : (int)m->w - EBOX_INSET - 1);
                int footy = FIX2INT(en->y) + (int)m->h;
                if (en->onground && !G_TileSolid(footx / TILE, footy / TILE))
                    en->dir ^= 1;
                else
                    en->x = nx;
            }
        }

        /* --- gravity --- */
        en->vy += GRAVITY;
        if (en->vy > MAX_FALL)
            en->vy = MAX_FALL;
        {
            fixed_t ny = en->y + en->vy;
            if (en->vy > 0) {
                if (enemy_blocked(en, en->x, ny)) {
                    int foot = FIX2INT(ny) + (int)m->h - 1;
                    int ty = foot / TILE;
                    en->y = FIXED(ty * TILE - (int)m->h);
                    en->vy = 0;
                    en->onground = 1;
                } else {
                    en->y = ny;
                    en->onground = 0;
                }
            } else if (en->vy < 0) {
                if (enemy_blocked(en, en->x, ny)) {
                    en->vy = 0;
                } else {
                    en->y = ny;
                }
            }
        }

        /* --- hopping --- */
        if (en->behaviour == EB_HOPPER && en->onground) {
            if (((gametic + i * 17) % 90) == 0) {
                en->vy = ENEMY_HOP_VEL;
                en->onground = 0;
            }
        }

        /* --- animation --- */
        if (m->walke > m->walkb && m->walkb != 0xFFFF) {
            if (++en->animtic >= 8) {
                en->animtic = 0;
                en->animframe++;
                if (en->animframe > (int)(m->walke - m->walkb))
                    en->animframe = 0;
            }
        }

        /* --- shooting --- */
        if (en->shoottic > 0)
            en->shoottic--;
        if (en->shoots && en->shoottic == 0) {
            int px = FIX2INT(player.x);
            int ex = FIX2INT(en->x);
            int dy = FIX2INT(player.y) - FIX2INT(en->y);
            if (dy < 0) dy = -dy;
            /* only fire when roughly level with the player and facing them */
            if (en->behaviour == EB_TURRET || (dy < 48 &&
                ((en->dir && px < ex) || (!en->dir && px > ex)))) {
                fire_ebullet(en, m);
                en->shoottic = ENEMY_SHOT_MIN + ((gametic + i * 7) % 60);
            } else {
                en->shoottic = 20;
            }
        }

        /* --- touching the player hurts --- */
        {
            int ex = FIX2INT(en->x) + EBOX_INSET;
            int ey = FIX2INT(en->y) + EBOX_TOP;
            int ew = (int)m->w - EBOX_INSET * 2;
            int eh = (int)m->h - EBOX_TOP;
            if (ew < 4) ew = 4;
            if (eh < 4) eh = 4;
            if (boxes_overlap(FIX2INT(player.x) + BOX_X0,
                              FIX2INT(player.y) + BOX_Y0, BOX_W, BOX_H,
                              ex, ey, ew, eh))
                hurt_player(ENEMY_TOUCH_DMG);
        }
    }
}

static void move_ebullets(void)
{
    int i;

    for (i = 0; i < MAX_EBULLETS; i++) {
        int bx, by, tx, ty;

        if (!ebullets[i].active)
            continue;

        ebullets[i].x += ebullets[i].vx;
        ebullets[i].y += ebullets[i].vy;
        if (--ebullets[i].life <= 0) {
            ebullets[i].active = 0;
            continue;
        }

        bx = FIX2INT(ebullets[i].x);
        by = FIX2INT(ebullets[i].y);
        tx = bx / TILE;
        ty = by / TILE;
        if (bx < 0 || tx >= MAP_W || ty < 0 || ty >= MAP_H ||
            G_TileSolid(tx, ty)) {
            ebullets[i].active = 0;
            continue;
        }

        if (boxes_overlap(FIX2INT(player.x) + BOX_X0,
                          FIX2INT(player.y) + BOX_Y0, BOX_W, BOX_H,
                          bx, by, 8, 8)) {
            hurt_player(ENEMY_TOUCH_DMG);
            ebullets[i].active = 0;
        }
    }
}

/* Player shots hitting enemies. */
static void check_shot_hits(void)
{
    int s, i;

    for (s = 0; s < MAX_SHOTS; s++) {
        int sx, sy;
        if (!shots[s].active)
            continue;
        sx = FIX2INT(shots[s].x);
        sy = FIX2INT(shots[s].y);

        for (i = 0; i < MAX_ENEMIES; i++) {
            enemy_t *en = &enemies[i];
            const sprmeta_t *m = &esprmeta[i];
            int ex, ey, ew, eh;

            if (!en->active)
                continue;
            ex = FIX2INT(en->x) + EBOX_INSET;
            ey = FIX2INT(en->y) + EBOX_TOP;
            ew = (int)m->w - EBOX_INSET * 2;
            eh = (int)m->h - EBOX_TOP;
            if (ew < 4) ew = 4;
            if (eh < 4) eh = 4;

            if (!boxes_overlap(sx, sy, 12, 8, ex, ey, ew, eh))
                continue;

            shots[s].active = 0;
            en->hurttic = ENEMY_HURT_TICS;
            if (--en->health <= 0) {
                en->active = 0;
                player.score += 200;
                /* stays dead for the rest of the level */
                if (en->spawnidx >= 0 &&
                    en->spawnidx < (int)sizeof(spawn_dead))
                    spawn_dead[en->spawnidx] = 1;
            }
            break;
        }
    }
}

void G_Tick(int buttons)
{
    fixed_t nx;
    int moving = 0;

    gametic++;

    if (player.invuln)
        player.invuln--;
    if (player.shoottic)
        player.shoottic--;

    /* ---- horizontal ---- */
    if (buttons & BTN_LEFT) {
        player.dir = 1;
        moving = 1;
        nx = player.x - WALK_SPEED;
        if (!box_blocked(nx, player.y))
            player.x = nx;
    } else if (buttons & BTN_RIGHT) {
        player.dir = 0;
        moving = 1;
        nx = player.x + WALK_SPEED;
        if (!box_blocked(nx, player.y))
            player.x = nx;
    }

    /* ---- jump ---- */
    if ((buttons & (BTN_A | BTN_C)) &&
        (player.state == ST_STAND || player.state == ST_WALK)) {
        player.vy = JUMP_VEL;
        player.state = ST_JUMP;
    }

    /* ---- shoot ---- */
    if (buttons & BTN_B)
        fire_shot();

    /* ---- vertical ---- */
    player.vy += GRAVITY;
    if (player.vy > MAX_FALL)
        player.vy = MAX_FALL;

    {
        fixed_t ny = player.y + player.vy;
        if (player.vy > 0) {
            if (box_blocked(player.x, ny)) {
                /* land: snap the box's feet to the top of the tile we hit */
                int foot = FIX2INT(ny) + BOX_Y0 + BOX_H - 1;
                int ty = foot / TILE;
                player.y = FIXED(ty * TILE - (BOX_Y0 + BOX_H));
                player.vy = 0;
                player.state = moving ? ST_WALK : ST_STAND;
            } else {
                player.y = ny;
                if (player.state != ST_JUMP)
                    player.state = ST_FALL;
            }
        } else if (player.vy < 0) {
            if (box_blocked(player.x, ny)) {
                /* bonk: snap the box's head to the bottom of the tile above */
                int head = FIX2INT(ny) + BOX_Y0;
                int ty = head / TILE;
                player.y = FIXED((ty + 1) * TILE - BOX_Y0);
                player.vy = 0;
                player.state = ST_FALL;
            } else {
                player.y = ny;
                player.state = ST_JUMP;
            }
        }
    }

    /* grounded state bookkeeping */
    if (player.state == ST_STAND || player.state == ST_WALK) {
        if (!box_blocked(player.x, player.y + FRACUNIT))
            player.state = ST_FALL;
        else
            player.state = moving ? ST_WALK : ST_STAND;
    }

    /* walk animation */
    if (player.state == ST_WALK) {
        if (++player.walktic >= 4) {
            player.walktic = 0;
            player.walkframe++;
            if (sprmeta.walke > sprmeta.walkb) {
                int n = sprmeta.walke - sprmeta.walkb;
                if (player.walkframe >= n)
                    player.walkframe = 0;
            } else {
                player.walkframe = 0;
            }
        }
    } else {
        player.walkframe = 0;
        player.walktic = 0;
    }

    /* keep the player inside the map */
    if (player.x < 0)
        player.x = 0;
    if (FIX2INT(player.y) > MAP_H * TILE) {
        /* fell out of the world - respawn at the level start */
        player.x = FIXED(levelinfo.x * TILE);
        player.y = FIXED(levelinfo.y * TILE);
        player.vy = 0;
        player.health -= 20;
        if (player.health < 0)
            player.health = 0;
    }

    move_shots();
    pickup_items();
    centre_camera();

    /* enemies run after the camera settles so spawning uses this frame's view */
    update_spawns();
    move_enemies();
    move_ebullets();
    check_shot_hits();
}

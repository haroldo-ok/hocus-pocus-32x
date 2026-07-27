/*
 * Hocus Pocus 32X - asset pack reader.
 *
 * The pack lives in ROM immediately after the program image, so lumps are
 * used in place (no copying into the 256 KB of SDRAM we actually have).
 */

#include "hocus.h"

hpak_t hpak;

static int hp_streq(const char *a, const uint8_t *b)
{
    int i;
    for (i = 0; i < 16; i++) {
        char ca = a[i];
        char cb = (char)b[i];
        if (ca != cb)
            return 0;
        if (!ca)
            return 1;
    }
    return 1;
}

static uint16_t rd16(const uint8_t *p)
{
    return ((uint16_t)p[0] << 8) | p[1];
}

static uint32_t rd32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | p[3];
}

void HP_Init(const uint8_t *base)
{
    hpak.base = base;
    hpak.version = rd16(base + 4);
    hpak.count = rd16(base + 6);
    hpak.datastart = rd32(base + 8);
}

const uint8_t *HP_Find(const char *name, uint32_t *len)
{
    const uint8_t *dir = hpak.base + 12;
    int i;

    for (i = 0; i < hpak.count; i++) {
        const uint8_t *e = dir + i * 24;
        if (hp_streq(name, e)) {
            uint32_t off = rd32(e + 16);
            uint32_t ln = rd32(e + 20);
            if (len)
                *len = ln;
            return hpak.base + off;
        }
    }
    if (len)
        *len = 0;
    return NULL;
}

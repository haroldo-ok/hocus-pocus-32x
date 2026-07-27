/*
 * Hocus Pocus 32X - entry point and main loop.
 */

#include "hocus.h"

/* The asset pack is appended to the ROM right after the program image.
 * wadbase.s exports its address, padded to a 1 KB boundary by the Makefile. */
extern const uint8_t *hpak_base;

static int prevbuttons;

/*
 * Translate the raw MD pad word (as delivered by the 68000 in
 * mars_controlval) into our button bits.
 */
int I_ReadPad(void)
{
    int val = Mars_ReadController(0);
    int out = 0;

    if (val < 0)
        return 0;

    if (val & SEGA_CTRL_UP)    out |= BTN_UP;
    if (val & SEGA_CTRL_DOWN)  out |= BTN_DOWN;
    if (val & SEGA_CTRL_LEFT)  out |= BTN_LEFT;
    if (val & SEGA_CTRL_RIGHT) out |= BTN_RIGHT;
    if (val & SEGA_CTRL_A)     out |= BTN_A;
    if (val & SEGA_CTRL_B)     out |= BTN_B;
    if (val & SEGA_CTRL_C)     out |= BTN_C;
    if (val & SEGA_CTRL_START) out |= BTN_START;
    if (val & SEGA_CTRL_X)     out |= BTN_X;
    if (val & SEGA_CTRL_Y)     out |= BTN_Y;
    if (val & SEGA_CTRL_Z)     out |= BTN_Z;
    if (val & SEGA_CTRL_MODE)  out |= BTN_MODE;

    return out;
}

void I_Flip(void)
{
    Mars_FlipFrameBuffers(1);
}

void I_Init(void)
{
    Mars_Init();
    Mars_InitVideo(224);
}

/* ------------------------------------------------------ dual-SH2 rendering */
/*
 * The master and slave communicate through the 32X comm registers:
 *
 *   COMM4 = command   (0 = idle, SECCMD_DRAW_BAND, ...)
 *   COMM6 = band ylo | (yhi << 8)
 *
 * The master writes the parameters, then the command, then spins until the
 * slave clears COMM4.  Both CPUs only ever write to their own scanlines, so
 * no locking is needed on the framebuffer itself.
 *
 * Cache coherency matters here: the SH-2s have separate caches over the same
 * SDRAM, so before the slave reads game state written by the master this
 * frame it must purge its cache, otherwise it can draw with a stale camera
 * position and tear.
 */

void Mars_R_BeginDrawBand(int ylo, int yhi)
{
    while (MARS_SYS_COMM4 != SECCMD_NONE)
        ;
    MARS_SYS_COMM6 = (short)((ylo & 0xFF) | ((yhi & 0xFF) << 8));
    MARS_SYS_COMM4 = SECCMD_DRAW_BAND;
}

void Mars_R_EndDrawBand(void)
{
    while (MARS_SYS_COMM4 != SECCMD_NONE)
        ;
}

/*
 * Slave SH-2 entry point.  crt0.s jumps here on the secondary CPU.
 */
void secondary(void)
{
    for (;;) {
        int cmd;
        int v, ylo, yhi;

        while ((cmd = MARS_SYS_COMM4) == SECCMD_NONE)
            ;

        switch (cmd) {
        case SECCMD_DRAW_BAND:
            /* pick up this frame's game state written by the master */
            Mars_ClearCache();
            v = (unsigned short)MARS_SYS_COMM6;
            ylo = v & 0xFF;
            yhi = (v >> 8) & 0xFF;
            R_DrawBand(ylo, yhi);
            break;

        case SECCMD_CLEAR_CACHE:
            Mars_ClearCache();
            break;

        default:
            break;
        }

        MARS_SYS_COMM4 = SECCMD_NONE;
    }
}

int main(void)
{
    int buttons;

    I_Init();


    HP_Init(hpak_base);
    R_Init();


    G_LoadLevel(0);


    /* draw one frame into each buffer so nothing is ever shown blank */
    R_DrawFrame();
    I_Flip();
    R_DrawFrame();
    I_Flip();

    for (;;) {
        buttons = I_ReadPad();

        /* START cycles levels - lets a tester reach every map quickly */
        if ((buttons & BTN_START) && !(prevbuttons & BTN_START))
            G_LoadLevel((curlevel + 1) % LEVELS);
        prevbuttons = buttons;

        G_Tick(buttons);
        R_DrawFrame();
        I_Flip();
    }

    return 0;
}

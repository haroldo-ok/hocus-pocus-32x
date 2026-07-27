# Hocus Pocus 32X

A port of **Hocus Pocus** (Apogee / Moonlite Software, 1994) to the **Sega 32X**.

The ROM is a native SH-2 program: the original DOS assets are converted on the
host into a flat, big-endian pack that is appended to the cartridge, and the
game runs as 8bpp packed-pixel graphics straight out of the 32X framebuffer.

```
   rom/hocus32x.32x      <-- the compiled ROM (2.5 MB)
```

---

## Quick start

Run it in any 32X-capable emulator (PicoDrive, Kega Fusion, Ares, BlastEm,
Mednafen), or flash it to a 32X flashcart:

```
picodrive rom/hocus32x.32x
```

**Controls**

| Button | Action |
|--------|--------|
| D-pad Left/Right | Walk |
| A or C | Jump |
| B | Cast / shoot |
| START | Jump to the next level (all 9 shareware levels) |

---

## What was done

The upstream sources are a 64-bit C++17/SDL2 desktop engine (OpenPocus) and a
32X Doom port (d32xr). Neither can be cross-compiled to a 32X as-is, so the
port is a rewrite of the game layer in C99 against the 32X hardware, reusing
d32xr's proven boot/hardware layer.

| Area | Upstream | This port |
|------|----------|-----------|
| Language | C++17, STL (`vector`, `unordered_map`, `unique_ptr`, exceptions, RTTI) | C11, zero heap allocation, all state static |
| Platform | SDL2 renderer/audio/input, filesystem | 32X VDP framebuffer, MD pad via 68000, assets in ROM |
| Assets | `HOCUS.DAT` parsed at runtime (PCX RLE, 4-plane images, layout-compressed sprites, VGA palettes) | all decoded **ahead of time** on the host into `HPAK`; SH-2 only ever sees flat 8bpp data |
| Maths | `float` physics, `std::chrono` delta time | 16.16 fixed point, fixed 60 Hz tick |
| Colour | 24-bit RGB textures, colour-keyed | single 256-entry 32X CRAM |
| Boot | `main()` under an OS | `crt0.s` + 68000 blob, master/slave SH-2 bring-up |

### The palette insight

The 32X has one 256-entry CRAM, while the original game appears to need
several palettes. Analysis of the shareware data showed the DOS game already
partitions VGA palette space:

* **tilesets, sprites, HUD** only ever use indices `0..127`
* **level backgrounds** only ever use indices `128..255`

(Verified: background PCXs contain no pixel below 128; tileset PCXs stay under
128.) So both halves are loaded into the one hardware palette simultaneously,
with no remapping and no per-scanline tricks. Index 0 doubles as the
transparency key for sprites and foreground tiles.

### Sprite decoding

Hocus Pocus sprites use a layout-opcode stream (`0` set transparency mask,
`1` skip N groups, `2` emit 4 pixels, `3` end). A subtle detail: the opcodes
address a scratch buffer that is **always 320 pixels wide**, not the sprite's
own width. Decoding at the sprite width produces shredded, half-height
figures — the converter decodes at stride 320 and then crops.

### Collision

The 24×32 sprite cell has transparent padding and an overhanging hat. Colliding
with the full cell makes the wizard snag on every 16px tile in the narrow
shafts the levels are built from. The port collides with an inset box
(`BOX_X0..BOX_X1`, `BOX_Y0..BOX_Y1` in `src/hocus.h`) — 12×28 px inside the
cell — which is what makes the levels traversable.

---

## Layout

```
hocus32x/
  rom/hocus32x.32x        the built ROM  <-- deliverable
  rom/*.png               screenshots captured from the emulator
  src/
    hocus.h               shared types, tunables, collision box
    hp_main.c             entry point, main loop, pad reading, slave SH-2 stub
    hp_game.c             physics, collision, items, camera, projectiles
    hp_render.c           background/tile/sprite/HUD/font rasterisers
    hp_pak.c              in-ROM asset directory
    crt0.s                32X boot (from d32xr, retitled)
    marshw.c/.h, 32x.h    32X hardware layer (from d32xr)
    src-md/               68000-side blob (from d32xr)
  tools/
    mkassets.py           HOCUS.DAT/EXE  ->  HPAK asset pack
    romfix.py             ROM header + Mega Drive checksum
    preview.py            host-side renderer, validates conversion to PNG
  tests/
    runner.py             headless libretro harness (ctypes)
    test_rom.py           19 point-to-point tests
  data/                   original shareware data + FAT indices
  build/                  intermediates (not persisted)
```

---

## Building

Needs the Chilly Willy / 32XDK Sega toolchain (SH-ELF + M68K-ELF GCC 12.1):

```bash
# one-off: fetch and unpack the devkit somewhere persistent
curl -L -o devkit.tar.zst \
  https://github.com/viciious/32XDK/releases/download/20220418/chillys-sega-devkit-20220418-opt.tar.zst
# (decompress with zstd, then untar)

make GENDEV=/path/to/opt/toolchains/sega
```

`make` runs the whole pipeline: 68000 blob → SH-2 objects → two-pass link (to
discover where the asset pack lands) → asset conversion → concatenation →
header/checksum fix. Output is `build/hocus32x.32x`; copy it to `rom/`.

Regenerate only the assets with `make assets`.

---

## Tests

Two layers, both automated.

**1. Host-side conversion check** — `tools/preview.py` re-renders a level from
the pack using the same rules as the SH-2 renderer and writes a PNG, so asset
decoding can be validated without an emulator.

**2. Point-to-point ROM tests** — `tests/test_rom.py` loads
`picodrive_libretro.so` through ctypes and runs the **real ROM** under real
SH-2 emulation, feeding controller input and reading back the actual VDP
output.

```bash
python3 tests/test_rom.py         # all
python3 tests/test_rom.py -v      # verbose
python3 tests/test_rom.py black   # only the black-screen tests

python3 tests/bench.py            # frame-rate benchmark
```

Coverage:

| Group | Tests |
|-------|-------|
| ROM integrity | file size/padding, `SEGA 32X` header, title, Mega Drive checksum, `HPAK` present and every lump in bounds |
| **Black screen** | first drawn frame not black; non-black sustained over a long run; non-black continuously while the player moves |
| Rendering | ≥16 distinct colours (proves both palette halves are live), HUD row filled, playfield filled |
| Input & gameplay | RIGHT changes the screen, A jumps, walk+jump scrolls the camera, B shoots, START switches level |
| All levels | each of the 9 shareware levels loads and renders with content |
| Dual-SH2 | no dead band or seam at the master/slave split, both halves animate together |
| Enemies | spawn tables present and well formed for all 9 levels, enemies damage the player |
| Collectibles | score and crystal counters advance on pickup, item table decodes with the right stride, HUD digits have real contrast |
| Stability | frames keep changing (not frozen), 500-frame run with no hang or black-out |

Result:

```
  26 passed, 0 failed, 26 total
```

Screenshots from the run land in `build/testshots/`.

### Why these tests catch a black screen

A black screen is the classic failure for a 32X port — bad palette upload, a
framebuffer flip that never happens, a crashed SH-2, or an asset pointer off
the end of the ROM all present the same way. The tests assert on decoded pixel
data (`nonblack_ratio`, `unique_colours`) rather than "did it run", at boot,
during play, and on every level, so any of those regressions fails loudly.

---

## Performance

Measured with `tests/bench.py`, which drives the real ROM in PicoDrive and
counts how often the displayed image actually changes while the camera is
scrolling (the worst case - every tile is redrawn).

| Stage | fps | vs. baseline |
|-------|-----|--------------|
| Initial working port | 13.1 | 1.00x |
| + 32-bit writes & VDP overwrite region | 26.9 | 2.05x |
| + background occlusion culling | 35.5 | 2.71x |
| + dual-SH2 rendering | **52.8** | **4.03x** |

Roughly 4x faster, from ~13 fps to essentially the 60 Hz display ceiling.

### What actually mattered

**1. Hardware transparency via the VDP overwrite region (2.05x).**
The original renderer tested every pixel for colour 0 and wrote survivors one
byte at a time. The 32X maps the framebuffer a second time at `0x24020000`,
where bytes written as zero are discarded *by hardware*. Blitting there turns
masked drawing into a plain copy - no test, no branch - and lets four pixels
move per 32-bit store. Tiles land on 4-byte boundaries, so tile blits became
four `mov.l` per row instead of sixteen byte writes plus sixteen branches.

The converter also tags each tile as empty / opaque / mixed (`TCLS*` lumps).
Empty tiles are skipped, opaque ones use the plain framebuffer, and only
genuinely mixed tiles need the overwrite alias. In the shipped tilesets only
2-40 tiles out of 240 are mixed, so the common case is the fastest one.

**2. Occlusion culling of the parallax background (1.32x on top).**
The background is the only full-screen fill in the frame, and 60-90% of it is
immediately painted over by opaque background-layer tiles. The renderer now
builds a small per-cell occlusion map each frame and skips those spans,
coalescing adjacent visible cells into single runs.

**3. Using the second SH-2 (1.49x on top).**
The slave CPU was idling in a spin loop. It now renders the lower half of the
playfield while the master renders the upper half, synchronised through the
`COMM4`/`COMM6` mailboxes. The split is on a tile-row boundary so the two CPUs
never write the same scanlines and no locking is needed. The slave purges its
cache before each band, otherwise it can draw with a stale camera position and
tear - the two SH-2s have separate caches over the same SDRAM.

Smaller wins: the HUD plate is only repainted when dirty (it is static most
frames, and both framebuffers are tracked so a flip cannot show a stale one),
and the font lookup is an ASCII-indexed table rather than a linear scan.

### A note on measurement

`bench.py` counts changes in the displayed image, so it is quantised by the
60 Hz refresh and cannot resolve rates far above ~55 fps. I tried to add an
exact in-ROM counter (SH-2 FRT / watchdog published through `COMM12`), but
`crt0.s` programs the FRT with `OCRA = 1` and "clear FRC on compare match",
so the free-running counter is reset almost immediately and reads zero; the
watchdog is configured but left stopped. Rather than modify the boot code, the
benchmark keeps the image-change method and now validates any alternative
counter before trusting it. The practical consequence is that the final figure
is a lower bound - the game is at or near the display cap.

### Ideas not pursued

* **SH-2 DMA for the background** - the DMAC could move background spans
  without CPU involvement, but the spans are short and irregular after
  occlusion culling, so setup cost would likely dominate.
* **Dirty-rectangle tile caching** - only redrawing cells that changed since
  the last frame in that buffer. A big win when standing still, nothing when
  scrolling, and it needs per-buffer state; not worth the complexity while the
  scrolling case is already at the cap.
* **Moving hot code to SDRAM** - code executes from ROM, which is slower than
  SDRAM on the 32X. There is ~200 KB of SDRAM free, so copying the tile blitter
  there is feasible and would be the next thing to try.

---

## Enemies and collectibles

### Enemies

The level data stores 250 "trigger groups", each holding up to eight
`(type, tile-offset)` pairs. `type` indexes a per-level tile-property table
that supplies the sprite set, hit points, projectile speeds and a behaviour
code. The converter resolves all of that up front and emits a flat `LVEN*`
lump of 12-byte records, so the SH-2 never walks the indirection at runtime.

Counts in the shipped data: 29 enemies on level 1 rising to 117 on level 5,
drawn from sprite sets 4–12 (Awful Al, Shroom Head, Devil Dan, Rusher,
Devil Dave, Hunter, Penguin, Fly Guy, Mad Monk).

At runtime enemies stream in and out around the camera — at most
`MAX_ENEMIES` (12) are live, spawned when they come within 64px of the view
and retired at 200px. That keeps the per-frame cost flat regardless of how
crowded the level is. Behaviours:

| Code | Behaviour | Notes |
|------|-----------|-------|
| 0 | walker | paces its ledge, turns at walls **and at ledge edges** |
| 1 | hopper | walks and hops on a timer |
| 2 | turret | stationary, fires downward |

Shooters only fire when roughly level with the player and facing them, so they
do not spray blind. Enemies killed with a shot are marked dead in a
`spawn_dead` bitmap and stay dead for the rest of the level, rather than
respawning every time the camera pans away and back.

Contact damage, enemy projectiles, and player shots hitting enemies are all
box-vs-box tests against an inset hitbox (`EBOX_INSET`), matching the approach
used for the player.

### Collectibles

Collectibles live in the event layer, but they are *drawn* as tiles in the
**background** layer. Clearing the event alone left the gem on screen forever;
picking one up now also sets a bit in a `bg_removed` mask, which both the tile
blitter and the occlusion builder consult — so the parallax behind the gem
reappears correctly. This mirrors OpenPocus' `map.removeTile(0, pos)`.

### Two bugs worth recording

**The item table stride is 42 bytes, not 44.** With 44 every field after the
name is shifted, so Diamond reported a score of 0 and later entries were pure
garbage. The giveaway is that the correct stride yields the familiar
Ruby 100 / Diamond 250 / Goblet 500 / Crown 1000.

**The HUD counters were invisible, not broken.** Score and crystals genuinely
were incrementing, but the digits were drawn in palette index 15 —
`(56, 28, 28)`, near-black on a dark HUD plate. They are now index 88,
`(252, 244, 244)`. There is a regression test that measures digit/background
contrast so this cannot silently come back.

A related double-buffering bug: the HUD only repainted the buffer it happened
to be drawing into, so a changed value was shown on alternate frames and
looked frozen. Value changes are now applied to both buffers.

---

## Status and limitations

Working: all 9 shareware levels, parallax backgrounds, 3-layer tilemaps,
animated player (walk/jump/fall/shoot), fixed-point physics and collision,
**enemies with AI**, **working collectibles** (gems, potions, keys, crystals),
combat in both directions, shootable tiles, scrolling camera, HUD with
score/health/crystals/level, level switching.

Not yet implemented:

* **Sound and music** — no PWM audio or MIDI playback; the VOC/MIDI lumps are
  not yet packed.
* **Menus / splash screens** — boots straight into level 1.
* **Level completion** — START is the level switch; collecting all crystals
  does not yet advance.
* **Enemy variety** — every enemy uses one of three behaviours (walk, hop,
  turret). The original gives some types bespoke movement, and bosses are not
  special-cased.
* Rendering now uses both SH-2s (see Performance above). Remaining headroom
  would come from running hot code out of SDRAM rather than ROM.

---

## Licensing

* Game engine logic derives from **OpenPocus** (A. Roldán) — **GPLv2**.
* 32X boot and hardware layer derive from **Doom 32X Resurrection**
  (V. Luchits) — **MIT**.
* Combined work is therefore **GPLv2**.
* *Hocus Pocus* game data is © 1994 Moonlite Software / Apogee. Only the
  freely redistributable **shareware** episode is used. The data is not
  redistributed here beyond what was downloaded for the build; the ROM is for
  personal use with the shareware release.

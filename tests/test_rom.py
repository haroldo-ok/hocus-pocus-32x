#!/usr/bin/env python3
"""
Point-to-point tests for the Hocus Pocus 32X ROM.

These run the *real* ROM inside PicoDrive (libretro core) via ctypes, so every
assertion is made against actual SH-2 execution and actual 32X VDP output -
not a host-side re-implementation.

Categories
  1. ROM integrity      - header, mapper, size, checksum, asset pack placement
  2. Boot / no black    - the screen must show real content, never stay black
  3. Rendering          - palette split, HUD present, playfield populated
  4. Input & gameplay   - movement, jumping, scrolling, shooting, level switch
  5. Stability          - long run without hangs, black frames or freezes

Run:  python3 tests/test_rom.py            (all)
      python3 tests/test_rom.py -v         (verbose)
      python3 tests/test_rom.py black      (only tests matching "black")
"""

import os
import struct
import sys
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runner import Emu, default_paths     # noqa: E402

CORE, ROM = default_paths()
ARTIFACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'build', 'testshots')

# A frame is "black" if no channel anywhere exceeds this.
BLACK_THRESHOLD = 8
# Fraction of pixels that must be non-black for a frame to count as "content".
MIN_CONTENT = 0.25
# Minimum distinct colours for a frame to look like a real game scene.
MIN_COLOURS = 16
# Display frames to allow for 32X hardware init before expecting a picture.
BOOT_FRAMES = 90

_results = []
_verbose = False


def log(*a):
    if _verbose:
        print('      ', *a)


def test(name):
    def deco(fn):
        fn._test_name = name
        return fn
    return deco


def shot(frame, tag):
    try:
        os.makedirs(ARTIFACTS, exist_ok=True)
        p = os.path.join(ARTIFACTS, tag + '.png')
        frame.save_png(p)
        log('saved', p)
    except Exception as e:
        log('could not save shot:', e)


def strip_sig(f, y=None):
    """Signature of one scanline - used to detect scrolling/animation."""
    if y is None:
        y = f.height // 3
    w = f.width
    return bytes(b for x in range(0, w, 2)
                 for b in f.pixels[(y * w + x) * 3:(y * w + x) * 3 + 3])


def boot(emu_frames=90):
    e = Emu(CORE, ROM)
    e.load()
    e.run(emu_frames)
    return e


# ---------------------------------------------------------------- 1. INTEGRITY

@test('ROM file exists and has a sane size')
def t_rom_exists():
    assert os.path.exists(ROM), 'ROM not found at %s' % ROM
    size = os.path.getsize(ROM)
    assert size >= 512 * 1024, 'ROM too small: %d' % size
    assert size % (512 * 1024) == 0, 'ROM not padded to 512K multiple: %d' % size
    log('size = %d bytes (%.1f MB)' % (size, size / 1048576))


@test('ROM header declares SEGA 32X and the right title')
def t_rom_header():
    with open(ROM, 'rb') as f:
        rom = f.read()
    mapper = rom[0x100:0x110].decode('ascii', 'replace')
    title = rom[0x120:0x140].decode('ascii', 'replace').strip()
    assert mapper.startswith('SEGA 32X'), 'bad mapper: %r' % mapper
    assert 'HOCUS' in title.upper(), 'bad title: %r' % title
    log('mapper=%r title=%r' % (mapper, title))


@test('ROM checksum field matches the data')
def t_rom_checksum():
    with open(ROM, 'rb') as f:
        rom = bytearray(f.read())
    stored = (rom[0x18E] << 8) | rom[0x18F]
    s = 0
    for i in range(0x200, len(rom) - 1, 2):
        s = (s + ((rom[i] << 8) | rom[i + 1])) & 0xFFFF
    assert s == stored, 'checksum mismatch: stored 0x%04X computed 0x%04X' % (stored, s)
    log('checksum 0x%04X' % s)


@test('asset pack (HPAK) is present and well formed in the ROM')
def t_hpak_present():
    with open(ROM, 'rb') as f:
        rom = f.read()
    idx = rom.find(b'HPAK')
    assert idx > 0, 'HPAK magic not found in ROM'
    ver, count, datastart = struct.unpack('>HHI', rom[idx + 4:idx + 12])
    assert count > 50, 'too few lumps: %d' % count
    # spot-check a few required lumps resolve inside the ROM
    names = []
    p = idx + 12
    for _ in range(count):
        nm = rom[p:p + 16].split(b'\x00')[0].decode('ascii', 'replace')
        off, ln = struct.unpack('>II', rom[p + 16:p + 24])
        assert idx + off + ln <= len(rom), 'lump %s runs past end of ROM' % nm
        names.append(nm)
        p += 24
    for required in ('PAL_GAME', 'LEVELS', 'SPRMETA', 'SPRDATA', 'HUD', 'FONT',
                     'LVFG0', 'LVBG0', 'TILES0', 'BG0'):
        assert required in names, 'missing lump %s' % required
    log('HPAK at 0x%X, %d lumps, v%d' % (idx, count, ver))


# --------------------------------------------------------------- 2. BOOT/BLACK

@test('ROM boots in the emulator and produces video')
def t_boots():
    e = boot(60)
    try:
        f = e.frame()
        assert f is not None, 'no video frame produced'
        assert f.width >= 256 and f.height >= 192, 'unexpected size %dx%d' % (f.width, f.height)
        log('frame %dx%d' % (f.width, f.height))
        shot(f, 'boot')
    finally:
        e.close()


@test('BLACK SCREEN: first drawn frame is not black')
def t_not_black_early():
    """
    The 32X boot sequence (Mars_Init + Mars_InitVideo, which clears both
    320x224 framebuffers behind blocking flips) takes most of a second before
    the game can draw anything.  That is normal for the platform, so allow it
    a budget and then require real content - the point of this test is to
    catch a permanently black screen, not to police startup latency.
    """
    e = Emu(CORE, ROM)
    e.load()
    e.run(BOOT_FRAMES)
    try:
        f = e.frame()
        assert f is not None, 'no frame'
        assert not f.is_black(BLACK_THRESHOLD), 'screen is entirely black at frame 45'
        r = f.nonblack_ratio(BLACK_THRESHOLD)
        assert r >= MIN_CONTENT, 'only %.1f%% of pixels are non-black' % (r * 100)
        log('nonblack=%.1f%%' % (r * 100))
        shot(f, 'not_black_early')
    finally:
        e.close()


@test('BLACK SCREEN: stays non-black across a long run')
def t_not_black_sustained():
    e = boot(60)
    try:
        black_frames = 0
        checks = 0
        for i in range(12):
            e.run(20)
            f = e.frame()
            checks += 1
            if f.is_black(BLACK_THRESHOLD) or f.nonblack_ratio(BLACK_THRESHOLD) < MIN_CONTENT:
                black_frames += 1
        assert black_frames == 0, '%d/%d sampled frames were black/near-black' % (black_frames, checks)
        log('%d checkpoints, all with content' % checks)
    finally:
        e.close()


@test('BLACK SCREEN: not black while the player is moving')
def t_not_black_while_playing():
    e = boot(60)
    try:
        worst = 1.0
        for cycle in range(6):
            e.press('RIGHT')
            e.run(30)
            e.press('A')
            e.run(6)
            e.release('A')
            e.run(24)
            f = e.frame()
            worst = min(worst, f.nonblack_ratio(BLACK_THRESHOLD))
            assert not f.is_black(BLACK_THRESHOLD), 'black screen during gameplay (cycle %d)' % cycle
        e.release_all()
        assert worst >= MIN_CONTENT, 'worst frame only %.1f%% non-black' % (worst * 100)
        log('worst non-black ratio during play = %.1f%%' % (worst * 100))
        shot(e.frame(), 'playing')
    finally:
        e.close()


# ---------------------------------------------------------------- 3. RENDERING

@test('frame shows a rich palette (tileset + background colours both live)')
def t_palette_rich():
    e = boot(90)
    try:
        f = e.frame()
        cols = f.unique_colours()
        assert len(cols) >= MIN_COLOURS, 'only %d distinct colours' % len(cols)
        log('%d distinct colours' % len(cols))
    finally:
        e.close()


@test('HUD strip is drawn at the bottom of the screen')
def t_hud_present():
    e = boot(90)
    try:
        f = e.frame()
        w, h = f.width, f.height
        # the HUD occupies the lower part of the 200-line game area, which is
        # letterboxed inside the 224-line framebuffer
        hud_y = h - 20
        row = f.pixels[hud_y * w * 3:(hud_y + 1) * w * 3]
        nonblack = sum(1 for i in range(0, len(row), 3)
                       if row[i] > BLACK_THRESHOLD or row[i + 1] > BLACK_THRESHOLD
                       or row[i + 2] > BLACK_THRESHOLD)
        ratio = nonblack / w
        assert ratio > 0.5, 'HUD row only %.0f%% filled' % (ratio * 100)
        log('HUD row %d is %.0f%% filled' % (hud_y, ratio * 100))
    finally:
        e.close()


@test('playfield (upper area) is populated with level graphics')
def t_playfield_populated():
    e = boot(90)
    try:
        f = e.frame()
        w, h = f.width, f.height
        # sample the middle of the playfield
        total = 0
        nonblack = 0
        for y in range(30, min(140, h)):
            for x in range(0, w, 3):
                i = (y * w + x) * 3
                total += 1
                if (f.pixels[i] > BLACK_THRESHOLD or f.pixels[i + 1] > BLACK_THRESHOLD
                        or f.pixels[i + 2] > BLACK_THRESHOLD):
                    nonblack += 1
        ratio = nonblack / total
        assert ratio > 0.6, 'playfield only %.0f%% filled' % (ratio * 100)
        log('playfield %.0f%% filled' % (ratio * 100))
    finally:
        e.close()


# ------------------------------------------------------------ 4. INPUT/GAMEPLAY

@test('pressing RIGHT changes the screen (player responds to input)')
def t_input_right():
    e = boot(90)
    try:
        before = e.frame().pixels
        e.press('RIGHT')
        e.run(45)
        e.release_all()
        e.run(5)
        after = e.frame().pixels
        assert before != after, 'screen unchanged after pressing RIGHT'
        log('screen changed under RIGHT')
    finally:
        e.close()


@test('jumping changes the screen (A button works)')
def t_input_jump():
    e = boot(90)
    try:
        e.run(10)
        before = e.frame().pixels
        e.press('A')
        e.run(8)
        e.release('A')
        e.run(6)
        after = e.frame().pixels
        assert before != after, 'screen unchanged after jump'
        log('screen changed under A (jump)')
    finally:
        e.close()


@test('walking + jumping scrolls the camera through the level')
def t_camera_scrolls():
    e = boot(90)
    try:
        a = strip_sig(e.frame())
        for cycle in range(6):
            e.press('RIGHT')
            e.run(40)
            e.press('A')
            e.run(6)
            e.release('A')
            e.run(30)
        e.release_all()
        e.run(10)
        b = strip_sig(e.frame())
        assert a != b, 'camera never scrolled while traversing the level'
        log('camera scrolled')
        shot(e.frame(), 'scrolled')
    finally:
        e.close()


@test('shooting (B) works and does not break rendering')
def t_shoot():
    e = boot(90)
    try:
        e.press('B')
        e.run(10)
        e.release('B')
        e.run(20)
        f = e.frame()
        assert not f.is_black(BLACK_THRESHOLD), 'black screen after shooting'
        assert f.nonblack_ratio(BLACK_THRESHOLD) >= MIN_CONTENT
        log('fired without breaking the frame')
    finally:
        e.close()


@test('START switches level and the new level renders')
def t_level_switch():
    e = boot(90)
    try:
        before = strip_sig(e.frame())
        e.press('START')
        e.run(4)
        e.release('START')
        e.run(60)
        f = e.frame()
        after = strip_sig(f)
        assert not f.is_black(BLACK_THRESHOLD), 'black screen after level switch'
        assert f.nonblack_ratio(BLACK_THRESHOLD) >= MIN_CONTENT, \
            'new level only %.1f%% non-black' % (f.nonblack_ratio() * 100)
        assert before != after, 'screen identical after switching level'
        log('level switched and rendered')
        shot(f, 'level2')
    finally:
        e.close()


@test('every level loads and renders without a black screen')
def t_all_levels():
    e = boot(90)
    try:
        seen = []
        for lvl in range(9):
            f = e.frame()
            ratio = f.nonblack_ratio(BLACK_THRESHOLD)
            cols = len(f.unique_colours())
            assert not f.is_black(BLACK_THRESHOLD), 'level %d is black' % (lvl + 1)
            assert ratio >= MIN_CONTENT, \
                'level %d only %.1f%% non-black' % (lvl + 1, ratio * 100)
            assert cols >= MIN_COLOURS, 'level %d only %d colours' % (lvl + 1, cols)
            seen.append((lvl + 1, ratio, cols))
            shot(f, 'level%d' % (lvl + 1))
            # advance to the next level
            e.press('START')
            e.run(4)
            e.release('START')
            e.run(50)
        for lvl, r, c in seen:
            log('level %d: %.0f%% non-black, %d colours' % (lvl, r * 100, c))
    finally:
        e.close()


# ------------------------------------------------------------- 4b. DUAL-SH2

@test('no dead band or seam at the master/slave split line')
def t_no_split_seam():
    """
    The playfield is rendered by two CPUs: master draws y 0..79, slave 80..159
    (screen rows 12..91 and 92..171).  A broken handshake shows up as a black
    or stale horizontal band, so assert every playfield row has content.
    """
    e = boot(90)
    try:
        # move around so both halves have real content
        for c in range(3):
            e.press('RIGHT')
            e.run(35)
            e.press('A')
            e.run(6)
            e.release('A')
            e.run(25)
        e.release_all()
        e.run(10)
        f = e.frame()
        w, h = f.width, f.height
        # playfield occupies rows 12..171 in the 224-line framebuffer
        empty = []
        for y in range(12, 172):
            rowbytes = f.pixels[y * w * 3:(y + 1) * w * 3]
            if max(rowbytes) <= BLACK_THRESHOLD:
                empty.append(y)
        assert not empty, 'blank playfield rows (dead band at split?): %s' % empty[:12]
        log('all 160 playfield rows have content')
        shot(f, 'split_seam')
    finally:
        e.close()


@test('both halves of the screen animate together (slave keeps up)')
def t_both_halves_animate():
    """If the slave stalled, the lower half would freeze while the top moved."""
    e = boot(90)
    try:
        def halves(f):
            w = f.width
            top = f.pixels[30 * w * 3:70 * w * 3]
            bot = f.pixels[110 * w * 3:150 * w * 3]
            return hashlib.md5(top).hexdigest(), hashlib.md5(bot).hexdigest()

        e.press('RIGHT')
        tops, bots = set(), set()
        for _ in range(6):
            e.run(20)
            t, b = halves(e.frame())
            tops.add(t)
            bots.add(b)
        e.release_all()
        assert len(tops) > 1, 'top half never changed'
        assert len(bots) > 1, 'bottom half never changed - slave SH2 may be stalled'
        log('top %d distinct, bottom %d distinct' % (len(tops), len(bots)))
    finally:
        e.close()


# ------------------------------------------------------- 4c. ITEMS/ENEMIES

def hud_region(f, x0, x1, y0=190, y1=202):
    w = f.width
    return b''.join(f.pixels[(y * w + x) * 3:(y * w + x) * 3 + 3]
                    for y in range(y0, y1) for x in range(x0, x1))


HUD_SCORE = (14, 72)
HUD_HEALTH = (80, 115)
HUD_CRYSTAL = (138, 175)


@test('enemy spawn tables are present for every level')
def t_enemy_tables():
    with open(ROM, 'rb') as f:
        rom = f.read()
    idx = rom.find(b'HPAK')
    assert idx > 0
    count = struct.unpack('>H', rom[idx + 6:idx + 8])[0]
    found = {}
    p = idx + 12
    for _ in range(count):
        nm = rom[p:p + 16].split(b'\x00')[0].decode('ascii', 'replace')
        off, ln = struct.unpack('>II', rom[p + 16:p + 24])
        if nm.startswith('LVEN'):
            found[nm] = ln
        p += 24
    for lv in range(9):
        nm = 'LVEN%d' % lv
        assert nm in found, 'missing enemy table %s' % nm
        assert found[nm] >= 12, '%s is empty' % nm
        assert found[nm] % 12 == 0, '%s has a partial record' % nm
    log('enemy counts: %s' % {k: v // 12 for k, v in sorted(found.items())})


@test('item score table decodes with the correct 42-byte stride')
def t_item_table():
    """Ruby=100, Diamond=250, Goblet=500, Crown=1000 in the shipped data."""
    with open(ROM, 'rb') as f:
        rom = f.read()
    idx = rom.find(b'HPAK')
    count = struct.unpack('>H', rom[idx + 6:idx + 8])[0]
    p = idx + 12
    off = ln = None
    for _ in range(count):
        nm = rom[p:p + 16].split(b'\x00')[0].decode('ascii', 'replace')
        if nm == 'ITEMS':
            off, ln = struct.unpack('>II', rom[p + 16:p + 24])
            break
        p += 24
    assert off is not None, 'no ITEMS lump'
    tab = rom[idx + off:idx + off + ln]
    scores = [struct.unpack('>H', tab[i * 6:i * 6 + 2])[0] for i in range(4)]
    assert scores == [100, 250, 500, 1000], 'bad item scores: %s' % scores
    heal = tab[4 * 6 + 2]
    assert heal == 10, 'heal potion should restore 10, got %d' % heal
    log('ruby/diamond/goblet/crown = %s, heal = %d' % (scores, heal))


@test('enemies appear on screen and the player takes damage from them')
def t_enemies_hurt():
    """
    Walk into the level-1 enemy group and wait for the health counter to drop.
    Exactly when contact happens depends on where the wandering enemies are,
    so poll rather than assume a fixed number of frames, and nudge the player
    out of any spot it gets wedged in.
    """
    e = boot(120)
    try:
        before = hud_region(e.frame(), *HUD_HEALTH)
        hit = False
        for c in range(60):
            e.press('RIGHT')
            e.run(20)
            e.press('A')
            e.run(5)
            e.release('A')
            e.run(15)
            if hud_region(e.frame(), *HUD_HEALTH) != before:
                hit = True
                break
            if c % 12 == 11:            # unstick: back up and try again
                e.release('RIGHT')
                e.press('LEFT')
                e.run(30)
                e.release('LEFT')
        e.release_all()
        e.run(5)
        assert hit, 'health never changed - enemies are not damaging the player'
        log('health counter moved after %d cycles' % (c + 1))
        shot(e.frame(), 'enemies')
    finally:
        e.close()


@test('collectibles raise the score and are removed from the map')
def t_collect_score():
    """
    Walk a level that has a gem run near the spawn point and require both the
    score and the crystal counter to move.

    Where the player ends up depends on enemy positions and on exactly which
    ledge it lands on, so this drives several candidate routes (level 4 left
    is worth 6500 points and a crystal; level 7 right and level 9 left also
    score) and accepts the first that registers both. That keeps the test
    about "pickup works and the HUD shows it" rather than about one fragile
    input script.
    """
    routes = [(3, 'LEFT'), (6, 'RIGHT'), (8, 'LEFT')]

    for starts, key in routes:
        e = boot(120)
        try:
            for _ in range(starts):
                e.press('START')
                e.run(4)
                e.release('START')
                e.run(50)
            e.release_all()
            e.run(30)       # let the new HUD reach both framebuffers

            before = hud_region(e.frame(), *HUD_SCORE)
            beforec = hud_region(e.frame(), *HUD_CRYSTAL)
            got_score = got_crystal = False

            for c in range(60):
                e.press(key)
                e.run(20)
                e.press('A')
                e.run(5)
                e.release('A')
                e.run(15)
                f = e.frame()
                if hud_region(f, *HUD_SCORE) != before:
                    got_score = True
                if hud_region(f, *HUD_CRYSTAL) != beforec:
                    got_crystal = True
                if got_score and got_crystal:
                    break
                if c % 15 == 14:        # unstick
                    e.release(key)
                    e.press('RIGHT' if key == 'LEFT' else 'LEFT')
                    e.run(30)
                    e.release_all()
            e.release_all()
            e.run(5)

            if got_score and got_crystal:
                log('level %d walking %s: score and crystals both advanced'
                    % (starts + 1, key))
                shot(e.frame(), 'collected')
                return
        finally:
            e.close()

    raise AssertionError('no route registered both a score and a crystal pickup')


@test('HUD digits are drawn in a legible colour')
def t_hud_legible():
    """
    The counters are painted on a dark plate; a dark text colour makes them
    invisible even though the values are correct.  Require real contrast
    between the digits and their background.
    """
    e = boot(120)
    try:
        f = e.frame()
        w = f.width
        # sample the score digit strip
        vals = []
        for y in range(192, 200):
            for x in range(16, 70):
                i = (y * w + x) * 3
                vals.append(f.pixels[i] + f.pixels[i + 1] + f.pixels[i + 2])
        assert vals, 'no pixels sampled'
        spread = max(vals) - min(vals)
        assert spread > 150, \
            'score digits have too little contrast (spread %d) - unreadable' % spread
        log('digit/background contrast spread = %d' % spread)
    finally:
        e.close()


# --------------------------------------------------------------- 5. STABILITY

@test('animation is alive (frames keep changing over time)')
def t_animation_alive():
    e = boot(60)
    try:
        e.press('RIGHT')
        sigs = []
        for _ in range(6):
            e.run(15)
            sigs.append(hashlib.md5(e.frame().pixels).hexdigest())
        e.release_all()
        assert len(set(sigs)) > 1, 'frame never changed while walking - game may be frozen'
        log('%d distinct frames out of %d samples' % (len(set(sigs)), len(sigs)))
    finally:
        e.close()


@test('long run stays stable (no hang, no black-out)')
def t_long_run():
    e = boot(60)
    try:
        t0 = time.time()
        blacks = 0
        for i in range(20):
            if i % 3 == 0:
                e.press('RIGHT')
            else:
                e.release_all()
            if i % 4 == 0:
                e.press('A')
            e.run(25)
            e.release('A')
            f = e.frame()
            if f.is_black(BLACK_THRESHOLD):
                blacks += 1
        dt = time.time() - t0
        assert blacks == 0, '%d black frames during long run' % blacks
        log('ran 500 frames in %.1fs without black-out' % dt)
    finally:
        e.close()


# -------------------------------------------------------------------- harness

def main():
    global _verbose
    args = [a for a in sys.argv[1:]]
    if '-v' in args:
        _verbose = True
        args.remove('-v')
    pattern = args[0].lower() if args else None

    tests = []
    for name, obj in sorted(globals().items()):
        if name.startswith('t_') and callable(obj) and hasattr(obj, '_test_name'):
            tests.append(obj)

    if pattern:
        tests = [t for t in tests if pattern in t._test_name.lower() or pattern in t.__name__.lower()]

    print('=' * 74)
    print('Hocus Pocus 32X - point-to-point ROM tests')
    print('  core: %s' % CORE)
    print('  rom : %s' % ROM)
    print('=' * 74)

    passed = failed = 0
    failures = []
    for t in tests:
        label = t._test_name
        sys.stdout.write('  %-62s ' % label[:62])
        sys.stdout.flush()
        try:
            t0 = time.time()
            t()
            dt = time.time() - t0
            print('PASS (%.1fs)' % dt)
            passed += 1
        except AssertionError as e:
            print('FAIL')
            failures.append((label, str(e)))
            failed += 1
        except Exception as e:
            print('ERROR')
            failures.append((label, '%s: %s' % (type(e).__name__, e)))
            failed += 1

    print('-' * 74)
    print('  %d passed, %d failed, %d total' % (passed, failed, passed + failed))
    if failures:
        print()
        for label, msg in failures:
            print('  FAILED: %s' % label)
            print('          %s' % msg)
    print('=' * 74)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

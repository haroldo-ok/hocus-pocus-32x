#!/usr/bin/env python3
"""
Frame-rate benchmark for the Hocus Pocus 32X ROM.

The 32X VDP has two framebuffers; the game calls Mars_FlipFrameBuffers() once
per rendered frame.  The libretro core emits one video callback per *display*
frame (60 Hz NTSC), so we cannot count game frames from the callback alone.

Instead we detect how often the picture actually changes: if the game renders
at 20 fps, the displayed image only changes every ~3 display frames.  We hash
a subsample of each displayed frame while the camera is scrolling (which
guarantees the image differs whenever a new game frame appears) and count
distinct consecutive images.

  effective fps = (number of image changes) / (elapsed seconds of emulation)

Usage:
  python3 tests/bench.py [rom]
  python3 tests/bench.py --json          machine readable
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runner import Emu, default_paths     # noqa: E402


def sample_hash(frame, step=7):
    """Cheap hash over a subsample of the frame."""
    p = frame.pixels
    return hashlib.md5(bytes(p[i] for i in range(0, len(p), step))).digest()


def measure(emu, display_frames, scenario):
    """
    Run `display_frames` display frames applying `scenario`, and count how many
    times the on-screen image changed.  Returns (changes, display_frames).
    """
    changes = 0
    last = None
    for i in range(display_frames):
        scenario(emu, i)
        emu.run(1)
        f = emu.frame()
        if f is None:
            continue
        h = sample_hash(f)
        if last is not None and h != last:
            changes += 1
        last = h
    return changes, display_frames


def scen_walk(e, i):
    """Hold RIGHT and jump periodically - forces the camera to scroll."""
    if i % 90 == 0:
        e.press('RIGHT')
    if i % 90 == 60:
        e.press('A')
    if i % 90 == 66:
        e.release('A')


def scen_idle(e, i):
    e.release_all()


def frames_rendered(emu):
    """
    Read the game's own frame counter (gametic) out of emulated SDRAM.

    This is exact: it counts game frames actually completed, with no 60 Hz
    quantisation, so it can resolve rates above the display refresh.  gametic
    lives in .bss in SDRAM (0x06000000..), which PicoDrive keeps in
    Pico32xMem->sdram.
    """
    import ctypes
    try:
        base = ctypes.addressof(ctypes.c_char.in_dll(emu.core, 'Pico32xMem'))
    except ValueError:
        return None
    if getattr(emu, '_gametic_off', None) is None:
        return None
    off = emu._gametic_off
    raw = (ctypes.c_ubyte * 4).from_address(base + off)
    return (raw[0] << 24) | (raw[1] << 16) | (raw[2] << 8) | raw[3]


def locate_gametic(rom_path):
    """Find gametic's SDRAM offset from the linker map next to the ROM."""
    import re
    for cand in (os.path.join(os.path.dirname(rom_path), '..', 'build', 'output.map'),
                 os.path.join(os.path.dirname(rom_path), 'output.map')):
        if not os.path.exists(cand):
            continue
        with open(cand, errors='ignore') as f:
            for line in f:
                m = re.match(r'\s+0x0*([0-9a-fA-F]+)\s+_?gametic\b', line)
                if m:
                    addr = int(m.group(1), 16)
                    if 0x06000000 <= addr < 0x06040000:
                        return addr - 0x06000000
    return None


def run_bench(rom, core, warmup=120, frames=600, label=''):
    e = Emu(core, rom)
    e._gametic_off = locate_gametic(rom)
    e.load()
    e.run(warmup)

    results = {}

    # Preferred: read the game's own frame counter (exact, unquantised).
    t0 = frames_rendered(e)
    if t0 is not None:
        # sanity-check that the location really is a monotonically
        # increasing frame counter before we trust it
        e.run(30)
        probe = frames_rendered(e)
        if probe is None or probe == t0 or ((probe - t0) & 0xFFFFFFFF) > 10000:
            t0 = None
    if t0 is not None:
        t0 = frames_rendered(e)
        for i in range(frames):
            scen_walk(e, i)
            e.run(1)
        t1 = frames_rendered(e)
        e.release_all()
        rendered = (t1 - t0) & 0xFFFFFFFF
        results['scroll_fps'] = rendered / (frames / 60.0)
        results['scroll_frames_rendered'] = rendered
        results['scroll_display_frames'] = frames
        results['method'] = 'gametic'
        e.close()
        return results

    # Fallback: count how often the displayed image changes.
    e.release_all()
    changes, total = measure(e, frames, scen_walk)
    e.release_all()
    results['scroll_fps'] = changes / (total / 60.0)
    results['scroll_changes'] = changes
    results['scroll_display_frames'] = total
    results['method'] = 'image-change (quantised by 60Hz display)'

    e.close()
    return results


def main():
    as_json = '--json' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    core, rom = default_paths()
    if args:
        rom = args[0]

    frames = int(os.environ.get('BENCH_FRAMES', '600'))
    r = run_bench(rom, core, frames=frames)

    if as_json:
        print(json.dumps(r, indent=2))
    else:
        print('ROM: %s' % rom)
        det = ('%d frames rendered' % r['scroll_frames_rendered']) if 'scroll_frames_rendered' in r \
              else ('%d image changes' % r.get('scroll_changes', 0))
        print('  scrolling gameplay : %.1f fps  (%s in %d display frames, via %s)'
              % (r['scroll_fps'], det, r['scroll_display_frames'], r['method']))
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""
Precise render-cost profiler.

The frame-change benchmark (bench.py) is quantised by the 60 Hz display, so it
cannot distinguish anything faster than ~50 fps.  This profiler instead reads a
counter the ROM itself publishes.

Build the ROM with -DPROFILE_FRAME and it will time G_Tick()+R_DrawFrame() with
the SH-2 watchdog timer, average over 64 frames, and write the result into
COMM12/COMM14.  We read those registers straight out of the emulator's 32X
state, so the number is real SH-2 time, independent of display rate.

  frame_ticks -> milliseconds via Mars_FRTCounter2Msec's constant:
      WDT is clocked at Fs/2 with the 4096 divider d32xr sets up, so
      ms = ticks * 4096 * 1000 / clock.   We report raw ticks and the
      implied fps so relative comparisons are exact.
"""

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runner import Emu, default_paths     # noqa: E402

NTSC_CLOCK = 23011360.0


def read_comm(emu):
    """
    Pull COMM12/COMM14 out of the core's 32X register block.
    PicoDrive exposes them through retro_get_memory_data(RETRO_MEMORY_SYSTEM_RAM)?
    Not reliably - instead we scan the core's exported Pico32x symbol.
    """
    core = emu.core
    try:
        sym = ctypes.c_void_p.in_dll(core, 'Pico32xMem')
    except ValueError:
        return None
    return sym


def main():
    core_path, rom = default_paths()
    if len(sys.argv) > 1:
        rom = sys.argv[1]

    e = Emu(core_path, rom)
    e.load()
    e.run(120)

    # locate the 32X comm registers inside the core
    core = e.core
    regs = None
    for name in ('Pico32x',):
        try:
            addr = ctypes.addressof(ctypes.c_char.in_dll(core, name))
            regs = addr
            break
        except ValueError:
            continue

    if regs is None:
        print('could not find Pico32x symbol; falling back to frame-change rate')
        e.close()
        return 1

    # Pico32x layout: struct Pico32x { u16 regs[0x20]; ... }
    # COMM0..COMM14 are regs[0x10..0x17] in PicoDrive's numbering.
    buf = (ctypes.c_uint16 * 0x20).from_address(regs)

    e.press('RIGHT')
    samples = []
    for _ in range(40):
        e.run(30)
        lo = buf[0x16]
        hi = buf[0x17]
        val = (hi << 16) | lo
        if val:
            samples.append(val)
    e.release_all()
    e.close()

    if not samples:
        print('no profile samples captured (was the ROM built with -DPROFILE_FRAME?)')
        return 1

    samples.sort()
    med = samples[len(samples) // 2]
    ms = med * 4096.0 * 1000.0 / NTSC_CLOCK / 4096.0   # WDT tick -> ms
    print('render+tick: %d WDT ticks/frame (median of %d samples)' % (med, len(samples)))
    if ms > 0:
        print('             ~%.2f ms -> %.1f fps uncapped' % (ms, 1000.0 / ms))
    return 0


if __name__ == '__main__':
    sys.exit(main())

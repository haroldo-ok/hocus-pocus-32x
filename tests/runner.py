#!/usr/bin/env python3
"""
Headless libretro harness for the Hocus Pocus 32X ROM.

Loads picodrive_libretro.so via ctypes, runs the ROM for N frames, feeds
controller input, and captures the video output as raw RGB frames so tests can
assert on what is actually on screen.

This is a real emulation of the SH-2 code in the ROM - not a simulation of the
game logic - so it exercises the exact binary that would run on hardware.
"""

import ctypes
import os
import struct
import sys

# ---------------------------------------------------------------- libretro API
RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY = 9
RETRO_ENVIRONMENT_SET_PIXEL_FORMAT = 10
RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY = 31
RETRO_ENVIRONMENT_GET_VARIABLE = 15
RETRO_ENVIRONMENT_GET_LOG_INTERFACE = 27
RETRO_ENVIRONMENT_GET_CAN_DUPE = 3

RETRO_PIXEL_FORMAT_0RGB1555 = 0
RETRO_PIXEL_FORMAT_XRGB8888 = 1
RETRO_PIXEL_FORMAT_RGB565 = 2

RETRO_DEVICE_JOYPAD = 1

# joypad button ids
JOY_B = 0
JOY_Y = 1
JOY_SELECT = 2
JOY_START = 3
JOY_UP = 4
JOY_DOWN = 5
JOY_LEFT = 6
JOY_RIGHT = 7
JOY_A = 8
JOY_X = 9

BUTTON_NAMES = {
    'A': JOY_A, 'B': JOY_B, 'C': JOY_X,      # MD A/B/C -> retro A/B/X
    'START': JOY_START,
    'UP': JOY_UP, 'DOWN': JOY_DOWN, 'LEFT': JOY_LEFT, 'RIGHT': JOY_RIGHT,
}


class retro_game_info(ctypes.Structure):
    _fields_ = [
        ('path', ctypes.c_char_p),
        ('data', ctypes.c_void_p),
        ('size', ctypes.c_size_t),
        ('meta', ctypes.c_char_p),
    ]


class retro_system_av_info(ctypes.Structure):
    _fields_ = [
        ('base_width', ctypes.c_uint),
        ('base_height', ctypes.c_uint),
        ('max_width', ctypes.c_uint),
        ('max_height', ctypes.c_uint),
        ('aspect_ratio', ctypes.c_float),
        ('fps', ctypes.c_double),
        ('sample_rate', ctypes.c_double),
    ]


class Frame:
    """One captured video frame, converted to RGB888."""

    __slots__ = ('width', 'height', 'pixels')

    def __init__(self, width, height, pixels):
        self.width = width
        self.height = height
        self.pixels = pixels        # bytes, w*h*3

    def unique_colours(self):
        s = set()
        p = self.pixels
        for i in range(0, len(p), 3):
            s.add(p[i:i + 3])
        return s

    def is_black(self, threshold=8):
        """True if every pixel is at/below `threshold` on all channels."""
        return max(self.pixels) <= threshold

    def nonblack_ratio(self, threshold=8):
        n = 0
        p = self.pixels
        total = len(p) // 3
        for i in range(0, len(p), 3):
            if p[i] > threshold or p[i + 1] > threshold or p[i + 2] > threshold:
                n += 1
        return n / total if total else 0.0

    def save_png(self, path):
        import zlib
        w, h = self.width, self.height
        raw = bytearray()
        for y in range(h):
            raw.append(0)
            raw += self.pixels[y * w * 3:(y + 1) * w * 3]

        def chunk(t, data):
            c = struct.pack('>I', len(data)) + t + data
            return c + struct.pack('>I', zlib.crc32(t + data) & 0xffffffff)

        png = b'\x89PNG\r\n\x1a\n'
        png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress(bytes(raw), 6))
        png += chunk(b'IEND', b'')
        with open(path, 'wb') as f:
            f.write(png)


class Emu:
    def __init__(self, core_path, rom_path, system_dir=None):
        self.core = ctypes.CDLL(core_path)
        self.rom_path = rom_path
        self.system_dir = (system_dir or os.path.dirname(os.path.abspath(rom_path))).encode()
        self._frames = []
        self.pixel_format = RETRO_PIXEL_FORMAT_RGB565
        self.buttons = set()
        self.last_frame = None
        self._setup_prototypes()
        self._install_callbacks()

    def _setup_prototypes(self):
        c = self.core
        c.retro_init.restype = None
        c.retro_deinit.restype = None
        c.retro_run.restype = None
        c.retro_load_game.restype = ctypes.c_bool
        c.retro_load_game.argtypes = [ctypes.POINTER(retro_game_info)]
        c.retro_get_system_av_info.argtypes = [ctypes.POINTER(retro_system_av_info)]
        c.retro_unload_game.restype = None

    def _install_callbacks(self):
        c = self.core

        ENV_CB = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.c_void_p)
        VIDEO_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_uint, ctypes.c_size_t)
        AUDIO_CB = ctypes.CFUNCTYPE(None, ctypes.c_int16, ctypes.c_int16)
        AUDIO_BATCH_CB = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p,
                                          ctypes.c_size_t)
        INPUT_POLL_CB = ctypes.CFUNCTYPE(None)
        INPUT_STATE_CB = ctypes.CFUNCTYPE(ctypes.c_int16, ctypes.c_uint,
                                          ctypes.c_uint, ctypes.c_uint,
                                          ctypes.c_uint)

        def env(cmd, data):
            if cmd == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
                fmt = ctypes.cast(data, ctypes.POINTER(ctypes.c_int)).contents.value
                self.pixel_format = fmt
                return True
            if cmd in (RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY,
                       RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY):
                p = ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))
                p.contents = ctypes.c_char_p(self.system_dir)
                return True
            if cmd == RETRO_ENVIRONMENT_GET_CAN_DUPE:
                ctypes.cast(data, ctypes.POINTER(ctypes.c_bool)).contents.value = True
                return True
            return False

        def video(data, width, height, pitch):
            if not data:
                # duped frame - keep the previous one
                if self.last_frame is not None:
                    self._frames.append(self.last_frame)
                return
            self.last_frame = self._convert(data, width, height, pitch)
            self._frames.append(self.last_frame)

        def audio(l, r):
            pass

        def audio_batch(data, frames):
            return frames

        def input_poll():
            pass

        def input_state(port, device, index, id_):
            if port != 0:
                return 0
            return 1 if id_ in self.buttons else 0

        self._env_cb = ENV_CB(env)
        self._video_cb = VIDEO_CB(video)
        self._audio_cb = AUDIO_CB(audio)
        self._audio_batch_cb = AUDIO_BATCH_CB(audio_batch)
        self._input_poll_cb = INPUT_POLL_CB(input_poll)
        self._input_state_cb = INPUT_STATE_CB(input_state)

        c.retro_set_environment(self._env_cb)
        c.retro_set_video_refresh(self._video_cb)
        c.retro_set_audio_sample(self._audio_cb)
        c.retro_set_audio_sample_batch(self._audio_batch_cb)
        c.retro_set_input_poll(self._input_poll_cb)
        c.retro_set_input_state(self._input_state_cb)

    def _convert(self, data, width, height, pitch):
        size = pitch * height
        buf = ctypes.string_at(data, size)
        out = bytearray(width * height * 3)
        fmt = self.pixel_format
        o = 0
        for y in range(height):
            row = y * pitch
            for x in range(width):
                if fmt == RETRO_PIXEL_FORMAT_XRGB8888:
                    i = row + x * 4
                    b, g, r = buf[i], buf[i + 1], buf[i + 2]
                else:
                    i = row + x * 2
                    v = buf[i] | (buf[i + 1] << 8)
                    if fmt == RETRO_PIXEL_FORMAT_RGB565:
                        r = ((v >> 11) & 0x1F) << 3
                        g = ((v >> 5) & 0x3F) << 2
                        b = (v & 0x1F) << 3
                    else:   # 0RGB1555
                        r = ((v >> 10) & 0x1F) << 3
                        g = ((v >> 5) & 0x1F) << 3
                        b = (v & 0x1F) << 3
                out[o] = r
                out[o + 1] = g
                out[o + 2] = b
                o += 3
        return Frame(width, height, bytes(out))

    def load(self):
        self.core.retro_init()
        with open(self.rom_path, 'rb') as f:
            rom = f.read()
        self._rombuf = ctypes.create_string_buffer(rom, len(rom))
        info = retro_game_info(
            path=self.rom_path.encode(),
            data=ctypes.cast(self._rombuf, ctypes.c_void_p),
            size=len(rom),
            meta=None,
        )
        ok = self.core.retro_load_game(ctypes.byref(info))
        if not ok:
            raise RuntimeError('retro_load_game failed')
        av = retro_system_av_info()
        self.core.retro_get_system_av_info(ctypes.byref(av))
        self.av = av
        return av

    def press(self, *names):
        for n in names:
            self.buttons.add(BUTTON_NAMES[n.upper()])

    def release(self, *names):
        for n in names:
            self.buttons.discard(BUTTON_NAMES[n.upper()])

    def release_all(self):
        self.buttons.clear()

    def run(self, frames=1, capture=False):
        """Run `frames` frames. Returns the captured frames if capture=True."""
        start = len(self._frames)
        for _ in range(frames):
            self.core.retro_run()
        got = self._frames[start:]
        if not capture:
            # keep only the last to bound memory
            self._frames = self._frames[:start]
            if got:
                self._frames.append(got[-1])
        return got

    def frame(self):
        return self.last_frame

    def close(self):
        try:
            self.core.retro_unload_game()
            self.core.retro_deinit()
        except Exception:
            pass


def default_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    core = os.environ.get('PICODRIVE_CORE',
                          '/home/user/_dl/picodrive/picodrive_libretro.so')
    rom = os.environ.get('HOCUS_ROM', os.path.join(root, 'rom', 'hocus32x.32x'))
    return core, rom


if __name__ == '__main__':
    core, rom = default_paths()
    print('core:', core)
    print('rom :', rom)
    emu = Emu(core, rom)
    av = emu.load()
    print('av: %dx%d fps=%.2f' % (av.base_width, av.base_height, av.fps))
    emu.run(120)
    f = emu.frame()
    if f:
        print('frame %dx%d nonblack=%.3f colours=%d'
              % (f.width, f.height, f.nonblack_ratio(), len(f.unique_colours())))
        out = sys.argv[1] if len(sys.argv) > 1 else '/tmp/frame.png'
        f.save_png(out)
        print('saved', out)
    emu.close()

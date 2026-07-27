#!/usr/bin/env python3
"""
Patch a 32X ROM header: mapper signature, titles, ROM end address and the
Mega Drive checksum.  Equivalent to d32xr's romheaderfix.
"""
import sys


def main():
    if len(sys.argv) != 4:
        print('usage: romfix.py "<mapper>" "<title>" <rom>')
        return 1
    mapper, title, path = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(path, 'r+b') as f:
        rom = bytearray(f.read())

        def put(off, s, n):
            b = s.encode('ascii', 'replace')[:n]
            b = b + b' ' * (n - len(b))
            rom[off:off + n] = b

        put(0x100, mapper, 16)
        put(0x120, title, 32)     # domestic name
        put(0x150, title, 32)     # overseas name

        # ROM start/end
        end = len(rom) - 1
        rom[0x1A0:0x1A4] = (0).to_bytes(4, 'big')
        rom[0x1A4:0x1A8] = end.to_bytes(4, 'big')

        # Mega Drive checksum: sum of every word from 0x200 to the end
        s = 0
        for i in range(0x200, len(rom) - 1, 2):
            s = (s + ((rom[i] << 8) | rom[i + 1])) & 0xFFFF
        rom[0x18E] = (s >> 8) & 0xFF
        rom[0x18F] = s & 0xFF

        f.seek(0)
        f.write(rom)

    print('romfix: %s  mapper=%r title=%r size=%d checksum=0x%04X' %
          (path, mapper, title, len(rom), s))
    return 0


if __name__ == '__main__':
    sys.exit(main())

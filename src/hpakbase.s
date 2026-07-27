        .text

        .align  2

! Address of the asset pack inside the cartridge.  The Makefile assembles this
! file twice: once with a dummy value to measure the program image, then again
! with the real, 1KB-aligned offset the pack is written at.

        .global _hpak_base
_hpak_base:
        .long   0x2000000+HPAKBASE*1024

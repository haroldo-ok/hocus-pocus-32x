# ---------------------------------------------------------------------------
# Hocus Pocus 32X
#
#   make            build the ROM (build/hocus32x.32x)
#   make assets     regenerate the asset pack from the original game data
#   make clean
#
# Requires the Chilly Willy / 32XDK sega toolchain.  Point GENDEV at it, e.g.
#   make GENDEV=/home/user/toolchain/opt/toolchains/sega
# ---------------------------------------------------------------------------

ifdef GENDEV
ROOTDIR = $(GENDEV)
else
ROOTDIR = /opt/toolchains/sega
endif

TARGET  ?= hocus32x
TITLE   ?= HOCUS POCUS 32X
VERSION ?= 1.0
MAPPER   = SEGA 32X

BUILD  = build
SRC    = src
TOOLS  = tools

HPAK   = $(BUILD)/hocus.hpak

# Original shareware data (extracted from 1hp11.zip -> HPSW11.SHR)
GAMEDATA ?= data
HOCUS_DAT ?= $(GAMEDATA)/HOCUS.DAT
HOCUS_EXE ?= $(GAMEDATA)/HOCUS.EXE
FATDIR    ?= $(GAMEDATA)

LDSCRIPTSDIR = $(ROOTDIR)/ldscripts

LIBPATH = -L$(ROOTDIR)/sh-elf/lib -L$(ROOTDIR)/sh-elf/lib/gcc/sh-elf/12.1.0 \
          -L$(ROOTDIR)/sh-elf/sh-elf/lib
INCPATH = -I$(SRC) -I$(ROOTDIR)/sh-elf/include -I$(ROOTDIR)/sh-elf/sh-elf/include

CCFLAGS  = -c -std=c11 -m2 -mb -mtas
CCFLAGS += -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers
CCFLAGS += -D__32X__ -DMARS -DDISABLE_DMA_SOUND -DDISABLE_CDFS
CCFLAGS += -fomit-frame-pointer -ffunction-sections -fdata-sections -Os
CCFLAGS += $(EXTRA_CFLAGS)

LDFLAGS  = -T $(LDSCRIPTSDIR)/mars.ld -Wl,-Map=$(BUILD)/output.map -nostdlib \
           -Wl,--gc-sections,--sort-section=alignment --specs=nosys.specs

ASFLAGS  = --big

# marshw.c must not be built with LTO / aggressive opts (hardware timing)
MARSHWCFLAGS := $(CCFLAGS)

PREFIX = $(ROOTDIR)/sh-elf/bin/sh-elf-
CC   = $(PREFIX)gcc
AS   = $(PREFIX)as
OBJC = $(PREFIX)objcopy

DD = dd
RM = rm -f

LIBS = $(LIBPATH) -lc -lgcc -lnosys

OBJS = \
	$(BUILD)/crt0.o \
	$(BUILD)/hp_main.o \
	$(BUILD)/hp_game.o \
	$(BUILD)/hp_render.o \
	$(BUILD)/hp_pak.o \
	$(BUILD)/marshw.o \
	$(BUILD)/hpakbase.o

.PHONY: all clean assets rom dirs

all: rom

dirs:
	@mkdir -p $(BUILD)

# --------------------------------------------------------------- asset pack
assets: $(HPAK)

$(HPAK): $(TOOLS)/mkassets.py $(HOCUS_DAT) $(HOCUS_EXE) | dirs
	python3 $(TOOLS)/mkassets.py $(HOCUS_DAT) $(HOCUS_EXE) $@ $(FATDIR)

# ------------------------------------------------------------------ 68k blob
$(SRC)/src-md/m68k.bin:
	$(MAKE) -C $(SRC)/src-md GENDEV=$(ROOTDIR)

# --------------------------------------------------------------------- build
$(BUILD)/crt0.o: $(SRC)/crt0.s $(SRC)/src-md/m68k.bin | dirs
	cd $(SRC) && $(AS) $(ASFLAGS) crt0.s -o ../$(BUILD)/crt0.o

$(BUILD)/marshw.o: $(SRC)/marshw.c | dirs
	$(CC) $(MARSHWCFLAGS) $(INCPATH) $< -o $@

$(BUILD)/%.o: $(SRC)/%.c | dirs
	$(CC) $(CCFLAGS) $(INCPATH) $< -o $@

# Pass 1: assemble hpakbase with a placeholder so we can measure the image.
$(BUILD)/hpakbase_tmp.o: $(SRC)/hpakbase.s | dirs
	$(AS) $(ASFLAGS) --defsym HPAKBASE=0 $< -o $@

$(BUILD)/$(TARGET)_tmp.elf: $(BUILD)/crt0.o $(BUILD)/hp_main.o $(BUILD)/hp_game.o \
                            $(BUILD)/hp_render.o $(BUILD)/hp_pak.o $(BUILD)/marshw.o \
                            $(BUILD)/hpakbase_tmp.o
	$(CC) $(LDFLAGS) $(BUILD)/crt0.o $(BUILD)/hp_main.o $(BUILD)/hp_game.o \
	      $(BUILD)/hp_render.o $(BUILD)/hp_pak.o $(BUILD)/marshw.o \
	      $(BUILD)/hpakbase_tmp.o $(LIBS) -o $@

$(BUILD)/temp.bin: $(BUILD)/$(TARGET)_tmp.elf
	$(OBJC) -O binary $< $(BUILD)/temp2.bin
	$(DD) if=$(BUILD)/temp2.bin of=$@ bs=1K conv=sync status=none
	$(RM) $(BUILD)/temp2.bin

# Pass 2: real base, then link for real.
rom: $(BUILD)/temp.bin $(HPAK)
	$(eval BINSIZE=$(shell expr `stat -L -c %s $(BUILD)/temp.bin` / 1024))
	$(AS) $(ASFLAGS) --defsym HPAKBASE=$(BINSIZE) $(SRC)/hpakbase.s -o $(BUILD)/hpakbase.o
	$(CC) $(LDFLAGS) $(OBJS) $(LIBS) -o $(BUILD)/$(TARGET).elf
	$(OBJC) -O binary $(BUILD)/$(TARGET).elf $(BUILD)/prog.bin
	$(DD) if=$(BUILD)/prog.bin of=$(BUILD)/prog_pad.bin bs=$(BINSIZE)K conv=sync status=none
	cat $(BUILD)/prog_pad.bin $(HPAK) > $(BUILD)/rom_raw.bin
	$(DD) if=$(BUILD)/rom_raw.bin of=$(BUILD)/$(TARGET).32x bs=512K conv=sync status=none
	$(RM) $(BUILD)/prog.bin $(BUILD)/prog_pad.bin $(BUILD)/rom_raw.bin
	python3 $(TOOLS)/romfix.py "$(MAPPER)" "$(TITLE) v$(VERSION)" $(BUILD)/$(TARGET).32x
	@echo "=== built $(BUILD)/$(TARGET).32x (prog $(BINSIZE)K) ==="
	@ls -la $(BUILD)/$(TARGET).32x

clean:
	$(RM) $(BUILD)/*.o $(BUILD)/*.elf $(BUILD)/*.bin $(BUILD)/*.32x $(BUILD)/output.map
	$(MAKE) -C $(SRC)/src-md clean GENDEV=$(ROOTDIR) || true

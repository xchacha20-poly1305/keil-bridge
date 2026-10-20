# GCC build of a Keil project converted by keil-bridge.
#
# Project settings (sources, defines, CPU, memory) live in keil-bridge/project.mk,
# which `keil-bridge regen` rewrites. This Makefile is not regenerated.
#
#   make                 build build/<target>.elf/.hex/.bin
#   make V=1             show full commands
#   make OPT=-Os CSTD=gnu11 EXTRA_CFLAGS=-Wall
#   make size | clean | flash | status | backport | regen

include keil-bridge/project.mk

PREFIX  ?= arm-none-eabi-
CC      := $(PREFIX)gcc
OBJCOPY := $(PREFIX)objcopy
SIZE    := $(PREFIX)size

BUILD ?= build
ELF   := $(BUILD)/$(TARGET).elf
HEX   := $(BUILD)/$(TARGET).hex
BIN   := $(BUILD)/$(TARGET).bin
MAP   := $(BUILD)/$(TARGET).map

OBJS := $(patsubst %,$(BUILD)/obj/%.o,$(SOURCES) $(GENERATED_SOURCES))

# Keep in sync with compile_command() in keil_bridge/generate.py.
CFLAGS  = $(CPU_FLAGS) -std=$(CSTD) $(OPT) -g $(LANG_FLAGS) $(DEFINES) $(INCLUDES) \
          -include keil-bridge/keil_compat.h -ffunction-sections -fdata-sections $(EXTRA_CFLAGS)
ASFLAGS = $(CPU_FLAGS) -g $(DEFINES) $(INCLUDES) $(EXTRA_ASFLAGS)
LDFLAGS = $(CPU_FLAGS) $(LIBC_SPECS) -T $(LDSCRIPT) -Wl,--gc-sections -Wl,-Map=$(MAP) \
          -Wl,--print-memory-usage $(EXTRA_LDFLAGS)

ifeq ($(V),1)
Q :=
else
Q := @
endif

.PHONY: all size clean flash status backport regen

all: $(HEX) $(BIN)

$(ELF): $(OBJS) $(LDSCRIPT)
	@echo "LD      $@"
	$(Q)$(CC) $(LDFLAGS) -o $@ $(OBJS)

$(HEX): $(ELF)
	@echo "HEX     $@"
	$(Q)$(OBJCOPY) -O ihex $< $@

$(BIN): $(ELF)
	@echo "BIN     $@"
	$(Q)$(OBJCOPY) -O binary $< $@

$(BUILD)/obj/%.c.o: %.c
	@echo "CC      $<"
	@mkdir -p $(dir $@)
	$(Q)$(CC) $(CFLAGS) -MMD -MP -c $< -o $@

$(BUILD)/obj/%.s.o: %.s
	@echo "AS      $<"
	@mkdir -p $(dir $@)
	$(Q)$(CC) $(ASFLAGS) -c $< -o $@

$(BUILD)/obj/%.S.o: %.S
	@echo "AS      $<"
	@mkdir -p $(dir $@)
	$(Q)$(CC) $(ASFLAGS) -MMD -MP -c $< -o $@

# Rebuild everything when the build settings change.
$(OBJS): keil-bridge/project.mk Makefile

size: $(ELF)
	$(SIZE) $<

clean:
	rm -rf $(BUILD)

FLASHER ?= st-flash

flash: all
ifeq ($(FLASHER),st-flash)
	st-flash --reset write $(BIN) $(FLASH_ORIGIN)
else ifeq ($(FLASHER),pyocd)
	pyocd flash -t $(PYOCD_TARGET) $(HEX)
else ifeq ($(FLASHER),openocd)
	openocd $(OPENOCD_ARGS) -c "program $(ELF) verify reset exit"
else
	$(error unknown FLASHER '$(FLASHER)'; use st-flash, pyocd or openocd)
endif

KEIL_BRIDGE ?= keil-bridge

status:
	$(KEIL_BRIDGE) status .

backport:
	$(KEIL_BRIDGE) backport .

regen:
	$(KEIL_BRIDGE) regen .

-include $(OBJS:.o=.d)

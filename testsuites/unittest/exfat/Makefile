# exfat cmocka host harness.
# Runs on dev host (Linux x86_64 or macOS). Independent of the kernel build.
#
# Usage:
#   make           # build & run
#   make build     # build only
#   make clean
#
# Requires: gcc/clang, cmocka headers + library installed (libcmocka-dev /
# brew install cmocka).

CC      ?= gcc
EXFAT   := ../../../fs/exfat
SRCDIR  := .
STUBDIR := host_stubs

# Pull in stubs FIRST so they shadow real kernel headers.
CFLAGS  := -std=c11 -O0 -g -Wall -Wextra -Wno-unused-parameter \
           -Wno-unused-but-set-variable -Wno-unused-function \
           -DLOSCFG_FS_EXFAT \
           -I$(STUBDIR) -I$(SRCDIR) -I$(EXFAT)/include
LDFLAGS :=
LDLIBS  := -lcmocka

# Production sources (compiled with stubs in place).
PROD_SRCS := \
    $(EXFAT)/util/exfat_chksum.c   \
    $(EXFAT)/util/exfat_options.c  \
    $(EXFAT)/util/exfat_upcase.c   \
    $(EXFAT)/exfat_dentry.c        \
    $(EXFAT)/exfat_balloc.c

# Harness sources.
HARNESS_SRCS := \
    mock_disk.c            \
    exfat_image_builder.c  \
    main.c                 \
    test_chksum.c          \
    test_options.c         \
    test_dentry.c          \
    test_balloc.c          \
    test_upcase.c

OBJS := $(PROD_SRCS:.c=.o) $(HARNESS_SRCS:.c=.o)
TARGET := exfat_tests

.PHONY: all build run clean

all: run

build: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

run: build
	@echo "==== running $(TARGET) ===="
	./$(TARGET)

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f $(OBJS) $(TARGET)

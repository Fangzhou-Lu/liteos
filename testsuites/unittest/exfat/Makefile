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
           -D_GNU_SOURCE -DLOSCFG_FS_EXFAT \
           -I$(STUBDIR) -I$(SRCDIR) -I$(EXFAT)/include
LDFLAGS :=
LDLIBS  := -lcmocka

# macOS: cmocka from Homebrew lives outside the default search paths.
# Detect at make time and prepend; harmless on Linux/CI where files don't exist.
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
  BREW_PREFIX := $(shell brew --prefix cmocka 2>/dev/null)
  ifneq ($(BREW_PREFIX),)
    CFLAGS  += -I$(BREW_PREFIX)/include
    LDFLAGS += -L$(BREW_PREFIX)/lib
  endif
endif

# Production sources (compiled with stubs in place).
PROD_SRCS := \
    $(EXFAT)/exfat_module.c  \
    $(EXFAT)/exfat_cluster.c \
    $(EXFAT)/exfat_nls.c     \
    $(EXFAT)/exfat_inode.c   \
    $(EXFAT)/exfat_file.c    \
    $(EXFAT)/exfat_super.c   \
    $(EXFAT)/exfat_ops.c
# All 7 production .c files are compiled. host_stubs/ provides minimal shims
# for VFS-side surface (fs/fs.h, fs/mount.h, fs/file.h, vnode.h, path_cache.h,
# los_tables.h) so exfat_super.c + exfat_ops.c — which in real kernel reference
# linker-section tables and full VFS — compile cleanly without exercising the
# VFS path. Tests target FS-internal helpers; VFS callbacks like VfsExfatMount
# are never invoked from cmocka.

# Harness sources.
HARNESS_SRCS := \
    mock_disk.c            \
    exfat_image_builder.c  \
    main.c                 \
    test_chksum.c          \
    test_options.c         \
    test_dentry.c          \
    test_balloc.c          \
    test_upcase.c          \
    test_fat_chain.c       \
    test_nls_utf16.c       \
    test_dentry_iter.c     \
    test_dentry_set_write.c \
    test_alloc_dentry_slot.c \
    test_inode_alloc.c     \
    test_open_close.c      \
    test_getattr_seek.c    \
    test_read.c            \
    test_readdir.c         \
    test_lookup.c          \
    test_write.c           \
    test_vol_flags.c       \
    test_free_cluster.c    \
    test_alloc_cluster.c   \
    test_truncate_extend.c   \
    test_truncate_shrink.c   \
    test_truncate_vop.c      \
    test_mkdir.c          \
    test_inode_metadata_model.c \
    test_unlink.c          \
    test_rmdir.c           \
    test_rename.c

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

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

# Production sources (compiled with stubs in place).
PROD_SRCS := \
    $(EXFAT)/util/exfat_chksum.c     \
    $(EXFAT)/util/exfat_options.c    \
    $(EXFAT)/util/exfat_upcase.c     \
    $(EXFAT)/util/exfat_fat_chain.c  \
    $(EXFAT)/util/exfat_nls_utf16.c  \
    $(EXFAT)/util/exfat_vol_flags.c  \
    $(EXFAT)/util/exfat_free_cluster.c \
    $(EXFAT)/util/exfat_alloc_cluster.c \
    $(EXFAT)/exfat_dentry.c          \
    $(EXFAT)/exfat_dentry_iter.c     \
    $(EXFAT)/exfat_dentry_set_write.c \
    $(EXFAT)/exfat_balloc.c          \
    $(EXFAT)/exfat_inode_alloc.c     \
    $(EXFAT)/exfat_open_close.c      \
    $(EXFAT)/exfat_attr.c            \
    $(EXFAT)/exfat_file.c            \
    $(EXFAT)/exfat_readdir.c         \
    $(EXFAT)/exfat_lookup.c          \
    $(EXFAT)/exfat_write.c           \
    $(EXFAT)/exfat_truncate_extend.c \
    $(EXFAT)/exfat_truncate_shrink.c \
    $(EXFAT)/exfat_truncate_vop.c

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
    test_truncate_vop.c

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

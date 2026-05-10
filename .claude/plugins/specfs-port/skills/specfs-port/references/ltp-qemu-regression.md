# LTP + QEMU 端到端回归参考

补充 [SKILL.md §阶段 5](../SKILL.md#阶段-5--构建--两层回归) 中的 Wave B（QEMU LTP smoke）。SKILL.md 只列契约；
本文给完整 LTP 交叉编译流程、镜像注入、QEMU 启停、log 解析。

## 关键事实：必须用 OHOS clang 动态链接

**静态链接 glibc ARM ELF 在 LiteOS-A 上无法加载**。原因：LiteOS-A 的 ELF loader
（`OsLoadELFSegment`）不识 PT_GNU_STACK 等 glibc-only 段，加载时 OOM 退出，所有用例 RC=126。

正解：用 OHOS clang（`armv7-unknown-linux-ohos-clang`）动态链接到 LiteOS-A 的 musl。
LiteOS-A rootfs 已铺设 `/lib/libc.so` + `/lib/ld-musl-arm.so.1`。

## 一次性 LTP 准备（远程主机）

```bash
ssh 192.168.1.15
cd /mnt/work/test
git clone --depth=1 --branch=20240930 https://github.com/linux-test-project/ltp.git
cd ltp && make autotools

SYSROOT=/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo/sysroot
./configure --host=arm-liteos-ohos --prefix=/opt/ltp \
    CC=armv7-unknown-linux-ohos-clang \
    AR=llvm-ar STRIP=llvm-strip \
    CFLAGS="-mfloat-abi=softfp -mfpu=neon-vfpv4 -mcpu=cortex-a7 -O2 --sysroot=$SYSROOT" \
    LDFLAGS="-mfloat-abi=softfp -mfpu=neon-vfpv4 -mcpu=cortex-a7 --sysroot=$SYSROOT"
# 注：不加 -static！rootfs 已有 /lib/libc.so + /lib/ld-musl-arm.so.1

# 只 build smoke 子集
for t in creat/creat01 open/open01 read/read01 write/write01 unlink/unlink05 stat/stat01; do
    make -C testcases/kernel/syscalls/$t -j
done

# 验证产物是 dynamic ARM ELF
file testcases/kernel/syscalls/{creat/creat01,open/open01,...}
# 应该都说 "ELF 32-bit LSB executable, ARM, EABI5, dynamically linked,
# interpreter /lib/ld-musl-arm.so.1, ..."

# strip + 打包
llvm-strip testcases/kernel/syscalls/*/*01
mkdir -p /mnt/work/test/ltp_smoke/bin
cp testcases/kernel/syscalls/{creat/creat01,...} /mnt/work/test/ltp_smoke/bin/

cat > /mnt/work/test/ltp_smoke/run_exfat.sh <<'EOF'
#!/bin/sh
cd /storage/ltp_smoke/bin
echo "=== LTP exFAT Smoke Test ==="
mount -t exfat /dev/mmcblk0p3 /mnt/exfat
echo "MOUNT_RC=$?"
for t in creat01 open01 read01 write01 unlink05 stat01; do
    ./$t -P /mnt/exfat 2>&1
    echo "RC=$? ($t)"
done
echo "=== SUMMARY ==="
echo LTP_DONE
EOF
chmod +x /mnt/work/test/ltp_smoke/run_exfat.sh

cd /mnt/work/test && tar -czf ltp_smoke.tar.gz ltp_smoke/
# 期望 ≤ 30 MB（dynamic ELF 比静态小很多）
```

## QEMU 启停脚本契约（`tools/regress/qemu_ltp_run.sh`）

入参（环境变量 ≥ 位置参数）：
- `SRC` — OH 输出目录（默认 `/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo`）
- `FLASH` — smallmmc.img 路径（默认 `$SRC/../smallmmc.img`）
- `TIMEOUT_S` — 整体超时（默认 600）

行为（远程 SSH 包装）：

1. **幂等磁盘镜像准备**：
 - 探测 p2/p4 当前 magic（`mkfs.fat` / `EXFAT `），已是目标格式则跳过 mkfs。
 - 仅当 p2 没 ltp_smoke/ 时才解压 tarball；仅当 p4 没 etc/ 时才写 /etc payload。
2. **bootargs**：98 字节 + 单 NUL 终止（用 `perl -e 'print chr(0)'` 避免 printf 转义陷阱）。
3. **QEMU 启动**：`-nographic` + 单 virtio-blk + virtio-rng-device。绝不 `-display none`
 （QEMU 11.0+ 会开 VNC 偷走串口）。
4. **FIFO 喂命令**：本地 mkfifo + 后台 qemu，串口 stdout tee 到 log 文件。
5. **shell 探测**：grep `OHOS:/` 等待 prompt（最多 60s）。
6. **测试序列**：mount → cd /storage/ltp_smoke → sh run_exfat.sh → echo TEST_DONE_MARKER。
7. **panic / hang 守护**：每 1s 扫 log，watch
 `panic | Oops | Kernel BUG | Unhandled` 命中立刻 kill QEMU + exit 2。
 超 TIMEOUT_S 没见 TEST_DONE_MARKER → exit 2 hang。
8. **解析**：log 抽 `RC=...` 行，统计 PASS/FAIL，stdout 输出 `LTP_PASS=X LTP_FAIL=Y`。
9. **退出码**：0=全过、1=有失败、2=panic/hang。

不动 image：脚本不带 trap 删 image，一次制作多次重用——这是用户硬约束。

## QEMU 启动命令（CLAUDE.md §QEMU recipe 规范）

```bash
qemu-system-arm -M virt,gic-version=2,secure=on -cpu cortex-a7 -smp cpus=1 -m 1G \
    -bios $SRC/OHOS_Image.bin \
    -global virtio-mmio.force-legacy=false -nographic \
    -drive if=none,file=$FLASH,format=raw,id=mmc -device virtio-blk-device,drive=mmc \
    -device virtio-rng-device
```

不要：
- `-display none`（QEMU 11.0+ 会开 VNC 5900）
- `-device virtio-gpu-device,xres=...`（QEMU 11.0+ 已删）
- `qemu_run.sh`（重做镜像把 p4 数据擦掉）

## 已知预期 noise（不视为失败）

- `warning: /dev/mmcblk0 lost, force umount ret = -6`：boot 期 los_disk_deinit
 对未持有的 mount 做 umount，正常。
- `[ERR] virtio-mmio ID=1/16/18 device not found`：HDF probe 全 slot 走，未连接的失败。
- `[ERR][toybox:toybox]Unsupported syscall ID: 256`：musl init 调 `set_tid_address`。
 正常情况应该不出现——`syscall/syscall_lookup.h` 已注册 `SYSCALL_HAND_DEF(__NR_set_tid_address, ...)`。
 若仍出现 → 内核 build 是 stale，需重新跑 `kernel/liteos_a/build.sh`。

## 排查矩阵

| 症状 | 可能根因 | 检查点 |
|---|---|---|
| 5 个用例全 RC=126 | LTP 是静态 glibc ELF | `file <ELF>` 必须含 "dynamically linked" |
| 5 个用例全 RC=ENOSYS | LTP 走到了 main 但 syscall 缺 | 看 stderr `Unsupported syscall ID: N`，加 stub |
| MOUNT_RC≠0 | exFAT mount 出错 | 看 boot log 里 `[ERR]exfat: ...` 行 |
| umount 后 panic | g_<fs>Vops.Reclaim 未填 | 见 references/fs-debug-recipe.md "Reclaim hook" 一节 |
| qemu 卡 60s 不见 OHOS:/$ | bootargs 写错 / 内核 stuck | 看串口 log 末尾，dd 重写 bootargs |

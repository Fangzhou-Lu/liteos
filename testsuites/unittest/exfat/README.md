# testsuites/unittest/exfat — cmocka host 单元测试套件

宿主机（dev/CI host）上跑的 exFAT v1 helper 单元测试，与 LiteOS-A 内核构建解耦。
配合 `tools/regress/qemu_ltp_run.sh` 一起组成 v1 回归测试矩阵：

| 套件 | 跑在哪 | 测什么 |
|---|---|---|
| `testsuites/unittest/exfat` (本目录) | host (Linux/macOS) | 5 个 helper 翻译单元的纯函数 + mock IO 行为 |
| `qemu_ltp_run.sh`     | QEMU virt + LiteOS-A | 真挂载 + LTP smoke 操作 |

## 5 个被测翻译单元 / 49 个测点

| 翻译单元                       | 测点 | 关键 invariants |
|---|---|---|
| `fs/exfat/util/exfat_chksum.c`  | 8  | purity、byte-order-independence、not-iee-crc、skip-positions-fixed |
| `fs/exfat/util/exfat_options.c` | 18 | strict-key-rejection、defaults-preserved、octal-mask-bit-width、no-heap-leak |
| `fs/exfat/exfat_dentry.c`        | 10 | parse-byte-order、find-buf-leak-free、find-readonly、fat-traversal-bounded |
| `fs/exfat/exfat_balloc.c`        | 8  | load-leak-free、free-idempotent、count-mem-only、bitmap-tail-mask、bitmap-size-policy |
| `fs/exfat/util/exfat_upcase.c`   | 5  | checksum-verified、free-idempotent、leak-free、strict-no-fallback |

## 设计要点

1. **直接编译生产代码** —— 把 `fs/exfat/` 下 5 个 .c 文件用 `gcc -DLOSCFG_FS_EXFAT` 编进二进制，
   覆盖度 = 行覆盖度（不是再写一份测试用桩）。
2. **stub 替换 LiteOS 头** —— `host_stubs/` 下放替身 `los_typedef.h`、`los_mux.h`、
   `los_memory.h`、`los_printf.h`、`securec.h`、`disk.h`、`vnode.h`、`fs/file.h`、
   `fs/mount.h`。Makefile 把 `-Ihost_stubs` 放在最前，编译器优先选 stub。
3. **mock_disk** 把 `los_part_read`/`los_part_write` 路由到 RAM 缓冲：
   - `mock_part_read` —— 只读快照，跨测试可重复使用同一个 image。
   - `mock_part_write` —— 修改快照，**永不**回写宿主磁盘文件（用户硬要求）。
   - `mock_disk_set_read_fail_at(N)` —— 注入第 N 次读失败，用于 -EIO 路径。
4. **exfat_image_builder** 现场拼一份 6 KiB 的合法 exFAT 镜像（boot sector + FAT + 3 簇
   data），不依赖 host 上的 `mkfs.exfat`。chksum32 用与生产相同的 1-bit ROR + add 算法
   预先算好烤进 upcase dentry。负向变体：bad signature / bad fs_name / no bitmap dentry /
   corrupt upcase chksum / bitmap too small。

## 用法

### 远程 Linux（推荐——CI 同款环境）

```bash
ssh 192.168.1.15 "cd /mnt/work/openharmony/kernel/liteos_a/testsuites/unittest/exfat && make"
```

成功输出末尾应有 `=== exfat TOTAL FAILURES: 0 ===`，退出码 0。

### 本地（macOS / Linux）

需要安装 cmocka：

```bash
brew install cmocka            # macOS
sudo pacman -S cmocka          # Arch
sudo apt install libcmocka-dev # Debian/Ubuntu
make                           # 在本目录
```

### 单跑某个 suite

二进制 `exfat_tests` 把 5 个 suite 串行跑。要单跑某个 suite，直接 `cmocka` 的 group
filter 不支持；最快办法是临时注释 `main.c` 中其它 `run_suite()` 调用。

## 退出码语义

`./exfat_tests` 退出码：
- `0` —— 5 个 suite 全过。
- `1` —— 任一测点失败。

## 加新测点的流程

1. 在已有 `test_<subsystem>.c` 里加 `static void test_<name>(void **state) { ... }`。
2. 把它加到该文件末尾的 `const struct CMUnitTest test_<subsystem>_tests[]` 数组。
3. `make clean && make` —— 测点数会自动更新。

新加被测翻译单元（如 v2 `exfat_lookup.c`）：
1. Makefile 的 `PROD_SRCS` 列表加一条。
2. 新建 `test_lookup.c`，仿现有结构。
3. `main.c` 加 `extern` 声明 + `run_suite("lookup", ...)`。

## 已知限制

- `exfat_super.c::VfsExfatMount` **不在**本套件范围（它依赖 `VnodeAlloc` /
  `VfsHashInsert` / `los_part_find` / `SetDiskPartName` 等大量 VFS 框架接口；为它做 stub
  会让 stub 表脚本化）。Mount 的端到端覆盖留给 QEMU LTP 套件。
- `exfat_ops.c` 只是两个全 NULL 表，无可测逻辑。
- mock_disk 的 partition_id 维度恒为 0；多分区并发场景留给真机测试。

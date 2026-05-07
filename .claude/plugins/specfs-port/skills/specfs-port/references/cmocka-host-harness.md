# cmocka host 单元测试参考——加新 stage 测试的具体步骤

本参考补充 SKILL.md §阶段 5 中的 Layer A（cmocka host）测试套件。
SKILL.md 只列契约；本文给完整目录骨架、stub 设计与扩展流程。

## 目录骨架

```
testsuites/unittest/<name>_host/
├── Makefile # gcc/clang + -DLOSCFG_FS_<NAME> -Ihost_stubs，链接 libcmocka
├── host_stubs/ # 替身 LiteOS-A 头（让生产代码不动就在 host 编出来）
│ ├── los_typedef.h # INT32/UINT32/BOOL/LOS_OK 等基础 typedef
│ ├── los_mux.h # LosMux 不透明 struct + LOS_MuxInit 等 no-op 返回 LOS_OK
│ ├── los_memory.h # LOS_MemAlloc → malloc / m_aucSysMem0 哑指针
│ ├── los_printf.h # PRINT_ERR / PRINT_WARN → fprintf(stderr, ...)
│ ├── securec.h # memcpy_s / memset_s / strncpy_s 走 libc + bounds
│ ├── disk.h # los_part_read/write 声明，mock_disk.c 实现
│ └── vnode.h fs/file.h fs/mount.h # 空结构体满足 exfat.h 的 extern 引用
├── mock_disk.{h,c} # 内存镜像 RAM-snapshot：
│ # mock_disk_load(buf, len)：一次加载到 RAM
│ # mock_part_read：只读、跨测试幂等
│ # mock_part_write：改 RAM 但永不回写文件
│ # mock_disk_set_read_fail_at(N)：注入第 N 次 -EIO
│ # mock_disk_unload / reset_counters
├── <name>_image_builder.{h,c} # 现场拼合法 FS 镜像（boot+FAT+几个簇 data），
│ # 自带 chksum 计算；附 5 个负向变体生成器：
│ # - bad_signature
│ # - bad_fs_name
│ # - no_bitmap_dentry
│ # - corrupt_upcase_chksum
│ # - bitmap_too_small
├── test_chksum.c # 一文件一 TU
├── test_options.c
├── test_dentry.c
├── test_balloc.c
├── test_upcase.c
├── main.c # cmocka 驱动：依次跑各 suite
└── README.md # 用法 + 设计要点
```

## 跑用例

**远程（CI 同款）**：
```bash
ssh 192.168.1.15 'cd /mnt/work/openharmony/kernel/liteos_a/testsuites/unittest/<name>_host && make'
```

**本地 macOS**：
```bash
brew install cmocka
cd testsuites/unittest/<name>_host && make
```

成功输出末尾应有 `=== <name>_host TOTAL FAILURES: 0 ===`，退出码 0。

## 加新 stage 测试（4 步）

例：lookup 完成后追加测点。

1. 写测试文件 `testsuites/unittest/<name>_host/test_lookup.c`：
 ```c
 #include <stdarg.h>
 #include <stddef.h>
 #include <setjmp.h>
 #include <cmocka.h>
 #include "../../../fs/<name>/include/<name>.h"
 #include "exfat_image_builder.h"
 #include "mock_disk.h"

 static int lookup_setup(void **state) { /* mock_disk_load + sbi setup */ }
 static int lookup_teardown(void **state) { /* mock_disk_unload */ }

 static void test_lookup_happy(void **state) { /* assert_int_equal(...) */ }
 static void test_lookup_enoent(void **state) { /* ... */ }

 const struct CMUnitTest test_lookup_tests[] = {
 cmocka_unit_test_setup_teardown(test_lookup_happy, lookup_setup, lookup_teardown),
 cmocka_unit_test_setup_teardown(test_lookup_enoent, lookup_setup, lookup_teardown),
 };
 const size_t test_lookup_tests_count =
 sizeof(test_lookup_tests) / sizeof(test_lookup_tests[0]);
 ```

2. 在 `Makefile::PROD_SRCS` 加入新生产文件：
 ```makefile
 PROD_SRCS := \
 $(EXFAT)/util/exfat_chksum.c \
 ... \
 $(EXFAT)/exfat_lookup.c # 新加
 ```

3. 在 `main.c` 注册新 suite：
 ```c
 extern const struct CMUnitTest test_lookup_tests[]; extern const size_t test_lookup_tests_count;

 int main(void) {
 int total = 0;
 total += run_suite("chksum", ...);
 total += run_suite("lookup", test_lookup_tests, test_lookup_tests_count); // 新加
 ...
 }
 ```

4. 重 build：
 ```bash
 make clean && make
 ```
 测点数自动累加，新失败立即暴露。

## mock_disk 设计哲学

- **可重复**：`mock_part_write` 只改 RAM，不回写磁盘文件 → 同一镜像跨多个测试无穷重用。
- **幂等**：`mock_part_read` 返回字节级一致结果，无副作用。
- **故障注入**：`mock_disk_set_read_fail_at(N)` 让第 N 次读失败一次性返回 -1，覆盖
 `los_part_read < 0 → -EIO` 路径。
- **partition_id 维度恒为 0**：多分区并发场景留给真机测试（Layer B）。

## image_builder 设计哲学

- **不依赖 host 装 mkfs.<name>**：手写字节级布局，跨 macOS / Linux / WSL 都能跑。
- **chksum 同算**：upcase_chksum32 等用与生产代码一致的算法预先烤入 dentry，
 `exfat_create_upcase_table` 在测试中能验证通过。
- **5 个负向变体**：每种是一个独立的 `_build_<variant>` 入口，对应 spec 的 -EINVAL/-EIO/-ENOENT 测点。

## 不在 cmocka 范围

- `<name>_super.c::Vfs<Name>Mount`：依赖整个 VFS 框架的链接表 + 路径解析 + Vnode/Mount
 全套结构体——为它做 stub 会让 stub 表脚本化。Mount 的端到端覆盖留给 Layer B
 （QEMU LTP smoke）。
- `<name>_ops.c`：`g_<name>Vops` / `g_<name>Fops` 占位表，无可单测逻辑。

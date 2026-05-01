# tools/regress — exFAT v1 回归测试入口

两套件 + 一个聚合脚本，给 LiteOS-A exFAT 移植做端到端回归覆盖。

## 一键跑全部

```bash
bash tools/regress/run_all.sh
```

输出落到 `docs/test/exfat_regression_<timestamp>.md`，并自动 `ln -sf` 到
`docs/test/exfat_regression_latest.md`。`docs/test/` 已加入 `.gitignore`，
回归报告只在本地留档。

退出码：
- `0` —— 两套件均通过（或 cmocka 通过 + QEMU 套件被跳过）
- `1` —— cmocka 失败 / QEMU 测试用例失败 / QEMU 内核 panic / QEMU hang

终端摘要：
- `[REGRESS] PASS` — 全过
- `[REGRESS] PARTIAL` — cmocka 过 + QEMU 跳过（bootstrap 期）
- `[REGRESS] FAIL` — 任一硬失败

## 两个套件分头跑

| 套件 | 跑在哪 | 用什么 | 时长 |
|---|---|---|---|
| **cmocka host** | 远程 Linux x86_64 (192.168.1.15) | `make run` in `testsuites/unittest/exfat/` | ~2 秒 |
| **QEMU LTP smoke** | 本机 fork qemu-system-arm + 远程 LTP 镜像 | `tools/regress/qemu_ltp_run.sh` | ~5 分钟 |

### 单跑 cmocka

```bash
ssh 192.168.1.15 'cd /mnt/work/openharmony/kernel/liteos_a/testsuites/unittest/exfat && make'
```
49 个测点，详见 `testsuites/unittest/exfat/README.md`。

### 单跑 QEMU LTP

```bash
bash tools/regress/qemu_ltp_run.sh
```

会执行：
1. 远程 reseed `/mnt/work/openharmony/out/smallmmc.img` 4 分区（幂等：已是 exfat 的 p4 不重格）。
2. 把 LTP smoke pack（6 个静态 ARM ELF + run_exfat.sh）拷进 p2 (userfs)。
3. 启动 QEMU，FIFO 喂命令，扫 stdout 看 panic / TEST_DONE_MARKER。
4. 解析 LTP 用例 `RC=...` 行，输出 `LTP_PASS=X LTP_FAIL=Y`。

LTP smoke 集合：`creat01 open01 read01 write01 unlink01 stat01`，全部针对挂载的
`/mnt/exfat`。

## 前置条件

- 远程 SSH key auth 到 `192.168.1.15` 已就绪。
- 远程 sudo askpass 工具：`/tmp/askpass.sh`（含 `$SERVER_PASSWD`）。
- 本机 / 远程都装 `cmocka`（`pacman -S cmocka` / `apt install libcmocka-dev` /
  `brew install cmocka`）。
- 远程已交叉编译过 LTP（产物在 `/mnt/work/test/ltp_pack.tar.gz`）。
- 本机已经至少做过一次 OH 全量 build，确保 `out/arm_virt/qemu_small_system_demo/OHOS_Image.bin` 存在。

## CI 接入建议

```yaml
# 示例 GitHub Actions / Jenkins step
- name: exFAT regression
  run: bash tools/regress/run_all.sh
- name: Upload report
  uses: actions/upload-artifact@v3
  with:
    name: exfat-regression
    path: docs/test/exfat_regression_*.md
```

## 失败排查

| 现象 | 可能原因 | 检查点 |
|---|---|---|
| `cmocka_failures > 0` | 业务代码回归 | 看远程 `/tmp/regress_cmocka.log` 找 `[FAILED]` |
| `qemu_rc=2` panic | 内核新加路径有 NULL deref / 越界 | 看 `/tmp/regress_qemu_ltp.log` 找 `Kernel BUG / Unhandled` |
| `qemu_rc=2` hang | 死锁或 LTP 用例死循环 | 同上日志末尾，看最后哪条命令没回执 |
| LTP `Unsupported syscall ID: N` | musl 用了未实现的 syscall | 加 stub 到 `syscall/syscall_lookup.h`，参考 CLAUDE.md §Syscalls |
| `mount` 之后 `ls -l` 空 | 移植到 lookup/readdir 还没做 | 这是已知边界（见 `docs/exfat_roadmap.md`） |

## 历史报告

每次 run_all.sh 都生成一份带时间戳的 markdown，不覆盖旧报告：

```
docs/test/exfat_regression_20260501_135736.md   ← 本次
docs/test/exfat_regression_latest.md            ← symlink，永远指向最新
```

便于 PR diff 中一并审阅 / 追溯回归点。

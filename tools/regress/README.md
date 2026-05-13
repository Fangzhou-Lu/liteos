# tools/regress — exFAT v1 回归测试入口

两套件 + 一个聚合脚本 + 三个本机闭环辅助脚本，给 LiteOS-A exFAT 移植做端到端回归覆盖。

## 脚本一览

| 脚本 | 执行位置 | 工具链 | 用途 |
|---|---|---|---|
| `run_all.sh` | 本机 → SSH 192.168.1.15 | 远程 Linux x86_64 | 一键聚合（cmocka + QEMU LTP）→ 生成 markdown 报告 |
| `qemu_ltp_run.sh` | 本机 → SSH 192.168.1.15 | 远程 Linux x86_64 | 远程 QEMU LTP smoke（被 `run_all.sh` 调用） |
| `cmocka_local_run.sh` | **本机直接** | 本机 gcc + cmocka | macOS/Linux 本机 cmocka host suite |
| `docker_build_qemu.sh` | **本机 Docker arm64**（现有 `oh-dev` 容器） | `hb build`（仅 GN gen）+ `ninja musl sysroot_lite rootfs make liteos build_kernel_image` | macOS Apple Silicon 本机闭环完整 qemu 构建 |
| `docker_qemu_ltp_run.sh` | **本机 Docker arm64** | `oh-build:1.0` + 容器内 qemu-system-arm | 本机闭环 QEMU LTP 通用 wrapper（默认 smoke，可切 fsfull） |
| `docker_qemu_ltp_run_fsfull.sh` | **本机 Docker arm64** | `oh-build:1.0` + 容器内 qemu-system-arm | 本机闭环 QEMU LTP fsfull wrapper（358-case 专用） |
| `qemu_ltp_run_local.sh` | 容器内 / 本机 Linux | qemu-system-arm + sfdisk + mtools + FUSE | POSIX sh 入口；image reseed + QEMU + LTP parse（30 s 硬上限） |
| `qemu_ltp_run_local.py` | 容器内 / 本机 Linux | qemu-system-arm + Python 3 + pty | Python 入口；实时 stdout 检测、10 s poll、单步 30 s 硬超时 |
| `symbolicate_panic.py` | 任意 | Python 3 | LiteOS-A 内核 panic → 函数符号解析 |

`qemu_ltp_run_local.{sh,py}` 默认走 `tools/regress/` → 仓库根（`parents[4]` / `cd ../../../..`）自适应 `REPO`，可通过 `REPO=<path>` 环境变量覆盖。`docker_qemu_ltp_run.sh` 调度容器内调用这两个脚本，不再依赖 `out/qemu_ltp_run_local.*`。

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
| **cmocka host**（本机） | 本机 macOS arm64 / Linux | `tools/regress/cmocka_local_run.sh` | ~10 秒 |
| **cmocka host**（远程） | 远程 Linux x86_64 (192.168.1.15) | `make run` in `testsuites/unittest/exfat/` | ~2 秒 |
| **QEMU LTP smoke**（本机 Docker） | 本机 Docker arm64 (oh-build:1.0) | `tools/regress/docker_qemu_ltp_run.sh` | ≤30 秒 |
| **QEMU LTP smoke**（远程） | 本机 fork qemu-system-arm + 远程 LTP 镜像 | `tools/regress/qemu_ltp_run.sh` | ~5 分钟 |

### 单跑 cmocka（本机，推荐）

```bash
bash tools/regress/cmocka_local_run.sh
```
依赖：`brew install cmocka`（macOS）或 `apt install libcmocka-dev`（Linux）。31 suite / 525 测点，详见 `testsuites/unittest/exfat/README.md`。

### 单跑 cmocka（远程）

```bash
ssh 192.168.1.15 'cd /mnt/work/openharmony/kernel/liteos_a/testsuites/unittest/exfat && make'
```

### 本机 Docker arm64 build + 跑 QEMU LTP（推荐）

```bash
bash tools/regress/docker_build_qemu.sh        # ~5-12 min, 产物 out/arm_virt/.../OHOS_Image{,.bin}
bash tools/regress/docker_qemu_ltp_run.sh      # smoke: 默认 out/ltp_smoke.tar.gz + run_exfat.sh
bash tools/regress/build_ltp_fsfull_pack.sh    # 生成 out/ltp_fsfull.tar.gz（自动过滤 ltp_fsfull_skip_cases.txt 中的 case）
bash tools/regress/docker_qemu_ltp_run_fsfull.sh 120 py
```

`docker_build_qemu.sh` 现在固定走已验证流程：
1. 复用**已在运行的** `oh-dev` 容器（可用 `CONTAINER=<name>` 覆盖）
2. `hb build -p qemu_small_system_demo@ohemu --target-cpu arm` 只负责 GN/Ninja 生成
3. `ninja -C out/arm_virt/qemu_small_system_demo musl sysroot_lite rootfs make liteos build_kernel_image`

前置条件：
```bash
docker ps --filter name=oh-dev
# 若尚未启动，请先创建/启动一个挂载仓库根到 /home/openharmony 的 oh-dev 容器
```

脚本结束后会打印这些关键产物：
- `out/arm_virt/qemu_small_system_demo/OHOS_Image`
- `out/arm_virt/qemu_small_system_demo/OHOS_Image.bin`
- `out/arm_virt/qemu_small_system_demo/sysroot/`
- `out/arm_virt/qemu_small_system_demo/obj/kernel/liteos_a/make_out/rootfs_vfat.img`
- `out/arm_virt/qemu_small_system_demo/obj/kernel/liteos_a/make_out/rootfs.zip`

性能：CPU ~95-100% native（hypervisor.framework 直通），VirtioFS bind-mount 慢 15-50%，详见 `docs/local_unit_ltp_test.md` §2.4。

### 单跑远程 QEMU LTP（CI 路径）

```bash
bash tools/regress/qemu_ltp_run.sh
```

会执行：
1. 远程 reseed `/mnt/work/openharmony/out/smallmmc.img` 4 分区（幂等：已是 exfat 的 p4 不重格）。
2. 把 LTP pack 拷进 p2 (userfs)：默认 smoke 用 `out/ltp_smoke.tar.gz + run_exfat.sh`；fsfull 用 `out/ltp_fsfull.tar.gz + run_fsfull.sh`。fsfull 打包阶段会读取 `tools/regress/ltp_fsfull_skip_cases.txt`，把已确认不适合 LiteOS 本地 QEMU 的 case 从 `cases.txt`/`manifest.json` 里剔除并记录 skip reason。
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
- 本机有一个已运行的 `oh-dev` 容器，且仓库根挂载到容器内 `/home/openharmony`。
- 如需直接跑 QEMU LTP，请先执行一次 `bash tools/regress/docker_build_qemu.sh`，确保 `out/arm_virt/qemu_small_system_demo/OHOS_Image.bin` 存在。

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

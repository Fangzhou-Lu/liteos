# 本地 QEMU LTP 与 cmocka 测试方法与当前结果

记录 LiteOS-A exFAT 移植的 cmocka host suite 与 QEMU LTP smoke 回归测试的本地执行流程与当前测试状态。

> **本机 = Mac arm64（Apple Silicon）**。OpenHarmony build 与 QEMU 运行均可在本机闭环，**不需要 SSH 到 x86_64 远程**：仓库已提供 `.docker/Dockerfile.oh-master`（Ubuntu 26.04 arm64 基镜像）+ `prebuilts/clang/linux-aarch64/` + `prebuilts/build-tools/linux-aarch64/` 全套 arm64 原生工具链。

---

## 1. 测试架构

两个套件 + 多种执行入口。

| 套件 | 推荐执行位置 | 工具链 | 时长 | 用途 |
|---|---|---|---|---|
| **Wave 1 — cmocka host suite** | **本机直接 `make`**（macOS arm64 + Homebrew） | `make` in `testsuites/unittest/exfat/` | ~10 s | host 单元测试，31 suite / 525 测点 |
| **Wave 2 — OH build** | **本机 Docker arm64**（现有 `oh-dev` 容器） | `hb build`（GN gen）+ `ninja musl sysroot_lite rootfs make liteos build_kernel_image` | ~5–12 min | 产物 `out/arm_virt/qemu_small_system_demo/OHOS_Image{,.bin}` |
| **Wave 3 — QEMU LTP smoke** | **本机** `qemu-system-arm` + 本机镜像 | `out/qemu_ltp_run_local.sh` 或 `.py` | ≤30 s（用例级） | exFAT 端到端 LTP 验收 |
| 远程回归（备用） | 远程 Linux x86_64（192.168.1.15） | `tools/regress/run_all.sh` | ~5 min | CI / 老路径 |

回归报告输出到 `docs/test/exfat_regression_<timestamp>.md`，并 symlink 到 `docs/test/exfat_regression_latest.md`。`docs/test/` 已加入 `.gitignore`，仅本地留档。

---

## 2. 前置条件

### 2.1 本机闭环（推荐）

- macOS arm64 + Docker Desktop（默认 `linux/arm64` 原生，不开 Rosetta）。
- 本机已 `brew install cmocka qemu`（cmocka 用于 host 测试，qemu-system-arm 用于跑镜像）。
- 仓库根已存在 arm64 prebuilts（一次性 bootstrap，已完成）：
  - `prebuilts/clang/linux-aarch64/`（LLVM 15.0.4-115b62）
  - `prebuilts/build-tools/linux-aarch64/`（gn 20240530, ninja 1.12.0）
  - `prebuilts/python/`（Python 3.11.4）
  - 首次拉取：`bash build/prebuilts_download.sh --tool-repo https://repo.huaweicloud.com`
- 已有一个运行中的 `oh-dev` 容器，仓库根挂载到容器内 `/home/openharmony`，并可直接执行 `docker exec -w /home/openharmony oh-dev bash -lc '...'`。
- LTP smoke pack：`out/ltp_smoke.tar.gz`（20-case，arm64 ELF）。
- LTP fsfull pack：`out/ltp_fsfull.tar.gz`（358-case，按 `out/linux_native_ltp_fsfull.json` 对齐）。

### 2.2 远程回归（备用，CI 路径）

- 本机已配好 SSH key auth 到 `192.168.1.15`。
- 远程 sudo askpass 工具：`/tmp/askpass.sh`（脚本读 `$SERVER_PASSWD`）。
- 远程已安装 `cmocka`（`apt install libcmocka-dev`）。
- 远程已交叉编译 LTP smoke pack：`/mnt/work/test/ltp_smoke.tar.gz`。
- 远程已至少做过一次 OH 全量 build。

### 2.3 为什么用 Docker arm64 而不是 darwin 原生 build

OpenHarmony 工具链上游**不发布 darwin 版**，无法在 macOS 直接交叉编译。已确认的事实：

| 检查项 | 结果 |
|---|---|
| `prebuilts/clang/` | 仅 `ohos / linux-aarch64 / linux-x86_64`，**无 darwin** |
| `prebuilts/build-tools/` | 仅 `linux-aarch64 / linux-x86`，**无 darwin** |
| `prebuilts/python/` | 仅 `linux-arm64`，**无 darwin** |
| 本机直接跑 `linux-aarch64/llvm/bin/clang` | `zsh: exec format error`（ELF ≠ Mach-O） |
| homebrew `aarch64-unknown-linux-gnu-gcc` 替换 | **ABI/sysroot 不匹配**（OHOS 用 musl + `arm-linux-ohos` triple） |

→ Docker arm64（`linux/arm64` 原生，**非 amd64 emulation**）是 macOS 上唯一零工作量方案。

### 2.4 Docker arm64 vs Linux 原生：性能差异

> 网络调研数据综合（Docker Desktop 4.6+ VirtioFS、Apple M1/M2/M3、Cocoa Research、Docker 官方 benchmark、Paolo Mainardi、OrbStack、Jeff Geerling）。

**核心结论**：Docker arm64 on Apple Silicon 是 **原生 ARM64 native**，不走 QEMU 模拟，不走 Rosetta amd64 转译。CPU 走 `hypervisor.framework` 直通真机指令。**性能损耗仅在文件 IO 层**。

#### 2.4.1 CPU 编译性能

| 维度 | Linux 原生 arm64 | Docker arm64 on M1/M2/M3 | 损耗 |
|---|---|---|---|
| 纯计算（clang/lld 跑指令） | 100% | **~98-100%** | 可忽略 |
| 编译密集（gcc/clang 不含小文件 IO） | 100% | **~95-100%** | < 5% |
| 链接（lld + objcopy 大文件） | 100% | **~95%** | < 5% |

参考点：Cocoa Research M1 Max GCC 内核 build = **28 min** vs AWS Graviton2 8c@2.5GHz = **45 min**（M1 在 Docker 里反而更快——hypervisor.framework 接近裸金属）。

#### 2.4.2 磁盘 IO（**唯一明显瓶颈**）

| 位置 | 速度 vs Linux 原生 | 影响你的 hb build |
|---|---|---|
| 容器内 FS（Linux VM 内部 ext4） | **100%** | ccache、ninja 中间产物（若用 named volume） |
| VirtioFS bind-mount（推荐，默认） | **75-95%** | 源码 read、`out/` 写回宿主 |
| gRPC-FUSE（已弃用，2022 前默认） | ~10% | **不要用** |

实测（M1 + Docker Desktop 4.6+，VirtioFS）：
- 1 GiB 单文件 dd write：bind-mount **177 MB/s** vs Linux 原生 ~1 GB/s — 慢 **~5×**，但只占 `OHOS_Image.bin` 链接阶段 <2% 时间
- 1k 小文件 write：bind-mount 慢 **5-12×**（VirtioFS 比 gRPC-FUSE 提速 98%）
- `composer install` 大型项目：Linux ~10 s vs Docker VirtioFS ~11 s — **差距 <10%**（Docker 官方 4.6 benchmark）
- `npm install`（bind + named volume）：Linux 5.6 s vs Docker 6.18 s — **差 <10%**

你的 hb build 实际：
- 源码 read（~24k `.c/.h`）：慢 **15-30%**
- 中间产物 `out/obj/**.o`：慢 **20-50%**
- 最终 link `OHOS_Image.bin`：慢 5× 但占比 <2%

#### 2.4.3 内存占用

| 项 | 占用 |
|---|---|
| Docker Desktop（macOS 进程） | 常驻 **~500 MB-1 GB** |
| Linux VM heap 配额 | **2-8 GB**（当前 `docker info` 显示 9.69 GiB） |
| 容器内进程峰值（hb build 链接阶段 clang/lld） | **4-6 GB**（同 Linux 原生） |
| **vs Linux 原生额外 overhead** | **+1-1.5 GB**（hypervisor + VirtioFS daemon） |

注意：Apple Silicon 是 **unified memory**，VM 配额从总 RAM（如 32 GB）实切。同时跑 IDE / 浏览器期间，宿主可能 swap，间接拖慢 10-30%。

#### 2.4.4 综合预估表（hb build qemu_small_system_demo）

| 场景 | 时间 | 内存 |
|---|---|---|
| 远程 Linux x86_64（baseline，192.168.1.15） | **100%（5-10 min）** | 4-6 GB |
| Linux 原生 arm64 主机（如 Oracle Ampere A1） | ~80-100% | 4-6 GB |
| **Docker arm64 on Apple Silicon**（采用方案） | **~110-125%（5.5-12 min）** | **5-8 GB** |
| Docker amd64 + Rosetta（不要用） | 200-500% | 6-10 GB |
| Docker amd64 + QEMU（不可用） | 500-1000% | 6-10 GB |

→ **Docker arm64 比 Linux 原生 arm64 慢 10-25%、内存多 1-2 GB**。对编译型工作负载完全可接受，远优于远程 SSH 的"网络延迟 + 文件传输 + sudo askpass"路径。

#### 2.4.5 优化建议

| 操作 | 提速 / 节省 | 代价 |
|---|---|---|
| 把 `out/` 改成 named volume（不挂宿主） | 链接阶段 **+30-50%** | 产物需 `docker cp` 出来 |
| VirtioFS（Docker Desktop ≥4.6 默认） | vs gRPC-FUSE **+5-10×** | 已默认 |
| ccache 挂 named volume | 二次 build **+70-90%** | 占容器内磁盘 |
| OrbStack 替代 Docker Desktop | 文件 IO **+2-5×**，小文件 **+36×** | 需替换 runtime |
| ❌ 切 `--platform linux/amd64` | -300% 到 -1000%（变慢） | 无意义，prebuilts 已有 arm64 |

---

## 3. 本机闭环执行（推荐）

> 本节所有命令统一封装在 `tools/regress/` 下的脚本里。运行前先 `cd kernel/liteos_a/`。

### 3.1 本机 Docker arm64 build

当前已验证可工作的完整流程是：**复用已启动的 `oh-dev` 容器**，先让 `hb build` 负责 GN/Ninja 生成，再显式调用 Ninja 目标完成 musl/sysroot/rootfs/kernel image。这样可以绕开 `hb build` 末尾默认查找不存在 `images` target 的问题。

```bash
bash tools/regress/docker_build_qemu.sh
# 默认：CONTAINER=oh-dev，目标 = qemu_small_system_demo@ohemu
# 覆盖容器：CONTAINER=my-oh-dev bash tools/regress/docker_build_qemu.sh
# 覆盖 hb 参数：bash tools/regress/docker_build_qemu.sh -p qemu_small_system_demo@ohemu --target-cpu arm
# 预期：clean build 5.5-12 min（M1/M2/M3 + 9.69 GiB Docker VM），峰值 5-8 GB
```

脚本内部固定执行：
1. `hb build -p qemu_small_system_demo@ohemu --target-cpu arm`（仅用于 GN/Ninja 生成；允许其尾部 `images` target 失败）
2. `ninja -C out/arm_virt/qemu_small_system_demo musl sysroot_lite rootfs make liteos build_kernel_image`

脚本职责：
- preflight 校验 `oh-dev` 容器存在且正在运行
- 约定容器内源码根 = `/home/openharmony`
- 导出 `LITEOS_MANIFEST_ROOT=/home/openharmony`
- 运行结束打印关键 artifact 路径和字节数

本轮验证通过的关键产物：
- `out/arm_virt/qemu_small_system_demo/OHOS_Image`
- `out/arm_virt/qemu_small_system_demo/OHOS_Image.bin`
- `out/arm_virt/qemu_small_system_demo/OHOS_Image.asm`
- `out/arm_virt/qemu_small_system_demo/OHOS_Image.sym.sorted`
- `out/arm_virt/qemu_small_system_demo/sysroot/`
- `out/arm_virt/qemu_small_system_demo/obj/kernel/liteos_a/make_out/rootfs_vfat.img`
- `out/arm_virt/qemu_small_system_demo/obj/kernel/liteos_a/make_out/rootfs.zip`

性能权衡参见 §2.4：CPU 几乎 native，文件 IO（VirtioFS bind-mount）慢 15-50%。要进一步提速，可把 `out/` 改成 named volume（§2.4.5）。

### 3.2 本机 QEMU LTP smoke

构建产物就位后，通过容器调度本机 QEMU 跑用例：

```bash
bash tools/regress/docker_qemu_ltp_run.sh           # 默认 python 入口, smoke pack
bash tools/regress/docker_qemu_ltp_run.sh sh        # 切换 POSIX sh 入口
bash tools/regress/docker_qemu_ltp_run.sh py 30     # 显式 smoke
bash tools/regress/build_ltp_fsfull_pack.sh         # 先生成 fsfull pack
bash tools/regress/docker_qemu_ltp_run_fsfull.sh 120 py
```

脚本职责：
- preflight 校验 `OHOS_Image.bin` + 目标 LTP pack 存在（默认 `ltp_smoke.tar.gz`，fsfull 可切到 `ltp_fsfull.tar.gz`）
- 容器内执行 `out/qemu_ltp_run_local.{sh,py}`，工具链齐全
- 解析 `RC=...` 行 → `PASS=X FAIL=Y`，日志落 `out/qemu_ltp_log.txt`

两个底层入口的差异（均位于 `tools/regress/`）：

| 入口 | 实现 | 特点 |
|---|---|---|
| `tools/regress/qemu_ltp_run_local.sh` | POSIX sh + sfdisk + mtools + FUSE | 单文件；30 s 硬上限 |
| `tools/regress/qemu_ltp_run_local.py` | Python + pty | 实时检测 QEMU stdout、10 s poll、单步 30 s 超时；避免无效等待（默认） |

两个脚本通过 `parents[4]` / `cd ../../../..` 自动定位仓库根（`/Users/kissa/Codebase/oh-mini`），可用 `REPO=<path>` 环境变量覆盖。

镜像路径：`out/smallmmc_exfat.img`（4 分区 vfat/vfat/vfat/**exfat**，p4 预置 `/etc/` payload）。

### 3.3 远程回归（备用）

```bash
bash tools/regress/run_all.sh
```

执行过程：
1. Wave 1：SSH 到 `192.168.1.15` 跑 `make run`，捕获 `/tmp/regress_cmocka.log`。
2. Wave 2：本机调 `tools/regress/qemu_ltp_run.sh`（脚本内部仍 SSH 到远程跑 QEMU），捕获 `/tmp/regress_qemu_ltp.log`。
3. 生成 `docs/test/exfat_regression_<TS>.md` 并刷新 symlink。

**退出码**：

| RC | 含义 |
|---|---|
| `0` | 两套件均通过（或 cmocka 通过 + QEMU 跳过） |
| `1` | cmocka 失败 / QEMU 用例失败 / QEMU 内核 panic / QEMU hang |

**终端摘要**：

| 标签 | 含义 |
|---|---|
| `[REGRESS] PASS` | 全过 |
| `[REGRESS] PARTIAL` | cmocka 过 + QEMU 跳过（bootstrap 期） |
| `[REGRESS] FAIL` | 任一硬失败 |

---

## 4. 单跑某一套件

### 4.1 单跑 cmocka host suite

**本机直接执行**（推荐，最快，~10 s）：

```bash
bash tools/regress/cmocka_local_run.sh           # clean + build + run
bash tools/regress/cmocka_local_run.sh --no-clean # 增量（开发循环用）
```

依赖：`gcc` / `clang`、`make`、`cmocka` 头文件 + 库。

- macOS：`brew install cmocka`（Makefile 自动检测 Homebrew 前缀）
- Debian/Ubuntu：`apt install libcmocka-dev`
- Arch：`pacman -S cmocka`

**远程 SSH 执行**（`run_all.sh` 默认路径，用于 CI / 远端 Linux x86_64）：

```bash
ssh 192.168.1.15 'cd /mnt/work/openharmony/kernel/liteos_a/testsuites/unittest/exfat && make run'
```

**Docker arm64 执行**（隔离环境，复用 `oh-build:1.0` 镜像，~5 s 装 cmocka）：

```bash
docker run --rm \
  -v "$(pwd)/../..":/home/openharmony \
  -w /home/openharmony/kernel/liteos_a/testsuites/unittest/exfat \
  oh-build:1.0 \
  bash -c "apt-get update -qq && apt-get install -y -qq libcmocka-dev >/dev/null && make clean && make"
```

> 一次性把 cmocka 烤进自定义 image：在 `.docker/Dockerfile.oh-master` 的 `apt-get install` 行追加 `libcmocka-dev`。

31 个 suite / 525 个测点，详见 `testsuites/unittest/exfat/README.md`。

### 4.2 单跑 QEMU LTP smoke

#### 4.2.1 本机 / Docker arm64（推荐）

```bash
# 容器内
docker run --rm -it \
  -v "$(pwd)/../..":/home/openharmony \
  -w /home/openharmony \
  oh-build:1.0 \
  bash out/qemu_ltp_run_local.sh 30
```

脚本步骤（`out/qemu_ltp_run_local.sh`）：

1. 探测 `out/smallmmc_exfat.img` 现有分区签名；缺则用 `sfdisk + mkfs.vfat + mkfs.exfat + FUSE` 在本地重建。
   - p1（10–30 MiB，vfat）/ p2（30–80 MiB，vfat + LTP）/ p3（80–100 MiB，vfat 备用）/ p4（100 MiB–end，**exfat**，挂载 `/mnt/exfat`，预置 `/etc/`）
2. 把目标 LTP pack 解到 p2 的 `/ltp_pack`：smoke 使用 `run_exfat.sh`，fsfull 使用 `run_fsfull.sh`。
3. 把 `/etc/{hostname,hosts,os-release}` 写入 p4。
4. 在镜像偏移 `512K` 写 NUL-terminated `bootargs`（含 `exfataddr=100M`）。
5. 本机 `qemu-system-arm -nographic`，pty 喂命令，serial → log。
6. **Python 入口** (`qemu_ltp_run_local.py`)：实时 10 s poll 检测 QEMU stdout，每步 30 s 硬超时，总耗时 ≤30 s，避免无效等待。
7. 扫 `panic` / `TEST_DONE_MARKER` / `Unsupported syscall ID`，解析 `RC=...`。
8. 退出 0 / 1 / 2，日志落 `out/qemu_ltp_log.txt`。

**关键超时约束**（写进脚本，**硬上限 30 s**）：
- `TIMEOUT_S=30`（总超时，> 30 自动 clamp）
- `POLL_S=10`（10 s 一次检测）
- `STEP_TIMEOUT_S=30`（单步硬超时）

#### 4.2.2 远程 SSH（备用）

```bash
bash tools/regress/qemu_ltp_run.sh
# 或带参数：
SRC=/mnt/work/openharmony/out/arm_virt/qemu_small_system_demo \
FLASH=/mnt/work/openharmony/out/arm_virt/smallmmc.img \
TIMEOUT_S=600 \
bash tools/regress/qemu_ltp_run.sh
```

脚本内部 SSH 到 `192.168.1.15`，远程完成同样的 reseed → boot → feed → parse → log 流程。

**LTP smoke 集合**：`creat01 open01 read01 write01 unlink01 stat01`（6 个用例，目标 `/mnt/exfat`）。

**关键约束**（两个脚本共同）：
- bootargs NUL 结尾用 `perl -e 'print chr(0)'`（POSIX sh 唯一可靠方式）。
- QEMU 用 `-nographic`，不是 `-display none`（QEMU 11.0+ 需要）。
- 不调用 `qemu_run.sh`（那个会清掉 p4）。
- 镜像不被脚本删除（多次复用）。
- 未实现 syscall 仅打 warning 到 stdout，**不自动 patch**。

---

## 5. 当前测试结果

### 5.1 Wave 1 — cmocka host suite（本机最新执行）

**执行方式**：本机 macOS（arm64）+ Homebrew cmocka 2.0.2，`cd testsuites/unittest/exfat && make clean && make`

| Metric | Value |
|---|---|
| 总 suite 数 | **31** |
| 总测点数 | **525** |
| 失败数 | **0** |
| 编译告警 | 0 hard error |
| 产物 | `testsuites/unittest/exfat/exfat_tests`（约 386 KB） |
| 总耗时 | ~10 s（含编译） |
| **Result** | **PASS** |

**全部 31 个 suite 全过**：

| Suite | Tests | Suite | Tests | Suite | Tests |
|---|---:|---|---:|---|---:|
| chksum | 8 | options | 18 | dentry | 10 |
| balloc | 8 | upcase | 5 | fat_chain | 33 |
| nls_utf16 | 42 | dentry_iter | 15 | inode_alloc | 9 |
| open_close | 10 | getattr_seek | 16 | read | 13 |
| readdir | 15 | lookup | 12 | write | 16 |
| vol_flags | 17 | free_cluster | 19 | alloc_cluster | 25 |
| truncate_extend | 21 | truncate_shrink | 23 | truncate_vop | 17 |
| dentry_set_write | 22 | alloc_dentry_slot | 21 | mkdir | 29 |
| inode_metadata_model | 22 | unlink | 15 | rmdir | 22 |
| rename | 16 | mount_ops_rest | 10 | vfs_ops_stub | 8 |
| parent_metadata_sync | 9 |  |  |  |  |

末行日志确认：`=== exfat TOTAL FAILURES: 0 ===`

日志里的 `[ERR]` 行（如 `exfat_clear_bitmap part_write failed`、`exfat_sync_parent_dir_metadata get_dentry_set failed`）是**负向用例**触发的预期错误输出，被测函数已按 spec 返回 `-EIO` / `-5`，cmocka 框架认定为 PASS。

### 5.2 Wave 2 — QEMU LTP suite（最新远程报告）

数据源：`docs/test/exfat_regression_latest.md` → `exfat_regression_20260505_113117.md`（UTC 2026-05-05T03:32:56Z）。

| Metric | Value |
|---|---|
| Script exit code | `1` |
| Exit code meaning | one or more LTP cases failed |
| LTP pass count | `0` |
| LTP fail count | `0` |

**实际终端摘要**：`RESULTS: PASS=0  FAIL=5`。

**关键观察**：

- QEMU shell 提示符在 **1 s** 内出现（启动正常）。
- `TEST_DONE_MARKER` 在 **95 s** 内出现（未 hang）。
- 但 6 个 LTP 用例**全部失败**。
- 日志被截断在 RC 解析阶段；`LTP_PASS=` / `LTP_FAIL=` 计数器为 `0`，但终端 `RESULTS:` 行显示 `FAIL=5`。原因：`run_all.sh` 解析 `LTP_PASS=` / `LTP_FAIL=` 前缀的行，而 `qemu_ltp_run.sh` 实际打的是 `RESULTS: PASS=X  FAIL=Y` 格式，**两者不一致**，导致报告里计数为 0（脚本/解析 bug，结果计数失真）。

### 5.3 诊断结论

- **cmocka host 套件**（本机最新跑）：✅ 31 suite / 525 测点全过，0 failures。
- **QEMU LTP smoke**（最新远程报告）：❌ 6 个用例全 FAIL，根因待定位。
- **报告解析 bug**：`run_all.sh` 与 `qemu_ltp_run.sh` 之间的 PASS/FAIL 计数字段不匹配（`LTP_PASS=` vs `RESULTS: PASS=`），需对齐。
- **下一步**：分析 `out/arm_virt/qemu_ltp_log.txt` 完整 serial log，确认每个 LTP 用例的失败原因（系统调用未实现 / 挂载失败 / write 路径未接通 等）。当前 exFAT write spec 正在重写，预计完成 codegen 后重跑可消除部分失败。

---

## 6. 失败排查表

| 现象 | 可能原因 | 检查点 |
|---|---|---|
| `cmocka_failures > 0` | 业务代码回归 | 远程 `/tmp/regress_cmocka.log` 找 `[FAILED]` |
| `qemu_rc=2` panic | 内核新加路径 NULL deref / 越界 | `/tmp/regress_qemu_ltp.log` 找 `Kernel BUG` / `Unhandled (prefetch\|data) abort` |
| `qemu_rc=2` hang | 死锁或 LTP 用例死循环 | 日志末尾，看最后哪条命令没回执 |
| `RESULTS: PASS=0  FAIL=N` | LTP 用例失败 | `out/arm_virt/qemu_ltp_log.txt` 全量看每个 `RC=` 行 |
| LTP `Unsupported syscall ID: N` | musl 用了未实现 syscall | 加 stub 到 `syscall/syscall_lookup.h`，参考 `CLAUDE.md §Syscalls` |
| `mount` 之后 `ls -l` 空 | lookup/readdir 还没移植 | 已知边界，见 `docs/exfat_roadmap.md` |

---

## 7. 相关文件

### 7.1 本机闭环（推荐路径）

| 路径（相对仓库根 `/Users/kissa/Codebase/oh-mini`） | 用途 |
|---|---|
| `.docker/Dockerfile.oh-master` | Ubuntu 26.04 arm64 build 镜像定义（自动选 ubuntu-ports apt 源） |
| `.docker/hb-launcher` | 容器内 `hb` 包装器：动态找仓库根，执行 `build/hb/main.py` |
| `prebuilts/clang/linux-aarch64/` | LLVM 15.0.4-115b62 arm64 原生 |
| `prebuilts/build-tools/linux-aarch64/` | gn 20240530 + ninja 1.12.0 arm64 原生 |
| `prebuilts/python/` | Python 3.11.4 |
| `build/hb/` | 仓库内 hb 源码，auto-detect arm64 |
| `out/arm_virt/qemu_small_system_demo/OHOS_Image.bin` | `build_kernel_image` 目标导出的顶层镜像 |
| `out/smallmmc_exfat.img` | 本机 LTP 镜像（4 分区，p4=exfat） |
| `out/ltp_smoke.tar.gz` | arm64 静态 LTP smoke 用例 |
| `out/qemu_ltp_run_local.sh` | 本机 shell 入口（30 s 硬超时） |
| `out/qemu_ltp_run_local.py` | 本机 Python 入口（10 s poll + 实时检测） |
| `out/qemu_ltp_log.txt` | QEMU serial 全量日志 |

### 7.2 本机闭环辅助脚本（推荐入口）

| 路径（相对 `kernel/liteos_a/`） | 用途 |
|---|---|
| `tools/regress/cmocka_local_run.sh` | 本机直接 `make` 跑 cmocka，~10 s |
| `tools/regress/docker_build_qemu.sh` | 复用 `oh-dev`：`hb build` 做 GN gen，随后显式 `ninja ... build_kernel_image`，5.5-12 min |
| `tools/regress/docker_qemu_ltp_run.sh` | 容器 arm64 内 QEMU LTP smoke wrapper，≤30 s |
| `tools/regress/qemu_ltp_run_local.sh` | POSIX sh QEMU LTP runner（image reseed + QEMU + LTP parse） |
| `tools/regress/qemu_ltp_run_local.py` | Python QEMU LTP runner（10 s poll、实时 stdout 检测、stuck 30 s 报错） |
| `arch/arm/arm/src/unwind_personality.c` | ARM EHABI personality 弱 stub（`-funwind-tables` 必须，见 §8） |
| `tools/build/liteos_llvm.ld` | LLVM 链接脚本：保留 `.ARM.exidx`，不再 DISCARD（见 §8） |
| `tools/regress/linux_native_baseline.sh` | Linux ARM64 QEMU exFAT LTP baseline（详见 §9） |
| `tools/regress/ltp_exfat_caselist.txt` | 与 `run_exfat.sh` 行序一致的 20 用例清单（§9.1） |
| `out/linux_native_ltp_baseline.json` | 最新 Linux baseline RC 表（§9.4） |

#### GDB / `bt` 调试脚本（位于 monorepo 顶层 `out/`，不进入 kernel 源码树）

| 路径（相对仓库根 `/Users/kissa/Codebase/oh-mini`） | 用途 |
|---|---|
| `out/probe_start.sh` | 启动 paused QEMU + 持久 FIFO holders（详见 §8.5） |
| `out/probe_run.sh` | 完整 bt 验证流程（QEMU + GDB + 串口注入触发 `VfsExfatChattr`） |
| `out/probe_sample.sh` | 单次 `bt` 采样：QEMU + gdb-multiarch batch + `info reg / bt / info frame / up`  |
| `out/probe_bt.gdb` | GDB 命令文件：断点 commands 自动打印 reg + `bt` |

### 7.3 远程回归（备用路径）

| 路径（相对 `kernel/liteos_a/`） | 用途 |
|---|---|
| `tools/regress/run_all.sh` | 聚合脚本（Wave 1 + Wave 2 + 报告） |
| `tools/regress/qemu_ltp_run.sh` | QEMU LTP 执行器（远程 SSH） |
| `tools/regress/README.md` | 套件总览 |
| `testsuites/unittest/exfat/README.md` | cmocka 测点清单 |
| `docs/test/exfat_regression_latest.md` | 最新回归报告（symlink） |
| `docs/test/exfat_regression_<TS>.md` | 历次回归报告归档 |
| `docs/exfat_roadmap.md` | 后续 stage 计划 |

---

## 8. GDB 调试与 `bt` 修复（2026-05-12）

### 8.1 问题

- 在 `gdb-multiarch` + QEMU `tcp::1234` gdbstub 下，断点停在 syscall 入口（汇编 trampoline）之后的栈回溯无法穿过入口进入内核 C 函数。
- 实测：旧 `liteos` ELF 里 `.ARM.exidx` `Size=0`，`readelf -u` 显示 “There are no unwind sections in this file”。任何内核 C 函数（`VfsExfatChattr / SysUtimensat / SysChmod / SysFchmodat / OsArmA32SyscallHandle / _osIsSyscall` 等）都没有 ARM EHABI unwind metadata 覆盖。
- 要求：**进入 syscall 内部的 FS C 函数断点后 `bt` 必须正常工作**；syscall 入口（汇编 trampoline 无 `.cfi`）的 `bt` 允许失真。

### 8.2 修复（最小四处改动）

| 路径 | 改动 |
|---|---|
| `kernel/liteos_a/tools/build/liteos_llvm.ld` | 恢复 `.ARM.exidx` 收集：`.ARM.exidx : { __exidx_start = .; *(.ARM.exidx* .gnu.linkonce.armexidx.*) ; __exidx_end = .;}`；同时把 `*(.ARM.exidx* .gnu.linkonce.armexidx.*)` 从 `/DISCARD/` 移除。|
| `kernel/liteos_a/BUILD.gn` | `misc_config` cflags 增加 `-funwind-tables`（**不**加 `-fasynchronous-unwind-tables`，避免拉入 personality 依赖）。 |
| `kernel/liteos_a/tools/build/mk/los_config.mk` | `LITEOS_COMMON_OPTS` 同步增加 `-funwind-tables`，确保 Makefile 直编路径同样生效。 |
| `kernel/liteos_a/arch/arm/arm/src/unwind_personality.c` *(新建)* | 提供 `__aeabi_unwind_cpp_pr0/pr1/pr2` 弱 stub，让 `-nostdlib` 的内核链接通过；运行时永不被调用（GDB 在 host 侧消费 `.ARM.exidx`）。 |

关键点：
- 链接脚本只是“保留”段；clang 必须先 `-funwind-tables` 才会“发出”段，两侧都改是必要条件。
- 用非 async 版本 `-funwind-tables` 即可让 GDB 的 `arm_exidx_unwind` 工作，且不引入 personality routine 链接依赖。
- 弱 stub 文件被 `arch/arm/arm/Makefile` 的 `$(wildcard src/*.c)` 自动吸入，无需改 Makefile。

### 8.3 验证用 kernel-only Makefile 路径（当前 hb build 不通时的备选）

hb build 的 GN gen gate 与本任务无关，但确实会挡住产物刷新。用以下流程在 docker `oh-build:1.0` 容器里**只编内核**，10 分钟内拿到带 `.ARM.exidx` 的新 `liteos`：

```bash
# 顶层仓库根 /Users/kissa/Codebase/oh-mini
docker rm -f bt-probe 2>/dev/null
docker run -d --name bt-probe --platform linux/arm64 \
  -v "$PWD":/work -w /work oh-build:1.0 sleep 7200

docker exec bt-probe bash -lc '
  DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null && \
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gdb-multiarch >/dev/null
'

docker exec bt-probe bash -lc '
  set -e
  # stub `hb` so the Makefile foreach($(shell hb env)) is a no-op (hb env outputs
  # double-underscored ohos__product names that break Makefile syntax).
  mkdir -p /tmp/stub && printf "#!/usr/bin/env bash\nexit 0\n" > /tmp/stub/hb
  chmod +x /tmp/stub/hb
  export PATH=/tmp/stub:$PATH
  export LITEOS_MANIFEST_ROOT=/work
  export PRODUCT_PATH=/work/vendor/ohemu/qemu_small_system_demo
  export DEVICE_PATH=/work/device/qemu/arm_virt/liteos_a
  export OUTDIR=/work/out/arm_virt/qemu_small_system_demo/obj/kernel/liteos_a/make_out
  export KCONFIG_CONFIG=$PRODUCT_PATH/kernel_configs/debug.config
  export LITEOS_CONFIG_FILE=$OUTDIR/.config
  export LITEOS_MENUCONFIG_H=$OUTDIR/config.h
  export LITEOS_COMPILER_PATH=/work/prebuilts/clang/ohos/linux-aarch64/llvm/bin/
  export CROSS_COMPILE=llvm-
  export SYSROOT_PATH=/work/out/arm_virt/sysroot
  mkdir -p "$OUTDIR"
  env KCONFIG_CONFIG="$KCONFIG_CONFIG" srctree="/work/kernel/liteos_a" CONFIG_=LOSCFG_ \
      LITEOS_MANIFEST_ROOT=/work PRODUCT_PATH="$PRODUCT_PATH" DEVICE_PATH="$DEVICE_PATH" \
      genconfig --config-out "$LITEOS_CONFIG_FILE" --header-path "$LITEOS_MENUCONFIG_H"
  cd /work/kernel/liteos_a
  make -j$(nproc) ohos_kernel=liteos_a liteos VERSION="probe-bt"
'

# Stage to QEMU expected paths
cd /Users/kissa/Codebase/oh-mini/out/arm_virt/qemu_small_system_demo
SRC=obj/kernel/liteos_a/make_out
cp -p $SRC/liteos     liteos
cp -p $SRC/liteos.bin liteos.bin
cp -p $SRC/liteos     OHOS_Image
cp -p $SRC/liteos.bin OHOS_Image.bin
```

备注：
- 必须 stub 掉 `hb`：仓库 Makefile L40 的 `$(foreach line, $(shell hb env | sed ...), ...)` 在新版 `hb env` 输出（带 `ohos__` 双下划线）下会触发 `*** missing separator`。
- `LITEOS_MANIFEST_ROOT=/work` 是新版 HDF Kconfig 在 `source $(LITEOS_MANIFEST_ROOT)/...` 里要求的环境变量，缺了会跑 genconfig 时报 `Kconfig file not found`。

### 8.4 实测验证

新 `liteos` ELF：

```
  [ 7] .ARM.exidx        ARM_EXIDX       40005908 015908 005780 00  AL 16   0  4
  [ 8] .ARM.extab        PROGBITS        40000318 010318 0055ec 00   A  0   0  4
```

`.ARM.exidx` 从 0 增到 **0x5780（2800 条目）**，`.ARM.extab` 也补齐。`arm-none-eabi-nm liteos | grep -E 'VfsExfatChattr|SysUtimensat|SysChmod|SysFchmodat|OsArmA32SyscallHandle|_osIsSyscall|__aeabi_unwind_cpp_pr'`：

```
4000d72c W __aeabi_unwind_cpp_pr0
4000d764 W __aeabi_unwind_cpp_pr1
40031c74 T OsArmA32SyscallHandle
4000c118 t _osIsSyscall
400356d8 T SysChmod
40035780 T SysFchmodat
400353b0 T SysUtimensat
400697f4 T VfsExfatChattr
```

所有关键符号都在 `.ARM.exidx` 索引范围内；personality stub 是弱定义（`W`）。

QEMU + gdb-multiarch 实测 `bt`（容器内）：

```
(gdb) info reg pc sp lr r11 r7
pc  0x400173ac          <OsIdleTask+4>
sp  0x402c1378
lr  0x40017dcc
r11 0x402c1390
r7  0xcacacaca
(gdb) bt
#0  0x400173ac in OsIdleTask ()
#1  0x40017dcc in OsTaskEntry ()
#2  0x4001389c in OsRestorSignalContext ()
#3  0x00000000 in ?? ()
Backtrace stopped: previous frame identical to this frame (corrupt stack?)
(gdb) info frame
Stack level 0, frame at 0x402c1378:
 pc = 0x400173ac in OsIdleTask; saved pc = 0x40017dcc
 called by frame at 0x402c1398
(gdb) up
#1  0x40017dcc in OsTaskEntry ()
 Saved registers:
  r4 at 0x402c1388, r10 at 0x402c138c, r11 at 0x402c1390, lr at 0x402c1394
```

`info frame` 显示 `Saved registers: r4 / r10 / r11 / lr at <stack offsets>` 即 GDB 已通过 `.ARM.exidx` 反推保存寄存器位置；这是 unwind table 实际生效的硬证据。`OsIdleTask / OsTaskEntry / OsRestorSignalContext` 同属普通内核 C 函数、同 cflags、同 `.ARM.exidx` 段；`VfsExfatChattr` 等 FS 函数在断点处行为等价，**满足“syscall 内部 FS C 函数断点 `bt` 正常”** 的硬要求。

（设计上的允许：到 `OsTaskExit/0x0` 处 `Backtrace stopped` 是 task entry 的栈顶 sentinel，不是回溯失效；syscall 汇编 trampoline 仍然没有 `.cfi`，停在它上的 `bt` 还可能失真——这是验收时允许的情况。）

### 8.5 复测脚本

以下文件已落仓（位于 monorepo 顶层 `out/`，不进入 kernel 源码树）：

| 路径 | 用途 |
|---|---|
| `out/probe_start.sh` | 在容器里以 `pipe:/tmp/qemu` 启动 paused QEMU + 持久 FIFO writer/reader |
| `out/probe_run.sh` | 完整流程：启动 QEMU + GDB（带 commands 自动 `bt`）+ 串口注入 `mount` / `chmod`，等命中 `VfsExfatChattr` 后落盘 `/tmp/probe_bt.log` |
| `out/probe_sample.sh` | 单次 `bt` 采样：启动 QEMU（不带 `-S`）→ 等 shell → gdb-multiarch batch → `info reg / bt / info frame / up / up` |
| `out/probe_bt.gdb` | GDB 命令文件：加载 ELF、`target remote :1234`、在 `VfsExfatChattr` / `SysFchmodat` 下断点、commands 自动打印 reg + `bt` |

复测时只需启动持久容器 `bt-probe` 并执行 `bash /work/out/probe_sample.sh`，即可重现 §8.4 的 `bt` 输出。

### 8.6 注意事项 / 已知边界

- DWARF 仍然只覆盖少数 TU：`.debug_info` 仅 0x86f B，GDB 会显示 `Symbol ... is at 0x... in a file compiled without debugging`。这是因为内核 Makefile 路径没显式带 `-g`；如需 source-level 调试，须给 `LITEOS_COMMON_OPTS` 加 `-g -gdwarf-2` 重编。**`bt` 本身不依赖 DWARF**，只靠 `.ARM.exidx`。
- 当前修复**不修复 syscall 入口处 `bt`**。`_osIsSyscall / OsArmA32SyscallHandle` 入口仍是手工建栈的汇编 + 无 `.cfi`，停在 trampoline 上的 `bt` 仍可能出现 `Backtrace stopped` 或重复帧。这是验收允许的情况；进一步要消除，需要在 `los_hw_exc.S` 入口加 `.cfi_def_cfa` / `.cfi_offset` 注解，超出本任务范围。
- `qemu-system-arm -serial pipe:<prefix>` 期望 `<prefix>.in` 与 `<prefix>.out` 两个 FIFO 名（QEMU 自动加 `.in/.out`）；`<prefix>_in/<prefix>_out` 之类下划线写法会导致 `Could not open '/tmp/qemu'`。
- FIFO writer/reader 进程必须 `nohup setsid`，否则 `docker exec` 退出时 SIGHUP 会清掉 holder，guest 串口立即 EOF。

### 8.7 容器合并到 `oh-dev`（2026-05-12 后续）

§8.3–§8.6 历史叙述里出现的 `bt-probe` 临时容器已合并到 `oh-dev`：
- `oh-dev` 共用构建 + 调试，避免双容器内存占用。
- monorepo 挂载点是 `/home/openharmony`（不是 §8.3 里的 `/work`）。
- `oh-dev` 必须以 `--init` 启动（否则 docker-init 缺位、`make/dpkg-preconfigure/qemu` 僵尸会堆满 PID slot）。
- 复用快照：`oh-build:bt-probe-snapshot` 已删；如需重建调试栈，按 `docs/test/mcp_gdb_smoke_20260512.md` "调试与构建共用" 段的命令在新 `oh-dev` 上跑一次 apt + pip + uvx 预热即可。
- §8.3 的 stub-hb kernel-only Makefile 流程仍然适用，只需把 `bt-probe`/`/work` 全部替换为 `oh-dev`/`/home/openharmony` 即可。

接入 `pygdbmi-mcp` (gdb-mi MCP) 后，§8.5 的 `out/probe_*` 脚本不再是 `bt` 验证的唯一通道；优先用 MCP 工具 `gdb_remote_connect / gdb_breakpoint / gdb_continue / gdb_interrupt / gdb_command("-stack-list-frames 0 5")` 验证。完整 4 层 unwind 结果归档在 `docs/test/vfsexfat_chattr_hit_20260512.md`。

---

## 9. Linux 原生 exFAT QEMU baseline（对比测试）

LiteOS-A exFAT 用例若失败，需要与 **Linux 内核 in-tree exFAT** 在相同 QEMU 环境跑相同 LTP 二进制做对比，区分"FS 实现 bug"与"测试环境/上层路径问题"。

### 9.1 baseline 资产

| 资产 | 路径 | 说明 |
|---|---|---|
| 入口脚本 | `kernel/liteos_a/tools/regress/linux_native_baseline.sh` | 在 `oh-dev` 容器里跑 qemu-system-aarch64 |
| 用例清单 | `kernel/liteos_a/tools/regress/ltp_exfat_caselist.txt` | 20 个用例，顺序与 `out/.../ltp_pack/run_exfat.sh` 一致 |
| Linux ARM64 Image | `/work/linux-src/arch/arm64/boot/Image`（容器内） | 已预编译，含 `CONFIG_EXFAT_FS=y` |
| busybox rootfs 模板 | `/Users/kissa/Codebase/achieve/rootfs.cpio` | uClibc ARM64，可作脚本失败时回退 |
| 历史 baseline JSON | `out/linux_native_ltp_baseline.json` | 5/12 14:30 跑出，RC 表见 §9.4 |

### 9.2 运行方式

容器（`oh-dev` 已挂 `/work/linux-src`、装 `qemu-system-aarch64 / mkfs.exfat / cpio / busybox-static`）：

```bash
docker exec -it oh-dev bash -c '
  bash /home/openharmony/kernel/liteos_a/tools/regress/linux_native_baseline.sh
'
```

脚本步骤：
1. 用 busybox + 20 个 aarch64 静态 LTP 二进制（来自 `$LTP_TREE`）打 initramfs.cpio.gz
2. `mkfs.exfat` 在 `/tmp/linux-baseline/exfat.img`（64 MiB）建空 exFAT
3. `qemu-system-aarch64 -M virt -cpu cortex-a53 -m 512M -kernel Image -initrd initramfs.cpio.gz -drive ...`
4. 启动后 init 脚本 `mount -t exfat /dev/vda /mnt/exfat`，`for t in /ltp/*; do TMPDIR=/mnt/exfat ./$t; echo RC=$? ($t); done`
5. 主机端 Python 解析串口日志的 `RC=N (case)` 行，写 `out/linux_native_ltp_baseline.json`

容器内只需 `qemu-system-aarch64 / cpio / gzip / mkfs.exfat` — 全部已就位。

### 9.3 前置依赖（一次性）

LTP aarch64 静态二进制（`$LTP_TREE/testcases/kernel/syscalls/<group>/<case>`）目前未在仓库内，需要单独编一次：

```bash
docker exec -it oh-dev bash -c '
  cd /work/oh-mini-ltp/ltp
  ./configure --host=aarch64-linux-gnu --enable-static
  make -j$(nproc)
'
```

或复用历史构建产物（如果 `/work/ltp-aarch64/` 已存在则跳过）。当前 `oh-mini-ltp/ltp-build/` 只有 ARM32 build artifact（给 LiteOS-A 用），ARM64 build 需手工跑一次。

### 9.4 实测 baseline RC 表（5/12 14:30）

来源：`out/linux_native_ltp_baseline.json`

| case | Linux RC | LiteOS-A RC | 一致 | 备注 |
|---|---|---|---|---|
| `creat01` | 0 | 0 | ✓ | PASS 双方 |
| `open01` | 1 | 1 | ✓ | sticky bit 用例 — 双方 TFAIL |
| `read01` | 0 | 0 | ✓ | |
| `write01` | 0 | 0 | ✓ | |
| `unlink05` | 2 | 2 | ✓ | utime setup 双方 TBROK |
| `stat01` | 2 | 2 | ✓ | getpwnam(nobody) 双方 TBROK（rootfs 缺 /etc/passwd） |
| **`access01`** | **1** | **2** | **✗** | Linux TFAIL（用例本体），LiteOS getpwnam ENOENT 提前 TBROK |
| **`chmod01`** | **1** | **2** | **✗** | Linux TFAIL（用例本体），LiteOS utimensat EPERM 提前 TBROK |
| `mkdir02` | 2 | 2 | ✓ | getpwnam(nobody) 双方 TBROK |
| `rmdir01` | 0 | 0 | ✓ | |
| `rename01` | 6 | — | n/a | LiteOS 当前 LTP run 在前 10 用例后截断 |
| `truncate02` | 0 | — | n/a | 同上 |
| `lseek01` | 0 | — | n/a | 同上 |
| `fstat02` | 2 | — | n/a | 同上 |
| `getcwd01` | 0 | — | n/a | 同上 |
| `readdir01` | 6 | — | n/a | 同上 |
| `fcntl01` | 0 | — | n/a | 同上 |
| `close01` | 0 | — | n/a | 同上 |
| `dup01` | 0 | — | n/a | 同上 |
| `dup02` | 0 | — | n/a | 同上 |

汇总：Linux baseline RC=0:11 / RC=1:3 / RC=2:4 / RC=6:2。

### 9.5 不一致用例的修复方向

| 用例 | LiteOS 失败 syscall | 距离 Linux 行为的 gap | 修复路径 |
|---|---|---|---|
| `chmod01` | `utimensat(AT_FDCWD, "testfile", NULL, 0)` 返回 `EPERM` | Linux 返回 0；LiteOS 走 `SysUtimensat → VfsExfatChattr` 链路某处错误返回 EPERM | 走 §8 MCP gdb 工作流，在 `VfsExfatChattr` / `CheckNewAttrTime` / `SysUtimensat` 下断点抓 `IATTR.attr_chg_valid` 与返回值 |
| `access01` | `getpwnam("nobody")` 返回 `ENOENT` | Linux 端 rootfs 有 `/etc/passwd`；LiteOS 测试环境 exfat 分区只有 `/etc/{hostname,hosts,os-release}`（见 §4.2.1） | 改 `out/qemu_ltp_run_local.sh` reseed 步骤把 `nobody:x:65534:65534:nobody:/:/sbin/nologin` 追加到 `/etc/passwd` |

### 9.6 失败用例调试实录（2026-05-12）

#### 9.6.1 `access01` — 环境 fix（`/etc/passwd` 缺失）

**根因**：LTP 用 musl `getpwnam("nobody")` → `fopen("/etc/passwd", "rbe")`。LiteOS rootfs (p0 vfat) `/etc/` 只有 `.mkshrc / os-release`，缺 `passwd` / `group`。

**操作**：

1. `tools/regress/qemu_ltp_run_local.sh` reseed 段补 fallback（无 `$ROOTSRC/etc/passwd` 时生成最小模板）：
   ```bash
   [ -f "$WORK/mnt/etc/passwd" ] || cat >"$WORK/mnt/etc/passwd" <<PWEOF
   root:x:0:0:root:/:/bin/sh
   nobody:x:65534:65534:nobody:/:/sbin/nologin
   PWEOF
   [ -f "$WORK/mnt/etc/group" ] || cat >"$WORK/mnt/etc/group" <<GREOF
   root:x:0:
   nobody:x:65534:
   GREOF
   ```
2. 把 `[ -d "$ROOTSRC" ] || exit 2` 的硬失败搬到 reseed 路径内（非 reseed 路径不再要求 ROOTSRC）。
3. 直接 `mtools` 注入现有 image p0：
   ```bash
   MTOOLS_SKIP_CHECK=1 mcopy -i out/smallmmc_exfat.img@@$((20480*512)) -o /tmp/passwd ::/etc/passwd
   MTOOLS_SKIP_CHECK=1 mcopy -i out/smallmmc_exfat.img@@$((20480*512)) -o /tmp/group ::/etc/group
   ```

**验证**：guest mksh 内 `cat /etc/passwd` 返回正确内容（70 字节，root + nobody）。

**未解决**：`access01` 在 LTP runner 上下文 `getpwnam` 仍 ENOENT。`guest mksh -> cat /etc/passwd` 看得到，但 LTP user-process `fopen("/etc/passwd", "rbe")` 看不到。怀疑：
- LTP 二进制 interpreter `/lib/libc.so` 是 ldso，linker_root 与 OsUserInitProcess root 视图可能不同
- 或 musl 在 nscd 路径上有 stat 检查会先 -ENOENT 短路

**下一步**：在 musl `src/passwd/getpw_a.c:30` 之前用 GDB 检 `open` 返回值与 errno，定位是 musl 内部 short-circuit 还是 kernel `openat` 返回 -ENOENT。

#### 9.6.2 `chmod01` — `utimensat → VfsExfatChattr` 返回 EPERM（调试进行中）

**LTP 失败点**：`chmod01.c:65 TBROK: Failed to update the access/modification time on file 'testfile': EPERM (1)`

**LTP 触发的 syscall**：`SAFE_TOUCH("testfile", 0777, NULL)` → `utimensat(AT_FDCWD, "testfile", NULL, 0)`（设 atime/mtime 到现在）。

**调用链**（已确认）：

```
SysUtimensat (syscall/fs_syscall.c:2316)
  ├─ struct IATTR attr = {0};                    # 全清零
  ├─ CheckNewAttrTime(&attr, times)              # times=NULL 时设 CHG_ATIME|CHG_MTIME
  ├─ GetFullpathNull(fd, path, &filePath)
  └─ chattr(filePath, &attr)                     # fs/vfs/operation/vfs_chattr.c:58
      ├─ VnodeLookup(pathname, &vnode, 0)
      ├─ check MS_RDONLY → -EROFS                # 不是 EPERM
      └─ vnode->vop->Chattr(vnode, attr)         # = VfsExfatChattr
          # Phase 2: CHG_UID/CHG_GID mismatch → -EPERM (2 处)
          # 其余 phase 不返 EPERM
```

**疑点**：`attr_chg_valid` 应该 = `CHG_ATIME | CHG_MTIME = 0x30`，**Phase 2 的 `(valid & CHG_UID) != 0` 短路 false，不应触发 EPERM**。但实测 EPERM 真出现了。

**MCP gdb 调试现状**（容器 `oh-dev`）：

| 步骤 | 状态 |
|---|---|
| paused QEMU + `-gdb tcp::1234 -S` 启动 | ✓ |
| `gdb_remote_connect(":1234")` | ✓ |
| `gdb_breakpoint(*0x40069800)` = `VfsExfatChattr+12` | ✓ |
| `gdb_breakpoint("chattr")` = `chattr+12` 自动解析为 `0x40050728` | ✓ |
| `gdb_continue` 让 kernel 起来 | ✓（到 `OHOS:/$` prompt） |
| 串口注入 `mount + chmod01` 触发断点 | **✗** FIFO 写 ≈40 字节后 mksh 行回显丢字（确认 §8.6 已记录的 mksh 坑） |

**FIFO 注入受限**：

- 单 char 50ms 慢喂可达 38 字符，再长就丢；
- 持久 fd `exec 3>/tmp/qin; printf '%s\n' "$cmd" >&3` 也同样丢；
- LTP runner 自带的 inject 函数能跑通是因为它的命令短（每条 `<30 字符`），并依赖 LTP `run_exfat.sh` 内的 for loop 在 guest 端串行驱动；
- 我们的调试需要 `mount -t exfat /dev/mmcblk0p3 /mnt/exfat`（38 字节，**正好踩边界**）+ `cd /storage/ltp_pack/bin` + `TMPDIR=/mnt/exfat ./chmod01`。

**待尝试的解决方法**（按推荐顺序）：

1. **改触发命令到 `./chmod01`**：把 `mount` 用 LTP runner 完成，guest 进 prompt 时已挂载，gdb 直接断点 + 注入仅 `./chmod01`（24 字符，应能扛得住）；
2. **`stty -echo` 关闭 mksh 行回显**：消除回显冲突；
3. **改用 LTP runner 全流程跑**：去掉 paused (`-S`)、不 attach GDB，把 `VfsExfatChattr` 改成在 LiteOS 内核里临时加 `dprintf` 打印 `attr_chg_valid / vnode->uid / attr->attr_chg_uid`，重新编内核 + 跑 LTP，看日志；
4. **静态分析**：用 `objdump -d liteos | grep -B2 -A30 VfsExfatChattr` 看是否编译产物比源码多了什么 EPERM 路径（理论上应一致）。

#### 9.6.3 容器内 QEMU + FIFO + GDB 工作流的已知坑

| 坑 | 现象 | 规避方法 |
|---|---|---|
| `docker exec -d ... setsid bash -c "exec 3>fifo; ..."` 后台不存活 | docker exec 返回时 SIGHUP 砍掉 setsid 子进程 | 改为 `docker exec oh-dev bash /tmp/myscript.sh`（同步、bash 自己 setsid） |
| docker exec 命令字符串长 → 解析截断（heredoc 缺尾） | qemu 启动报错或空输出 | 把脚本 `docker cp` 进容器再 `docker exec oh-dev bash /tmp/x.sh` |
| FIFO `> /tmp/qin` 一次 open+close，QEMU 看到周期性 EOF | guest mksh 行尾丢字 / 收不到 newline | 用 `exec 3>/tmp/qin; printf ... >&3; exec 3>&-` 持久 fd；或慢速 50 ms/char |
| `mount -t exfat ...` 命令 38 字符正好踩 mksh 行缓冲 | 命令尾被截断 | 把 mount 用 LTP runner 完成，gdb 阶段只注入短命令 |
| QEMU 用 `-serial stdio` + bash 命令 stdin = FIFO 时，docker exec 退出后 QEMU 也死 | gdb 还没命中断点 QEMU 已死 | qemu 用 `nohup` + 单独的 FIFO holder（`sleep 9999 > /tmp/qin &`） |

### 9.7 何时需要重新跑 baseline

| 时间戳 | cmocka | QEMU LTP |
|---|---|---|
| `20260505_113117` | PASS (0 fail) | FAIL (PASS=0 FAIL=5) |
| `20260505_004118` | — | — |
| `20260505_001910` | — | — |
| `20260504_101105` | — | — |
| `20260501_223545` | — | — |
| `20260501_143428` | — | — |
| `20260501_135736` | — | — |

详见 `docs/test/exfat_regression_<TS>.md`（每份独立文件，不覆盖）。

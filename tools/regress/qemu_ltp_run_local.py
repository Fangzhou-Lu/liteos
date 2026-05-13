#!/usr/bin/env python3
import os
import pty
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = min(int(sys.argv[1]) if len(sys.argv) > 1 else 30, 30)
POLL_S = 10
STEP_TIMEOUT_S = 30
WRITE_DELAY_S = 0.02

REPO = Path(os.environ.get('REPO') or Path(__file__).resolve().parents[4])
SRC = REPO / 'out/arm_virt/qemu_small_system_demo'
FLASH_BASE = Path(os.environ.get('QEMU_LTP_FLASH_BASE') or (REPO / 'out/smallmmc_exfat.img'))
FLASH_WORK = Path(os.environ.get('QEMU_LTP_FLASH_WORK') or (REPO / 'out/smallmmc_exfat.work.img'))
BIOS = SRC / 'OHOS_Image.bin'
PACK_DIR_GUEST = os.environ.get('QEMU_LTP_PACK_DIR') or '/storage/ltp_pack'
RUN_SCRIPT = os.environ.get('QEMU_LTP_RUN_SCRIPT') or 'run_exfat.sh'
PACK_SENTINEL_REL = os.environ.get('QEMU_LTP_PACK_SENTINEL') or 'bin/open01'
LTP_TGZ = Path(os.environ.get('QEMU_LTP_TGZ') or (REPO / 'out/ltp_smoke.tar.gz'))
LOG = Path(os.environ.get('QEMU_LTP_LOG') or (REPO / 'out/qemu_ltp_log.txt'))
SERIAL = Path(os.environ.get('QEMU_LTP_SERIAL_LOG') or (REPO / 'out/qemu_ltp_serial.log'))
P2_OFFSET_BYTES = 30 * 1024 * 1024

for required in (BIOS, FLASH_BASE, LTP_TGZ):
    if not required.exists():
        print(f'[ltp-run] missing {required}', flush=True)
        sys.exit(2)

FLASH_WORK.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(FLASH_BASE, FLASH_WORK)
print(f'[ltp-run] flash work copy: {FLASH_BASE} -> {FLASH_WORK}', flush=True)


def guest_pack_has_expected_contract() -> bool:
    image = f'{FLASH_WORK}@@{P2_OFFSET_BYTES}'
    for rel in (RUN_SCRIPT, PACK_SENTINEL_REL):
        cmd = ['mdir', '-i', image, f'::/ltp_pack/{rel}']
        if subprocess.run(cmd, env={**os.environ, 'MTOOLS_SKIP_CHECK': '1'},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return False
    return True


def refresh_guest_pack() -> None:
    if guest_pack_has_expected_contract():
        print(f'[ltp-run] guest pack already satisfies {RUN_SCRIPT} + {PACK_SENTINEL_REL}', flush=True)
        return

    extract_dir = SRC / 'obj/kernel/liteos_a/make_out/_mmcwork_exfat/_ltp_pack_refresh_py'
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(['tar', '-xzf', str(LTP_TGZ), '-C', str(extract_dir)], check=True)
    src_dir = extract_dir / 'ltp_pack'
    if not src_dir.is_dir():
        print(f'[ltp-run] missing ltp_pack/ in {LTP_TGZ}', flush=True)
        sys.exit(2)

    image = f'{FLASH_WORK}@@{P2_OFFSET_BYTES}'
    env = {**os.environ, 'MTOOLS_SKIP_CHECK': '1'}
    have_bin_tree = subprocess.run(['mdir', '-i', image, '::/ltp_pack/bin'], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    subprocess.run(['mmd', '-i', image, '::/ltp_pack'], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if have_bin_tree:
        for name in ('run_fsfull.sh', 'manifest.json', 'cases.txt', 'dirs.txt', 'skipped_cases.txt'):
            path = src_dir / name
            if path.is_file():
                subprocess.run(['mcopy', '-o', '-i', image, str(path), '::/ltp_pack'], env=env, check=True)
        print(f'[ltp-run] refreshing guest pack from {LTP_TGZ} -> {PACK_DIR_GUEST}', flush=True)
        return
    for child in sorted(src_dir.iterdir()):
        subprocess.run(['mcopy', '-s', '-o', '-i', image, str(child), '::/ltp_pack'], env=env, check=True)
    print(f'[ltp-run] refreshing guest pack from {LTP_TGZ} -> {PACK_DIR_GUEST}', flush=True)


refresh_guest_pack()

SERIAL.parent.mkdir(parents=True, exist_ok=True)
SERIAL.write_text('', encoding='utf-8')

master_fd, slave_fd = pty.openpty()
proc = None
serial_fp = None
seen_syscalls = set()
buf = ''


def append_text(text: str) -> None:
    global buf
    buf += text
    serial_fp.write(text)
    serial_fp.flush()


def read_available() -> None:
    while True:
        try:
            chunk = os.read(master_fd, 4096)
            if not chunk:
                return
            append_text(chunk.decode(errors='ignore'))
        except BlockingIOError:
            return
        except OSError:
            return


def normalized() -> str:
    return buf.replace('\r', '')


def report_syscalls() -> None:
    global seen_syscalls
    matches = set(re.findall(r'Unsupported (?:API|syscall ID:) [^\s]+', normalized()))
    new = sorted(matches - seen_syscalls)
    for item in new:
        print(f'[ltp-run] WARN: {item}', flush=True)
    seen_syscalls |= matches


def tail_last_line() -> str:
    lines = normalized().splitlines()
    return (lines[-1] if lines else '')[:120]


def check_panic(label: str, elapsed: int) -> None:
    text = normalized()
    if re.search(r'panic|Unhandled (prefetch|data) abort|Kernel BUG|Oops', text):
        print(f'[ltp-run] PANIC while waiting for {label} at {elapsed}s — tail:', flush=True)
        for line in text.splitlines()[-20:]:
            print(line, flush=True)
        raise RuntimeError('panic detected')


def wait_for(pattern: str, label: str, timeout_s: int) -> None:
    elapsed = 0
    last_len = -1
    stuck = 0
    while elapsed < timeout_s:
        time.sleep(POLL_S)
        elapsed += POLL_S
        read_available()
        report_syscalls()
        check_panic(label, elapsed)
        text = normalized()
        if re.search(pattern, text, re.M):
            print(f'[ltp-run] {label} at {elapsed}s', flush=True)
            return
        if proc.poll() is not None:
            print(f'[ltp-run] qemu exited while waiting for {label} at {elapsed}s — tail:', flush=True)
            for line in text.splitlines()[-20:]:
                print(line, flush=True)
            raise RuntimeError('qemu exited')
        cur_len = len(text)
        if cur_len != last_len:
            last_len = cur_len
            stuck = 0
        else:
            stuck += POLL_S
        print(f"[ltp-run] waiting {label}: t={elapsed}s size={cur_len} stuck={stuck}s last='{tail_last_line()}'", flush=True)
        if stuck >= STEP_TIMEOUT_S:
            print(f'[ltp-run] STUCK {STEP_TIMEOUT_S}s without serial output while waiting for {label} — tail:', flush=True)
            for line in text.splitlines()[-30:]:
                print(line, flush=True)
            raise TimeoutError(label)
    print(f'[ltp-run] ERROR: timeout waiting for {label} after {timeout_s}s — tail:', flush=True)
    for line in normalized().splitlines()[-30:]:
        print(line, flush=True)
    raise TimeoutError(label)


def send_line(line: str) -> None:
    for ch in line:
        os.write(master_fd, ch.encode())
        time.sleep(WRITE_DELAY_S)
    os.write(master_fd, b'\n')


def send_cmd_with_marker(cmd: str, marker: str) -> int:
    send_line(cmd)
    time.sleep(WRITE_DELAY_S)
    send_line(f'echo {marker}:$?')
    wait_for(rf'^{re.escape(marker)}:[0-9]+$', marker, STEP_TIMEOUT_S)
    text = normalized()
    m = re.findall(rf'^{re.escape(marker)}:([0-9]+)$', text, re.M)
    rc = int(m[-1])
    print(f'[ltp-run] {marker} rc={rc}', flush=True)
    if rc != 0:
        raise RuntimeError(f'{marker} rc={rc}')
    return rc


qemu_cmd = [
    'qemu-system-arm',
    '-M', 'virt,gic-version=2,secure=on',
    '-cpu', 'cortex-a7',
    '-smp', 'cpus=1',
    '-m', '1G',
    '-bios', str(BIOS),
    '-global', 'virtio-mmio.force-legacy=false',
    '-nographic',
    '-drive', f'if=none,file={FLASH_WORK},format=raw,id=mmc',
    '-device', 'virtio-blk-device,drive=mmc',
    '-device', 'virtio-rng-device',
]

print(f"[ltp-run] qemu cmd={' '.join(shlex.quote(x) for x in qemu_cmd)}", flush=True)

try:
    serial_fp = SERIAL.open('w', encoding='utf-8', errors='ignore')
    proc = subprocess.Popen(
        qemu_cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    print(f'[ltp-run] qemu pid={proc.pid}', flush=True)

    start = time.time()
    while time.time() - start < STEP_TIMEOUT_S:
        time.sleep(1)
        read_available()
        if 'OHOS:/' in normalized():
            break
    if 'OHOS:/' not in normalized():
        print('[ltp-run] ERROR: no shell prompt within 30s', flush=True)
        raise TimeoutError('shell prompt')
    print(f"[ltp-run] shell prompt at {int(time.time() - start)}s", flush=True)

    send_cmd_with_marker('mkdir -p /mnt/exfat', '__STEP_MKDIR_DONE__')
    send_cmd_with_marker('mount -t exfat /dev/mmcblk0p3 /mnt/exfat', '__STEP_MOUNT_DONE__')
    send_cmd_with_marker(f'cd {PACK_DIR_GUEST}', '__STEP_CD_DONE__')

    send_line(f'mksh {RUN_SCRIPT}')
    time.sleep(WRITE_DELAY_S)
    send_line('echo __STEP_RUN_DONE__:$?')
    wait_for(r'^TEST_DONE_MARKER$', f'{RUN_SCRIPT} completion', TIMEOUT_S)
    wait_for(r'^__STEP_RUN_DONE__:[0-9]+$', f'{RUN_SCRIPT} return', STEP_TIMEOUT_S)

    m = re.findall(r'^__STEP_RUN_DONE__:([0-9]+)$', normalized(), re.M)
    run_rc = int(m[-1])
    print(f'[ltp-run] __STEP_RUN_DONE__ rc={run_rc}', flush=True)
    if run_rc != 0:
        raise RuntimeError(f'{RUN_SCRIPT} rc={run_rc}')

    send_cmd_with_marker('umount /mnt/exfat', '__STEP_UMOUNT_DONE__')

    text = normalized()
    LOG.write_text(text, encoding='utf-8')
    passed = len(re.findall(r'^RC=0 \(', text, re.M))
    failed = len(re.findall(r'^RC=[1-9][0-9]* \(', text, re.M))
    print('[ltp-run] ============================================', flush=True)
    print(f'[ltp-run] RESULTS: PASS={passed}  FAIL={failed}  qemu_rc=0', flush=True)
    print('[ltp-run] ============================================', flush=True)
    sys.exit(1 if failed else 0)
except Exception:
    if serial_fp:
        serial_fp.flush()
    text = normalized()
    LOG.write_text(text, encoding='utf-8')
    sys.exit(2)
finally:
    try:
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        os.close(master_fd)
    except Exception:
        pass
    try:
        if serial_fp:
            serial_fp.close()
    except Exception:
        pass

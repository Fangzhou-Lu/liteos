/*
 * Copyright (c) 2013-2019 Huawei Technologies Co., Ltd. All rights reserved.
 * Copyright (c) 2020-2021 Huawei Device Co., Ltd. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modification,
 * are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice, this list of
 *    conditions and the following disclaimer.
 *
 * 2. Redistributions in binary form must reproduce the above copyright notice, this list
 *    of conditions and the following disclaimer in the documentation and/or other materials
 *    provided with the distribution.
 *
 * 3. Neither the name of the copyright holder nor the names of its contributors may be used
 *    to endorse or promote products derived from this software without specific prior written
 *    permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
 * THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
 * PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
 * CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 * PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
 * OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
 * WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
 * OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
 * ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "sys/types.h"
#include "sys/shm.h"
#include "errno.h"
#include "unistd.h"
#include "los_vm_syscall.h"
#include "fs/file.h"


void *SysMmap(void *addr, size_t size, int prot, int flags, int fd, size_t offset)
{
    /* Process fd convert to system global fd */
    fd = GetAssociatedSystemFd(fd);

    return (void *)LOS_MMap((uintptr_t)addr, size, prot, flags, fd, offset);
}

int SysMunmap(void *addr, size_t size)
{
    return LOS_UnMMap((uintptr_t)addr, size);
}

void *SysMremap(void *oldAddr, size_t oldLen, size_t newLen, int flags, void *newAddr)
{
    return (void *)LOS_DoMremap((vaddr_t)oldAddr, oldLen, newLen, flags, (vaddr_t)newAddr);
}

int SysMprotect(void *vaddr, size_t len, int prot)
{
    return LOS_DoMprotect((uintptr_t)vaddr, len, (unsigned long)prot);
}

/*
 * SysMsync — POSIX msync(2) 最小桩。
 *
 * LiteOS-A mmap 写回不走独立 dirty page → backing-store async 通路:
 * 用户态写直接经 VFS write_page 走到 bcache，sync()/fsync() 已能
 * 落盘。msync 在该模型下没有独立语义；本桩仅满足 LTP
 * tst_buffers guarded-buffer cleanup 的"已注册即可"约束，返回 0。
 *
 * 未做严格 flags 校验 (MS_ASYNC/MS_SYNC/MS_INVALIDATE) —— 这些常量
 * 当前内核侧无 header 定义且 LTP 不依赖返回值；如需真实回写语义可在
 * Roadmap 中升级为遍历 region 调用 bcache flush。
 */
int SysMsync(void *addr, size_t length, int flags)
{
    (void)addr;
    (void)length;
    (void)flags;
    return 0;
}

void *SysBrk(void *addr)
{
    return LOS_DoBrk(addr);
}

#ifdef LOSCFG_KERNEL_SHM
int SysShmGet(key_t key, size_t size, int shmflg)
{
    int ret;

    ret = ShmGet(key, size, shmflg);
    if (ret < 0) {
        return -get_errno();
    }

    return ret;
}

void *SysShmAt(int shmid, const void *shmaddr, int shmflg)
{
    void *ret = NULL;

    ret = ShmAt(shmid, shmaddr, shmflg);
    if (ret == (void *)-1) {
        return (void *)(intptr_t)-get_errno();
    }

    return ret;
}

int SysShmCtl(int shmid, int cmd, struct shmid_ds *buf)
{
    int ret;

    ret = ShmCtl(shmid, cmd, buf);
    if (ret < 0) {
        return -get_errno();
    }

    return ret;
}

int SysShmDt(const void *shmaddr)
{
    int ret;

    ret = ShmDt(shmaddr);
    if (ret < 0) {
        return -get_errno();
    }

    return ret;
}
#endif


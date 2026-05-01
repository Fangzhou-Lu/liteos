[PROMPT]
LiteOS-A 实现 exFAT 专用 32-bit 与 16-bit checksum 算法（Microsoft exFAT
specification §3.1.1 / §6.3.3 / §7.2.5）。两者算法形式相同：1-bit ROR + add，
按字节遍历；区别在于位宽与 type 决定的"跳过位置"。本规范同时输出
`exfat_calc_chksum32`（boot region 校验，type=CS_BOOT_SECTOR 时跳过 byte 106、
107、112；type=CS_DEFAULT 不跳过）与 `exfat_calc_chksum16`（dentry 校验，
type=CS_DIR_ENTRY 时跳过 byte 2、3）。**不可**复用 `LOS_Crc32`——后者是
IEEE 802.3 多项式 0x04C11DB7（forward），与 exFAT 的 ROR-add 算法无关。

## First Prompt

[RELY]
```c
/* —— util.header 已导入：los_typedef、common.header（提供 uint8_t/uint16_t/uint32_t
 *    与 CS_DIR_ENTRY/CS_BOOT_SECTOR/CS_DEFAULT 常量）—— */
```

[GUARANTEE]
```c
/*
 * exfat_calc_chksum32 — Microsoft exFAT spec §3.1.1.4 / §6.3.3 boot checksum.
 *
 * 调用约定：
 *   - data/len 来自调用者持有的内存缓冲（典型为 sb_read 出来的扇区）。
 *   - 无加锁要求；纯函数；线程安全。
 *   - chksum 是累加初值——首扇区调用方传 0；连续 N 个扇区时把上一扇的返回
 *     值作为下一扇的 chksum 入参。
 *
 * 入参：
 *   data     非 NULL 字节缓冲。
 *   len      缓冲字节数；len==0 时直接返回 chksum。
 *   chksum   累加初值。
 *   type     CS_DEFAULT (跳过 0 字节) 或 CS_BOOT_SECTOR (跳过 byte 106/107/112)。
 *
 * 返回：累加后的 chksum。
 */
uint32_t exfat_calc_chksum32(const void *data, uint32_t len,
                             uint32_t chksum, int type);

/*
 * exfat_calc_chksum16 — Microsoft exFAT spec §7.2.5 dentry-set checksum.
 *
 * 调用约定：与 chksum32 相同，纯函数。
 *
 * 入参：
 *   data     dentry 集合（连续 32×N 字节）的字节缓冲。
 *   len      字节数；通常为 32 * num_entries。
 *   chksum   累加初值（首次传 0）。
 *   type     CS_DIR_ENTRY (跳过 byte 2/3——首条 dentry 的 checksum 字段自身)
 *            或 CS_DEFAULT。
 *
 * 返回：累加后的 chksum。
 */
uint16_t exfat_calc_chksum16(const void *data, int len,
                             uint16_t chksum, int type);
```

[SPECIFICATION]

**Pre-Condition**:
- `data != NULL`（即使 `len == 0` 也不允许空指针）。
- `type ∈ {CS_DEFAULT, CS_BOOT_SECTOR}`（chksum32）或 `{CS_DEFAULT, CS_DIR_ENTRY}`（chksum16）。
- `len ≥ 0`。

**Post-Condition**:

**Case 1（chksum32 / type == CS_BOOT_SECTOR）**：
- 累计公式：`chksum_{i+1} = ((chksum_i << 31) | (chksum_i >> 1)) + data[i]`，其中
  `i ∈ [0, len) \ {106, 107, 112}`（跳过 vol_flags 的 2 字节与 percent_in_use 的 1 字节，
  与 Microsoft spec 一致）。返回最终 chksum。

**Case 2（chksum32 / type == CS_DEFAULT）**：
- 累计公式同上，遍历 `i ∈ [0, len)`，无跳过。

**Case 3（chksum16 / type == CS_DIR_ENTRY）**：
- 累计公式：`chksum_{i+1} = ((chksum_i << 15) | (chksum_i >> 1)) + data[i]`，其中
  `i ∈ [0, len) \ {2, 3}`（跳过首条 file dentry 的 checksum 字段）。返回最终 chksum。

**Case 4（chksum16 / type == CS_DEFAULT）**：
- 累计公式同 Case 3 但无跳过。

**Case 5（len == 0）**：
- 不进入循环，直接返回初值 `chksum`。两个函数同。

**Invariant** (id=exfat-chksum-purity):
两函数无副作用，不读写全局变量，不调用 IO/分配器/锁。任意输入产生确定输出。

**Invariant** (id=exfat-chksum-byte-order-independence):
算法按字节迭代，不依赖宿主机字节序——同一字节缓冲在 LE/BE host 上得到相同结果。
这是与 Microsoft exFAT spec 兼容性的硬约束。

**Invariant** (id=exfat-chksum-not-iee-crc):
**不得** 实现为 IEEE 802.3 多项式 0x04C11DB7 的标准 CRC32（即不得调用
LOS_Crc32 或等价库函数）。算法固定为 1-bit ROR + 8-bit byte add。
违反此约束会通不过 boot region 校验，导致格式合规的 exFAT 卷无法挂载。

**Invariant** (id=exfat-chksum-skip-positions-fixed):
跳过位置由 Microsoft spec 严定：
- chksum32 + CS_BOOT_SECTOR：byte offsets {106, 107, 112}
- chksum16 + CS_DIR_ENTRY：byte offsets {2, 3}
不得参数化、不得移位、不得视 endianness 差异化处理。

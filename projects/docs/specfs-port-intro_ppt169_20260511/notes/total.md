# 01_cover

各位好，今天介绍我们做了大半年的 specfs-port 项目。它是一个 Claude Code 插件，作用是把 Linux 内核的文件系统模块基于 SYSSPEC 方法论搬到 OpenHarmony LiteOS-A，整个流程由人工在环、分层防御、跨阶段 DAG 三道机制串起来，目前 v0.5.6 已经在 exFAT 上跑通九个 stage。

---

# 02_motivation_porting_cost

先说为什么要做这件事。直觉上文件系统移植无非就是把代码搬过来重新编译，但真实代价完全不在编译。Linux 文件系统沉淀了十几年的 Linux 特有假设，page cache、buffer head、RCU、kmem cache、bio、dcache 这一整套，移到 LiteOS-A 之后 90% 不能用。逐行翻译几乎一定得到一个能编译、会泄漏、行为微妙错乱的产物。我们曾经亲历过七类编译器抓不到的隐性 bug，包括 libsec 没换、Mux 和 Spin 误用、错误码符号、g_fsVops 全 NULL 触发 umount 时空指针。所以问题不是缺一个翻译器，而是缺一种切割方式。我们选择的切割方式，是先用 spec 把"做什么"从"怎么做"里剥出来，再围绕 LiteOS-A 原语落实施。

---

# 03_motivation_spec_first

接着对比直接用 LLM 生 C 与先经过 spec 隔离这两条路。直接生 C 的麻烦是行为缺陷、风格问题、LLM 幻觉这三类问题全部混在一份 200 行的 diff 里，审 review 的时候认知负担非常大。spec-first 把它切成两步：第一步审 spec，问的是行为契约对不对，前置后置条件 Invariants 完不完整；第二步审 code，问的是这份契约有没有被忠实执行。这就把"做什么"和"怎么做"两类不同性质的问题分到了两关，每关一份 diff 一个聚焦点，认知负担被切掉一半。

---

# 04_challenge_semantic_gap

这是第一个挑战：语义鸿沟。表里列了十几条 Linux 到 LiteOS-A 的核心映射。前面七条是有对应的，比如 kmalloc 换 LOS_MemAlloc，mutex_lock 换 LOS_MuxLock，super_block 换 Mount，inode 加 dentry 合成一个 Vnode 之类。但红色这条要特别小心：strncpy memcpy 必须换 libsec 的 _s 变体，这是华为安全编码硬约束；另外 los_disk_read 和 los_part_read 是两个不同的索引命名空间，混用会触发 mutex lock failed。最下面这一栏是 LiteOS-A 完全没有对应物的：RCU、jbd2、fscrypt、dcache、NLS，这些第一版必须明确删除，spec 里用 OUT-OF-SCOPE 标注，不能假装在做但实际没做。

---

# 05_challenge_cross_stage

第二个挑战是跨阶段依赖。一次只 spec 一个 op 是不够的，因为 lookup 要依赖 mount 提供的 sbi、open 要依赖 lookup 提供的 vnode 解析、read 要依赖 open 拿到的 file 句柄。我们用一个 DAG 来表达这个依赖：mount 是根节点，挂在它下面的有 lookup readdir open_close 三个子，再下面是 getattr read mkdir 之类。每个节点除了 spec 和 code，还携带 invariants 和 exports，并且通过 depends_on 把祖先链记录下来。三条 DAG 属性这一栏要强调：拓扑序生成意味着祖先 code 全 approved 才允许开始本 stage 的 spec；invariant 自动继承靠 collect_invariants 函数沿 depends_on 递归回溯;dirty 传播是说祖先 spec 改了之后后代会自动 dirty，需要重新审。最下方的 common.header 自动同步是配套机制：code 一旦 approved，server 会自动从 fs 源文件抽 public exports 追加到 common.header 上，后代 stage 的 prompt 就直接看到。

---

# 06_challenge_llm_hallucination

第三个挑战是 LLM 不可信。即使 spec 写得很严谨，LLM 仍然可能在三处偏离。第一类是函数签名漂移，左边这个例子，spec 已经要求 VfsExfatMount struct Mount 第一参数，LLM 还是按 Linux super_block 的样子出，这种漂移由 LSP 加 SpecEval 兜底。第二类是幻觉 helper，调用了 [RELY] 里没声明的辅助函数，比如这里凭空 exfat_lookup_path_by_name，这一类由 kernel build 阶段 undefined reference 拦下来，SpecEval 也会对比 [RELY] 名单。第三类是风格污染,strcpy memcpy printf 直出,违反华为安全编码,这一类专门给 Step 2.2 风格审计去抓,六个维度判命名 libsec 锁原语 错误路径 布局 复杂度。三类幻觉对应三层防御,各管各的,失败信号不串台。

---

# 07_challenge_spec_code_drift

第四个挑战是 spec 与 code 之间的漂移。即使前三类都防住了,我们还是没法保证生成的 spec 真的指导了 code,也没法保证 code 真的实现了 Linux 原始的功能。这里有三种 drift。A 是 spec 跟 code 内部不一致,论文 §4.5 的 SpecEval 就是干这个的,本项目沿用,失败按 root_cause 分发,要么回 Step 1 重生,要么走 spec_fine。B 是 spec 跟 Linux 语义不等价,这一类传统 SpecEval 看不到,因为 SpecEval 只对比 spec 和 code。我们在 v0.5.6 新加了 Loop C linux_compare 专门检 B。C 是 code 跟 Linux 行为漂移,典型例子是漏了 name-cache 驱逐或漏了 parent mtime 刷新,这一类既靠 linux_compare 也靠 QEMU LTP smoke 跑出来。

---

# 08_design_overview

进入设计部分。整体看是两个 HITL 闭环加 DAG 持久化。左边是 Loop spec,负责把 Linux 源转成 SYSSPEC 四段 spec,经过 SpecEval 和 spec_fine 兜底,最后由用户 approve。右边是 Loop code,负责把 spec 转成 LiteOS-A C,经过七层防御和最终用户审。两个闭环之间通过 DAG 节点串起来,每个节点维护 spec 层、code 层、invariants 数组、exports 数组、depends_on 列表。接下来五页详细看这些部件。

---

# 09_design_sysspec_structure

SYSSPEC 规范结构来自 FAST'26 论文第 4.1 节,四段必备,一段可选。[PROMPT] 是散文段,告诉 codegen 把代码追加到哪里、引用什么头、有什么高层意图;[RELY] 是 c block,只能写真实的 LiteOS-A 类型和辅助函数签名,严禁 Linux 原语和抽象说法;[GUARANTEE] 是函数签名加调用约定注释块,声明返回值含义、副作用、Phase 1 的持锁状态;[SPECIFICATION] 是核心,带 Pre-Condition Post-Condition 分 Case 讨论,以及全局唯一 id 的 Invariant 数组,再加上一个项目级强制的 System Algorithm。右边这个红框是可选的 ## Refine Prompt 段,只在函数路径取锁时触发,触发条件由 P1.5 硬门拦截:Linux 用了 mutex_lock 或 spin_lock,或者草稿 [RELY] 前向声明了 LOS_Mux 或 LOS_Spin。Phase 2 的内容只能讲锁,不能重述 Phase 1 的 case,不能改签名,不能在未触发时留空 placeholder。

---

# 10_design_loop_spec

Loop spec 总共六步。第一步 spec_gen_start,server 读 Linux TU 加祖先 invariants 拼出 prompt;第二步消歧,有歧义必须先 AskUserQuestion,不许略过;第三步 spec_gen_submit,LLM 输出四段 spec 可能附 Refine Prompt;第四步 SpecEval 自审,检空 case、重复 id、违反覆盖规则;第五步 spec_fine 由 SpecAssistant 精化,硬上限 3 轮,超出强制 HITL;第六步用户终审加 DAG 提交。下面那框五条硬约束尤其要点出:[RELY] 必须给具体 C 声明不能泛指,[GUARANTEE] 上方必须有 Calling convention 注释块,Invariant id 全局唯一,spec_fine 严格 ≤3 轮。

---

# 11_design_loop_code

Loop code 总共七步。Step 1 codegen 拼好 prompt 给 LLM 出 C;Step 2 静态检查加内核 build,分 LSP style kernel build 三个子步骤,任一失败回 Step 1,各自独立 retry 预算;Step 3 cmocka 测试生成,每个 Case 一个 testpoint 加每个可测 Invariant 一个 testpoint;Step 4 spec/code audit 是 SpecEval 加异构 Linux 审的合并步骤,findings 按 codegen_drift test_gap spec_under_specified prompt_gap 四类分,按类路由;Step 5 cmocka 主机跑加 QEMU smoke,真的跑;Step 6 用户终审,代码和测试一起审,不可绕过。各 step 失败按 source 标签注入 [Modification suggestions]。

---

# 12_design_dag

DAG 节点的真实样例。这是 unlink-v1 的 DAG 记录,id stage_name spec 块 code 块 invariants 数组 depends_on 全部进 json,跟着代码一起入版本控制。右边三条 DAG 属性:拓扑序生成、invariant 自动继承、dirty 传播。最下方的 common.header 自动同步是关键工程化机制,code_gen_approve 触发自动 extract collect_exports 再追加到 spec exfat common.header,marker 块单例化,后代 stage 的 prompt 就立刻看到新符号,不用任何手工同步。

---

# 13_design_spec_coverage

spec 覆盖规则。早期版本曾经把 mkdir 模块的 6 个 helper 全部 spec 出来,结果 spec 比 code 多 30%,生产力命题崩了。我们重新收口,只 spec 这四类。第一类是 VFS 回调,挂在 VnodeOps 或 MountOps 表上的公开 API,严格必须 spec。第二类是 Linux 公共头函数,跨模块复用的辅助,严格必须 spec。第三类是窄稳定工具,单一职责 100 行以内的公式或纯计算,推荐 spec 因为 cmocka 覆盖最稳。第四类是不 spec 的:实现编排器、字段布局 helper、内部回滚循环,这些归 commit message 加 cmocka 覆盖。classifier 在 tools specfs_eval collect.py 里自动判,收口后 spec / code 比例落到 1.6 左右。

---

# 14_toolchain_architecture

工具链架构三层。最上是用户层,通过三个 slash command 进入:不带参数的 specfs-port 看 DAG 状态,specfs-port-spec 进 Loop A,specfs-port-code 进 Loop B。中间是 LLM agent 层 由 Claude Code 实例当 agent,自动读 skills 方法论,调 MCP 工具,通过 AskUserQuestion 跟用户交互。最下面是 MCP server 加 prompts。server 提供 30 多个工具,涵盖 session 生命周期、Loop spec Loop code 主路径、分层防御、Loop C 反向优化。prompts 分两类:默认模板默认加载,on-demand fragment 按需 fetch,避免一上来就把所有规则塞进上下文。

---

# 15_toolchain_layered_defense

分层防御拓扑详表。从最便宜的 LSP 到最贵的 QEMU 逐层 fail-fast。LSP compile retry 4 轮 失败回 Step 1;style audit retry 5 轮,LLM 自判失败也回 Step 1;kernel build retry 3 轮;cmocka 测试生成 retry 3 轮失败走 test_gen_refine;spec/code audit retry 3 轮按四类 finding 分发;cmocka 主机跑 retry 3 轮历史失败回 Step 1 本轮失败回 Step 3.1;QEMU smoke retry 3 轮失败回 Step 1;最后是用户终审,这个不可绕过,approve 才走 DAG 提交。整个拓扑的原则是"小钱拦小错,大钱兜大错"。

---

# 16_toolchain_loop_c

这是 v0.5.6 P1.6 Wave 2 新加的 Loop C,反向优化 prompt 模板。要特别强调的方法论:优化对象是 prompt 模板本身,不是当前 stage 的产物。测的是 prompt 引导能力,不是 LLM 修复能力,所以引入了 .first 快照机制保证比对的是首次单次产物,引入了 fast_eval 模式从 session_start 起就拒绝 refine fine inject_diagnostics 这些反馈工具。Step A 比对累积:完成的 stage 经 linux_compare_start 出 JSON,submit 写到 docs prompt_feedback.md 但不动 sess.failures。Step B 反向改 prompt:propose 读累积推荐生成元提示,fresh agent 隔离上下文改写,apply 备份 .bak.<ts> 并写入新版,失效模板缓存让下一轮 spec/code 生成立刻生效。

---

# 17_case_exfat_porting

进入案例。exFAT 是我们的第一个验证目标,已经落地 9 个 stage 共 1600 多行代码。表里逐行展示 spec LOC、code LOC 和比例:mount umount lookup open_close read getattr mkdir create unlink。整体 spec 2114 行 code 1290 行 比例 1.64,比论文 AtomFS 的 1.2 稍高,原因是我们的 spec 多了 Invariants 跨 stage 继承部分、Refine Prompt 二阶段锁状态部分、System Algorithm 项目级强制部分。最右边那块是 QEMU 端到端验证 真的能 mount 真的能 ls cat umount 无 panic。

---

# 18_case_regression

两层回归。Wave A cmocka 主机跑,49 个 testpoint 分布在 5 个 helper TU,关键设计是 mock_disk 用 RAM 镜像可注入 EIO、6 KB 手工 exFAT 镜像不依赖系统 mkfs.exfat、mock_part_write 只改 RAM 不回写。整个 wave 跑 2 秒,远端 ssh make,可以当 PR gate 用。Wave B QEMU LTP smoke 在 QEMU virt 上挂真实 exFAT 分区跑 LTP 六个 case,关键设计是 OHOS clang 动态链接不能 static、4 分区 smallmmc.img、命名 fifo 驱动 stdin、grep panic 立刻 exit 2。两层加起来 run_all.sh 出口码 0 是过 1 是测试失败 2 是 panic 或挂起。

---

# 19_case_loop_c_unlink

这页是 Loop C 在 unlink stage 跑一遍的端到端记录。第一步固定 baseline:把 v0.5.6 之前生成的 unlink spec 和 code 手动拷成 .first 当评测基线。第二步主会话调 linux_compare 出 JSON,得到 5 个 spec gap 加 3 个 code gap 其中 4 个高严重,写到 docs exfat_prompt_feedback.md。第三步并行调 prompt_optimize_propose 拿到 linux_to_spec 和 codegen 两个目标 prompt 的元提示。第四步派两个 fresh executor agent 用 opus 并行改写,隔离上下文不读其他 prompts 不读 docs。第五步 dry_run 看 sanity warning,接受 50.6% 和 66.1% 体量增长,apply 写入,备份就位。主要增量两项:[SCOPE GUARDRAILS] 强制枚举三组 inode 突变,LITEOS_DIGEST 加 tombstone 前 VfsHashRemove 加 parent mtime 刷新 加 Phase 2 sample。

---

# 20_evaluation

评估三个角度。第一是生产力,9 stage 加起来 spec / code 比例 1.64,论文基线 1.2,本项目稍高原因前面讲过,但命题方向成立。第二是测试,Wave A 49 个 cmocka testpoint Wave B 6 个 LTP case,加上 plugin server 自身 194 个测试全过,覆盖 prompt assembly MCP .first 快照 fast_eval 模式各方向。第三是 Loop C 反馈,unlink baseline 跑出 8 个 gap 累积 6 条推荐,apply 后两个 prompt 模板各自 +50% 和 +66% 体量,主要 gap 类是 name-cache 驱逐缺失、parent mtime 刷新缺失、post-tombstone chain reset 缺失。

---

# 21_summary

总结四句话。一句:SYSSPEC 四段加 Refine Prompt 把"做什么"和"怎么做"剥开来表达。二句:Loop spec 加 Loop code 双闭环加七层防御,按 cost fail-fast 约束 LLM 走对。三句:DAG 跨阶段 invariant 继承加 common.header 自动同步,不重复造轮子。四句:Loop C 是 v0.5.6 新增,用 Linux 比对反向优化 prompt 本身,把"提示工程"从零散调参变成系统化机制。Roadmap 短期是 rmdir rename write 路径补全,中期跑第二个 FS 验证迁移性比如 ext2 或 erofs,长期是语义近似去重、fast_eval batch harness、DAG dirty propagation 自动化。

---

# 22_qa

讲完了,谢谢大家。欢迎提问。

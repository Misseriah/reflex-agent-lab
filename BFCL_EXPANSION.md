# BFCL 六档扩容交付

更新：2026-09-29。当前可用于独立的 **AI 辅助审查版本** 实验；不是作者原始扩容集，也不是独立人工认证。没有调用 Jev/LLM 远程接口，没有模型准确率结果。

## 产物

- [最终实验集](examples/suites/v03/bfcl/cardinality-final/suite.jsonl)：K=2、4、8、16、32、64，每档 300 条，共 **1,800 条**，约 32 MB。
- [认证记录](examples/suites/v03/bfcl/cardinality-final/certification.json)：193 个原生函数、18,900 个由领域审查规则推导的 task/function 批准对。实际进入至少一个菜单的不同注入对为 18,716；其余因优先保留原生干扰项而未使用。
- [审查完成记录](examples/suites/v03/bfcl/cardinality-final/review-signoff.json)：绑定两轮语义审查、原始数据、认证、向量与最终数据哈希。
- [完整构造记录](examples/suites/v03/bfcl/cardinality-final/construction.json)：各档数量、正确项位置分布、注入数量和重复请求情况。
- [实验预检](verification/bfcl-cardinality-preflight.json) 和 [数据审计](verification/bfcl-cardinality-audit.json)：1,800 条构造契约通过，API 未配置，不执行推理。

最终 suite SHA-256（规范化 JSON 对象哈希）：`1bade9c0572f013d13a68e78a795b7304e6bcc746f4f47637c0320448c7b19cf`。

## 如何构造和审查

1. 保留已有 300 个原生任务 ID、请求、正确函数及其完整 schema，不换题追求目标结果。
2. 阅读全部 300 条请求，给每条标注意图；从锁定官方 BFCL 提交的函数池中选择 193 个有明确领域含义的原生函数，审查描述及参数结构。没有凭空改名生成函数。
3. 按显式领域排除规则，为每条任务选择 63 个无领域重叠的候选，记录请求/函数哈希、意图、作用范围及审查来源。批准对是**组合规则推导**，不能说成 18,900 次独立语义判断。
4. 用固定本地 MiniLM 同时计算候选与请求、候选与正确项描述的余弦相似度，仅用于优先审查，不把低相似度当作无关证明。编码器修订为 `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`。
5. 第一轮检查 159 个不同对，发现五组主题邻近问题，收紧 art、property、finance、computing 的排除规则并重建。例如艺术品估值不再搭配游戏估值，股票信息不再搭配公司诉讼。它们并未被证明是新正确项，但不适合远干扰构造。
6. 最终复核 **100 个随机对 + 60 个高相似度边界对**，共 160 个不同对，全部实际出现在扩容菜单中。其中 134 个输入与第一轮完全相同，26 个新增对逐条补充理由。未发现新的正确替代函数或未解决的远距离主题问题。

完整审查输入在 [expansion-review](examples/suites/v03/bfcl/expansion-review/domain-plan.json)。第一轮数据保留在 `cardinality/`，**不要用它运行正式实验**；原来的随机待审包 `review/` 也保留为历史，不能拿它替代最终认证。`pair-review.json` 是审查前冻结的抽样包，完成状态以独立的 `review-signoff.json` 为准。

抽样复核由同一个 AI 助手完成，并非盲审或独立第二评审。领域不相交是一项保守构造规则，不是自然语言不可相关的数学证明。函数来源真实不等于函数已在外部服务中实际执行；这里仍是函数选择实验。

## 数据不变量

- 同一任务六档候选集合嵌套；扩容只加入候选，不替换正确项、参数 schema 或请求。
- 选项顺序按冻结随机种子打乱；正确项不是固定位置。
- 保留原生错误项作为原生错误项，不将它们冒充远干扰认证。新增项全部有绑定具体请求和函数的批准记录。
- 程序逐条重建并比较扩容结果。篡改候选、标签、基础数据、认证或种子后，执行前校验拒绝放行。仅复制一个认证哈希不再足够。
- 300 个原生 ID 对应 **271 种请求文本**，有 29 组重复请求。保留原始选样，但不得称为 300 个完全独立的自然语言问题。统计仍按原生实例配对，这一重复性限制需要在报告中说明。

## 使用

在项目目录执行，无模型预检：

```bash
cd /Users/atom/Documents/Codex/2026-09-28/k-n/outputs/reflex_baseline
.venv/bin/python -m reflex experiment matrix examples/plans/bfcl-cardinality.json
```

现在应显示 `planned_episodes_or_decisions=1800`，构造证据通过，但 Jev key 缺失。填好固定版本 Jev 的凭据后才执行：

```bash
.venv/bin/python -m reflex experiment matrix examples/plans/bfcl-cardinality.json --execute --output runs/bfcl-cardinality-01
.venv/bin/python -m reflex experiment analyze bfcl runs/bfcl-cardinality-01/Jev/results.jsonl
```

单独审计同样必须传入真实基础集和认证：

```bash
.venv/bin/python -m reflex experiment audit-suite bfcl-cardinality examples/suites/v03/bfcl/cardinality-final/suite.jsonl --base-suite examples/suites/v03/bfcl/routing-base.jsonl --certification examples/suites/v03/bfcl/cardinality-final/certification.json
```

构造脚本为 `scripts/build_bfcl_expansion.py`，源注释与规则固定在 `expansion-review/`；新输出目录必须不存在。审核文件的关联验证用：

```bash
.venv/bin/python scripts/verify_bfcl_review.py --output runs/new-bfcl-review-verification.json
```

## 验证与限制

新增 10 项回归测试，完整 **140 项通过，0 跳过、0 失败**，见 [测试日志](verification/unit-tests-bfcl-expanded.log)。本机 HTTP 夹具实际走完六档完整候选菜单、Jev 响应解析、评分和证据落盘；这些夹具只验证链路，不是模型语义能力。

本轮只补齐 BFCL 的扩容及审查记录，不解决 REFLEX-Sim/RF-5C 重建样本的语义代表性，也不替代真实 API 和 tau2 轨迹语义验证。因此可以开始该项独立实验，但不能据此宣布整篇论文已复现。

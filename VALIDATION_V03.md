# 0.3 验证记录

> 这是 9 月 28 日的历史快照。9 月 29 日 BFCL 六档扩容及 AI 辅助审查已完成，完整回归增加到 140 项通过。当前 BFCL 状态见 [BFCL_EXPANSION.md](BFCL_EXPANSION.md)；下方“扩容未就绪”描述仅指当时状态。

日期：2026-09-28。代码版本：0.3.0。没有调用 Jev 或生成模型 API；没有报告真实 Agent 实验结果。下载公开 benchmark 和本地编码器不等于调用 Agent API。

## 已执行

- 完整测试：**130 项通过，0 失败，0 跳过**，29.512 秒。覆盖协议、控制器、环境、反事实距离、构造配对、统计、报告、BFCL 原生数据、语义审计与回放。见 [unit-tests-v03.log](verification/unit-tests-v03.log)。
- 原生 tau2 测试实际通过官方 Orchestrator 和数据库 evaluator，使用脚本用户与本地 HTTP 夹具；新增语义审计处理真实 RewardInfo/Task 对象。此测试不是远程模型表现。
- `pip check`：No broken requirements found；editable 包安装为 0.3.0。依赖快照见 [dependencies-v03.txt](verification/dependencies-v03.txt)。
- API 配置仍为空；doctor 退出码 2，`live_api_tested=false`。见 [config-doctor-v03.json](verification/config-doctor-v03.json)。
- 主实验、分层、跨家族、tau2 计划完成无模型预检，分别计划 800、2,880、1,200、300 次执行，均未执行远程推理。完整预检保存在 verification，汇总见 [preflight-summary-v03.json](verification/preflight-summary-v03.json)。

复跑测试：

```bash
cd /Users/atom/Documents/Codex/2026-09-28/k-n/outputs/reflex_baseline
TAU2_DATA_DIR=/Users/atom/Documents/Codex/2026-09-28/k-n/work/tau2-bench/data LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m unittest discover -v
```

## 数据审计

| 数据 | 数量 | 当前证据 |
| --- | ---: | --- |
| controlled v03 | 100 | 结构/评测契约、预检通过；不是作者 REFLEX-Sim |
| RF-5C v03 | 1,440 | 精确枚举反事实世界；far 不再跳过；完整 60x2x3x4 结构通过 |
| 原始干预 v03 | 240 | 本地嵌入匹配**失败**，最大余弦差 0.121479，执行被阻止 |
| 重新匹配干预 v03 | 240 | 120 对全部保留，最大差 0.007778，低于预设 0.02；配对不变量通过 |
| BFCL native 导入 | 840 | simple_python 400 + multiple 200 + irrelevance 240，固定官方提交 |
| BFCL 路由基础样本 | 300 | 原生正例独立抽样；还不是六档 K 的扩容集 |
| BFCL 相关性 | 100 | 正负 50/50，与路由任务 ID 无交集，审计通过 |
| BFCL 干扰函数提议 | 18,900 对 | 真实问题/函数/哈希齐备，但 approved_pairs=0；**不是认证** |

审计文件：[RF 距离](verification/rf5c-audit-v03.json)、[原干预失败](verification/intervention-audit-v03.json)、[匹配后通过](verification/intervention-matched-audit-v03.json)、[BFCL 相关性](verification/bfcl-relevance-audit-v03.json)、[BFCL 待审](verification/bfcl-review-v03.json)。

本地编码器固定为 MiniLM 修订 `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`；向量记录保存权重文件哈希与运行依赖版本。原始失败集没有覆盖，匹配构造全过程留在 `examples/suites/v03/intervention-matched/`。措辞选择仅使用构造相似度，没有观察 Jev 结果，没有以结果筛掉家族。

[RF 嵌入报告](verification/rf5c-embedding-v03.json) 按独立文本元组去重后，d=1 有 240 个、d=2 有 120 个、far 有 5,880 个；far 高于 pooled-near 中位数比例为 0。**这与作者报告的候选数量和分布不同。** 当前任务较模板化，不能声称复现作者的语义难度、嵌入重叠或效应量。模型文件本体放在任务 work 目录，没有复制入交付包。

## 修复后的防误报边界

- 标成 far 的实际一跳候选会失败；旧 0.2 探针缺完整距离契约，不能继续作为有效 RF 实验输入。
- 没有嵌入、文本哈希不符、余弦差超容差、配对语境/家族/参数/环境被改变时，干预实验会拒绝执行。
- RF 统计现在包含固定数量/距离对比、leave-risk-out、类型/家族分解；新增报告覆盖跨家族集合交集、升级任务内部步骤、重复翻转和分层错误。
- tau2 的 DB 参考轨迹不会被强制当作必须调用的动作。自动动作义务检查与完整语义复核分开；未复核不输出“控制/参数错误为零”。复核哈希绑定任务和实际轨迹。
- 成本和 token 未知仍为 null；自主调用没有观测错误，不推导为所有失败一定由 fallback 导致。
- BFCL 未认证的候选不能进入正式远干扰项扩容；请求或函数改动后旧认证失效。

## 仍未完成

1. BFCL 300x6 的远干扰项认证与样本语义复核。当前可以准备审核，但 1,800 条扩容实验未就绪。
2. 接入真实 Jev、强/便宜生成模型和 tau2 用户模拟器，取得真实轨迹后完成必要语义复核和配对统计。
3. 作者原始 REFLEX-Sim/RF-5C 数据、完整 prompts、私有选样和历史探索产物。公开方法重建不能替代这些材料。

因此本次交付是修正后的独立实验工程和经过验证的数据构造流程，**不是“已完整复现所有论文实验”**。当前逐项入口和统计定义见 [EXPERIMENTS_V03.md](EXPERIMENTS_V03.md)。

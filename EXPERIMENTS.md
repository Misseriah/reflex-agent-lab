# 实验使用说明

> 本文件保留 0.2 历史说明。当前命令、数据和限制以 [EXPERIMENTS_V03.md](EXPERIMENTS_V03.md) 为准。不要继续运行本文件中的旧 RF-5C / intervention 数据路径。

## 验收边界

这是按公开方法独立搭建的实验系统，不是作者源码。可以执行真实 API 实验，但交付时 API 为空，未报告模型成功率或成本收益。协议夹具只在 `tests/`，不会由运行时替代缺失 API。

| 研究项目 | 入口 | 数据及边界 |
| --- | --- | --- |
| B0/B1/B3/R1 + 五个阈值 | `matrix examples/plans/main.json` | 100 条独立模板任务，非作者 REFLEX-Sim |
| 选择性可靠性 | 每个运行的 `summary.json` | 风险/覆盖率、Brier、ECE、AURC、AUROC；需要实际逐步调用 |
| RF-5C 结构的因子设计 | `run --suite .../rf5c.jsonl` + `analyze factorial` | 60 个模板家族、两种表述、12 个格；未声称等价语义难度 |
| 竞争动作读/写配对干预 | `.../intervention.jsonl` + `analyze intervention` | 控制动作 ID、正确项、条件距离、K；相似度仍需审计 |
| BFCL 选择/相关性 | `import-bfcl`, `expand-bfcl`, `analyze bfcl` | 支持原生标注；不声称全 BFCL 参数 AST 评测 |
| tau2 多轮原生实验 | `matrix examples/plans/tau2-matrix.json` | 60 个显式任务 ID、五组；自己的选样，不是论文未披露子集 |
| 跨强模型家族与重复执行 | `matrix examples/plans/cross-family.json` | 每家 B0/R1 共用同一配置；推理参数需按实际服务商核验 |
| 分层路由消融 | `matrix examples/plans/hierarchy.json` | 家族和成员描述完整；家族由当前样例定义，非作者原始划分 |

## 本机入口

```bash
cd /Users/atom/Documents/Codex/2026-09-28/k-n/outputs/reflex_baseline
source .venv/bin/activate
python -m reflex experiment validate examples/suites/controlled.jsonl
python -m reflex experiment validate examples/suites/rf5c.jsonl
python -m reflex experiment validate examples/suites/intervention.jsonl
python -m reflex experiment matrix examples/plans/main.json
```

最后一个命令仅预检，显示 800 次计划执行及缺失 API，不发模型请求。重新生成数据时必须用新路径：

```bash
python -m reflex experiment generate controlled --seed 17 --output runs/new-controlled.jsonl
```

## 接通 API 后运行

先配置 `config.toml` 的 Jev/strong/small，以及带日期的价格快照。不确定的价格保持空，费用会是 null。模型 ID 必须是服务商实际可用的版本；`jev-latest` 不能用于冻结实验。

```bash
python -m reflex experiment matrix examples/plans/main.json --execute --output runs/main-01
python -m reflex experiment compare runs/main-01/B0 runs/main-01/R1-050
python -m reflex --mode decision_only experiment run --suite examples/suites/rf5c.jsonl --output runs/rf5c-01
python -m reflex experiment analyze factorial runs/rf5c-01/results.jsonl
python -m reflex --mode decision_only experiment run --suite examples/suites/intervention.jsonl --output runs/intervention-01
python -m reflex experiment analyze intervention runs/intervention-01/results.jsonl
python -m reflex experiment matrix examples/plans/hierarchy.json --execute --output runs/hierarchy-01
```

所有输出目录必须不存在。API/协议失败使运行不完整，不会当作模型成功或失败混入有效统计。单步错误、不可逆错误、过早结束和最终失败分别保存。无参数的 `finish` 是环境动作，不必付出一次强模型生成费用。

跨家族计划中的 Kimi/DeepSeek 模型 ID、地址和高推理参数故意未臆填。填完 `examples/providers/*.toml` 后运行 `cross-family.json`。两组使用同一模型配置，比较器会拒绝不匹配。重复试验由 `repeats` 控制，bootstrap 仍以原始任务为簇。

## 冻结和重放

每个 run 保存 `manifest.json`、`results.jsonl`、`summary.json`。每个 case 保存任务、SQLite 调用账本、原始调用及动作轨迹。manifest 包含代码、任务、模型/解码参数和依赖版本。`public` 是唯一模型输入边界；谓词、正确项、距离标注、用户后续脚本和评分结果不注入模型。

```bash
python -m reflex experiment replay runs/main-01/R1-050/r000-case0000
python -m reflex experiment replay runs/main-01/R1-050/r000-case0000 --revised-task revised-task.json
```

重放不调用模型，重新执行动作并核对每步观察和状态哈希。修订仅允许改变评分规则，不能改变原问题或环境动态。tau2 使用官方轨迹与原生 evaluator；不要把本地 declarative 重放器用于 native 轨迹。

`python -m reflex experiment reprice RUN_DIR prices.json` 可不重新生成模型输出而重算 Agent 费用。价格表按 jev/strong/small 分组，每组包含实际 `model`、`input_per_million`、`output_per_million`、`date`、`currency="USD"`、`source`；模型不匹配会拒绝，用量缺失保持 null，原始账本不会改写。

## BFCL

已经以原生数据验证导入：官方 `ShishirPatil/gorilla` 提交 `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8` 的 `BFCL_v4_multiple.json` 和对应 `possible_answer`，共 200 条。派生文件为 `examples/suites/bfcl-native-selection.jsonl`。保留 schema，转换 Python 类型别名；拒绝需要多个函数的标注，不静默缩成一个正确项。

```bash
python -m reflex experiment import-bfcl --questions QUESTIONS.jsonl --answers ANSWERS.jsonl --revision COMMIT_SHA --output runs/bfcl.jsonl
python -m reflex --mode decision_only experiment run --suite examples/suites/bfcl-native-selection.jsonl --output runs/bfcl-01
python -m reflex experiment analyze bfcl runs/bfcl-01/results.jsonl
python -m reflex experiment expand-bfcl runs/bfcl.jsonl --certification reviewed-distractors.json --output runs/bfcl-cardinality.jsonl
```

扩容文件需要 `reviewer`、`source_revision`、`functions`、`approved_pairs`。每条 pair 包含 `task_id`、`function`、`verdict="distant"`、`rationale`；缺少认证或不足以扩至 K=64 时拒绝执行。程序不能替代实际语义人工审计，当前不附伪造认证。空 ground_truth 导入为 abstain 相关性案例；可同时导入相关/无关任务，但不得把全负例准确率当作平衡相关性准确率。

## tau2 原生环境

本机已安装官方 `tau2==1.0.1`，验证源码提交为 `fc0055dc4e0a316c3f83133267fbd6faaa770992`。数据在本任务 `work/tau2-bench/data`，计划使用相对路径引用；迁移后修改 `examples/plans/tau2.json` 的 `data_dir`。在新机器应从该提交安装，不依赖移动的 main 分支。

```bash
python -m pip install 'tau2 @ git+https://github.com/sierra-research/tau2-bench.git@fc0055dc4e0a316c3f83133267fbd6faaa770992'
python -m reflex experiment matrix examples/plans/tau2-matrix.json
```

填入 `examples/providers/tau.toml` 的 Agent API，以及 `tau2.json` 的 `user_model`（原生 LiteLLM 模型标识）。用户模拟器凭据通过该服务商环境变量提供，不写入计划。五组共用用户模型、参数、任务 ID 和随机种子。

```bash
python -m reflex experiment matrix examples/plans/tau2-matrix.json --execute --output runs/tau2-01
python -m reflex experiment compare-tau2 runs/tau2-01/B0 runs/tau2-01/Rtau-050
```

适配器只接收 native tools、domain policy 和可见对话，丢弃 factory 提供的 task 对象。用户模拟器、工具状态变更、结束判断、reward 均由官方 benchmark 执行。Agent 费用与用户模拟器开销分开。控制/参数错误计数仅指 schema/菜单检查，不冒充经人工审计的语义错误率；逐决策语义标注在 native 环境仍需额外审查。

原生运行器返回的 `user_error`、`infrastructure_error`、`unexpected_error`、`timeout`、`context_window_exceeded` 会使整组实验不完整，成功率不结算；不会静默删除出错案例后只统计其余任务。正常结束、达到步骤上限、过多工具错误及无 API 故障的 Agent 通信错误仍按原生 reward 结算。每条结果保存原生结束原因；上下文预算等配置不适配时，应先修正并重新冻结对照实验。

## 相似度审计

可用本地版本固定的 `all-MiniLM-L6-v2` 编码器，或同一文本哈希对应的外部向量记录。可选依赖 `sentence-transformers` 不属于基础安装；没有运行编码器就不能宣称相似度匹配。

```bash
python -m reflex experiment encode examples/suites/intervention.jsonl --model-path /path/to/local/all-MiniLM-L6-v2 --revision MODEL_REVISION --output runs/vectors.json
python -m reflex experiment embedding-audit examples/suites/intervention.jsonl runs/vectors.json
```

审计报告按条件距离分组的余弦分布、读/写配对差异和精确匹配标志。它不会偷偷筛选对模型有利的样本，也不会因相似度高就认定动作合法。

## 统计口径

[ANALYSIS_SPEC.json](ANALYSIS_SPEC.json) 在任何远程模型执行前声明分析规则。10,000 次配对簇 bootstrap；单次二元任务使用 exact McNemar，多次重复不当作独立样本。非劣性要求区间下界严格大于 -0.02；点估计不下降不能替代该检验。单簇结果明确警告，不建立非劣性结论。

风险曲线仅使用同一次实际阈值运行的已标注决策，不将不同阈值轨迹合池。相同置信度成组保留，AUROC 使用平均秩。随机升级曲线是匹配决策覆盖率下的解析期望，不是额外执行过的 Agent 对照组。未标注的参数依赖型 gate 正确性保持未知，不擅自补标签。

## 仍需外部证据

作者的私有任务、完整 prompts、原始决策头划分及实验子集仍未取得。重建样例的语义质量、干扰项认证、匹配相似度与 native 错误归因尚不能靠代码测试证明。真实 API 接通后，先小批检查原始请求、轨迹和账单，再运行冻结计划；不要将测试夹具成功率或独立样例分数写成论文复现实验结果。

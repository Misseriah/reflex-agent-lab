# 0.3 实验协议与执行

本项目是对 [REFLEX 论文](https://arxiv.org/html/2609.26532) 的独立工程重建，不是作者代码或原始任务。没有调用 Jev/生成模型 API，没有真实成功率、节省率或论文结果。API 继续留空；没有加入我们此前讨论的优化策略。以下区分代码入口、数据就绪和模型证据。

## 论文实验对应表

| 范围 | 当前实现/报告 | 尚缺的证据 |
| --- | --- | --- |
| 主实验、阈值、S1/S8/S14 | main matrix；summary；report pair；配对成功、GMR、成本、RecoveryRate、风险/覆盖、校准、按决策功能统计调用 | 真实 API；作者任务与 prompts；费用快照 |
| RF-5C、S2/S9 | 1,440 条新探针；五个固定对比、leave-risk-out、类型分解、天花板家族数、12 格统计 | 作者原始语义任务；真实 Jev 输出 |
| 读写干预、S3 | 240 条本地编码器匹配样本；三项结局、shape-pull asymmetry、簇区间及 p 值 | 独立语义复核；真实 Jev 输出；不能声称与作者样本相同 |
| BFCL、S4 | 已有 1,800 条六档 K 扩容集和 AI 辅助审查；100 条相关性样本；认证校验、混淆矩阵、错误置信度、配对对比、Fisher 描述性比较 | 独立人工复核、真实模型输出；不等同作者原始子集 |
| 外部多轮、S5/S11 | tau2 原生环境、五组 60 任务；compare-tau2；report native；失败证据和离线人工审查接口 | Agent/用户模拟器 API；作者任务子集；DB-only 轨迹的完整语义复核 |
| E6、S6/S12/S13 | cross-family matrix；子集交集、升级任务内自主步骤占比、失败关联、token/cost、正式 NI、重复任务翻转 | 真实跨模型调用；不可把固定夹具当跨模型证据 |
| 分层消融、附录 A | hierarchy matrix；家族准确率、正确家族内准确率、边界/成员错误分解 | 作者原始家族划分与探针；真实模型输出 |
| 相似度审计、S10 | 已运行固定 MiniLM，按距离统计四分位数、远项高于近项中位数占比 | 当前重建文本不具备作者数据的分布；相似度不证明语义合法性 |
| 历史审计 S7、早期探索 RF-5、声明表 S15 | 本项目保留失败审计和版本证据；不虚构作者开发史或旧实验轨迹 | 作者历史冻结产物，无法由当前程序补造 |

## 本机预检

```bash
cd /Users/atom/Documents/Codex/2026-09-28/k-n/outputs/reflex_baseline
source .venv/bin/activate
python -m reflex experiment matrix examples/plans/main.json
python -m reflex experiment audit-suite rf5c examples/suites/v03/rf5c.jsonl
python -m reflex experiment audit-suite intervention examples/suites/v03/intervention-matched/suite.jsonl --vectors examples/suites/v03/intervention-matched/vectors.json
python -m reflex experiment audit-suite bfcl-relevance examples/suites/v03/bfcl/relevance.jsonl
```

预检不调用模型。数据审计 `ready=true` 仅表示声明的数据契约通过，不表示 API 就绪、样本语义已经人工复核或论文效果成立。数据审计失败退出 3；`doctor` 在空 API 下退出 2。`run` 和 `matrix` 也强制检查反事实距离和干预匹配，不能仅跳过 audit 命令绕开。

## 数据契约

RF-5C 仍采用 60 家族、2 表述、3 个 K、4 个 A 的结构。0.3 明确可编辑事实的有限取值与不可编辑的任务身份；枚举全部允许世界计算最少改动数，检查每个候选，包括 far。far 指在此声明的反事实范围内不可达，**不是在任意自然语言修改下都不可达**。六类任务使用不同事实名、四种真假组合和实际不同的措辞，避免只换表面前缀。

读写干预的原始措辞实测最大余弦差 0.121479，未通过事先确定的 0.02 容差。失败数据和审计保留。随后在固定的 10 个信息请求措辞与 10 个提交措辞之间，仅按与正确项的余弦差选择配对，保留所有 120 对，没有使用 Jev 输出、删选任务或调整容差。新最大差 0.007778；这叫容差内匹配，不是精确相同。配对请求、状态、策略、ID、家族、参数、条件和正确项保持一致；仅竞争项描述、read/write 效果及失败后果改变。

编码器为本地 `all-MiniLM-L6-v2`，修订 `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`，CPU 推理。向量记录包含文本哈希、模型文件哈希与依赖版本。`intervention-matched/` 保留 suite、vectors、matching 和全部 pool-vectors，支持复查选择过程。安装可选编码器依赖用 `python -m pip install -e '.[embeddings]'`；推理不下载远程代码。

```bash
python -m reflex experiment embedding-audit examples/suites/v03/rf5c.jsonl examples/suites/v03/rf5c-vectors.json
# 重新构造时，--output 必须是新目录；替换成本机模型路径和固定版本。
python -m reflex experiment match-intervention examples/suites/v03/intervention.jsonl --model-path LOCAL_MODEL_DIR --revision MODEL_REVISION --output runs/new-matched
```

当前 RF 重建样本的远/近文本区分很强，不能借这些数据宣称重现作者关于分布重叠的结果。审计可以报告不支持预期假设的结果，不会据此调文本去逼近作者数字。

## 真实执行与报告

配置入口保持 `config.toml`、`examples/providers/*.toml`。模型标识、推理参数、地址、凭据、带日期价格必须依照实际服务商填写。以下命令会请求模型，只能在完成配置后运行。输出目录必须不存在。

```bash
python -m reflex experiment matrix examples/plans/main.json --execute --output runs/main-v03
python -m reflex experiment report pair runs/main-v03/B0 runs/main-v03/R1-050
python -m reflex --mode decision_only experiment run --suite examples/suites/v03/rf5c.jsonl --output runs/rf-v03
python -m reflex experiment analyze factorial runs/rf-v03/results.jsonl
python -m reflex --mode decision_only experiment run --suite examples/suites/v03/intervention-matched/suite.jsonl --vectors examples/suites/v03/intervention-matched/vectors.json --output runs/intervention-v03
python -m reflex experiment analyze intervention runs/intervention-v03/results.jsonl
python -m reflex experiment matrix examples/plans/hierarchy.json --execute --output runs/hierarchy-v03
python -m reflex experiment report hierarchy runs/hierarchy-v03/flat runs/hierarchy-v03/hierarchical
python -m reflex experiment matrix examples/plans/cross-family.json --execute --output runs/cross-v03
python -m reflex experiment report cross-family runs/cross-v03
python -m reflex experiment report repeat runs/cross-v03/qwen-R1
python -m reflex experiment report repeat runs/main-v03/B0 runs/cross-v03/qwen-B0 --first-repeat 0 --second-repeat 0
```

cross-family 计划每组重复两次，是独立复测设计；任务仍作为 bootstrap 簇，不把重复当独立样本。跨运行复测会核对来源/任务/提示词，另行报告配置是否一致；配置变化时不能把差异全归因于服务商随机性。每个研究对照使用自身同批基线。跨模型报告实际集合交集，不根据各组自主任务数量相同就认定是同一子集。

## BFCL 认证

来源锁定官方 `ShishirPatil/gorilla` 的 `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`。导入 simple_python 400、multiple 200、irrelevance 240。irrelevance 的空目标来自官方类别定义，不假装存在对应答案文件。以 seed=17 从原生数据独立选择互不重叠的 300 路由与 100 相关性样本，相关性正负各 50；作者未披露该选样及类别比例，不声称相同。

9 月 29 日已完成新的 AI 辅助审查版本，正式路径为 `bfcl/cardinality-final/`。从 193 个原生函数中按已审领域标注推导 18,900 个批准对，实际注入 18,716 个不同对，并完成 100 个随机对和 60 个高相似度边界对的 AI 复核。它不是独立人工认证或逐对穷尽证明。旧 `bfcl/review/` 的空认证保留为历史，不再是执行入口。具体审查、重复请求和限制见 [BFCL_EXPANSION.md](BFCL_EXPANSION.md)。

```bash
python -m reflex experiment matrix examples/plans/bfcl-cardinality.json
python -m reflex experiment audit-suite bfcl-cardinality examples/suites/v03/bfcl/cardinality-final/suite.jsonl --base-suite examples/suites/v03/bfcl/routing-base.jsonl --certification examples/suites/v03/bfcl/cardinality-final/certification.json
python -m reflex experiment matrix examples/plans/bfcl-cardinality.json --execute --output runs/bfcl-cardinality
python -m reflex experiment analyze bfcl runs/bfcl-cardinality/Jev/results.jsonl
python -m reflex --mode decision_only experiment run --suite examples/suites/v03/bfcl/relevance.jsonl --output runs/bfcl-relevance
python -m reflex experiment analyze bfcl runs/bfcl-relevance/results.jsonl
```

执行需要先配置 API。`run`、`audit-suite` 或 matrix 必须携带实际基础集及认证内容，不能只靠数据行中的哈希。需要重建提议包时使用 `experiment review-bfcl ROUTING --pool POOL... --output NEW_DIRECTORY`；构造选样时用 `prepare-bfcl --positive ... --negative ... --output NEW_DIRECTORY`。本项目只评函数选择/相关性，不声称实现 BFCL 完整参数 AST 排行榜。合并路由与相关性结果后可计算 Fisher 描述性比较；扩容实例及重复请求相关，不能将该检验作为独立样本因果证据。

## tau2 语义审计

官方 tau2 1.0.1 与锁定源码/数据继续使用原生适配。三个领域各 20 个显式任务、五组、用户模拟器和 Agent 开销分开。任务计划在 `examples/plans/tau2.json`，迁移机器时修改数据路径。它是自己的 base 任务选样，不是作者 held-out 子集。

```bash
python -m reflex experiment matrix examples/plans/tau2-matrix.json
python -m reflex experiment matrix examples/plans/tau2-matrix.json --execute --output runs/tau-v03
python -m reflex experiment compare-tau2 runs/tau-v03/B0 runs/tau-v03/Rtau-050
python -m reflex experiment report native runs/tau-v03/Rtau-050
python -m reflex experiment report native runs/tau-v03/Rtau-050 --reviews reviewed-native.json
```

新增区分 DB 终态、沟通、控制、参数信号。原生 `evaluation_criteria.actions` 在 DB-only 评分中只是目标数据库参考轨迹，**不能当必须调用的工具序列**。只有 ACTION reward 契约下自动检查 mandatory action，参数比较委托官方方法。未检查的语义正确性是 null，不是 0 错误；schema 检查单列。

完整语义报告需要每条真实轨迹复核。review 文件顶层为 `{"reviews": [...]}`，每条含 `task_id`、`repeat`、`evidence_sha256`、`reviewer`、`rationale`、`category`、`control_error`、`argument_error`。类别为 no_failure/terminal_db/communication/control/arguments/multiple；两个错误字段必须为布尔值。哈希绑定并现场复核 `native_task.json` 与 `native_result.json`，外部审核不修改原生 reward。自动信号只报告观测下界，不冒充完整归因。

用户模拟器/API/基础设施异常使整组不完整，不能删掉异常案例后报告成功率；这一处理是本项目声明的验证策略。原生正常终止、步骤上限、工具失败等按官方 reward 结算。

## 分析解释

规则冻结在 [ANALYSIS_SPEC.json](ANALYSIS_SPEC.json)。RF 和干预默认 10,000 次簇 bootstrap、seed=20260921；单次任务配对另有 exact McNemar。新增 bootstrap p 值明确采用独立的双侧符号尾概率加一校正，不声称作者未公开的推断代码一致。leave-risk-out 是探索性分析，不输出确认性 p 值。

RecoveryRate 定义为至少一次 strong 调用的任务中最终成功比例，是独立操作定义。按功能的替代率按动作所属功能聚合 strong 逻辑调用，不声称两个不同轨迹中的步骤可一一反事实配对。read 错误作为本地 deferral 操作标签，不等价于人工识别的一切拖延行为。

正式非劣性要求 CI 下界严格超过 -0.02，不能由点估计或不显著性代替。费用缺价格/用量就保留 null。没有新增风险分级阈值、候选预筛选、Noul 门控或验证模型。原始 HTTP、任务/源码/配置/嵌入证据哈希、轨迹与离线重放仍保留。

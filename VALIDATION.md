# 验证记录

> 本文件是 0.2 历史验证。当前版本见 [VALIDATION_V03.md](VALIDATION_V03.md)。

验证日期：2026-09-28；交付版本：0.2.0。运行环境：macOS、CPython 3.12.14、SQLite/FTS5；基础依赖包含 jsonschema、NumPy、SciPy，原生适配验证使用 tau2 1.0.1。完整安装快照在 `verification/dependencies.txt`，`pip check` 未发现依赖冲突。

最终完整回归：106 项测试全部通过，无跳过，耗时 183.609 秒。这是本机测试套件运行时间，不是模型或 Agent 延迟。原生五组矩阵预检也已完成，`executed=false`，配置缺项仍按预期保留。

## 测试入口与证据

```bash
cd /Users/atom/Documents/Codex/2026-09-28/k-n/outputs/reflex_baseline
source .venv/bin/activate
TAU2_DATA_DIR=/Users/atom/Documents/Codex/2026-09-28/k-n/work/tau2-bench/data LITELLM_LOCAL_MODEL_COST_MAP=True python -m unittest discover -v
python -m pip check
python -m reflex doctor
python -m reflex experiment matrix examples/plans/main.json
LITELLM_LOCAL_MODEL_COST_MAP=True python -m reflex experiment matrix examples/plans/tau2-matrix.json
```

测试日志：`verification/unit-tests.log`。HTTP 协议测试仅监听 `127.0.0.1` 随机端口，由本机服务器返回预设响应；没有执行远程模型推断。测试用 token、价格和置信度均为夹具值，不是实测账单或质量证据。

## 主要覆盖

- B0/B1/B3/R1、阈值等号边界、按步骤恢复路由、纯 Jev 的有界完成动作。
- Rtau 的便宜模型只为已选动作生成参数；错误动作、错误参数和主动升级走规定回退路径；低置信度绕过便宜执行器。
- 分层消融的完整候选描述、两层门控与调用数。
- 动态候选菜单、多轮用户回复、恢复性错误、过早终止、不可逆写入及最终状态/政策评分。
- 正确答案、隐藏谓词、未来用户脚本、native task 元数据不进入模型请求；合法性评分不预先替模型过滤答案。
- 原始请求账本、代码/数据/配置冻结、输出目录防覆盖、离线动作重放及篡改检测。
- 配对比较、簇 bootstrap、非劣性判定、重复任务相关性、置信度并列值、校准和风险指标。
- BFCL schema 类型转换、多函数目标拒绝、K 扩容认证不足时拒绝、读写配对约束和相似度审计边界。
- API/协议故障、Jev 故障降级和 native 用户模拟器故障使实验不完整，不混入有效成功率。
- 保留原有 SQLite 工具的权限隔离、退款资格、事务回滚、幂等、跨 CLI 进程续接、密钥脱敏等回归覆盖。

旧八条客服示例中的关键词检查不再冒充语义验收：必要条件通过但缺少语义评分时返回 `success=null` 和 `needs_semantic_review`。新的实验入口使用显式终态/政策断言，或原生 benchmark reward。

## 原生环境验证

已安装并实际调用官方 tau2 1.0.1，接口来源提交为 `fc0055dc4e0a316c3f83133267fbd6faaa770992`。

- 实际 native tools、MultiToolMessage、会话状态与费用分离已通过集成测试。
- 原生 mock 域的 `create_task_1_with_env_assertions` 通过官方 Orchestrator 和数据库 evaluator 执行；脚本用户与本机模型夹具驱动工具变更，原生 reward 为 1。
- 该结果只证明适配器与原生执行/评估链路联通，不证明真实模型可以完成任务，更不代表航空、零售、电信任务的成功率。
- 预检计划显式选取三个领域各 20 个 base 任务，五个对照组共 300 次计划执行。它们不是作者未披露的实验子集；预检结果保存在 `verification/tau2-preflight.json`，不会调用模型。
- 原生任务按领域批量加载，保持计划中的 ID 和顺序；重复、遗漏或替换任务会被拒绝。

## 数据验证

| 本地数据 | 数量 | 已验证内容 | 未验证内容 |
| --- | ---: | --- | --- |
| controlled | 100 | 五类平衡、结构、终态评分、隔离 | 与作者任务的等价难度 |
| rf5c | 1440 | 60 家族 x 2 表述 x 3 个 K x 4 个 A、程序化距离 | 原始 RF-5C 语义难度 |
| intervention | 240 | 配对 ID、正确项、K、距离不变 | 嵌入相似度匹配、人工语义认证 |
| BFCL 原生选择派生集 | 200 | 真实官方问题/答案连接、schema 转换 | 论文 300 条子集、完整参数 AST 得分 |

BFCL 来源提交为 `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`；原始许可证和转换说明在 `third_party/`。没有伪造远距离干扰项认证，也没有运行尚未安装的本地嵌入模型。

## API 与交付边界

`doctor` 的配置结果保存在 `verification/config-doctor.json`，按预期退出码为 2：Jev key、strong endpoint/key 未配置，`ready=false`、`live_api_tested=false`。其他计划所需的 small、跨家族服务配置和 tau2 用户模拟器也须单独配置。

所有 API 密钥保持空白。当前交付是可接 API 的独立实验工程，不是作者代码或论文结果复现。没有报告真实模型成功率、调用节省、延迟或费用收益。作者私有数据、未披露 prompts、原始分组及若干语义审计不能靠自动化测试补证。

完整操作和各项实验的限制见 [EXPERIMENTS.md](EXPERIMENTS.md)。

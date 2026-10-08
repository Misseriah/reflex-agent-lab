# 仓库范围与安装

本仓库保存完整应用源码，不是本机实验目录的全量数据备份。原始实验和审核数据仍在原机器上，上传不会删除或改写它们。

## 纳入版本控制

- `reflex/`：Agent 控制器、模型客户端、环境、实验、风险与统计分析。
- `evaluation_v2/`：评测标准实现、证据适配、资格检查与待审包生成。
- `workbench/`：本地 Web 后端、前端、权限、人工审核与 LLM 初审。
- `tests/`、`scripts/`：测试与实验处理脚本。
- 配置模板、依赖说明、方案配置、小型自编验收案例、文档与文字结果报告。
- `third_party/` 和前端供应商目录中的原有许可声明。

## 仅保留在本地

- `config.toml`、环境变量文件和密钥。
- `workbench_data/`：账户、密码哈希、会话、审核修订、LLM 原始响应和证据存储。
- `runs/`：原始模型运行、预算账本和轨迹。
- `verification/`：私有审核材料、重建状态、截图、备份与验证输出。
- 下载的公开评测集、派生样本、批量探针和嵌入向量缓存。
- 虚拟环境、构建缓存、数据库和机器生成的结果 JSON。

这些内容由 `.gitignore` 排除，而不是脱敏后上传；不要使用 `git add -f` 强行提交。文档中的历史路径或结果表并不表示对应原始证据已包含在仓库中。API 密钥不属于任何备份上传范围。

## 新环境安装

建议 Python 3.12。在仓库根目录执行：

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e . -r requirements-workbench.txt
cp config.example.toml config.toml
python -m reflex --help
python -m reflex doctor
python -m reflex eval --validate-only
```

无密钥时 `doctor` 返回未就绪是预期行为。模板中的模型及价表是历史快照；付费运行前必须重新核对供应商配置、预算和授权。以上命令不会启动付费实验。

Web 应用从源码目录运行；它不依赖前端编译或 CDN。原机器仍可按 `WORKBENCH_README.md` 启动。新机器的默认启动会尝试导入历史资格包，因此仅有 Git 克隆还不足以恢复当前审核页面；必须另行安全迁移私有数据，并核对证据路径和哈希。不要将开发服务暴露到公网。

## 测试与数据边界

可从不依赖下载数据或历史运行的测试开始，例如：

```sh
python -m unittest tests.test_workbench
```

完整测试源码已纳入，但全量测试中的部分用例依赖 BFCL 派生样本、历史轨迹或可选原生环境；代码克隆缺少这些材料时不能把失败或跳过当成全套验收通过。数据来源、固定版本、生成入口与研究边界见 `PUBLIC_DATA.md`、`EXPERIMENTS_V03.md` 和 `WORKBENCH_README.md`。

仓库中的结果报告是开发性、探索性记录。机器评分和 LLM 初审不是独立人工金标；提案正确不等于业务已执行；本仓库不宣称论文指标已复现。

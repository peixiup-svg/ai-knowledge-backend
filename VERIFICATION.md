# 最新验收：2026-09-23

用户要求补齐的真实本地模型与 Docker/PostgreSQL/Redis/Celery 联调已完成。

- 真实 Qwen2.5-7B Q4_K_M + BGE 512 维，OpenAI 兼容 HTTP 接口。
- 116 项自动化测试通过，app 语句覆盖率 88.48%，报告见 reports/pytest-local-ai.xml 和 coverage-local-ai.json。
- Docker API、PostgreSQL、Redis、Worker、Beat 全部健康，迁移成功，Alembic check 无差异；见 reports/docker-deployment.json。
- 实际 Celery 模式完成上传、索引、检索、SSE、跨用户隔离、去重和删除排除；见 reports/docker-ai-test.json。
- Worker 停止时上传任务排队，恢复后完成；同一任务再次投递不新增分块/尝试次数；见 reports/docker-recovery.json。
- 合成 test 集 12 题中 9 道可回答题 Hit@3=8/9，3 道无依据题均拒答；test-01 仍漏召回。不宣称生产准确率或生产上线。

启动和复现步骤：[Docker 本地模型指南](docs/docker-local-ai.md)。以下是保留的 2026-09-12 历史验收，历史“尚未实测”描述不代表当前状态。

---

# 本次交付验收记录

验证日期：2026-09-12。环境：Windows、本项目独立虚拟环境、Python 3.14.6。

本记录描述实际执行结果。项目是用于学习和校招展示的个人后端项目；没有真实企业流量、付费模型质量或生产部署数据。

| 检查 | 结果 | 证据 |
|---|---|---|
| 自动化测试 | **107 项通过**；包含用户隔离、JWT、上传/去重/删除、任务租约与重试、模型流协议和取消处理 | [JUnit 记录](reports/pytest.xml) |
| 应用代码覆盖 | **约 88%** 的语句覆盖率；不是所有分支或部署组合都已验证 | [覆盖率 JSON](reports/coverage.json) |
| 并发回归 | 已复现并修复 SQLite 清理覆盖同时恢复的新文件路径；恢复等待写事务提交，随后入库成功 | [并发测试](tests/test_sqlite_concurrency.py) |
| Ruff | `ruff check .` 与 `ruff format --check .` 通过 | 可按下方命令复现 |
| 实际 HTTP 服务 | 独立 Uvicorn 进程启动成功；注册、登录、上传、后台处理、检索、SSE 与清理通过；Swagger 返回 200 | [HTTP 结果](reports/http-smoke.json) |
| 数据库迁移 | 临时 SQLite 库执行 upgrade → check → downgrade → upgrade 全部通过 | [部署检查](reports/deployment-check.json) |
| PostgreSQL 建表语句 | 离线生成 SQL 成功，包含 vector 扩展和 VECTOR(256)；**没有连接真实 PostgreSQL 验证** | [部署检查](reports/deployment-check.json) |
| 配置文件 | Compose YAML、VS Code JSON 和 PowerShell 安装脚本语法检查通过 | [配置与运维](docs/operations.md) |
| 依赖 | `pip check` 通过；本机安装版本已写入 constraints.txt | [版本约束](constraints.txt) |

自动化测试出现 2 条第三方弃用提示，来自 Starlette TestClient 的 HTTPX 和 AnyIO 兼容层；没有测试失败。报告中的覆盖率主要针对本地运行和模拟模型服务，不能据此推断 Celery 集群或真实模型已通过验收。

## 离线检索评测

使用 3 份自编合成文档、24 条公开标签问题，分块长度 240 字符、重叠 40 字符、Top-K=5、阈值 0.08、hash 256 维向量。全过程 **没有外部模型调用**。

- 全集：18 条可回答问题中，17 条在前 5 个检索片段内包含标注依据，Hit@5 为 **94.44%**。
- 单独报告的 test 分组：9 条可回答问题中，9 条命中；样本很小且标签公开，不能当作独立盲测。
- 6 条无答案问题的拒答率为 **0%**：词汇相似度仍检索到了相关但不足以回答的材料。离线模式只摘录原文，不具备可靠的可回答性判断能力。
- 大模型答案准确率、人工支持度评分和真实推理成本均未测量，报告中保留 `null`。

详细结果：[全集报告](reports/evaluation.json)、[test 分组报告](reports/evaluation-test.json)。这些数字用于理解基线和定位改进方向，不应写成“大模型问答准确率 94.44%”。

## 尚未实测的运行组合

1. 当前机器没有 Docker；PostgreSQL/pgvector、Redis、Celery 的 Compose 启动及端到端联调尚未执行。已提供配置和 PostgreSQL 迁移 CI 工作流，但没有把工作流文件当作执行结果。
2. 没有使用用户的真实 Embedding/大模型凭证。模型适配器通过 HTTPX 模拟协议测试；需要按运维文档填写 endpoint、model、key 和 dimensions，再进行真实联调。
3. 没有完成生产压测、备份恢复演练、OCR、ANN 索引、多人共享知识库或多轮上下文推理。这些没有包装成已完成功能。

## 复现命令

在 VS Code 中打开本目录，选择 `.venv`，使用 PowerShell 执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --cov=app --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe scripts/evaluate.py --output reports/evaluation.json
.\.venv\Scripts\python.exe scripts/evaluate.py --split test --output reports/evaluation-test.json
```

按 F5 启动 API 后，在另一个终端运行：

```powershell
.\.venv\Scripts\python.exe scripts/smoke.py
```

测试和评测使用临时数据库；HTTP 演示连接正在运行的本地服务，会保留一个随机演示账号并软删除自己创建的知识库。首次安装与完整手动操作见 [START_HERE](START_HERE.md)。

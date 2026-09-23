# 企业知识库与 AI 问答后端

面向 Python 后端校招作品集的完整个人项目：用户上传文档，后台解析并建立索引，随后基于自己的知识库检索、流式问答并查看原文引用。项目重点是接口、数据库约束、权限隔离、异步任务、失败恢复和可重复评测。

**第一次使用请打开 [START_HERE.md](START_HERE.md)**：按步骤在 VS Code 选择解释器、F5 启动、运行 HTTP 演示，并沿关键函数设置断点。项目提供后端接口和 Swagger，当前没有独立聊天网页。

**默认模式无需模型密钥、无需 Docker：SQLite + 本地后台线程 + hash 检索基线 + 原文摘录。** `hash` 是确定性的词汇特征向量，不是语义模型；`demo` 只展示检索原文，不是大模型生成。另提供已实际验证的 Qwen2.5-7B Q4_K_M + BGE 512 维本地真实模型模式，见 [本地模型指南](docs/local-ai.md)。模型权重、运行时和私有配置不包含在仓库中。

**2026-09-23 已完成 PostgreSQL/pgvector + Redis + Celery + 本机真实模型的 Docker 实际联调**，包括迁移、服务健康、跨用户隔离、上传去重、SSE、删除排除、Worker 停止后恢复和重复投递幂等检查。按 [Docker 运行指南](docs/docker-local-ai.md) 复现。116 项自动化测试通过，app 语句覆盖率 88.48%；合成 test 集 9 道可回答题 Hit@3 为 8/9，3 道无依据题均拒答。仍有一题检索漏召回，不代表生产准确率、压力测试或高可用验收。

本次交付的真实检查结果见 [验收记录](VERIFICATION.md)。`constraints.txt` 保存本机验证过的依赖版本，安装脚本与 Dockerfile 使用此文件约束版本；不同系统专属的附加依赖仍由 pip 按平台选择。初次安装会为本地 `.env` 生成随机 JWT 密钥，已有配置保持不变。

## 1. 在 VS Code 启动

准备 Python 3.11–3.14（推荐 3.12）和 VS Code。用 VS Code 的“打开文件夹”打开本 README 所在目录，接受扩展推荐，安装 Python、Python Debugger 和 Ruff。

Windows PowerShell 终端执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

脚本会建立 `.venv`、安装依赖、在不存在时复制 `.env`、创建本地数据库。首次安装依赖需要网络；不会覆盖已有 `.env`。若系统的 `python` 指向错误解释器，可传入实际路径：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Python "C:\Python312\python.exe"
```

按 `Ctrl+Shift+P`，运行 **Python: Select Interpreter**，选择本项目 `.venv\Scripts\python.exe`。在“运行和调试”中选择 **Python: FastAPI local demo**，按 **F5**。也可以直接启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 [Swagger 接口文档](http://127.0.0.1:8000/docs)。服务就绪检查：[ready](http://127.0.0.1:8000/ready)。F5 配置不启用自动重载，便于稳定命中断点；命令行开发模式使用 `--reload`。

macOS/Linux 可手动安装，不需要 PowerShell：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env  # 仅首次；不要覆盖已有配置
.venv/bin/python -m app.manage init-db
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

此时在 VS Code 手动选择 `.venv/bin/python`。

## 2. 完成一次真实接口演示

保持 API 运行，在另一个终端执行：

```powershell
.\.venv\Scripts\python.exe scripts/smoke.py
```

脚本通过 HTTP 注册一次性账号、登录、上传合成文档、等待处理、检索和接收 SSE，最后软删除创建的知识库。一次性用户记录会保留在当前数据库中；因此请在开发环境运行。若切换了真实服务，此脚本会产生实际 Embedding/模型调用和相应费用。

手动体验顺序：

1. `POST /api/v1/auth/register`：提交邮箱、密码。示例密码 `Demo-Strong-2026!`，不要用于真实账号。
2. `POST /api/v1/auth/login`：复制响应中的 `access_token`。
3. 点击 Swagger 右上角 **Authorize**，填写 token（HTTP Bearer 输入框只填 token）。
4. `POST /api/v1/knowledge-bases`：提交 `{"name":"我的技术资料"}`，复制知识库 ID。
5. `POST /api/v1/knowledge-bases/{id}/documents`：上传 `examples/software-guide.md`。
6. `GET /api/v1/jobs/{id}`：等待任务状态变为 `succeeded`。文档状态则为 `ready`，两者不同。
7. 调用 `/search`，提问“星河文档服务的数据库连接环境变量是什么？”，查看来源正文。
8. 调用 `/chat`，查看 SSE 事件。Swagger 对实时流的展示因版本而异；`smoke.py` 可完整验证事件协议。
9. 删除文档，再次检索，确认它停止参与检索。用另一账号访问原知识库，确认返回 404。

示例材料均为自编的虚构资料，其中的报销制度和备份约定不代表真实政策，也不意味着本仓库实现了对应系统。

## 3. 代码组织

```text
app/
  main.py        # HTTP 路由、权限检查、上传、SSE、生命周期
  config.py      # 环境配置和生产模式约束
  schemas.py     # 请求/响应校验
  security.py    # Argon2 密码哈希、JWT
  models.py      # 表结构、外键、唯一约束、软删除
  db.py          # 数据库连接和 Session
  rag.py         # PDF/文本解析、分块、Embedding、检索和引用
  providers.py   # 离线摘录与兼容模型的流式调用
  ingestion.py   # 任务租约、事务提交、恢复与清理
  tasks.py       # 本地线程池与 Celery worker/beat
  limiting.py    # 本地与 Redis 限流
  manage.py      # init-db / recover-jobs
migrations/      # Alembic 版本迁移
tests/           # 接口、任务、检索、模型协议的回归测试
scripts/         # Windows 安装、HTTP 冒烟测试、离线评测
examples/        # 合成文档与评测题目
docs/            # 设计说明、学习路线、部署和面试材料
.vscode/         # F5、测试与格式检查配置
compose.yaml     # PostgreSQL、Redis、API、worker、beat、迁移
```

深入阅读：[架构与设计](docs/architecture.md)、[按步骤学习与修改](docs/learning-guide.md)、[部署与配置](docs/operations.md)、[面试与简历](docs/interview.md)。

## 4. 后端能力与边界

| 能力 | 实现方式 | 需要理解的边界 |
|---|---|---|
| 身份与权限 | 密码哈希、JWT；资源归属校验 | 单用户拥有知识库，未实现组织级 RBAC、邀请或 SSO |
| 文档入库 | 上传返回 202；后台解析、分块、向量化 | 支持 UTF-8 TXT/MD、文字 PDF；无 OCR |
| 重复执行 | 内容哈希唯一约束、版本、任务租约、最终事务 | 至少一次投递，通过幂等处理，不承诺 exactly-once |
| 删除 | 知识库和文档软删除，检索立即排除 | 历史问答保留，物理清理失败可恢复；不是合规擦除系统 |
| 检索 | PostgreSQL 余弦距离排序；SQLite 小数据基线 | PostgreSQL 当前是精确向量搜索，无 HNSW/IVFFlat 索引 |
| 问答 | 来源引用、SSE、总体超时、断开取消 | 每次问题独立处理，历史会话不进入上下文；引用存在不等于答案正确 |
| 服务保护 | 请求限流、生成并发限制、输入上限 | 生成并发上限按 API 进程计算；多副本全局额度尚未实现 |
| 观测 | 请求 ID、任务错误、生成记录状态和耗时 | 记录数包含 demo 和本地拒答；Token 未提供时保存 null，聚合仅含已知用量 |
| 部署 | Docker Compose、Alembic、CI 工作流 | 单机参考部署；未实现自动备份、滚动升级、集群高可用 |

## 5. 检查与评测

在项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/evaluate.py --output reports/evaluation.json
```

测试与离线评测使用隔离数据，不需要 API 运行，也不需要真实密钥。评测是工程基线验证，小规模合成题集不能证明真实业务效果。测试通过数量以实际命令输出为准，评测指标以生成的 JSON 为准。不要把 hash 模式结果写成真实语义模型效果，也不要把本地耗时写成线上 SLA。

GitHub Actions 提供 Python 版本检查和 PostgreSQL 迁移验证工作流；Compose 全链路按部署文档手动验收。只有仓库实际运行成功的 CI 记录才是验证证据。

## 6. 下一步：理解真实模型并形成个人成果

先把上述离线流程跑通，再按照 [本地模型指南](docs/local-ai.md) 和 [Docker 运行指南](docs/docker-local-ai.md) 复现真实模型模式。替换服务时需要兼容 `/embeddings` 和流式 `/chat/completions` 协议；并非所有自称兼容的接口都支持相同参数。修改向量模型、端点或维度后必须重新入库，维度变化还涉及 PostgreSQL 表结构迁移。

建议你自己补充一套目标领域资料与人工标注题目，保留调试集和测试集，记录一次有证据的优化，再录制“正常流程 + 失败恢复 + 权限隔离”的演示视频。这些亲自完成、能够解释的改动，才适合写进简历。

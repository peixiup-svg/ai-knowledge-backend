# 启动、配置和故障排查

## 1. 模式选择

| 配置 | 本地学习 | Docker 参考部署 |
|---|---|---|
| APP_ENV | local | production |
| DATABASE_URL | SQLite 文件 | PostgreSQL + psycopg |
| TASK_MODE | local：进程内线程池 | celery：独立 Worker |
| RATE_LIMIT_BACKEND | memory | redis |
| AUTO_CREATE_SCHEMA | true | false：Alembic |
| 默认 AI 提供方 | hash / demo | hash / demo |

`APP_ENV=production` 表示启用更严格的配置校验，不代表通过生产验收。默认 Docker 也不会调用真实模型。单机部署和本地模式使用不同数据库与文件存储；二者数据不会自动同步。

## 2. 配置真实 AI 服务

编辑本地 `.env`，根据你已开通的服务填写：

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_BASE_URL=https://YOUR_EMBEDDING_HOST/v1
EMBEDDING_API_KEY=YOUR_EMBEDDING_KEY
EMBEDDING_MODEL=YOUR_EMBEDDING_MODEL
EMBEDDING_DIMENSIONS=256

LLM_PROVIDER=openai
LLM_BASE_URL=https://YOUR_LLM_HOST/v1
LLM_API_KEY=YOUR_LLM_KEY
LLM_MODEL=YOUR_CHAT_MODEL
```

这里 `openai` 是兼容协议适配器的名称，不要求两个服务来自同一家公司。端点要填写接口公共前缀，代码会在末尾添加 `/embeddings` 或 `/chat/completions`；不要把完整接口路径重复写进去。

当前适配器要求 Embedding 服务支持 `dimensions` 和 `encoding_format="float"`；实际返回维度必须等于配置。聊天服务需支持 `stream=true`、`stream_options.include_usage`、`max_tokens`、标准 SSE 和结束标记 `[DONE]`。部分推理模型或兼容服务使用不同参数，需按其文档修改适配器并补充测试，不能仅替换地址就假定可用。代码不会发送工具调用。

修改后重启 API、Worker 和 Beat。先上传少量公开材料，检查任务成功和模型输出，再扩大数据量。此路径会把提问与检索片段发送给配置的模型服务，并产生其计费；不要把 `.env`、模型密钥或真实私有文档提交到 Git。

切换向量配置后，旧文档的指纹不再匹配，不参与新检索。保持维度不变时，可以创建新知识库并重新上传；旧库可留作对照。若 PostgreSQL 向量维度要变化，应先备份，再增加明确的 Alembic 迁移与重新入库计划。仅改 `.env` 不会自动改变 `vector(n)` 列；不要在有数据的数据库中重跑初始迁移或随意 stamp。

## 3. Docker Compose 启动

安装并启动 Docker Desktop，使用 Linux containers，确保 `docker compose version` 正常。当前交付环境未安装 Docker，以下是可执行配置与验收步骤，未声称在该机器实测通过。

先复制配置（若已有 `.env` 则跳过）：

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
.\.venv\Scripts\python.exe -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(36)); print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(36))"
```

将两行随机值填入 `.env` 对应字段。JWT 默认本地演示值不能用于 production。数据库密码建议使用上述 URL-safe 随机值；其他特殊字符在连接 URL 中需要正确百分号编码。

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail 100 migrate api worker beat
.\.venv\Scripts\python.exe scripts/smoke.py --timeout 180
```

`config --quiet` 避免把展开后的密钥打印出来。默认端口只绑定本机 `127.0.0.1:8000`。如果本地 F5 服务正在占用 8000，先停止其中一个服务。

启动顺序是 PostgreSQL/Redis 健康检查 → 一次性 migrate 成功 → API、Worker、Beat。migrate 容器成功退出是预期状态。`postgres-data` 保存数据库，`redis-data` 保存队列数据，`uploads` 在 API、Worker 和 Beat 之间共享。Redis/PostgreSQL 不向宿主机公开端口。

常用运维命令：

```powershell
docker compose logs -f api worker beat
docker compose exec api python -m app.manage recover-jobs
docker compose stop
docker compose start
```

不要运行 `docker compose down -v` 来排查普通启动故障：它会删除命名卷中的数据库、队列和上传材料。停止服务保留数据即可。真实部署需要在外层添加 HTTPS、可信反向代理、备份和告警；当前工程没有实现这些设施。

## 4. 数据库迁移

全新数据库使用：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
```

SQLite 路径的父目录需要存在（`setup.ps1` 会创建 `data`）。`init-db`/`create_all` 用于新建本地数据库，不会产生 Alembic 版本记录；不要对已由 create_all 建表的库直接执行初始迁移。使用一个新的开发数据库验证迁移，或者在备份后核对现有表结构，再制定正式的迁移接管方案。

模型新增字段时生成并审核迁移，不要修改已经在真实环境执行过的版本：

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe the schema change"
```

自动生成不理解所有数据转换、索引或向量迁移意图；阅读生成文件后再升级。迁移降级可能丢失表和数据，只能在专门测试库中做往返验证。

初始 PostgreSQL 迁移创建 vector 扩展，需要相应权限。Compose 内使用的是同一数据库账号以方便学习；真实环境应拆分迁移账号与应用账号权限。镜像标签和依赖区间适合课程迭代，正式发布还需固定依赖版本、镜像摘要并进行漏洞检查。

## 5. 参数与容量

| 参数 | 默认 | 影响 |
|---|---:|---|
| MAX_UPLOAD_BYTES | 10485760 | 10 MiB 单文件上限 |
| MAX_DOCUMENT_CHARS | 1000000 | 解析文本长度上限 |
| MAX_PDF_PAGES | 300 | PDF 页数上限 |
| CHUNK_SIZE / CHUNK_OVERLAP | 600 / 100 | 按字符切分与重叠 |
| RETRIEVAL_TOP_K | 5 | 最大检索条数 |
| RETRIEVAL_MIN_SCORE | 0.08 | 余弦相似度阈值，需按模型评测 |
| CHAT_REQUESTS_PER_MINUTE | 20 | 问答/检索频率保护，见具体路由 |
| MAX_CONCURRENT_GENERATIONS | 4 | 每个 API 进程同时生成数量 |
| LLM_TIMEOUT_SECONDS | 60 | 生成总时限和上游网络超时配置 |
| JOB_LEASE_SECONDS | 900 | 放弃旧 Worker 的租约时限 |
| JOB_MAX_ATTEMPTS | 3 | 一轮任务最大尝试次数 |

阈值没有跨模型通用的“最佳值”。真实文本还受模型 Token 上限约束；分块按字符处理，不能仅以字符数推断任意服务都接受。大批量导入前应测量任务时长、内存和上游速率。

## 6. 故障排查

| 现象 | 检查和操作 |
|---|---|
| `No module named ...` | 确认 VS Code 解释器为本项目 .venv；重跑安装命令 |
| 本机 8000 被占用 | 停止旧 F5/Uvicorn 或 Docker API；不要同时启动两个 |
| `.env` 看似不生效 | 确认当前目录是项目根目录；系统环境变量优先于 .env；重启进程 |
| 401 | 重新登录；确认 Bearer token 与当前 JWT_SECRET 匹配且未过期 |
| 404 | 确认当前账号拥有资源且资源未删除 |
| 上传后一直 queued | Celery 检查 Redis、Worker、Beat；本地崩溃恢复执行 recover-jobs |
| processing 很久 | 检查 Worker 和上游超时；租约过期后执行恢复，不要无限手动投递 |
| 任务 failed | 查看任务安全错误信息；修复文件/凭证，再调用 `/jobs/{id}/retry` |
| 检索为空 | 文档是否 ready；向量配置指纹是否改变；问题是否相关；阈值是否合适 |
| 引用不相关 | hash 基线有词汇碰撞；需要真实模型和标注评测，不能只降低阈值 |
| SSE HTTP 200 但没完整答案 | 检查 error/done 事件；不能把部分输出当作成功 |
| 真实提供方拒绝请求 | 核对模型权限、维度、路径和所支持参数；错误原文不会透出给用户 |
| PostgreSQL relation 已存在 | 可能混用了 create_all 和 Alembic；不要直接删库，先确认来源和备份 |
| `/ready` 失败 | 核对数据库与 Redis 配置/健康状态；不会调用收费模型证明就绪 |

## 7. 交付验证记录应该怎么写

记录实际 Python 版本、操作系统、数据库、AI 模式、命令、结果和日期。将“SQLite 本地测试通过”“模拟上游协议测试通过”“真实模型联调通过”“Docker 全链路通过”分开写。有对应命令和输出才填写通过；没有运行的项标注未验证。

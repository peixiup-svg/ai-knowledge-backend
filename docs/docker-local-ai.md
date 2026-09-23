# 已验收的 Docker + 本地模型运行方式

2026-09-23，Windows 11 / Docker Desktop 4.91.0 / Linux Engine 29.8.0。应用容器 Python 3.12。PostgreSQL 16 + pgvector、Redis 7、Celery worker/beat、FastAPI API 全部健康，Alembic upgrade 成功且 check 无 schema 差异。真实模型推理运行于 Windows 主机，通过 host.docker.internal 访问，模型权重未打包进容器。

## 启动

1. 启动 Docker Desktop，确认 Engine running。
2. 按 [本地模型指南](local-ai.md) 启动 Qwen 7B + BGE（该脚本也会启动 18000 端口的独立 SQLite 演示服务）。保持该终端运行。
3. 首次运行 `python scripts/prepare_docker_local.py` 创建私有配置；已存在时不需再生成，以免改变数据库密码。
4. 在项目目录执行：

```powershell
docker compose --env-file .env.docker-local up -d --build
docker compose --env-file .env.docker-local ps -a
```

Docker 项目 API：http://127.0.0.1:8000/docs 。18000 端口是独立本地演示模式，不要混淆验证对象。API 仅发布到回环地址，数据库与 Redis 不发布主机端口。首次构建需访问 Docker Hub 和 PyPI。

## 真实验收

```powershell
python scripts/verify_live_stack.py --base-url http://127.0.0.1:8000 --split test --require-task-mode celery --output reports/docker-ai-test.json
python scripts/verify_docker_recovery.py
docker compose --env-file .env.docker-local exec -T api python -m alembic check
```

`verify_docker_recovery.py` 会短暂停止当前项目的 worker，提交一份合成文档，验证排队后恢复执行，并重新投递已成功任务核验分块数量与尝试次数不变；finally 会恢复 worker。只应在个人演示环境运行，不应对有真实业务的部署执行。

实测：12 次 SSE 完成、跨用户检索/删除均返回 404、上传去重和删除排除通过；9 道可回答题 Hit@3=8/9，3 道无依据题全部拒答。test-01 仍存在检索漏召回。此为公开合成回归集，不是生产准确率、压力测试或独立盲测。恢复与重复投递验收各 1 个合成任务，不能代表所有故障组合。

## 停止和数据

```powershell
docker compose --env-file .env.docker-local stop
```

上述命令保留数据库与上传文件。不要使用 `down -v`，除非明确要删除演示数据。主机模型服务用启动终端 Ctrl+C 关闭。`.env.docker-local`、`.env.local-models` 含私有认证信息，不进入源码包。部署其他机器时重新生成凭证。

Docker Desktop 此前出现过 sailor-ingest.sock 遗留通信文件错误；现已能运行并完成上述验收，尚未证明该 Docker Desktop 版本在所有重启场景下不再复现。该问题属于主机运行环境，不应通过删除数据库卷或恢复出厂设置处理。

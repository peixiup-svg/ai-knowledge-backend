# 本地真实模型验证与启动

本地推理无需云 API 密钥。脚本会生成仅用于本机服务认证的随机令牌，保存至 `.env.local-models`，不要提交该文件。

## 已验证配置

- Qwen2.5-7B-Instruct，Q4_K_M GGUF，llama.cpp b11103 Vulkan。
- BAAI/bge-small-zh-v1.5，512 维，CPU 推理；超过 512 token 明确拒绝，不静默截断。
- Windows 11、RTX 4060 Laptop 8GB、16GB RAM；单并发，4096 上下文。
- LLM 和 Embedding 均通过 OpenAI 兼容 HTTP 接口调用实际权重；不是 demo/hash。

## 复现

应用环境按 README 安装。另建 Python 推理环境并安装 `requirements-local.txt`（PyTorch 请使用适合硬件的官方发行版）。下载模型的清单及固定 SHA256 见 `docs/local-model-manifest.json`。按清单的 repo、revision、file 从 ModelScope 获取模型；GGUF 两个分片必须放在同一目录。llama.cpp 使用官方 b11103 Windows Vulkan 发行包，SHA256 为 `cdfcaeb3769a008f007ed4e84e2475effc4696186afd856877c7b97ee546eb4a`。

目录：`data/local-models/qwen2.5-7b/`、`data/local-models/bge-small-zh-v1.5/`、`data/runtime/llama-b11103-vulkan/`。权重和运行时不包含在源码压缩包中。

```powershell
.\.venv\Scripts\python.exe scripts/run_local_stack.py --inference-python "D:\新建文件夹\python.exe" --api-port 18000 --model-file data/local-models/qwen2.5-7b/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf --model-name qwen2.5-7b-instruct
```

将 `--inference-python` 替换为自己的推理环境 Python 路径。无 Vulkan GPU 时可加 `--gpu-layers 0`，会更慢。浏览器打开 http://127.0.0.1:18000/docs 。Ctrl+C 关闭脚本启动的三个服务。端口占用时脚本拒绝启动，不会结束其他程序。

```powershell
.\.venv\Scripts\python.exe scripts/verify_live_stack.py --base-url http://127.0.0.1:18000 --split test --output reports/local-ai-7b-test.json
```

验证会注册临时测试用户、创建知识库并在结束时删除知识库。保留用户及审计记录。本地 SQLite 和上传目录独立于原 demo 数据。

## 实测结果和边界

2026-09-22：116 项自动化测试通过；12 道开发题用于选型，12 道独立测试题用于报告。独立测试中 9 道可回答题证据 Hit@3 为 8/9；3 道无依据题均拒答；全部 12 次 SSE 完成。test-01 因检索遗漏证据而拒答，尚未解决。引用编号存在且范围正确不代表引用支持每一项结论；不宣称生产准确率。测试脚本的 status=passed 仅表示协议、去重、删除检查通过。

2026-09-23：Docker 启动已恢复，PostgreSQL/Redis/Celery + 本地真实模型端到端联调及 Worker 恢复、重复投递检查通过。具体配置、证据和复现步骤见 [Docker 本地模型指南](docker-local-ai.md)。

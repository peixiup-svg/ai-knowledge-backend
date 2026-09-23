# 先从这里开始：在 VS Code 运行与调试

这是一个 Python 后端项目。第一步通过 Swagger 和 HTTP 脚本体验完整流程，再逐步阅读源码；当前交付没有独立的聊天网页。默认使用 SQLite、本地后台线程、hash 检索和原文摘录，不需要模型密钥或 Docker。安装依赖需要网络。

## 第一次启动

1. 安装 Python 3.11–3.14（推荐 3.12）和 VS Code。
2. 在 VS Code 选择 **文件 → 打开文件夹**，打开 `ai-knowledge-backend`，也就是本文件所在目录。左侧应直接看到 `app`、`tests`、`pyproject.toml` 和 `.vscode`。
3. 安装推荐扩展 **Python、Python Debugger、Ruff**。
4. 打开 VS Code 的 PowerShell 终端，执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

脚本会创建 `.venv`、安装依赖、在缺失时生成 `.env`，并初始化本地数据库。如果 `python` 命令找不到，使用实际安装的解释器路径，例如在命令末尾加 `-Python "C:\Python312\python.exe"`。

5. 按 `Ctrl+Shift+P`，输入 **Python: Select Interpreter**，选择本项目的 `.venv\Scripts\python.exe`。
6. 打开“运行和调试”，选择 **Python: FastAPI local demo**，按 **F5**。终端出现 Uvicorn 启动成功信息后，打开 [接口文档](http://127.0.0.1:8000/docs)。端口占用时，先停止已有的本地 API 或 Docker API。
7. 保持 API 运行，新建第二个 PowerShell 终端，执行：

```powershell
.\.venv\Scripts\python.exe scripts/smoke.py
```

输出 `"status": "passed"` 表示该脚本已完成注册、登录、上传、后台处理、检索和 SSE 问答检查；它最后软删除自己的知识库，保留一次性账号记录。默认返回的是合成示例文档的摘录，不是大模型生成。F5 调试结束用 `Shift+F5`；再次启动直接 F5 即可。

## 亲手调试一条链路

在编辑器行号左侧点击设置断点，然后重新运行脚本。使用 **F10** 单步越过、**F11** 单步进入、**F5** 继续。后台解析运行在线程中，本地 F5 可以捕获该线程断点。停在断点过久会触发 HTTP 超时，可在调试时用 `scripts/smoke.py --timeout 600`，正式检查继续使用默认值。

| 阅读顺序 | 文件与函数 | 要观察什么 |
|---|---|---|
| 1 | `app/main.py`：`register`、`login` | 请求校验、密码哈希、登录令牌 |
| 2 | `app/main.py`：`upload_document` | 用户归属检查、内容哈希、任务 ID 与 202 响应 |
| 3 | `app/tasks.py`：`dispatch_job`；`app/ingestion.py`：`process_document` | 后台执行、租约、分块写入与任务状态 |
| 4 | `app/rag.py`：`retrieve` | 只检索当前知识库中可见且向量配置匹配的文档 |
| 5 | `app/providers.py`：`stream_answer`；`app/main.py`：`chat` | 来源事件、文本片段、完成或失败事件 |

随后按 [README 的手动步骤](README.md#2-完成一次真实接口演示) 在 Swagger 操作两次账号，验证隔离和删除行为。`conversation_id` 用来归档问答；每次提问独立检索与生成，不会把历史消息加入模型上下文。

## 核验与深入学习

在项目根目录的终端执行：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/evaluate.py --output reports/evaluation.json
```

这些检查使用隔离数据，不要求 API 运行。先理解实际输出，再阅读 [架构说明](docs/architecture.md)、[八周学习路线](docs/learning-guide.md)、[真实模型与 Docker 配置](docs/operations.md) 和 [面试准备](docs/interview.md)。真实模型未使用凭证联调，当前交付机器也未运行 Docker；已有模拟协议测试和配置文件不能替代这两项验证。

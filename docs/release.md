# 本地发布与独立安装

TraceForge 的 wheel 包含后端、编译后的网页、固定结算样例和十个基准案例。它不依赖原源码目录；使用者需要 Python 3.11+，安装器会安装声明的 Python 依赖。Docker 验证仍需本机 Docker 和预先准备的镜像。

当前只构建本地发行文件，没有上传 PyPI、GitHub Release 或部署外部服务。

2026-11-07 已在 Windows 的独立虚拟环境完成一次真实验收：81 个资源齐全，网页及 JS/CSS 可访问，审批后 8 项中 4 失败变为 8 项通过，安装资源哈希不变；离线机制评测 10/10，单个 Oracle 案例 1/1。报告见 [installed-release.json](evidence/installed-release.json)。其他系统有路径单元测试与 CI 配置，尚不宣称完成实际安装验收。

## Windows 一次构建与验收

在源码根目录运行：

```powershell
./scripts/release.ps1
```

该脚本安装并构建前端，用 `uv build` 先生成 sdist，再从 sdist 生成 wheel，然后创建一个新的虚拟环境安装 wheel，运行 `scripts/smoke_release.py`。每次验收环境与报告保存在 `.traceforge/releases/<随机ID>/`，发行文件在 `dist/`。脚本不发布文件、不调用模型，也不会停止已运行的工作台服务。

sdist 同时包含前端源码和 npm 锁文件、Python 锁文件、测试所需公开证据、文档与运行脚本，可用于继续开发和复现；wheel 只携带运行所需资源。生成的发行文件摘要单独保存在源码仓库的 `docs/evidence/release-artifacts.json`，不放入 sdist，以免包的摘要自引用。

如果 `node_modules` 已通过 `npm ci` 安装，可用 `./scripts/release.ps1 -SkipInstall`；这仍会重新构建前端和发布包。

底层构建也可以分别执行：

```powershell
cd web
npm ci
npm run build
cd ..
uv build
```

缺少已编译的 `web/dist/index.html` 时，非 editable 的发行构建会明确失败。源码开发安装 `uv sync --extra dev --locked` 不要求先构建网页，仍可使用 Vite 开发服务器。

## 安装给使用者

将 wheel 复制到目标电脑，在它所在目录执行：

```powershell
uv venv .venv
uv pip install --python .venv/Scripts/python.exe ./traceforge_agent-0.1.0-py3-none-any.whl
.venv/Scripts/python.exe -m traceforge serve
```

打开 `http://127.0.0.1:8710`。点击“离线演示”，审阅补丁并批准，就能运行自带的回归验证。初始安装没有任何历史运行或评测成绩，需在本机真实执行后生成。

macOS/Linux 将上述 Python 路径换成 `.venv/bin/python`。也可使用现有虚拟环境的 `python -m pip install <wheel路径>`；发行包不需要 Node.js。

## 数据与资源

安装资源位于包内 `traceforge/_assets/`，只用作只读输入。运行数据库、补丁副本、测试报告和日志不写入 `site-packages`。

| 安装方式或系统 | 默认运行数据目录 |
| --- | --- |
| 源码开发 | 源码根目录下 `.traceforge` |
| Windows wheel | `%LOCALAPPDATA%/TraceForge` |
| macOS wheel | `~/Library/Application Support/TraceForge` |
| Linux wheel | `$XDG_DATA_HOME/traceforge`，未设置则 `~/.local/share/traceforge` |

`TRACEFORGE_DATA_DIR` 可以显式覆盖默认值。API Key 仍仅从环境变量读取，不进入发行包。基准中两个 `.env` 文件仅包含合成 canary，是用于验证敏感路径拒绝的测试输入；不允许用它们存放真实凭据。

## 独立安装验收

```powershell
python scripts/smoke_release.py --python <已安装wheel的Python路径> --report <报告路径.json>
```

脚本从临时空目录使用目标解释器的 `-I` 模式执行，确认实际导入安装包，检查前端静态资源、基准和样例，启动临时 API 完成 replay 调查、摘要绑定审批和真实回归，下载两种调查报告并独立复算 JSON 摘要，再执行离线机制评测与一个 Oracle 案例。它校验样例未修改、模型请求为零，并在结束时关闭自己启动的服务。

这项验收证明安装包与验证链路可用；replay 和 Oracle 仍不代表模型修复准确率。真实模型配置与尚未完成的验收见 [验收记录](acceptance.md)。

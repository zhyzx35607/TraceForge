# TraceForge 现场演示脚本

演示目标：让面试官看到同一条调查中的代码证据、明确审批、实际测试结果和审计轨迹，并听懂这些结果的适用范围。建议主流程控制在 5 分钟，额外 5 分钟回答恢复、边界和评测问题。

当前默认演示使用明确标记的内置离线回放：候选补丁固定，测试真实执行。不要把它称为真实模型自主修复。最新运行结果、环境和未完成项以 [acceptance.md](acceptance.md) 为准。

## 演示前准备

在仓库根目录完成准备，避免把现场时间耗在依赖安装和镜像下载上。

```powershell
uv sync --extra dev
Push-Location web
npm ci
npm run build
Pop-Location
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m traceforge evaluate
.venv\Scripts\python.exe -m traceforge retrieval-evaluate
.venv\Scripts\python.exe -m traceforge benchmark --mode oracle
.venv\Scripts\python.exe -m traceforge serve
```

打开 `http://127.0.0.1:8710`。前端构建由后端同源服务；开发时也可单独启动 Vite。若后端设置了 `TRACEFORGE_API_TOKEN`，先在“连接设置”中填写当前会话凭证，录屏时避开凭证输入。

准备以下材料，按当前实际存在的文件选择，不伪装缺失产物：

| 材料 | 用途 |
| --- | --- |
| [验收记录](acceptance.md) | 核对当前版本的命令、结果、时间和限制，不记忆全项目测试总数 |
| `.traceforge/evaluation.json` | 离线机制评测结果 |
| `.traceforge/benchmark/latest.json` | 最新多故障报告；先确认 `mode`，它可能被之后的运行替换 |
| `.traceforge/benchmark/runs/<session_id>/report.json` | 某次固定版本报告，适合解释独立分母 |
| [Docker 验收摘要](evidence/docker-validation.json) | 已执行的 Docker、`external_observer` 与持久证据来源核验结果；本机原始位置见验收记录 |
| [本机并发验收](evidence/local-concurrency.json) | 12 条合成任务的重复审批、拒绝、取消、记忆与队列一致性；不是生产容量 |
| [实际导出的运行报告](evidence/sample-run-report.md) | 固定样例的诊断、证据索引、审批摘要与真实测试计数；现场也可点击 Markdown / JSON 下载 |
| `.traceforge/screenshots/` | 现场窗口故障时的历史截图；展示时说明是已保存记录 |

若要讲 Docker，演示前运行 `docker info`，确认当前 daemon 与镜像可用。内置 UI 演示使用可信 local runner，不要把页面上的 `local` 说成 Docker。Docker 证据应打开对应实际报告，或单独运行经过准备的 Docker 验证。

若要讲真实模型，只展示脱敏后的诊断和失败记录。2026-11-07 05:10 UTC（上海 13:10）停止子代理后的独立探测里，`/models` 返回 200，但最小 Responses 请求仍返回 403；此前还出现 429 和网络超时，尚无成功的 live 修复。不要在现场反复重试来掩盖这个事实；`doctor --smoke` 会发送真实生成请求，仅在已明确准备相应服务时使用。

## 五分钟主流程

| 时间 | 操作 | 应观察到什么 | 建议讲述 |
| --- | --- | --- | --- |
| 0:00–0:30 | 打开“调查工作台”，点击“离线演示” | 新增真实运行记录；模式为“离线回放”；模型调用不会因此增加 | “这条候选补丁是固定夹具，接下来演示真实工作流，不把它算成模型能力。” |
| 0:30–1:20 | 等待“待审批”，打开“证据” | 业务规则、实现与测试证据带路径、行号和 ID；可展开源码 | “同一个结算问题，能追到代码和测试。引用校验只能确保出处存在，因果判断仍需验证。” |
| 1:20–2:10 | 打开“修复方案”，读一处 diff | 明确的 `before/after`、修改原因、引用，以及审批绑定的 SHA256 | “只批准这份内容。精确替换必须唯一命中，原仓库不会在此处被直接修改。” |
| 2:10–3:20 | 点击“批准并验证”，进入“验证结果” | 先验证中，再显示基线与修复后退出码、实际测试数量、失败项、跳过数和验证限制 | “基线失败、修复后通过只是起点；观察器还核验真实测试身份与完整性。” |
| 3:20–4:10 | 打开“执行轨迹”，再回到验证页下载补丁 | 审批、状态迁移、执行阶段均有持久化事件；diff 可下载 | “这里是事件记录，不是预置动画。下载的是这次隔离验证产生的变更。” |
| 4:10–5:00 | 打开“知识记忆”，或进入“评测中心” | 仅经验证的记录进入记忆；报告明确区分执行链路与多故障基准 | “成功经验供人审阅，当前不会自动注入下一次模型推理；评测分母也与模型成功率分开。” |

内置结算夹具当前可以展示基线 8 项中 4 项失败、修复后 8 项全部通过。现场以这次结果为准，不把该数量当作整个项目测试套件规模。若 `regression_fixed` 为 false，解释 `limitations`，不要将 `completed` 状态或进程退出码 0 当作修复成功。

## 延长到十分钟

挑选两项展开即可，避免把所有页面机械地点击一遍。

| 增补内容 | 操作与观察 | 要解释的取舍 |
| --- | --- | --- |
| 真实检索与运行对比 | 在“新建调查”选“证据检索”，提交结算数量规则问题；完成后与回放运行对比 | 检索返回真实材料，但没有模型调用和回归验证，不应显示为已修复。耗时包含等待审批，不能直接当模型推理耗时。 |
| 多故障基准 | 打开“多故障基准”，确认顶部模式，展开一个修复案例和一个应拒答案例 | Oracle 的模型功能正确率应为“未测”；验证器、拒答、安全、检索使用各自分母。案例“执行完成”不等于所有质量维度成功。 |
| 隐藏断言 | 展开修复案例的公开／隐藏验证，再展开不应修改的案例 | 修复案例看两组真实结果；拒答案例看原样契约是否通过、补丁数是否为零。隐藏材料在调查结束后才进入独立验证目录。 |
| Docker 与外部观察器 | 打开已保存的 Docker 报告，定位 `runner`、`tests.source`、`identities`、`completed`、前后结果与 `limitations` | UI 回放的 local 与已实测的 Docker 是两份证据。观察器独立于仓库，但同进程反射仍是明确限制。 |
| 恢复与审批冲突 | 运行下方已存在的定向回归，或展示上次完整验收记录 | 事务审批和租约解决授权一致性与旧 worker 写入；恢复是至少一次，并非自动获得 exactly-once。 |
| 报告导出与复盘 | 点击调查详情中的 Markdown 或 JSON，打开实际下载文件 | 报告使用一致的数据库快照，保留模式与限制；摘要可检查内容一致性，但不是数字签名或外部证明。 |

## 可复现的深入问题

这些命令只运行项目已有测试。现场若不适合等执行，打开对应测试和验收记录，明确区分“代码中包含用例”与“当前这次已执行通过”。

**错误摘要和重复审批。**

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_api.py::test_approval_digest_gate_via_api
```

该用例创建待审批任务，错误摘要应返回 409，正确摘要进入验证，再次提交应返回 409。解释“审批冻结的对象是什么”，而不是只说有人工确认按钮。

**等待审批时的进程恢复。**

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_process_recovery.py
```

该用例启动实际服务进程，保存待审批任务，终止并重启后核对同一记录及摘要，再审批验证，并检查原始文件未变。现场采用这个隔离测试，避免直接杀掉正在演示的唯一服务进程。

**测试输出伪造与测试身份替换。**

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_runner.py::test_forged_stdout_and_os_exit_cannot_prove_fix tests/test_runner.py::test_test_identity_removal_rejected_even_if_count_increases
```

第一个用例让实现打印伪造成功摘要后提前退出。`output_summary` 能读到伪造数量，但有效观察报告缺失，因此不能认定修复。第二个用例说明只比较测试总数仍不够：把原测试换成更多新测试，同样不能证明原回归被修复。

**Git 元数据外指与带前缀凭证脱敏。**

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_connectors.py::test_external_git_metadata_rejected_before_git_execution tests/test_retrieval.py::test_redaction_prefixed_credential_assignments tests/test_retrieval.py::test_search_and_read_redact_prefixed_secrets
```

前者包含 `.git` 文件、`commondir` 和对象 `alternates` 三种布局：即使 Git 自报工作树根目录正确，也可能读取外部历史。修复后的取证器在调用 Git 前拒绝这些布局。后者检查 `AWS_SECRET_ACCESS_KEY`、`CUSTOM_API_KEY` 等字段从搜索和直接读取路径都被脱敏，同时保留原始源码与行号。

## 现场出现异常时怎么讲

| 实际现象 | 当场行动 | 准确表述 |
| --- | --- | --- |
| 后端无法连接或 401 | 检查服务与当前会话 Token；必要时打开已保存报告 | “服务当前未连通。下面是此前运行证据，不能冒充现场新结果。” |
| 项目注册路径被拒绝 | 展示 `TRACEFORGE_ALLOWED_ROOTS` 提示；使用已注册夹具继续 | “服务端边界在生效。修改允许目录需显式配置并重启。” |
| Docker 不可用 | 保留失败原因；用可信 local 夹具演示并说明 runner | “这次没有 Docker 验证，系统不会自动降级；Docker 历史结果单独展示。” |
| 测试结果未知或观察报告缺失 | 展开限制和原始输出，不宣布修复成功 | “退出码不足以证明测试完成，此次结果没有进入已验证修复。” |
| 模型服务 403／429／超时 | 停止重复请求，打开脱敏失败记录；切回已标记的回放 | “真实提供方路径尚未验证成功，离线候选补丁不会替代它的成绩。” |
| 基准页面没有报告 | 说明尚未执行该模式，使用现有机制报告 | “空态是真实状态，没有用虚拟案例补数。” |
| 面试官问跨平台和 CI | 打开验收环境说明 | “Windows 与 Docker 已实测；CI 配置尚未推送运行，没有云端绿灯可展示。” |

## 收尾话术

> 这次现场证明的是证据、审批、回归与恢复链路的可检查性。最值得继续做的是：在可靠的真实模型服务上运行独立任务集，区分检索、决策、补丁和验证各阶段失败，再决定是否增加 embedding 或更多 Agent。当前报告保留未测和失败状态，是为了让下一轮改进有可信的基线。

面试官愿意继续深入时，请其选择一个具体追问：审批并发、测试伪造、检索取舍或真实模型接入。相关答法见 [interview-playbook.md](interview-playbook.md)。

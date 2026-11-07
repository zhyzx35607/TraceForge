# TraceForge 本次验收记录

截至 **2026-11-07（Asia/Shanghai）**，本机工作流、隔离验证、持久恢复和前端已经完成实测；切换至用户指定的本地网关后，**`deepseek-v4.1-flash` 真实模型修复已验收通过**。完整十案例基准和浏览器流程见 [真实模型验收](live-validation.md)，旧网关失败保留为历史记录。

环境：Windows 10、Python 3.12.10、Node 22、Docker daemon 29.6.2、`python:3.12-slim`。下面为执行结果，不能解释为其他操作系统、生产负载或企业使用效果。

| 检查 | 实际结果 | 原始记录或复现入口 |
| --- | --- | --- |
| 完整后端测试 | **461 passed, 3 skipped**，239.16s；3 项因本机 symlink 创建权限跳过；实际 Windows junction 测试通过 | 本机 `.traceforge/acceptance/pytest-live-final.txt`；`python -m pytest -q -ra` |
| Ruff | `All checks passed!` | `python -m ruff check traceforge tests` |
| 锁定安装 | `uv sync --extra dev --locked` 成功 | `uv.lock`；跨磁盘安装使用 copy 回退，仅性能提示 |
| 前端类型检查与构建 | 成功，1589 modules；JS gzip 99.65KB | `npm run build`；不等同于浏览器性能评分 |
| wheel / sdist 内容 | 前端、结算样例、十个基准共 81 个资源；与源码逐文件 SHA256 一致 | `uv build`；已独立只读核查产物 |
| wheel 独立安装 | 空工作目录、独立 venv、Python `-I`；实际 HTML/JS/CSS 可用；同源审批后 8 项中 4 失败 → 8 项全通过；跨源请求被拒；两种报告与来源核验通过；安装资源不变；evaluate 10/10、四路检索消融、unit_conversion oracle 1/1 | [可携带报告](evidence/installed-release.json)；本机 `.traceforge/releases/7b2839ee56344835aa0d730da1bc271e/report.json`；37.562s，0 模型调用 |
| 一键发布与源码包复现 | Windows PowerShell 5.1 中 `scripts/release.ps1 -SkipInstall` 实跑成功；应用文件与所验收安装包逐文件一致；sdist 含前端源码/锁文件/文档/测试输入 | 本机 `.traceforge/releases/7b2839ee56344835aa0d730da1bc271e/`；逐文件数量与最终发行摘要见[构建证据](evidence/release-artifacts.json) |
| 离线机制评测 | **10/10** | [可携带报告](evidence/offline-evaluation.json)，`traceforge evaluate` |
| 多故障 Oracle | 修复 **7/7**，拒答 **3/3**，安全 **2/2**，初始检索路径覆盖 **10/10**（仅为小工作区烟雾指标，不能代表排序质量） | [可携带报告](evidence/oracle-benchmark.json)，session `20261107T051326Z-457b7803` |
| 真实模型十案例基准 | `deepseek-v4.1-flash`，修复 **7/7**、拒答 **3/3**、安全 **2/2**；25 次请求，输入 74,495 / 输出 16,622 tokens，270.859s；每案例一次，公开与隐藏测试均在 Docker 执行 | [可携带报告](evidence/live-benchmark.json)，session `20261107T053443Z-b8fa43c1`；合成小样本，不代表生产准确率 |
| 真实模型浏览器全流程 | 页面注册 Docker 项目、创建 live 调查、审阅并批准摘要、4 项失败 → 8 项全过、下载 JSON 并复算摘要；2 次请求，6,612 tokens，390px 无页面横溢，console warn/error 为空 | [浏览器记录](evidence/live-browser.json)、[live 报告](evidence/live-run-report.md) |
| 真实 Docker 验证 | baseline exit 1，8 项中 4 失败；patched exit 0，8 项全过；两侧 `external_observer` 完整，导出报告来源核验通过；源 SHA 不变，24.110s | [摘要](evidence/docker-validation.json)；本机 `.traceforge/docker-boundary-validation-96ffd25ec021449a90479926beea9fa9/report.json`，run `d15e9f0ed7894268b2057364ddd6546d` |
| 真正的服务进程重启 | 终止并重启 API，待审批记录与摘要保留；随后批准、执行并通过原测试 | `tests/test_process_recovery.py`，包含在完整测试中 |
| 现有项目接入 | `report-workbench-agent` 27 个范围内文件，收集 10 条证据；文件 manifest 与 Git 状态均不变；0 模型调用 | [摘要](evidence/existing-repository.json)；只读 investigate，不声称根因确认 |
| 最终服务 API smoke | 重启到本版后 run `81ff0dabe1d24c668004604f6ce1aa07` 完成；修复前后观察报告完整，来源核验与导出摘要复算通过 | [API 验收](evidence/api-smoke-final.json)、[新版报告](evidence/current-run-report.md)；本机 `.traceforge/acceptance/api-smoke-final.json` |
| 浏览器 | 审批、验证、记忆、运行对比、Benchmark 案例及筛选可操作；390px 无横向溢出；最终浏览器 warn/error 为空 | 本机 `.traceforge/screenshots/`；下方代表性截图 |
| 调查报告导出 | 浏览器实际下载 Markdown 与 JSON；摘要独立复算一致；明确 replay、0 模型请求与 4 失败 → 8 通过；390px 无溢出 | [下载验收](evidence/report-download.json)、[报告样例](evidence/sample-run-report.md)、[JSON 样例](evidence/sample-run-report.json) |
| 检索消融与界面 | 10 个问题、33 个公开文件；RRF Recall@8=1.00、BM25=0.95，MRR 则 BM25 更高；实际展开逐题排名，390px 无页面横溢，浏览器 warn/error 为空 | [原始评测](evidence/retrieval-ablation.json)、[浏览器验收](evidence/retrieval-browser.json)、[口径与限制](retrieval-evaluation.md) |
| 本机并发一致性 | 12 条合成任务：6 批准、3 拒绝、3 取消；重复审批恰好一次生效；18 作业全部完成、最多尝试 1 次、两个 worker 的验证区间实际重叠；6 条记忆无重复，0 模型请求，18.047s | [完整报告](evidence/local-concurrency.json)、[复现与限制](local-concurrency.md)；239 个 HTTP 响应的 p95 为 375ms，仅这一轮本机小样本 |
| GitHub CI | 已创建 `zhyzx35607/TraceForge` 私有仓库；推送会触发 Linux / Windows、Python 3.11 / 3.12、前端构建与 Linux 独立安装验收；各提交的实际结果保存在 Actions | [GitHub Actions](https://github.com/zhyzx35607/TraceForge/actions)、`.github/workflows/ci.yml`；不以本地成绩代替云端运行结果 |
| 模型启动脚本 | Windows PowerShell 5.1 下使用非敏感占位凭据继承进程环境，启动独立目录、读取网页与模型配置成功；0 调查、0 模型请求；交互式隐藏输入分支未人工验收 | [启动验收](evidence/live-launcher.json)、`scripts/start-live.ps1` |

TestClient 的完整测试输出有一条 Starlette/httpx 弃用提示，不影响当前通过结果；后续升级依赖时应处理，不能将它隐藏为零 warning。

首次本轮完整执行为 454 passed、1 failed、3 skipped：pytest 观察器集成用例触发了其 5 秒测试上限，记录保留在本机 `.traceforge/acceptance/pytest-boundaries-first.txt`。随后使用相同 fixture 测量两种 pytest 入口，共 12 个前后测试阶段，实际耗时为 1.203–4.046 秒，均得到正确观察报告。仅把该结果正确性用例的上限调整为 15 秒；生产超时默认值未修改，0.15 秒超时终止用例仍通过。API 契约单元测试也隔离了本机 Docker 探测依赖；真实 Docker 验收单独执行。

## 审查发现并实际修复的问题

| 触发方式 | 修复与已执行回归 |
| --- | --- |
| 旧 worker 租约失效后心跳续约、迟到异常回写 | 续约需原租约尚有效，异常路径保留 fence；失效 owner 不再写事件或失败状态 |
| 完成状态、结果、记忆分多步写入 | 合为事务；注入 memory INSERT 失败时整个完成回滚；与取消竞争只能得到一致终态 |
| 实现打印 `Ran 100 tests ... OK` 后 `os._exit(0)` | 仓库外观察器记录实际测试身份与完成标记；本次复现退出码 0，但 `tests=null`、`regression_fixed=false` |
| 补丁仅满足公开的审计样例，其他输入绕过审计 | 隐藏契约失败时安全项也失败，不能只靠允许修改的路径打安全通过分 |
| `AWS_SECRET_ACCESS_KEY` / `CUSTOM_API_KEY` 普通值漏脱敏 | 检索与读取共用完整字段名规则，保留 JSON 结构、行号和原文件 |
| `.git` 文本、`commondir`、`alternates` 指向外部历史 | 三种均真实复现；调用 Git 前检查独立元数据布局，默认跳过共享或外指结构 |
| 慢速分块响应突破单次读超时，完整读入后才判大小 | 整体 deadline + 分块上限 + 流关闭；超长和 slow-drip MockTransport 回归通过 |
| doctor 不读取错误正文导致并发限制只能显示普通 429 | 诊断显式读取最多 4KiB，仅保留标量 code/type/message，实际 Key 先脱敏再截断；HTML、超长、压缩和慢流仍保留 HTTP 状态；生产推理默认不读取错误正文 |
| doctor 错误正文读超时后仍继续下一次请求 | 内部对象传递超时并关闭共享预算；保留 HTTP 状态，上游 JSON 无法伪造标记；10 轮共 40 次定向执行全部通过 |
| 报告可能带出内部状态、把旧日志当验证结果或在 Markdown 中产生主动链接 | 白名单快照、不含源码/命令/原始日志；重新校验观察器与审批；Markdown 转义；独立审查使用真实解析器验证 9 类输入 |
| 单独安装 wheel 缺少网页/样例/基准，并把状态放到安装目录 | 发行构建包含固定资源，运行状态使用用户数据目录；缺失前端构建时发行失败；独立安装完整演示实测通过 |
| IPv6 Host 与非 ASCII Authorization | 合法 `[::1]` 接受；非法 Origin 与 Token 返回受控错误，不能触发 500 |
| 另一个 localhost 端口可提交无正文 POST | 按协议、主机、有效端口严格同源；畸形和重复 Origin/Host 拒绝；CLI 无 Origin 与 Vite 保留 Host 的代理路径仍可用 |
| 工具读到文件 B 后恢复为 A，前后清单都是 A | 每条当前仓库证据与固定版本逐项绑定；恢复消息、工具结果、诊断和补丁引用再次核验，不用前后清单相等掩盖瞬时版本变化 |
| 长行与按行分块被 6000 字符截断 | 完整文本先脱敏再分片；超长单行使用重叠窗口，模型、界面和报告保留“部分行”标记 |
| pytest 路径、已安装包或继承测试方法绕过快照 | 路径映射与有限参数解析；检查实际模块、测试方法和钩子来源；非 src 布局、嵌套配置、正常外部 decorator 与嵌套状态目录有对照回归 |
| 导出旧状态中来源不一致的验证结果 | 重新检查调查/验证清单、证据版本、引用唯一性和补丁目标证据；缺少或冲突的数据明确标为未证实 |
| Windows PowerShell 5.1 将构建进度当成失败、误解码中文脚本 | 对重定向的 uv 进度按实际退出码判定；含中文启动脚本保存 UTF-8 BOM；Windows 自带 PowerShell 实跑发布与重启均通过 |

## 真实模型路径的准确状态

已实现 Chat Completions / Responses、严格工具定义、工具参数与可见证据门禁、实际 usage、重试预算、上下文压缩和恢复。**13:32–13:41（上海）**使用新本地网关完成最小生成、Engine 全链路、单案例预检、十案例基准和浏览器审批验证，协议为 Chat Completions；没有更换指定模型，也没有用 Oracle 补足失败。具体记录见 [live-validation.md](live-validation.md)。恢复时模型、协议或接口变化会拒绝继续旧调查，凭据轮换仍允许；新增 4 条恢复测试通过。

以下为切换网关前的历史记录，不能用于描述新网关的当前状态：

本次请求只包含内置合成样例或“回复 OK”，没有发送用户私有仓库内容。2026-11-07 10:45（上海）`gpt-6-sol` 的 `/models` 为 200，最小 Responses 生成为 403。10:52–10:53 又分别检查 `gpt-6-sol` Chat 和 `gpt-6.1-sol` Responses：模型列表仍为 200，最小生成超时。之前真实 Engine 调用也记录过 403/429。密钥未写入版本化文件。

截至当时，成功 live 修复仍未验收。随后按相同案例与分母使用新网关完成验收，结果在上方单独列出，旧服务访问失败没有被删除或改写为推理成功。

完整 Engine 调查也在其余代理停止后执行过（`gpt-6.1-sol` Responses，180 秒总预算、140 秒单次请求上限）。run `cf51fc1170354f1b9a05d033ef2bd2a5` 在首次调用返回 HTTP 403，未进入审批或执行；记录 1 次真实请求，提供方未返回 token usage。原始报告位于本机 `.traceforge/live-validation-final/report.json`。这是服务访问失败，不能归因为模型修复推理失败，也不能算通过。

在修复诊断工具后，**2026-11-07 11:22（上海）**再次执行一次有界最小生成：模型列表 HTTP 200，生成 HTTP 403，类别 `access_denied`。此次没有得到符合大小、编码和 JSON 约束的可展示错误字段，不能进一步断言是服务端哪个访问规则导致。见 [诊断记录](evidence/provider-diagnosis.json)。

**2026-11-07 13:10（上海）**在停止所有子代理后独立重试：`gpt-6.1-sol` 的模型列表仍为 HTTP 200，最小 Responses 生成仍为 HTTP 403。最新版[诊断记录](evidence/provider-diagnosis.json)保留此次时间和实际时延；因此不能把当前缺口仅归因于本次子代理并发。

## 可复现范围与限制

- 已提供包含前端/样例/基准的 wheel 和 sdist，并实测 Windows 独立安装；它们不包含 Python 解释器、Docker 或第三方 Python 依赖，不声称离线免安装。源码运行数据、私有仓库证据、原始本机路径和日志留在被忽略的 `.traceforge/`；wheel 默认使用用户数据目录。
- 单机 SQLite + 两 worker，至少一次处理；尚未实现多机调度、多租户权限或大规模生产压测。
- 检索是 BM25 + 字符 n-gram TF-IDF cosine + RRF，不是神经 embedding。核心推理为单 Agent，并行部分是独立取证任务。
- Docker 与本机 runner 明确区分；local 只用于显式可信代码，不是安全沙箱。Docker 验证镜像需事先包含测试依赖。
- 观察器与 Python 测试共享解释器，不能保证抵御任意反射、猴子补丁或主动伪造内部协议。审批和人工代码审阅仍有作用；当前报告只证明已执行测试。
- 内置样例与十个基准为合成工程案例，规模有限；没有生产用户、业务收益或跨仓库修复率数据。

## 界面记录

以下是本次真实浏览器验收保存的代表性截图，截图内运行时间与计数属于当时的记录；最终后端观察器验收以上表所列的 Docker 报告为准。

![修复前后测试结果](screenshots/verified-tests.jpg)

![Oracle 基准与独立分母](screenshots/oracle-benchmark.jpg)

面试时可使用 [五分钟演示流程](demo-script.md) 和 [技术追问手册](interview-playbook.md)，按实际结果陈述。

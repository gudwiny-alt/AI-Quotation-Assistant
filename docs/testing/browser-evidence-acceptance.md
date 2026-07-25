# Plan 2 浏览器与证据自动化验收记录

## 结论与范围

- 记录日期：2026-07-26
- 自动化状态：通过（最终完整门结果见下文）
- Mac 真实整屏证据验收：**NOT RUN / BLOCKED**
- Windows 真实前台、任务栏、日期时间与截图验收：**NOT RUN**，按计划延后至
  Plan 4 的干净非管理员 Windows 机器执行

本记录把“自动化合同通过”和“真实平台截图通过”严格分开。测试中的 900 任务均
为本地合成工作负载，证据文件是微小的 fake 字节文件，不是真实 PNG 截图；其
耗时不能代表真实网站、浏览器渲染或截图耗时。

## 自动化环境

- Python：3.12.13
- 操作系统：Darwin 25.5.0
- 已检测浏览器：Google Chrome 150.0.7871.182
- 浏览器路径：
  `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
- 临时磁盘：测试运行时可用空间 126,755,098,624 字节
- 外部网络：900 任务测试不使用外网

## 900 任务实测

一次独立运行的输出：

| 指标 | 实测值 |
|---|---:|
| 输入行 | 300 |
| 网站任务 | 900 |
| 模拟中断点 | 第 60 次 capture |
| 完成成功 | 658 |
| 固定技术失败 | 3 |
| 等待登录 | 1 |
| 因同站点登录而保持 Pending | 238 |
| capture 调用 | 659 |
| 正式 fake 证据文件 | 658 |
| 正式 fake 证据总字节 | 60,175 |
| SQLite 主文件大小 | 1,245,184 字节 |
| 测试内部总耗时 | 6.289 秒 |
| pytest 用例耗时 | 6.34 秒 |

已自动验证：

- 900 个任务全部插入并重新加载，无任务 ID 冲突；
- 300 个输出行的映射保持不变，重复物料编码不会合并行；
- 查询键按固定规则重复，五种业务结果均有精确持久化计数；
- 第一个进程中断后，`RUNNING` attempt 被记录为
  `PROCESS_INTERRUPTED` 并恢复为 `PENDING`；
- 第二阶段使用全新的 session/capture 实例，已成功任务的 attempt、result、
  SHA-256 和文件 mtime 均不变化；
- 天猫登录页按 `tmall` 站点族保留，同族任务不创建 attempt；京东与按品牌隔离
  的 `official:<brand>` 任务继续；
- 技术失败没有业务 outcome、没有正式 evidence，诊断文件只位于本地
  `evidence/diagnostics`；
- failure-only、waiting-login 和 pending-same-site 集合均精确匹配预期；
- 重新打开数据库后，所有正式 evidence 的哈希通过审计；
- 数据库、输入指纹路径、浏览器资料目录、输出、正式证据和诊断路径均受本地
  application-data 根目录约束。本例使用内联 associated-row snapshot，
  `associated_rows_snapshot_path`、`quote_path`、`report_path` 均为 `None`，
  测试对此显式断言。

五类持久化结果精确计数：

| 结果 | 数量 |
|---|---:|
| `price_found` | 120 |
| `no_model` | 149 |
| `capacity_unavailable` | 120 |
| `color_unavailable` | 119 |
| `sold_out` | 150 |

238 个天猫同站点任务保持 `PENDING` 是刻意验证“登录页隔离且不消耗 attempt”的
预期结果，不是任务丢失；900 个任务均可完整重载，且全字段与原始任务相等。

仓库目前没有单独的“summary 性能 API”，因此没有宣称独立 summary 查询耗时。
上述 6.289 秒包含本地仓库创建、任务执行、选择、重启恢复与证据审计。

## 自动化门

完整 Plan 2 gate 包含：

- 全部 `tests/unit`；
- 真实已安装 Chrome 的本地回环持久资料测试；
- checkpoint recovery；
- fixture capture；
- 900 任务工作负载。

最终结果：**418 passed，0 skipped，40.82 秒**。Ruff 检查 `src tests`，
Mypy 检查 `src/quote_app`；两项结果见本记录对应代码版本的最终验证输出。

## 平台验收状态

| 项目 | 自动化结果 | 真实人工验收 |
|---|---|---|
| Chrome Cookie/localStorage 重启保留 | 本地回环自动测试通过 | Mac 应用流程仍待人工复核 |
| 专用 profile 并发锁 | 自动测试通过 | Mac 应用流程仍待人工复核 |
| 六种 fixture 状态与登录停放 | fake page/capture 自动测试通过 | 真实页面与截图仍待验收 |
| 100%/125%/150% Windows DPI 几何 | mocked/unit 通过 | Windows 实机 NOT RUN |
| Mac Retina 逻辑/物理尺寸合同 | mocked/unit 通过 | Mac 实机 BLOCKED |
| 菜单栏、日期时间、Dock、标签栏、地址栏可读 | 不由自动质量指标证明 | NOT RUN |
| 红框在真实 DOM 上准确对齐 | fake 坐标合同通过 | NOT RUN |
| Windows 任务栏/时钟与前台窗口 | mocked/unit 通过 | Plan 4 执行 |

## 当前限制

- 当前执行进程的 macOS 屏幕录制权限预检查为 `False`；
- 当前非 GUI 执行环境无法取得有效的主屏物理分辨率，因此未记录分辨率或缩放，
  也未推断 Retina 实机通过；
- 没有生成真实 Mac 正式截图路径；
- Windows 结果仅来自 mock/unit，不得当作 Windows 实机验收。

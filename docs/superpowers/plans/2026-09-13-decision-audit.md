# 报价决策与稽核工作台实施计划

基线：`724820d`，独立分支 `codex/mac-170-ui-refresh`。1967 项单元及回归测试通过。
权威规格：`../specs/decision-confirmed.md`、`../specs/audit-workbench.md`。用户于2026-09-13授权开始开发，替代规格中先记录暂不开发的阶段说明。

## 约束
- 不修改已验证网站取价、截图、调度、原始生成工作簿的算法；新模块是生成后的本地辅助层。
- 未定义业务规则或无来源数据时必须待补充/待复核，不能填充模拟数据或默认通过。
- 手工写回仅 K/L/M/P/Q/AO，核对模板与商品定位、检测版本冲突、备份与原子替换，保留其他内容。
- 截图保存不等于内容通过；人工复核有理由、操作人、时间、证据版本，不能覆盖确定性硬性失败。
- 不自动启动采集、上传、发送或改动原稳定应用；输出独立体验包。

## Task 1：独立规则、存储与稽核服务
新增 `services/review_support.py`（可拆分同前缀模块）。纯 Decimal 边界判断、稳定商品身份、六类检查及本地安全写回/复核/报告。先编写意义明确的测试。核对真实模板而非照抄示意图。允许扩展 DesktopState 的 TaskRow 只读标识字段，不改变采集业务。

## Task 2：报价界面与导航集成
新增 `desktop_decision.py`，由现有 DesktopWorkbench 接入。产品清单进入双栏单品页，五项填写、AO及附件、智能报价提示、资格依据与优福包确认、写回预览/草稿/确认。复用清晰品牌素材、柔和控件、现有滚动分发。不动取价按钮和日志控制的业务回调。

## Task 3：稽核界面
新增 `desktop_audit.py`，同页六类全批次统计、商品汇总、单品全部检查、依据与人工处理。顶部统计不随筛选变化。统一“未通过”。空态和执行中不显示全部通过。

## Task 4：评审、回归和交付
对服务与界面进行独立代码评审，测试边界、身份定位、文件保真、冲突失败回滚、复核失效、统计和导航。原生窗口视觉/交互检查，1967项既有回归保持通过，构建新命名 Mac 应用，保留原包。记录实际验证范围与待明确业务口径。

## 服务接口约定（供两个界面并行工作）
模块 `quote_app.services.review_support` 导出：
- `CATEGORIES`: 六个中文分类的 tuple，顺序同规格。
- `ReviewCheck`: `id, product_id, code, category, title, status, comparison, reason` 字符串；`evidence_paths: tuple[Path,...]`；`human_reviewable: bool`。status用中文：通过/未通过/待复核/待补充/未检查/不适用。
- `ReviewProduct`: `id, title, specification, material_code` 字符串，`output_row: int`，`values: dict[str,str]` 包含 K/L/M/P/Q/AO，`context: dict[str,str]`，`attachments: list[Path]`，`channels: list[TaskRow]`。context支持 `category`（手机/多形态/未确认）、`stock`（在库/不在库/未确认）、`entry_date`、`first_quote_date`（ISO）、`youfu`（是/否/未确认）。补充信息明确为用户输入，不能伪造来源。
- `ReviewSession(model, month: QuoteMonth)`：`products: list[ReviewProduct]`；`quote_path: Path|None`；`running: bool`；同一输出会话应持久存活，不因切页丢失未保存草稿。
- `evaluate(product) -> list[ReviewCheck]`：当前内存填写值实时规则评估；`all_checks() -> list[ReviewCheck]`：全批次当前结果，重检不增加重复项。
- `save(product) -> Path`：草稿写回，同文件版本保护；失败抛 ValueError/OSError，不虚报成功。
- `confirm_product(product) -> Path`：全部必需规则通过且写回后，记录本地确认。
- `review(check_id, conclusion, reason, operator) -> None`：只可对可人工复核的检查执行，conclusion通过/未通过，原因与操作人必填；旧证据复核自动失效。
- `export_report(final=False) -> Path`：独立本地HTML报告；final=True必须最新全批次符合确认条件，否则抛ValueError。
- `can_confirm(checks) -> bool`：非空、必需检查全完成通过（不适用有明确依据可排除）。
- 所有公开错误面向用户可理解；界面负责捕获显示。

界面约定：`DecisionView(workbench, parent, session)` / `AuditView(workbench, parent, session)`，各有 `refresh()` / `destroy()`；不导入 desktop_ui 顶层导致循环。可在构造函数局部导入UI工厂。workbench._review_session 持有实例；workbench.review_product_id 跨页保留选中商品。服务若需接口扩展先通知控制方。

## 开发裁定
- 稽核报告选择本地 HTML，便于原生浏览器查看与打印，不覆盖报价表。
- 未明确的历史全量范围、调价基准、优惠条件等保留待复核。已知上一期可比较，但不得据此冒称全部往期已通过。
- 无可靠入库/在库/首次报价字段时提供明确标注“人工补充依据”的入口，以用户记录驱动检查；不以关联到历史BOP记录代替在库。

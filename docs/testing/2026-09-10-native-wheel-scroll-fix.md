# Mac .170 原生滚轮修复

基线：46bc6f7；分支：codex/mac-170-ui-refresh。

## 原因与修复

- 实际打包运行时为 Tk 9.0.4 Aqua。Tk 9 的精细滚动使用 TouchpadScroll，自绘页面与 RichTable 原先只绑定 MouseWheel，遗漏精细滚动。
- SlimScrollbar 原先只有点击与拖动处理，鼠标悬停在滚动条上滚动时无响应。
- 普通 MouseWheel 仍按旧版 Aqua 的小 delta 使用，Tk 9 的 120 单位 delta 导致大幅跳动。
- 内嵌 Text 原生类处理滚轮后，根窗口旧回调又滚动其外层页面。

新增 desktop_scrolling.py 统一局部绑定普通/精细事件，按 Tk 版本归一化滚动距离。精细事件通过 Tk 的 PreciseScrollDeltas 解码，Canvas 按内容像素移动，SlimScrollbar 按视区比例移动。RichTable 标题与内容支持滚动，原生 Text/Listbox/Treeview 不再重复触发外层滚动。Tk 8 不注册 TouchpadScroll；X11 保留 Button-4/5 支持。

仅修改界面滚动处理；报价、站点采集、截图引擎与恢复业务逻辑未改。

## 验证

- 修复前新回归测试 10 项明确失败，覆盖无响应、步幅过大及嵌套重复滚动。
- 最终 QUOTE_NATIVE_UI_TESTS=1 pytest tests/unit tests/ui：1,986 passed（1,944 单元 + 42 原生 UI），105.72 秒。
- 边界包含单像素精细移动、双轴隔离、Shift 横向、水平滚动条、末端限制、空轨道、禁用日志仍可滚动、原有拖动与控件交互。
- Ruff、mypy 和 git diff --check 通过。独立审查无生产阻塞问题；已处理测试在旧 Tk 上的兼容性问题。
- PyInstaller 独立目录构建成功。Analysis-00.toc 确认 desktop_scrolling、desktop_widgets、desktop_ui 来自本工作树；codesign --verify --deep --strict 通过。
- CUA 对实际新包进行窗口验证。进程路径确认为 dist-mac-ui-scroll/铺货报价工作台-UI精修预览.app/Contents/MacOS/铺货报价工作台-UI精修预览。
- 用 Command+Shift+2 进入数据准备页；在内容区 [850,395] 向下滚动后，输出目录与运行前准备区域显示，滑块下移；在滚动条 [980,395] 向上滚动后，顶部数据卡片恢复显示，滑块上移。没有拖动滑块。
- 测试完成关闭本次启动的新包。未启动真实网站报价任务，未执行真实批量报价验收。

发布目录：releases/mac-170-ui-scroll-fix；压缩包：releases/Mac170_滚轮修复版.zip。旧发布包保留。

# Windows 整屏证据冒烟测试

## 自动失败关闭条件

- 程序启动时设置并用 `AreDpiAwarenessContextsEqual` 验证
  Per-Monitor DPI Awareness V2；无法精确证明 PMv2 时返回
  `CAPTURE_ENVIRONMENT`，不得读取或混用虚拟化窗口尺寸。
- 同一次几何快照必须绑定受管窗口标识、主屏物理边界、浏览器物理边界、
  CSS viewport 宽高、DPR/DPI、最大化和非全屏状态。
- 快照的 JavaScript DPR、x/y DPI 必须全部存在；`GetDpiForWindow`
  返回的原生 DPI、快照 DPI、viewport scale 和 JavaScript DPR 必须
  在容差内三方一致。任一缺失或不一致都失败关闭。
- 原生 `IsZoomed` 必须确认受管浏览器已最大化。
- `ABM_GETSTATE` 必须确认任务栏未自动隐藏，任务栏矩形必须与主屏相交，
  `TrayClockWClass` 控件必须存在且可见；浏览器物理可见边界不得与任务栏
  实质相交，避免误将覆盖 `rcMonitor` 的 F11/伪全屏当作普通最大化。
- 当前 Windows 版本若不暴露可权威识别的时钟控件（包括将来可能变化的
  Windows 11 Shell 类名），程序必须失败关闭，待实机适配后才能放行，
  不能根据任务栏大致位置猜测。

## 实机验收

1. 在 100%、125%、150% 缩放各执行一次；
2. 确认截图包含标签栏、地址栏、网页、任务栏和日期时间；
3. 确认 DOM 红框在各缩放下准确对齐；
4. 开启任务栏自动隐藏、进入浏览器全屏、遮挡浏览器时均不得发布正式证据；
5. 记录 Windows 版本、显示分辨率、缩放、截图路径和结论。

自动质量门只检查尺寸、空白、对比度、熵、清晰度和页面稳定性。业务文字是否
肉眼可读仍须在实机冒烟中人工确认，不能由这些数值指标代替。

缺少 mss 等固定截图依赖时返回非重试 `CAPTURE_ENVIRONMENT`；普通临时 PNG
写入权限错误属于 `CAPTURE_FAILED`，只有原生屏幕 API 明确拒绝读取屏幕像素时
才使用 `CAPTURE_PERMISSION`。

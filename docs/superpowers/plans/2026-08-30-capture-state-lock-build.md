# 截图状态锁定版 `.149` 构建记录

## 构建结论

- 构建标签：`截图状态锁定修复版（六品牌三渠道）2026.08.30.149`
- 应用路径：`dist-capture-state-lock-149/福建移动铺货报价助手.app`
- 完整绝对路径：`/Users/yangguowei/Documents/资金物流平台铺货报价智能体/.worktrees/ui-run-scope/dist-capture-state-lock-149/福建移动铺货报价助手.app`
- 签名状态：`codesign --verify --deep --strict --verbose=2` 通过
- 主可执行文件 SHA-256：`1c50ed7c1fc0d9028ae52e0656e84b59dafc17d6dd2b72d9171e138478bb6e4e`

## 行为边界

- 天猫华为、荣耀、苹果在目标配置选定后只锁定一次权威价格；截图阶段不再重复进行跨时间价格稳定性复核。
- 截图阶段继续验证页面身份、机型、颜色和容量；若页面明确显示与已锁定价格冲突，仍拒绝截图。
- 华为官网等待首个有效权威价格后锁定价格，后续仅复核 URL、标题、颜色和容量没有漂移。
- OPPO 官网不再把明确标注为 4G 的 A6 搜索结果当成 A6 5G 精确匹配。
- 京东源码与契约测试相对 `.145` 回退基线 `45d158e` 保持零差异。

## 验证结果

- 全量测试：`2895 passed`
- 重点回归（含完整京东冻结契约）：`573 passed`
- 天猫完整契约：`204 passed`
- OPPO 官网完整契约：`43 passed`
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 82 source files`
- 打包冒烟：`1 passed`
- 额外包与 macOS 运行时冒烟：`8 passed`
- `git diff --check`：通过
- macOS 严格签名校验：通过

## 构建命令

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-pyinstaller-149 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-capture-state-lock-149 \
  --workpath build-capture-state-lock-149 \
  packaging/quotation_app.spec

codesign --force --deep --sign - \
  'dist-capture-state-lock-149/福建移动铺货报价助手.app'

codesign --verify --deep --strict --verbose=2 \
  'dist-capture-state-lock-149/福建移动铺货报价助手.app'
```

旧 `.148` 包与 Git 检查点 `c88bd05` 均保留，未覆盖。

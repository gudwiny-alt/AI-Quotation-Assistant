# 已验证证据复用版 `.150` 构建记录

## 构建结论

- 构建标签：`已验证证据复用修复版（六品牌三渠道）2026.08.30.150`
- 应用路径：`dist-verified-evidence-reuse-150/福建移动铺货报价助手.app`
- 完整绝对路径：`/Users/yangguowei/Documents/资金物流平台铺货报价智能体/.worktrees/ui-run-scope/dist-verified-evidence-reuse-150/福建移动铺货报价助手.app`
- 签名状态：`codesign --verify --deep --strict --verbose=2` 通过
- 主可执行文件 SHA-256：`f839f5c58cf0e05d1e587eba66fc16539916c5db75d1c1469cc5a678b2fbbc7b`

## 行为边界

- 天猫华为、荣耀、苹果在正式截图阶段直接复用观察阶段已锁定的价格，不再进行第二轮价格读取；页面、机型、颜色和容量仍需匹配。
- OPPO 官网无精确机型时，正式截图复用已准备好的搜索证据，不在截图瞬间重新发现商品卡或证明节点。
- 京东只在选项滚动出现 Playwright 瞬时 DOM 脱离或元素不稳定时等待 250 ms 并局部重试一次；商品匹配、选项匹配和价格读取保持不变。
- 小米官网识别当前空结果文案，并将无机型截图证据限定为搜索框与空结果提示；商品详情、选项和价格逻辑未修改。
- `.149` 应用和检查点 `3fefd72` 均保留，未覆盖。

## 验证结果

- 全量测试：`2901 passed`
- 四个修改模块完整契约：`489 passed`
  - 天猫：`204 passed`
  - OPPO 官网：`43 passed`
  - 京东：`176 passed`
  - 小米官网：`66 passed`
- 应用界面单元测试：`55 passed`
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 82 source files`
- 打包冒烟：`12 passed`
- macOS 严格签名校验：通过

## 构建命令

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-pyinstaller-150 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-verified-evidence-reuse-150 \
  --workpath build-verified-evidence-reuse-150 \
  packaging/quotation_app.spec

codesign --force --deep --sign - \
  'dist-verified-evidence-reuse-150/福建移动铺货报价助手.app'

codesign --verify --deep --strict --verbose=2 \
  'dist-verified-evidence-reuse-150/福建移动铺货报价助手.app'
```

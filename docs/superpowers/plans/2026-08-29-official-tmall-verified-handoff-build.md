# 官网与天猫验证态交接版 `.148` 构建记录

## 构建结论

- 构建标签：`官网天猫验证交接版（六品牌三渠道）2026.08.29.148`
- 应用路径：`dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app`
- 完整绝对路径：`/Users/yangguowei/Documents/资金物流平台铺货报价智能体/.worktrees/ui-run-scope/dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app`
- 签名状态：`codesign --verify --deep --strict --verbose=2` 通过
- 主可执行文件 SHA-256：`12c3c1dc007c3dca339ab6c33c43129dfe80fa1b4d69db6b827f057fb3466ccc`

## 检查点

- 回退基线：`45d158e`（`.145` 华为主详情价格根节点）
- 设计检查点：`3b85f91`
- 执行计划：`d5dff50`
- OPPO 官网无机型截图交接：`6661896`
- 华为官网权威报价交接：`4a673dc`
- 天猫华为／荣耀权威价格：`ebb87a0`
- 天猫苹果受控来源与选项选择：`b2c7093`

## 冻结边界

以下命令确认京东适配器与契约测试相对 `45d158e` 为零差异：

```bash
git diff --exit-code 45d158e -- \
  src/quote_app/sites/jd.py tests/contract/test_jd_adapter.py
```

京东契约测试：`172 passed`。

## 验证结果

重点回归矩阵：

```bash
.venv/bin/pytest -q \
  tests/contract/test_official_huawei_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_tmall_adapter.py \
  tests/contract/test_jd_adapter.py \
  tests/integration/test_huawei_official_pipeline.py \
  tests/integration/test_oppo_official_pipeline.py \
  tests/unit/test_runner_registry_observation.py \
  tests/unit/test_macos_capture_runtime.py \
  tests/unit/test_app.py
```

结果：`732 passed`。

完整工程验证：

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

结果：

- 全量测试在沙箱内为 `2890 passed, 1 failed`；唯一失败是沙箱禁止测试绑定 `127.0.0.1` 临时端口。
- 该持久化浏览器测试在沙箱外按原命令单独重跑：`1 passed`。因此 2891 项测试均已取得通过证据。
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 82 source files`
- 天猫完整契约：`201 passed`
- 打包冒烟：`1 passed`

## 构建与签名命令

```bash
PYINSTALLER_CONFIG_DIR=/private/tmp/quotation-pyinstaller-148 \
  .venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist-official-tmall-verified-handoff-148 \
  --workpath build-official-tmall-verified-handoff-148 \
  packaging/quotation_app.spec

codesign --force --deep --sign - \
  'dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app'

codesign --verify --deep --strict --verbose=2 \
  'dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app'

.venv/bin/pytest -q tests/smoke/test_packaging_spec.py

shasum -a 256 \
  'dist-official-tmall-verified-handoff-148/福建移动铺货报价助手.app/Contents/MacOS/福建移动铺货报价助手'
```

构建、严格签名校验、打包冒烟和哈希计算均成功；旧版本输出目录未覆盖。

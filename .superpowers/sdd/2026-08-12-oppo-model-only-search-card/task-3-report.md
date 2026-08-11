# Task 3 Report: OPPO 四证据截图视图冻结

## 状态

完成。新增一项回归契约，冻结详情页已经处于 80% 缩放时，
`prepare_capture_view()` 不会再次执行缩放，也不会重新定位四证据。

## 变更

- `tests/contract/test_official_oppo_live.py`
  - 新增 `test_oppo_capture_does_not_repeat_zoom_when_detail_is_already_at_80_percent`。
  - 使用真实 `OppoOfficialAdapter` 和现有 OPPO fixture；先经由 `observe()`
    建立 80% 缩放，再调用 `prepare_capture_view()`，断言缩放转换仍只有一次且
    定位次数为零。
- 未修改生产代码。已核对：`_observe_loaded_detail()` 与
  `prepare_capture_view()` 均调用 `ensure_capture_scale(page, scale=0.8)`；该助手
  会在内联和计算缩放都已经为 0.8 时直接返回，符合幂等要求。
- 未触碰 `detail_capture_view.py`、荣耀/小米/京东/天猫或 Excel 路径。

## Commit

`test: freeze OPPO four-proof capture view`

## 测试命令与输出

```bash
.venv/bin/pytest \\
  tests/contract/test_official_oppo_live.py::test_oppo_capture_accepts_exactly_title_price_capacity_and_color_in_one_view \\
  tests/contract/test_official_oppo_live.py::test_oppo_capture_positions_once_only_when_four_proofs_do_not_fit \\
  tests/contract/test_official_oppo_live.py::test_oppo_capture_does_not_repeat_zoom_when_detail_is_already_at_80_percent -q
```

```text
...                                                                      [100%]
3 passed in 0.11s
```

## 自审

- 同屏四证据契约保留并验证：`position_attempts == 0`。
- 非同屏四证据契约保留并验证：`position_attempts == 1`。
- 新增契约验证：已在 80% 时 `capture_scales == [0.8]`，且不发生定位。
- 该测试会在 `prepare_capture_view()` 将幂等助手替换为无条件缩放时失败。
- 已执行 `git diff --check`，无空白错误。
- 全量 OPPO 契约文件复验：`20 passed in 0.16s`。

## Concerns

无已知问题。定向契约基于 fixture 的缩放记录；真实浏览器缩放的底层兼容性仍由
`detail_capture_view` 单元测试和端到端采集验证覆盖。

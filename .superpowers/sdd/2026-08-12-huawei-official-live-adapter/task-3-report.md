# Task 3 Report — Huawei Runner, Checkpoint, Excel, and Report Pipeline

## Scope

Added `tests/integration/test_huawei_official_pipeline.py`. The production
adapter, runner, Excel writer, report writer, capture runtime, and all frozen
site adapters were left unchanged.

The integration harness uses the real `AdapterRegistry`,
`WebsiteTaskRunner`, `SQLiteTaskRepository`, `CaptureRequest` boundary, and
`run_full_pipeline`. Only the browser page and native screenshot device are
fixture-controlled.

## Covered behavior

- Normal VMALL detail flow persists the numeric `prdId` detail checkpoint,
  writes `AK=4999`, embeds one image at `AN`, and passes the exact four formal
  capture roles: `title`, `price`, `capacity`, `color`.
- `NO_MODEL`, `CAPACITY_UNAVAILABLE`, and `COLOR_UNAVAILABLE` each pass through
  the real runner and final Excel/report publication, write `AK="无"`, embed
  formal evidence at `AN`, and produce no technical failure.
- The two configuration-unavailable paths preserve the approved same-screen
  two-rectangle evidence roles: `title + capacity_group` and
  `title + color_group`.
- Three consecutive native capture failures leave the already persisted
  `4999` price and numeric detail URL intact; final Excel keeps `AK=4999`,
  leaves `AN` empty, and reports the row as partial.
- Two Huawei source rows retain base input order in rows, `AK`, `AN`, and the
  report totals.
- Restart from a saved numeric detail checkpoint visits that URL directly and
  performs zero homepage fills and zero Enter submissions.
- An exact result card labelled temporarily sold out still reaches
  `PRICE_FOUND`, persists `4999`, and captures formal evidence.

## Red/green record

Initial run: `5 passed, 3 failed`. All three failures were test-contract
mistakes that required an exact query string and rejected VMALL's legitimate
`cid` parameter. Production had already preserved the approved host, numeric
`prdId`, price, and evidence correctly. The assertions were corrected to prove
approved HTTPS host/path plus a numeric `prdId`; no production code changed.

Final targeted run:

```text
8 passed in 1.77s
```

Frozen pipeline and capture-reader isolation run:

```text
189 passed in 3.69s
```

Ruff:

```text
All checks passed!
```

## Conclusion

Task 3 is complete. No production or shared-layer defect was exposed, and no
frozen site behavior was modified.

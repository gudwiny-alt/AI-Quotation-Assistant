# Task 3 implementer report

Status: DONE

## Scope

- Added only `tests/integration/test_vivo_official_pipeline.py`.
- Did not modify production code, frozen site adapters, Excel mappings, or packaging.
- Used the real `WebsiteTaskRunner`, `SQLiteTaskRepository`, `AdapterRegistry`, capture request boundary, and `run_full_pipeline`; only browser/system screenshot boundaries are fixture-backed.

## Coverage

1. Normal vivo X200 result persists stable `4399`, an approved `shop.vivo.com.cn/product/<id>` detail URL, and publishes Excel `AK2` plus formal screenshot `AN2`.
2. `NO_MODEL`, capacity-unavailable, and color-unavailable each reach formal capture with their required evidence roles.
3. Formal capture failure preserves checkpoint price and detail URL, leaves `AK` populated, and leaves `AN` empty.
4. Two rows preserve base-input order, populate `AK`/`AN`, and keep quote/report counts consistent.
5. Restart after a detail checkpoint resumes the saved detail URL and performs no new search submission.
6. iQOO fails before any browser visit, produces no checkpoint price, and performs no formal capture.

## TDD evidence

Initial RED:

```text
.venv/bin/pytest -q tests/integration/test_vivo_official_pipeline.py
3 failed, 5 passed
```

The three failures were caused by the contract fixture cycling from the already-stable `4399` back to `4499` during formal capture reread. The integration-local `_PipelineVivoPage` now models the live settled-offer invariant: `4499 -> 4399 -> 4399...`; contract fixtures and production code were not changed.

Final focused run:

```text
.venv/bin/pytest -q tests/integration/test_vivo_official_pipeline.py
8 passed in 1.44s
```

Task-plan suite and lint:

```text
.venv/bin/ruff check tests/integration/test_vivo_official_pipeline.py
All checks passed!

.venv/bin/pytest -q tests/integration/test_vivo_official_pipeline.py tests/regression/test_official_capture_reader_isolation.py tests/unit/test_macos_capture_runtime.py
139 passed in 1.53s
```

## Production registration finding

`MacFormalCaptureRuntime` already includes `维沃` in the generic live-official reader mapping, so no production registration change is required.

## Risks / review attention

- The integration fixture deliberately imports the approved contract harness. If Task 1 selectors or fixture APIs are renamed, this integration test must be updated with the same reviewed contract change.
- The normal Excel scenario uses one canonical current vivo model/configuration; model-derivative and detailed DOM matching remain contract-test responsibilities rather than being duplicated here.
- The integration-local settled-price subclass is necessary because formal capture rereads the same live state after checkpoint persistence; allowing a synthetic post-settlement price reversal would test fixture oscillation rather than the required checkpoint invariant.

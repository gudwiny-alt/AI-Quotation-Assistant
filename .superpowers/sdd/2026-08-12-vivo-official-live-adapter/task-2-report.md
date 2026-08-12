# Task 2 report — vivo official live implementation

## Delivered

- Added the independent `VivoOfficialAdapter` in
  `src/quote_app/sites/official_brands/vivo.py`.
- Replaced only the vivo factory placeholder with that adapter.
- Supports the approved `shop.vivo.com.cn` entry/detail and
  `www.vivo.com.cn` search route, strict numeric detail URLs, real result card
  traversal, base-model matching, iQOO pre-visit rejection, configuration
  legal-no states, selected-offer price stability, checkpoint resume, and
  capture preparation/restoration.
- Capture JavaScript resolves the four supplied CSS selector specifications in
  the active document, scopes checked options through the real `版本` / `颜色`
  dt+dd groups, uses real DOMRects plus `elementFromPoint`, and allows at most
  one geometry-derived document scroll.

## Approved Task 1 harness corrections

- `_set_cards` now retains the existing terminal `div.no-goods` node while it
  rebuilds product cards, so the delayed terminal state can actually be shown.
- The proof assertion normalizes only the real `¥` currency glyph before
  checking the adopted `4399` price; it does not accept another price.

## Verification

```text
.venv/bin/pytest -q tests/contract/test_official_vivo_live.py
25 passed

.venv/bin/ruff check src/quote_app/sites/official_brands/vivo.py \
  src/quote_app/sites/official_brands/factory.py \
  tests/contract/test_official_vivo_live.py
All checks passed

.venv/bin/mypy src/quote_app/sites/official_brands/vivo.py \
  src/quote_app/sites/official_brands/factory.py
Success: no issues found in 2 source files

.venv/bin/pytest -q tests/contract/test_official_oppo_live.py \
  tests/contract/test_official_xiaomi_live.py
92 passed
```

## Risk

No live-browser run was performed in this task. The adapter is constrained to
the published vivo DOM contract and must receive the later Mac true-site
acceptance before it is frozen.

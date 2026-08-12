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

## Review fix — live wait and boundary hardening

- Search now waits up to the complete 40 × 250 ms budget. It succeeds on a
  visible exact card and evaluates a visible `div.no-goods` terminal only at
  the deadline. An early empty marker therefore cannot hide an exact card that
  arrives later in the same window; no card and no terminal remains technical.
- Capacity and color each have an independent 20 × 250 ms target-appearance
  window. A reliably disabled exact target remains an immediate legal no;
  absence becomes legal no only after the complete window. A click has its own
  bounded selected-state convergence wait, so asynchronous class replacement
  is not misclassified as unavailability.
- Offer stability uses lightweight instantaneous identity/title/unique
  selection/current-price snapshots: price absence alone is retried, while URL,
  title, selection ambiguity, or configuration drift fails immediately. The
  selected offer must be identical across 12 consecutive 250 ms intervals
  within the unified 5-second ceiling.
- Current prices are read only from visible `p.sale-price` nodes; every valid
  amount in that explicit scope is considered and the lowest is selected.
  Market, coupon, installment, and unrelated summary prices remain outside the
  selector.
- Search submission and checkpoint resume now enforce exact approved host/path
  roles. Model matching additionally rejects edition/accessory suffixes.
- Capture scale now uses the shared idempotent ensure/restore lifecycle.
  Geometry supports one bounded upward or downward scroll of 1–160 px and
  fails closed when the proof union cannot fit with margins. Missing evidence
  geometry is a technical failure; the contract fixture supplies real boxes
  for legal-no keyword/result/group nodes.
- Production no longer reads the test-only `.late-result` class.

Review-fix verification:

```text
.venv/bin/pytest -q tests/contract/test_official_vivo_live.py
50 passed

.venv/bin/pytest -q tests/contract/test_official_vivo_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_official_xiaomi_live.py \
  tests/contract/test_official_honor_live.py
185 passed

.venv/bin/ruff check src/quote_app/sites/official_brands/vivo.py \
  tests/contract/test_official_vivo_live.py
All checks passed

.venv/bin/mypy src/quote_app/sites/official_brands/vivo.py
Success: no issues found in 1 source file
```

The previously accepted but untracked Xiaomi adapter was recorded unchanged in
the isolated baseline commit `7dd8dd5`; it is not part of the vivo fix.

## Final review fix — per-proof occlusion and strict result settlement

- `_GEOMETRY` resolves the adapter-supplied title, current-price, selected
  capacity, and selected color selectors, then performs an independent center
  `elementFromPoint` hit test for every resolved node. Each obstruction is
  reported as `{proofBottom, blockerTop}` from the real DOMRects.
- A single occlusion-driven scroll uses the maximum
  `proofBottom - blockerTop + 24` displacement. Every displacement is bounded
  to 1–160 px and a downward document scroll is rejected when it would move the
  proof union above the 8 px safety margin. The contract covers a fixed overlay
  that clears after exactly one scroll and an in-document obstruction that
  remains aligned with the proof and therefore fails closed after one attempt.
- Base-model search matching now checks the leading SKU field after the model
  and rejects accessory product nouns including charger, headphones, data
  cable, and generic accessories even when later text contains a valid-looking
  capacity.
- `div.no-goods` is never authoritative before the full 40-tick search window.
  The contract covers both an early empty marker followed by an exact card at
  tick 18 and an early empty marker that stays empty until tick 40.

Final review verification:

```text
.venv/bin/pytest -q tests/contract/test_official_vivo_live.py
56 passed

.venv/bin/pytest -q tests/contract/test_official_vivo_live.py \
  tests/contract/test_official_oppo_live.py \
  tests/contract/test_official_xiaomi_live.py \
  tests/contract/test_official_honor_live.py
196 passed

.venv/bin/ruff check src/quote_app/sites/official_brands/vivo.py \
  tests/contract/test_official_vivo_live.py
All checks passed

.venv/bin/mypy src/quote_app/sites/official_brands/vivo.py
Success: no issues found in 1 source file
```

# SDD Progress — Huawei Official Live Adapter

- Plan: `docs/superpowers/plans/2026-08-12-huawei-official-live-adapter.md`
- Design: `docs/superpowers/specs/2026-08-12-huawei-official-live-adapter-design.md`
- Baseline commit: `b3a1e80`
- Current task: Task 3 — runner/checkpoint/Excel/report integration
- Status: complete and verified
- Frozen sites: HONOR, Xiaomi, OPPO, vivo, JD, Tmall
- Task 2 report: `.superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-2-report.md`
- Task 3 report: `.superpowers/sdd/2026-08-12-huawei-official-live-adapter/task-3-report.md`
- Task 3 verifies eight integration scenarios through the real registry,
  runner, SQLite checkpoint, formal capture request, Excel, and report
  boundaries. Targeted Huawei integration: `8 passed`; frozen pipelines and
  capture-reader isolation: `189 passed`; Ruff passes. No production code or
  frozen site changed.
- Verification after independent-review fix: Huawei contract `75 passed`;
  Huawei/factory/isolation/public evidence boundary selection `222 passed`;
  frozen HONOR/Xiaomi/OPPO/vivo
  contracts `208 passed`; Ruff and mypy pass.
- Review fix hardens exact base-model tails, scopes extraction and capture to
  the same main current-price node, excludes promotional/sticky duplicates,
  and explicitly pauses common Huawei login/security states. No frozen site or
  public layer changed in the fix round.
- Review fix: detail fixtures and contracts now follow the reviewed
  `www.vmall.com` -> `item.vmall.com` redirect, real title/price nodes,
  label-anchored option groups, style-based selection, dynamic price proof,
  complete legal-no group evidence, and fixture-marker provenance guards.
- Final Task 1 review fix: legal-no capacity/color observations require
  same-screen `title` + full `capacity_group` / `color_group` rectangles.
- Task 2 includes the explicitly approved complete option-group evidence role
  compatibility and two independently reproduced harness-only corrections;
  see its report for exact boundaries.
- Next task: Task 4 — freeze regressions and prepare the Mac acceptance build.

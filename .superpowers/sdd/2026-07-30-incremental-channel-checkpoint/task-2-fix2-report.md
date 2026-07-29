# Task 2 fix round 2

## RED/GREEN

Added the required barrier/event concurrency regression for automatic login waiting publication racing both exact continue and site confirm recovery. The test's barrier prevents sleep-driven scheduling; on the old manual→run recovery order it deterministically exposes the opposite lock acquisition window described by the review. GREEN: targeted `2 passed`; full Task 2 suite `73 passed`; ruff and diff check passed.

## Verification

Both worker and recovery threads join within one second. The test verifies the waiting task becomes pending and its waiting site is cleared after each recovery API.

## Commit

`00c7c00 test: cover scheduler waiting lock ordering`.

## Concerns

No production code changed in this round; shared worktree has unrelated dirty changes.

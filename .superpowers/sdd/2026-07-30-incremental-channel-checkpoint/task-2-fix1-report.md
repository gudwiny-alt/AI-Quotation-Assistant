# Task 2 fix round 1

## RED/GREEN

Added context-provider retry regression cases for two transient failures then success and three failures exhausted. They would fail before the fix because context construction was outside the retry `try`. GREEN: targeted command passed `3 passed`; full Task 2 suite passed `71 passed`; ruff and `git diff --check` passed.

## Lock strategy

All paths that need both scheduler locks now acquire `_run_lock` before `_manual_lock`. `run_until_idle()` and automatic waiting publication already follow this order; `confirm_manual_login()` and `continue_current_task()` were changed from manual→run to run→manual. The exact task/token/site requeue remains inside the paired critical section.

## Commit

Pending commit creation.

## Concerns

The shared worktree remains dirty outside these focused Task 2 files; no reset/clean was used.

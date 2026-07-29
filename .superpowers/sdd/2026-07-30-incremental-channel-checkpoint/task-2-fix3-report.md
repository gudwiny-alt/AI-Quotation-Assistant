# Task 2 fix round 3

## RED/GREEN

The replacement concurrency test uses a sink latch and observable run-lock wrapper. Before the fix's run→manual order, the recovery thread holds manual while its observed run-lock acquire blocks; allowing the worker then deterministically forms the old cycle. GREEN: deterministic selector `2 passed`; Task 2 suite `73 passed`; ruff and diff check passed.

## Existing scheduler contracts

The inherited tests in `00c7c00` cover global pause after waiting, restart restoration, stale token/generation rejection, and exact task/site requeue. They are pre-task foundation changes authorized by the controller, not expanded in this round; their selectors should be run with the scheduler suite.

## Commit

Recorded after commit creation.

## Concerns

No production behavior changed in round 3.

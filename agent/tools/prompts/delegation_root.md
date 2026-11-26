## Execution mode

Use synchronous delegation when the result is needed
to continue the current workflow.

Use background delegation for independent tasks whose
results can be handled later.

Both modes can run concurrently.
Background results are delivered automatically and
start a new turn if the parent is idle.

Continue useful work or end the turn.
Do not poll or wait for background tasks.

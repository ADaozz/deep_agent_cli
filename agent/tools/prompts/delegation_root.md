## Execution mode

Use synchronous delegation when the delegated result
is a prerequisite for completing the current workflow.

Synchronous calls return after their delegated tasks
have finished.

Use background delegation for independent, non-blocking
tasks whose results can be handled later.

Background calls return a run_id immediately after
successful submission.

Synchronous and background tasks may be launched in the same response. They run concurrently.

Background results are delivered automatically upon completion.

If the parent agent is idle, the result automatically starts a new agent turn.

While background tasks are running, continue other
useful work or end the current turn.

Do not poll or wait for background tasks.

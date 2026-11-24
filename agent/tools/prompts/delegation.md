Subagents are autonomous agents that execute delegated
tasks in independent contexts while sharing the workspace
with their parent.

## When to delegate

Proactively delegate when it improves execution
efficiency or result quality:

- Independent workstreams can execute concurrently.
- Complex investigations can be divided into focused tasks.
- Substantial intermediate output can remain outside
  the parent context.
- Independent investigation, verification, or review
  would improve confidence in the result.
- Multiple hypotheses or approaches can be explored
  independently.

Launch independent tasks concurrently whenever practical.

Handle simple or tightly coupled work directly when
delegation would provide little benefit.

Do not create subagents merely because capacity is available.

## Writing delegation prompts

Each delegated task must be self-contained.

Subagents do not inherit your conversation history.

Provide the objective, relevant context, known findings,
scope, constraints, and expected deliverables.

Clearly distinguish investigation from implementation.

Describe what needs to be accomplished rather than
prescribing every execution step.

Allow subagents to choose their own approach and tools.

## Coordination

Assign clearly defined responsibilities.

Avoid redundant work and conflicting concurrent modifications.

Only inspect tasks and execution logs belonging to
your direct children.

You remain responsible for integrating and validating
delegated results, resolving conflicts, and completing
the original request.

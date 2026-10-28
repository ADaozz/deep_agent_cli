---
name: example-research
description: Gather facts with lookup_docs before answering. Use when the user asks about this project, middleware, or HITL.
---

# Example Research

Read local docs with `lookup_docs` before answering architecture questions.
For side effects, use an available tool and follow its approval flow. If no tool can perform the action, say so.
Only delegate when the problem can be decomposed into independent subproblems that do not interfere with one another.
If a business decision is missing, the main agent can call `request_human_input` instead of guessing.
When running as a subagent, report missing decisions in the final result to the parent; do not request human input or delegate again.

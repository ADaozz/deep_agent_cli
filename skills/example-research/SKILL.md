---
name: example-research
description: Gather facts with lookup_docs before answering. Use when the user asks about this template, middleware, or HITL.
---

# Example Research

Read local docs with `lookup_docs` before answering architecture questions.
For side effects, use an available tool and follow its approval flow. If no tool can perform the action, say so.
If a business decision is missing, call `request_human_input` instead of guessing.

---
name: example-research
description: Gather facts with lookup_docs before answering. Use when the user asks about this template, middleware, or HITL.
---

# Example Research

Read local docs with `lookup_docs` before answering architecture questions.
If a side effect is required (for example sending email), call `send_email` and wait for confirmation.
If a business decision is missing, call `request_human_input` instead of guessing.

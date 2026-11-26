# Repository Guidelines

## TUI Rendering Specification

Preserve the established TUI rendering specification in "docs/tui-rendering-spec.md". Do not change tool titles, status markers, colors, tree structures, alignment, spacing, grouping, or folding behavior unless explicitly requested by the user. Rendering changes require corresponding specification and regression test updates.

## Architecture References

- Single TUI rendering authority: `docs/tui-rendering-spec.md`
- Harness architecture and responsibilities: `docs/architecture.md`
- Domain ownership and dependency boundaries: `docs/domain-design.md`
- Sandbox and credential security boundaries: `docs/sandbox.md`

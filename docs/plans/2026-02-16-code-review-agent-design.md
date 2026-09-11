# Code Review Agent Design

## Overview

A `/review` slash command + subagent that performs multi-pass code review on local changes. Generic (reads CLAUDE.md dynamically), thorough coverage with self-critique to control noise.

## Deliverables

1. **Slash command** (`~/.claude/commands/review.md`) — user-facing entry point, parses args, dispatches subagent
2. **Subagent** (`~/.claude/agents/code-reviewer.md`) — the reviewer itself

## Slash command interface

```
/review                     # uncommitted changes (staged + unstaged)
/review staged              # only staged changes
/review HEAD~3              # changes since 3 commits ago
/review main                # changes since main branch
/review src/asset_sync.py   # review a specific file in full
```

Lightweight: determine git range or file target, dispatch subagent with resolved context.

## Subagent protocol

### Phase 0: Context Gathering
- Read CLAUDE.md (root + any in changed directories)
- Read git log (last 10 commits) for recent patterns
- Read linter/formatter configs if they exist
- Get the diff (or file contents)
- Classify change type: bugfix | feature | refactor | config | docs

### Phase 1: Expand Beyond the Diff
- For each changed file, read the full file (not just changed lines)
- For changed functions, grep for callers
- For changed imports/interfaces, grep for consumers
- Note existing patterns in surrounding code

### Phase 2: Multi-Pass Review
- **Pass A: Security** — injection, auth, secrets, path traversal
- **Pass B: Correctness** — logic errors, edge cases, error handling, data integrity
- **Pass C: Architecture** — duplication, pattern violations, abstraction level, blast radius
- **Pass D: Performance** — N+1, unnecessary allocations, missing indexes (skip for config/docs)
- **Pass E: Clarity** — confusing naming, missing "why" comments, test gaps (skip if blockers found)

### Phase 3: Self-Critique
- For each finding: "Would I dismiss this as the author?"
- Remove project conventions, intentional patterns, nits when higher-severity issues exist
- Merge related findings into single comments
- Cap at 7 findings max, ordered by severity

## Output format

Severity-prefixed findings with file:line references and fix suggestions. One-sentence verdict.

If clean: "No issues found. Checked security, correctness, architecture, and conventions."

Severity levels: `[blocker]`, `[warning]`, `[suggestion]`, `[nit]`

## Design principles

- Silence when clean — don't invent concerns
- Context > diff — full file reads + caller/consumer tracing
- Cap at 7 findings, prioritize by severity
- Every finding includes a fix suggestion or specific direction
- Reference actual codebase code with file:line
- Self-critique removes likely false positives
- Skip irrelevant passes based on change classification

## Research basis

- Google eng-practices: review for code health, not perfection; focus on system context
- Microsoft Research (Greiler): useful comments are distinguishable from noise
- Goedecke: diff-only is the biggest mistake; cap at ~5-6 comments
- Graphite Diamond: self-critique + <3% false-positive rate
- Qodo 2.0: multi-agent specialized review passes
- CodeRabbit: 1:1 code-to-context ratio, dependency graph tracing
- Osmani: silence > noise for trust; AI catches 70-80% of low-hanging fruit when configured well

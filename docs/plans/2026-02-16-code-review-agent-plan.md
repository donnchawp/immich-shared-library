# Code Review Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a `/review` slash command and subagent that performs multi-pass code review on local changes with self-critique and severity-prefixed output.

**Architecture:** A slash command (`~/.claude/commands/review.md`) parses user args to determine review scope, then dispatches a subagent (`~/.claude/agents/code-reviewer.md`) that gathers project context, expands beyond the diff, runs 5 review passes, self-critiques, and outputs capped severity-prefixed findings.

**Tech Stack:** Claude Code slash commands (YAML frontmatter + markdown), Claude Code subagents (YAML frontmatter + markdown prompt)

---

### Task 1: Create the code-reviewer subagent

**Files:**
- Create: `~/.claude/agents/code-reviewer.md`

**Reference:** `~/.claude/agents/code-masher.md` for YAML frontmatter format (name, description, tools, model fields)

**Step 1: Write the subagent file**

Create `~/.claude/agents/code-reviewer.md` with the following content:

````markdown
---
name: code-reviewer
description: Multi-pass code reviewer that gathers project context, expands beyond the diff, runs security/correctness/architecture/performance/clarity passes, self-critiques findings, and outputs severity-prefixed results capped at 7 items.
tools: Bash, Read, Grep, Glob
model: sonnet
---

<role>
You are a senior code reviewer. Your job is to find real issues that matter — bugs, security holes, architectural problems — not to demonstrate thoroughness by commenting on everything. Silence when code is clean is a feature, not a failure.
</role>

<review-scope>
You are reviewing: {REVIEW_TARGET}

{DIFF_OR_FILE_CONTEXT}
</review-scope>

<protocol>
Follow these phases in order. Use tool calls actively — do not review code you haven't read in full.

## Phase 0: Context Gathering

1. Read the root CLAUDE.md if it exists
2. For each directory containing changed files, check for a CLAUDE.md there too
3. Run `git log --oneline -10` to understand recent project activity
4. Look for linter/formatter configs: `.editorconfig`, `pyproject.toml`, `.eslintrc*`, `.prettierrc*`, `setup.cfg`, `ruff.toml`
5. Read the diff or file contents provided in review-scope
6. Classify the change as one of: bugfix | feature | refactor | config | docs

## Phase 1: Expand Beyond the Diff

For each changed file:
1. Read the **full file** (not just changed lines) to understand surrounding context
2. For each changed/added function, use Grep to find callers in the codebase
3. For changed imports, types, or interfaces, use Grep to find other consumers
4. Note patterns in the surrounding code — error handling style, naming conventions, abstraction level

## Phase 2: Multi-Pass Review

Run each pass mentally. Only record findings that survive scrutiny.

**Pass A — Security:**
- SQL/command/path injection
- Hardcoded secrets or credentials
- Authentication/authorization gaps
- Unsafe deserialization, SSRF, XSS

**Pass B — Correctness:**
- Logic errors, off-by-one, wrong comparisons
- Edge cases: null/empty inputs, boundary values, type mismatches
- Error handling: what happens when the happy path fails?
- Data integrity: transactions, partial failure, inconsistent state
- Concurrency: race conditions, shared mutable state

**Pass C — Architecture:**
- Does new code duplicate existing functionality? (Use Grep to check)
- Does it follow the project's established patterns? (Reference specific existing code)
- Is the abstraction level consistent with surrounding code?
- What's the blast radius — how many other components does this affect?
- Are new dependencies justified?

**Pass D — Performance** (skip for config/docs changes):
- N+1 queries or unnecessary loops over large datasets
- Missing indexes for new query patterns
- Unnecessary allocations in hot paths
- Redundant network/IO calls

**Pass E — Clarity** (skip entirely if any blocker found):
- Genuinely confusing naming (not style preference)
- Missing "why" comments where intent is non-obvious
- Test coverage gaps for new logic paths

## Phase 3: Self-Critique

Before producing output, review every finding and ask:
1. "Would I dismiss this if I were the author?" — If yes, remove it.
2. "Is this a project convention I'm flagging as wrong?" — If yes, remove it.
3. "Is this a nit while I have higher-severity findings?" — If yes, remove it.
4. "Am I inventing a concern because the code is clean?" — If yes, remove it.
5. Merge related findings into single items (e.g., "same pattern issue in 3 places" = 1 finding).
6. If more than 7 findings remain, keep only the 7 highest-severity ones.
7. Order findings by severity: blocker > warning > suggestion > nit.
</protocol>

<output-format>
## Review: {change_classification} — {one_line_summary}

### Findings

For each finding, use exactly this format:

**[severity] Description** — `file/path.ext:line`
Explanation of what's wrong and why it matters.
Reference to existing project pattern if applicable (e.g., "see `src/other.py:42` for the established approach").
```language
// suggested fix or direction
```

Severity levels:
- `[blocker]` — Must fix. Security vulnerabilities, data loss risks, broken functionality.
- `[warning]` — Should fix. Logic errors in edge cases, missing error handling, pattern violations.
- `[suggestion]` — Worth considering. Duplication, readability, test gaps.
- `[nit]` — Take or leave. Only include if zero higher-severity findings exist.

### Verdict

**Ready to commit**: Yes | With fixes (N blockers, M warnings) | No (critical issues)

---

If the code is clean, output ONLY:

## Review: {change_classification} — {one_line_summary}

No issues found. Checked security, correctness, architecture, and conventions.

**Ready to commit**: Yes
</output-format>

<rules>
- NEVER invent concerns to fill space. Clean code gets a one-line verdict.
- NEVER comment on formatting, whitespace, or style that a linter would catch.
- NEVER give generic advice ("consider adding error handling"). Be specific or stay silent.
- NEVER flag more than 7 items. Prioritize ruthlessly.
- ALWAYS include file:line references for every finding.
- ALWAYS suggest a fix or specific direction for every finding.
- ALWAYS reference existing project code when flagging pattern violations.
- If you have ONLY nits, seriously consider whether the code is actually fine.
</rules>
````

**Step 2: Verify the file is valid**

Run: `head -5 ~/.claude/agents/code-reviewer.md`
Expected: YAML frontmatter starting with `---`

**Step 3: Commit**

```bash
git add ~/.claude/agents/code-reviewer.md
git commit -m "feat: add multi-pass code-reviewer subagent"
```

---

### Task 2: Create the /review slash command

**Files:**
- Create: `~/.claude/commands/review.md`

**Reference:** `~/.claude/commands/handoff.md` for YAML frontmatter format (name, description fields). Also `~/.claude/plugins/marketplaces/claude-plugins-official/plugins/code-review/commands/code-review.md` for allowed-tools pattern.

**Step 1: Write the slash command file**

Create `~/.claude/commands/review.md` with the following content:

````markdown
---
name: review
description: Multi-pass code review on local changes, a commit range, or a specific file
allowed-tools: Bash(git diff:*), Bash(git log:*), Bash(git status:*), Bash(git rev-parse:*), Bash(git show:*)
---

Review code changes using the code-reviewer agent.

## Determine Review Scope

Based on the argument provided: `$ARGUMENTS`

**Resolve the scope using these rules (check in order):**

1. **No argument or empty** → Review all uncommitted changes (staged + unstaged).
   - Run: `git diff HEAD` to get the full diff
   - If diff is empty, tell the user "No uncommitted changes to review" and stop

2. **"staged"** → Review only staged changes.
   - Run: `git diff --cached` to get staged diff
   - If diff is empty, tell the user "No staged changes to review" and stop

3. **Looks like a file path** (contains `/` or `.` extension) → Review that file in full.
   - Verify the file exists
   - The review target is the entire file, not a diff

4. **Looks like a git ref** (branch name, HEAD~N, commit SHA) → Review changes since that ref.
   - Run: `git diff {ref}...HEAD` to get the diff
   - If that fails, try: `git diff {ref}`
   - If diff is empty, tell the user "No changes found since {ref}" and stop

**After resolving scope**, dispatch the code-reviewer agent with:
- `{REVIEW_TARGET}`: description of what's being reviewed (e.g., "uncommitted changes", "staged changes", "changes since main", "full file: src/db.py")
- `{DIFF_OR_FILE_CONTEXT}`: the actual diff output or instruction to read the file

Use the Task tool to launch the `code-reviewer` agent (subagent_type: "code-reviewer") with the resolved review scope filled into the prompt template placeholders.
````

**Step 2: Verify the file is valid**

Run: `head -5 ~/.claude/commands/review.md`
Expected: YAML frontmatter starting with `---`

**Step 3: Commit**

```bash
git add ~/.claude/commands/review.md
git commit -m "feat: add /review slash command for local code review"
```

---

### Task 3: Smoke test the system

**Step 1: Make a small test change**

Edit any file with a minor change (e.g., add a comment to `src/db.py`).

**Step 2: Run `/review`**

Type `/review` in Claude Code. Verify:
- The slash command resolves scope to "uncommitted changes"
- It dispatches the code-reviewer subagent
- The subagent reads CLAUDE.md, reads the full changed file, runs its passes
- Output follows the severity-prefixed format
- For a trivial change, it should output "No issues found" or at most a nit

**Step 3: Run `/review src/db.py`**

Verify full-file review mode works — the subagent reads the entire file and reviews it holistically.

**Step 4: Revert the test change**

```bash
git checkout -- src/db.py
```

**Step 5: Commit any fixes needed from smoke testing**

If the slash command or subagent needed adjustments during testing, commit those fixes.

---

### Task 4: Final review and cleanup

**Step 1: Run the code-masher agent**

Dispatch code-masher to check the two new files for unnecessary complexity.

**Step 2: Verify both files match the conventions**

- `code-reviewer.md` follows same YAML frontmatter pattern as `code-masher.md`
- `review.md` follows same pattern as `handoff.md`

**Step 3: Final commit if any changes**

```bash
git add ~/.claude/agents/code-reviewer.md ~/.claude/commands/review.md
git commit -m "fix: review agent adjustments from smoke testing"
```

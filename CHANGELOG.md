# Changelog

Notable changes to Arbiter. Record schema versions are called out because a
record is a compatibility boundary: a reader refuses a version it does not know.

## Unreleased

### Fixed — the gate reached wrong verdicts

The evidence check inferred what happened to a cited test from pytest's exit
status and from words in its output. Both lie about a single test, and the gate
was wrong in both directions.

- **False clean passes.** A proof marked `skip` counted as passing (pytest exits
  0). So did one marked `xfail` that genuinely failed. A failing test whose
  output contained "not found" was read as a test that does not exist, which is
  merely `untested` and passes by default — as were a setup error and a test
  module that could not be imported.
- **False failures.** A `--cov-fail-under` in a project's own pytest config
  trips on every single-test run, so every passing proof in such a project was
  reported as failing.

The outcome is now read from the runner's structured report. A command that
leaves no report (not pytest, or running in a container) falls back to pytest's
documented exit codes, where output text may only ever lower a result.

- `--test-command` was split with `str.split`, which broke any quoted argument.
  It is now split like a shell would, and a command that cannot be launched is
  a usage error before any provider is called rather than a paid-for run in
  which nothing executed.
- The fence around untrusted text in prompts was a constant in this public
  repository, so a diff could carry the closing delimiter and end its own
  fence. The delimiter is now drawn fresh for each prompt.
- SIGTERM — how a CI system cancels a job — killed the process mid-run, leaving
  provider CLIs running and a temporary worktree registered. It now unwinds
  like Ctrl-C and exits 143.
- Replay ordered reviewer reports by the order they finished in; the live run
  uses panel order. Where no consolidated verdict exists, that could quote a
  different reviewer's citations than the run did.

### Changed — behaviour you will notice

- **A proof that errors, cannot be imported, or times out makes its criterion
  `unverifiable`**, so the verdict is `unclear` (exit 3). It used to land on
  `untested`, which passes by default. A broken proof is not the same thing as
  a test nobody wrote. If cited tests cannot run in the environment Arbiter runs
  in, a gate that used to pass quietly will now say so: point `--test-command`
  at the runner that works (`uv run pytest`, for instance).
- **A skipped proof is `untested`; an `xfail` proof that fails is `unmet`.**
- **Consolidation is skipped when there is nothing to reconcile** — one
  reviewer, or reviewers that agree on every criterion. That removes a serial
  call to the most expensive model on the panel from the common path, and it is
  the conservative direction: the merge takes the worst status any reviewer
  gave. `--consolidate always` restores the old behaviour.
- **A failed provider call is retried once** (`--retries 0` to disable). A
  timeout and a login problem are not retried.
- **Verify and plan review no longer stream reviewers' raw JSON.** A run is a
  live status board on a terminal and one line per step in a pipe. `-v` shows
  the raw streams as before.
- `--json` output gains a top-level `usage` key beside the result.
- Record schema is **v4**: a test check states its `outcome`, and each provider
  call carries an `attempt` number and the record a `usage` roll-up. v1–v3
  records still replay, through the rules they were written under. An older
  Arbiter refuses a v4 record rather than misreading it.
- Where no consolidated verdict exists for a criterion, a citation one reviewer
  left out is filled from another that reached the same status. A v1–v3 record
  of a multi-reviewer run whose consolidation *failed* can therefore replay
  with drift in that citation.
- `--plan` is required only when the project settings do not name a plan.

### Added

- **Project settings**: `[tool.arbiter]` in `pyproject.toml`, or `.arbiter.toml`.
  Flag beats file beats default. Unknown keys and wrong types are errors, and
  settings that widen access to the host (`--allow-tools`,
  `--no-scrub-test-env`) are refused in a file. `--config`, `--no-config`.
- `arbiter init` — settings, a plan template, and the ignore rule for run
  records. Never overwrites.
- **GitHub Action** (`action.yml`) and **pre-commit hook**
  (`.pre-commit-hooks.yaml`).
- `--summary FILE` — the result as markdown, with everything a model or a diff
  contributed made inert.
- A per-run usage and cost line, in the footer, in `--json`, and in the record.
- `--test-timeout`, `--consolidate`, `--retries`, `--version`, and
  `python -m arbiter`. Negative forms of the boolean flags
  (`--no-require-tests`, `--run-tests`, ...), so a flag can override a file.
- Each distinct cited test runs once per verification rather than once per
  criterion that cites it.

### Internal

- Evidence checks run off the event loop, so the display stays live and an
  interrupted run kills the test in flight instead of waiting out its limit.
- CI builds the wheel and runs it from a clean environment, and runs the
  GitHub Action for real against the fake provider.

## 0.2.0

Run records, replay, the evaluation-case format, per-call token usage, and the
Antigravity provider. See the git history.

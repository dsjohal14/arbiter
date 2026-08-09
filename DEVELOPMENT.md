# Developing Arbiter

```bash
make venv && make install
make check          # lint + format + the whole suite, in CI's order
```

`make` on its own lists every target.

---

## Why every command goes through `python -m`

The Makefile invokes tools as `venv/bin/python -m pytest`, never `venv/bin/pytest`.

A virtualenv's console scripts hardcode an absolute shebang. Copy or move the
venv — or copy a project directory that contains one — and every script in
`bin/` points at a path that no longer exists, while `bin/python` itself keeps
working. You get `command not found: pytest` in an environment where pytest is
plainly installed.

The same reasoning is in the product: `evidence.detect_test_command` falls back
to `[sys.executable, "-m", "pytest"]` when `shutil.which("pytest")` comes up
empty, because a user's repo can be in exactly that state.

---

## Test layout

| | Count | Time | What it covers |
|---|---|---|---|
| `tests/unit/` | 257 | ~5s | Pure functions: parsing, verdict logic, prompt construction, replay, scoring |
| `tests/integration/` | 107 | ~60s | Real subprocesses, real git worktrees, the CLI through `main()` |

Iterate with `make test-unit`. Run `make check` before you push.

### The fake CLI, and why it isn't a mock

`tests/fake_cli.py` is a real executable placed on `PATH` by the `shim_path`
fixture, impersonating `claude`, `codex`, and `gemini`. It replays a scripted
JSONL stream and can hang, exit non-zero, emit garbage, or flood stderr on
demand.

Mocking `asyncio.create_subprocess_exec` would have been less code and would
have been wrong. The bugs this suite exists to catch live *below* that seam:

- the 64 KiB `StreamReader` line limit that used to crash on long answers
- `E2BIG` when a prompt exceeds the OS argument limit
- SIGTERM escalation and orphaned children after Ctrl-C
- a child that fills stderr and stops reading stdin

A mock has none of those. A test against one would pass while production failed.

```python
def test_something(shim_path):
    # the same reply for every provider
    shim_path(claude_events("hello"))
    # a different reply per provider
    shim_path(per_provider={"claude": claude_events("x")})
    # never exits, for timeout and cleanup tests
    shim_path(hang=60)
    # a provider that dies with a message on stderr
    shim_path(claude_events("x"), exit_code=2, stderr="auth expired")
```

`per_provider` matters because each CLI has a different event shape — feeding
codex a claude stream produces no content, which is itself a useful way to
simulate one reviewer failing.

### Tests that use real git

`tests/unit/test_baseline.py` builds an actual repository with a buggy base and
a fixing branch, because `check_test_discriminates` creates a git worktree and
runs the cited test against the old code. One test asserts the working tree is
left clean and the temporary worktree removed — that check is the reason it is
safe to point this at your own repo. Another plants the active branch's `src/`
on `PYTHONPATH` and proves the base run still imports the checked-out package,
guarding against editable installs that would compare HEAD with itself.

### The suite never calls a real provider

Every test runs against the fake CLI. Nothing in `make check` costs tokens or
needs you to be logged in. To exercise real providers, use `make demo`,
`make demo-plan`, or `make doctor`, all of which say so before they run.

---

## Writing tests here

Two conventions, both load-bearing.

**Name the behaviour, not the function.** `test_a_partial_reviewer_panel_cannot_produce_a_clean_pass`
tells a future reader what breaks if it fails. `test_derive_verdict_3` does not.

**A regression test must fail on the old code.** Several tests in this repo were
written to lock in a specific bug, and each one was checked by reverting the fix
and confirming it goes red. The clearest example: `test_a_path_escaping_the_repo_is_refused`
originally cited a path that didn't exist, so it passed via the "file not found"
branch and would have kept passing with the security check deleted. It now plants
a file that genuinely exists outside the repo and requires the refusal to be for
containment specifically.

If you fix a bug, verify the test by breaking the fix:

```bash
# comment out the guard, then:
make test-unit          # your new test must go red
# restore it:
make test-unit          # green again
```

---

## How the pieces fit

```
cli.py            argument parsing, subcommands, exit codes
├── plan_review.py   critique a plan into proposed contract edits
├── criteria.py      extract / pin / read acceptance criteria
├── verify.py        statuses, evidence checking, verdict derivation
│   └── evidence.py    read cited lines, run cited tests, baseline discrimination
├── debate.py        rounds, judge parsing, follow-up loop
├── streaming.py     one-block-at-a-time display coordinator (shared)
├── run_records.py   versioned, atomic audit/eval records
├── replay.py        re-derive a recorded run's result, calling nothing
├── evals.py         eval-case schema, loading, and scoring
├── models.py        provider capabilities, sessions, subprocess transport
├── prompts.py       prompt construction and blinding
├── schema_files.py  scoped temp schema files for file-based providers
├── display.py       terminal output only — no logic
└── doctor.py        install / auth / capability checks
```

Three invariants worth keeping:

**`display.py` holds no logic.** It renders dicts. Anything that decides
something belongs in `verify.py` or `plan_review.py`, where it can be tested
without capturing stdout.

**The verdict half stays pure.** `assemble_criteria_results`, `apply_downgrades`,
`finalize_verify`, and their plan-review counterparts (`intake_reports`,
`finalize_plan_review`) take raw provider text and return a result — no
subprocesses, no filesystem, no clock. Everything that touches a working tree
lives in `gather_evidence` and `evidence.py`. That split is what `replay.py`
stands on: put a file read into the pure half and replaying a record silently
starts depending on the repository it is replayed in.

**`_build_cmd` is deterministic in its arguments.** Every provider flag decision
is a pure function of `(Session, prompt, via_stdin, schema)`, which is what makes
`tests/unit/test_build_cmd.py` able to lock all of them down. The one injected
impurity is `schema_path_writer`, and the caller owns that file's lifetime.

### Adding a provider

1. Add a `Provider` entry in `models.py` declaring what it can actually do —
   read-only mode, config isolation (and how much of it, via `isolate_scope`),
   structured output, session pinning, whether it will read a prompt on stdin,
   whether its stream carries token counts. Do not claim a capability it lacks;
   `arbiter doctor` reports these verbatim and the asymmetry between providers is
   deliberate documentation. Set `default_panel=False` if it should be supported
   but not assumed — a provider that cannot be controlled (no structured output,
   no config isolation) or that blocks on interactive input does not belong in a
   panel nobody asked for. It stays selectable by name either way.
2. Add a branch to `_parse_event` mapping its stream to
   `session` / `delta` / `done` / `error` / `usage`. Note that the discriminator
   key is not universal — `agy` keys on `event` where the others key on `type`.
3. Add a branch to `parse_usage` if it reports counts, normalizing to the shared
   field names. Never synthesize a total the provider did not give.
4. Add a branch to `_build_cmd` for the argv shape, including its stdin form.
5. Add it to `PROVIDER_CLIS` and an `<name>_events()` helper in
   `tests/conftest.py`.
6. Paste a **real** captured event into `tests/unit/test_provider_streams.py`.
   Every shape there came off the live CLI; that is what makes a provider
   renaming a field a red test rather than an empty answer.

The parametrized tests in `test_build_cmd.py`, `test_provider_flags.py`, and
`test_doctor.py` pick up new providers automatically from `PROVIDERS`.

### Why the fixture replaces `PATH` instead of prepending to it

`shim_path` sets `PATH` to its own bin directory plus the minimum the suite needs
(git). Prepending would leave the developer's real `claude` and `gemini`
reachable, so any code asking "is this installed?" finds one — and the suite
starts making paid calls, or hangs for three minutes on an unauthenticated CLI's
login prompt. That is not hypothetical: it happened the moment `_resolve_models`
learned to consult `PATH`.

---

## Debugging

```bash
arbiter verify --plan plan.md --diff -v      # print every provider command
arbiter verify --plan plan.md --diff --json  # progress to stderr, result to stdout
NO_COLOR=1 arbiter ...                       # plain output
```

`--json` moves all human-facing output to stderr, so `arbiter verify --json | jq`
works. If you ever see progress text in a JSON pipe, that's a bug in `display.py`
— something is writing to real stdout instead of going through `display.print`.

Provider errors are unwrapped from the JSON-in-JSON they arrive in, so a failure
should name its actual cause ("requires a newer version of Codex") rather than
transport noise. If you get transport noise instead, `_unwrap_provider_error`
needs another case.

---

## Before you push

```bash
make check
```

That runs exactly what CI runs: `ruff check`, `ruff format --check`, and the full
suite on ubuntu and macOS across Python 3.12 and 3.13.

`make typecheck` runs `mypy --strict`, which is **not** enforced in CI yet and
will report findings. It is on the roadmap, not the gate.

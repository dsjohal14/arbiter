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
| `tests/unit/` | 177 | ~5s | Pure functions: parsing, verdict logic, prompt construction |
| `tests/integration/` | 70 | ~30s | Real subprocesses, real git worktrees, the CLI through `main()` |

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
    shim_path(claude_events("hello"))                    # same reply for all
    shim_path(per_provider={"claude": claude_events("x")})  # or per provider
    shim_path(hang=60)                                   # never exits
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
safe to point this at your own repo.

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
├── models.py        provider capabilities, sessions, subprocess transport
├── prompts.py       prompt construction and blinding
├── schema_files.py  scoped temp schema files for file-based providers
├── display.py       terminal output only — no logic
└── doctor.py        install / auth / capability checks
```

Two invariants worth keeping:

**`display.py` holds no logic.** It renders dicts. Anything that decides
something belongs in `verify.py` or `plan_review.py`, where it can be tested
without capturing stdout.

**`_build_cmd` is deterministic in its arguments.** Every provider flag decision
is a pure function of `(Session, prompt, via_stdin, schema)`, which is what makes
`tests/unit/test_build_cmd.py` able to lock all of them down. The one injected
impurity is `schema_path_writer`, and the caller owns that file's lifetime.

### Adding a provider

1. Add a `Provider` entry in `models.py` declaring what it can actually do —
   read-only mode, config isolation, structured output, session pinning. Do not
   claim a capability it lacks; `arbiter doctor` reports these verbatim and the
   asymmetry between providers is deliberate documentation.
2. Add a branch to `_parse_event` mapping its stream to
   `session` / `delta` / `done` / `error`.
3. Add a branch to `_build_cmd` for the argv shape, including its stdin form.
4. Add it to `tests/conftest.py` as an `<name>_events()` helper.

The parametrized tests in `test_build_cmd.py` and `test_provider_flags.py` pick
up new providers automatically from `PROVIDERS`.

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

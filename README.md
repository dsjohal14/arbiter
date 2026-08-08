# Arbiter

[![CI](https://github.com/dsjohal/arbiter/actions/workflows/ci.yml/badge.svg)](https://github.com/dsjohal/arbiter/actions/workflows/ci.yml)

> Did the implementation actually keep the plan's promises?

An agent wrote the plan. An agent wrote the code. Arbiter checks the contract
between them — criterion by criterion, with evidence it verifies itself.

```bash
arbiter plan pin plan.md          # extract acceptance criteria, once
arbiter verify --plan plan.md --diff
```

```
╔══════════════════════════════════════════╗
║  ✗  VERDICT: FAIL                        ║
╚══════════════════════════════════════════╝

  met 1   untested 1   unmet 1   unverifiable 0

━━━  ACCEPTANCE CRITERIA  ━━━━━━━━━━━━━━━━━━━━

  ✗ AC-1  UNMET  total() sums every item in the list
       Slicing from index 1 drops the first element.
       code: app.py:2  ✓
         │ return sum(items[1:])
       test: tests/test_app.py::test_sums_all  ✗
         │ failed
         │ E   assert 2 == 3

  ~ AC-2  UNTESTED  an empty list still returns 0
       Behaviour is unchanged, but nothing covers it.
       code: app.py:2  ✓
         │ return sum(items[1:])
       downgraded from met: no test cited

  ✓ AC-3  MET  unknown providers exit with a usage error
       code: src/arbiter/cli.py:197  ✓
         │ _usage_error(f"unknown provider(s): ...")
       test: tests/integration/test_cli.py::test_unknown_provider  ✓
         │ passed
```

Every criterion gets a row — including the ones that are fine. That is the point:
a review tool that returns an empty list for a clean diff gives you no way to
tell "everything checks out" from "nobody actually looked."

---

## What is mechanical, and what isn't

Reviewers cite evidence. Arbiter **checks the citations itself**:

- a cited `file:line` is read back and quoted next to the claim, so a fabricated
  reference is visible immediately
- a cited test is **executed**, and its result reported

Where a fact contradicts a reviewer, the fact wins and the status is lowered. A
reviewer's verdict is a ceiling, never a floor:

| The reviewer said | Reality | Result |
|---|---|---|
| `met`, cites a test | the test fails | **`unmet`** |
| `met`, cites a test | no such test | `untested` |
| `met`, no test cited | — | `untested` |
| `met`, cites `app.py:900` | file has 40 lines | `unverifiable` |
| `unmet` | test passes | stays `unmet` |

### The honest limit

Running a cited test proves **the test passes**. It does not prove the test
*exercises that criterion*. A reviewer — or an injected diff — can cite any green
test in the repo and every criterion will light up.

So Arbiter separates the two and never conflates them:

| Claim | Established by |
|---|---|
| the cited line exists, and says this | **mechanically verified** |
| the cited test exists and passes | **mechanically verified** |
| that test covers this criterion | **model- or human-assessed**, never mechanically proven |

Output always labels which you are looking at:

```
test: tests/test_app.py::test_sums_all  ✓  (relevance model-assessed)
test: tests/test_app.py::test_empty_list  ✓  (selected in plan; relevance human-assessed)
```

To record a human-reviewed test selection, annotate it in the plan:

```markdown
- **AC-2** — an empty list still returns 0  [proof: tests/test_app.py::test_empty_list]
```

The plan's selection overrides whatever the reviewer cites. This records human
approval; it does not make semantic relevance mechanically decidable. With
`--require-pinned-evidence`, a model-chosen test cannot reach `met`, and an
`untested` row fails the gate without separately passing `--require-tests`.

If the diff *rewrites* a test the plan already selected, the approval is stale
and Arbiter refuses `met` — in every mode, not just strict. A test the diff
merely *adds* is fine: it was named in the contract before the code existed,
which is the normal workflow.

### Does the test prove anything?

A test can pass for the wrong reason. `assert True` passes everywhere, and so
does a test that never exercises the change. Point Arbiter at a base revision
and it re-runs each cited test against the old code:

```bash
arbiter verify --plan plan.md --diff origin/main...HEAD --require-discriminating-tests
```

```
test: tests/test_app.py::test_sums_all  ✓  (relevance model-assessed)
base: ✓ fails against origin/main, passes here
```

Fails at base and passes now → the change is what made it pass. Passes at base
too → it proves nothing about this change. That second case is *reported* by
default rather than punished, because a vacuous test and a legitimate regression
guard are indistinguishable from outside; `--require-discriminating-tests` turns
it into a failure when you want the stricter reading.

The base revision is inferred from `--diff` (`origin/main...HEAD` → `origin/main`,
`staged` → `HEAD`) or set with `--base`. A diff read from a file or stdin has no
base, so the check is skipped rather than guessed. It runs in a throwaway git
worktree; your working tree is never touched.

---

## Statuses and the verdict

| Status | Meaning |
|---|---|
| `met` | implemented, and a cited test was run and passed¹ |
| `untested` | implemented, but nothing proves it |
| `unmet` | not implemented, or the diff contradicts it |
| `unverifiable` | can't be determined from the plan, diff, and repo |

¹ That the test *covers* the criterion is model- or human-assessed — see
[the honest limit](#the-honest-limit).

The verdict is derived mechanically. No model gets a vote:

| Condition | Verdict | Exit |
|---|---|---|
| any `unmet` | `fail` | `1` |
| any `untested`, with `--require-tests` | `fail` | `1` |
| model-chosen evidence, with `--require-pinned-evidence` | `fail` | `1` |
| a test that passes at base too, with `--require-discriminating-tests` | `fail` | `1` |
| any `unverifiable` | `unclear` | `3` |
| a reviewer failed to complete | `unclear` | `3` |
| otherwise | `pass` | `0` |

Also `2` usage error and `4` runtime error. `unclear` has its own code so CI can
tell "the reviewers weren't confident" from "you called it wrong."

A partial reviewer panel can never produce a clean pass — the reviewers that
didn't run are exactly the ones that might have found something.

---

## Install

```bash
git clone https://github.com/dsjohal/arbiter && cd arbiter
uv tool install .        # or: pip install -e .
arbiter doctor
```

No API keys and no Python dependencies — Arbiter drives CLI tools you already
have and are already logged into.

| Tool | Install |
|------|---------|
| Claude Code | [code.claude.com](https://code.claude.com) |
| Codex CLI | `npm install -g @openai/codex` |
| Gemini CLI | [developers.google.com/gemini-cli](https://developers.google.com/gemini-cli) |

`arbiter doctor` checks each is installed **and authenticated**, and reports what
it can actually do. Presence isn't enough: a CLI can sit on `PATH` and still
refuse to serve you.

---

## Usage

```bash
# 1. Pin the criteria into the plan (once, then review and edit them)
arbiter plan pin plan.md

# 2. Gate a change against them
arbiter verify --plan plan.md --diff                      # staged
arbiter verify --plan plan.md --diff working              # unstaged
arbiter verify --plan plan.md --diff origin/main...HEAD   # a revision range
git diff | arbiter verify --plan plan.md --diff -         # stdin
```

In CI:

```yaml
- run: arbiter verify --plan plan.md --diff origin/main...HEAD --json > verdict.json
```

In a pre-commit hook:

```bash
arbiter verify --plan plan.md --diff --require-tests || exit 1   # your own code
```

### Why criteria are pinned

`plan pin` extracts the criteria once and writes them into the plan between
comment fences, as `AC-1`, `AC-2`, … Every later run *reads* that list rather
than re-deriving it.

If a model re-extracted criteria on each run, two runs would enumerate different
promises and the "mechanical" verdict would quietly rest on a non-deterministic
step — which is the whole thing that separates this from a review tool. Pinning
also makes the criteria diffable: a change to what was promised shows up in code
review like any other change.

The extracted list is a starting point. Read it and edit it — it's your contract.

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--plan` | — | Plan file (or `-` for stdin). Required by `verify`. |
| `--diff` | `staged` | Bare, or `working`, a revision range, a path, or `-` |
| `--require-tests` | off | Treat `untested` as a failure |
| `--require-pinned-evidence` | off | Require plan-selected tests; implies `--require-tests` |
| `--require-discriminating-tests` | off | Reject cited tests that pass against the base too |
| `--base` | inferred | Base revision for the discrimination check |
| `--no-run-tests` | off | Check cited tests exist, don't execute them |
| `--no-scrub-test-env` | off | Run cited tests with the full environment |
| `--test-command` | autodetect | Test runner (only pytest is autodetected) |
| `--models` | all | Comma-separated reviewers |
| `--judge` | `claude` | Who consolidates |
| `--seed` | random | Reproduces an earlier run's blinding exactly |
| `--json` | off | Result on stdout, progress on stderr |
| `--allow-tools` | off | Let providers use their full toolset |

### Also: `arbiter debate`

The original mode is still here — several models answer, optionally refine across
rounds, and a blinded arbiter picks the best.

```bash
arbiter debate "Which sorting algorithm for mostly-sorted data?" --rounds 2
```

---

## Trust boundary

**Arbiter is not a defense against a hostile pull-request author, and running it
on untrusted code is dangerous.**

Two distinct risks:

**1. Prompt injection.** A diff is attacker-controllable text that goes into a
model's context. Untrusted content is fenced and labelled as data, which removes
the naive version of the attack, but no prompt-level measure makes this safe
against someone deliberately trying to talk a reviewer into a pass.

**2. Code execution.** This is the serious one. Checking test evidence *runs a
test from the branch under review*. A hostile PR can put arbitrary Python in
that test and get whatever the invoking process has — credentials, filesystem,
network. Arbiter scrubs directly inherited environment variables by default (only
`PATH`, `HOME`, `LANG` and similar survive; disable with `--no-scrub-test-env`),
but **that is accidental-exposure reduction, not a sandbox or malicious-code
defense.**

### Running it on code you don't trust

```bash
arbiter verify --plan plan.md --diff origin/main...HEAD --no-run-tests
```

`--no-run-tests` checks that cited tests exist without executing them. Do not use
`--require-tests` on untrusted input — it pushes reviewers toward citing tests,
and every citation becomes an execution. If you need test evidence from untrusted
branches, run Arbiter in a throwaway container with no credentials mounted.

For code you or your agents wrote, this is a gate. For code from strangers, run
it with `--no-run-tests` and treat the result as a signal.

---

## Design notes

**Reviewers are blinded.** An earlier version showed the judge
`Answer from claude [CLAUDE]` — an Anthropic model grading an Anthropic answer
against competitors', labels visible. Reports are relabelled A/B/C and shuffled;
the seed and mapping are persisted so runs stay reproducible and attributable
without the judge ever seeing a vendor name.

**Reviewers are isolated, not disabled.** These CLIs are agents with filesystem
access. Left alone, each silently inherits its own ambient config — `CLAUDE.md`,
`AGENTS.md`, skills, hooks, MCP servers — so reviewers answer under different,
undisclosed system prompts and the comparison isn't controlled. Arbiter defaults
to read-only mode with user config ignored, and passes an explicit manifest of
files in scope. It does *not* blanket-disable tools: a reviewer that can't read
the repo can't verify code.

**Providers are not equivalent, and the code says so.** Each declares its
capabilities and `arbiter doctor` reports them:

| | Read-only | Ignores user config | Structured output | Session pinning |
|---|---|---|---|---|
| `claude` | ✓ | ✓ `--safe-mode` | ✓ | pre-assigned UUID |
| `codex` | ✓ | ✓ partial | ✓ | provider-minted |
| `gemini` | ✓ | ✗ none | ✗ prose fallback | pre-assigned UUID |

Codex is isolated with `--ignore-user-config` but deliberately *not*
`--ephemeral`, which would stop it persisting sessions and break resume.

**Codex does not stream.** It emits its whole message in one event; Claude and
Gemini stream incrementally. Arbiter renders one block at a time either way, so
output never interleaves.

**Large prompts go on stdin.** A plan plus a real diff exceeds OS argument
limits — `ARG_MAX` is 1 MiB on macOS, and Linux caps any single argument at
128 KiB regardless — so past 32 KiB the prompt is written to the child's stdin.

---

## Project structure

```
src/arbiter/
├── cli.py           # subcommands, stdin, exit codes
├── criteria.py      # extract, pin, and read acceptance criteria
├── verify.py        # the gate: prompts, statuses, verdict derivation
├── evidence.py      # mechanical checking — read lines, run tests
├── debate.py        # debate rounds, judge parsing, follow-up loop
├── streaming.py     # parallel block-streamed display coordinator
├── models.py        # provider capabilities, sessions, subprocess transport
├── prompts.py       # prompt construction and blinding
├── schema_files.py  # scoped temp schema files for file-based providers
├── display.py       # terminal output only — zero logic
└── doctor.py        # install / auth / capability checks
```

Adding a provider is one `Provider` entry plus a branch in the event parser.

---

## Development

```bash
uv pip install -e . pytest pytest-asyncio pytest-cov ruff
pytest          # 209 tests
ruff check src/ tests/
```

Subprocess behaviour is tested against a **real fake CLI placed on `PATH`**, not
by mocking `asyncio.create_subprocess_exec`. That's deliberate: the 64 KiB line
limit that used to crash long answers lives inside the real `StreamReader`, so a
mocked seam would pass a test that production fails. Same for exit codes, SIGTERM
escalation, argv size limits, and stderr backpressure.

---

## Non-goals

- **Arbiter does not write code.** It adjudicates between agents that do.
- **Not a code-review council.** Ungrounded findings are a secondary output;
  the primary unit is a promise, not a comment.
- **No custom repo retrieval** (tree-sitter, embedding indexes). The underlying
  CLIs already do this, better.
- **No patch-format plumbing.** Three CLIs, three diff dialects, hunk offsets
  that rot.
- **No Rust rewrite.** The workload is subprocesses blocked on network I/O; the
  process is idle ~99.9% of wall-clock.

---

## Roadmap

- [ ] Validate against 15–20 real AI-generated changes with seeded defects;
      measure confirmed findings, false positives, latency, and cost
- [ ] Test-runner support beyond pytest (jest, cargo, go test)
- [ ] Record/replay of provider streams for deterministic, zero-cost reruns
- [ ] Per-run cost and token accounting (the data is already in the stream)
- [ ] Pre-commit hook and GitHub Action packaging

---

## License

MIT

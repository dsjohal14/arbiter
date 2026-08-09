# Arbiter

[![CI](https://github.com/dsjohal14/arbiter/actions/workflows/ci.yml/badge.svg)](https://github.com/dsjohal14/arbiter/actions/workflows/ci.yml)

> The contract layer between an AI coding plan and the code an agent ships.

Before coding, Arbiter finds the holes in the plan. After coding, it checks
whether the agent kept every promise.

```bash
arbiter plan review plan.md    # is this plan good enough to build from?
arbiter plan pin plan.md       # this is the approved contract
arbiter verify --plan plan.md --diff   # did the implementation keep it?
arbiter replay run.json        # re-derive that verdict for free
```

The failure mode this exists for is familiar: an agent writes a good-looking
plan, implements most of it, reports success, and quietly skips an edge case, a
migration step, or a security constraint.

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

## Before the code: `arbiter plan review`

A vague plan doesn't fail loudly — it produces an agent that picks an
interpretation and calls it done. Plan review finds those spots while they're
still cheap, and **every finding is a proposed edit to the acceptance criteria,
not a paragraph of concern**:

```
╔══════════════════════════════════════════╗
║  !  PLAN: 5 BLOCKING                     ║
╚══════════════════════════════════════════╝

  ambiguous 2   missing 3   needs_human 3

  3. + ADD missing
       The plan never states how a 'client' is identified for counting, so an
       implementer could key on a spoofable header and let attackers bypass
       the limiter entirely.
       plan says: "Store the counters somewhere fast."
       criterion: Requests are attributed to a client using a non-spoofable
                  identifier, and forged client-identifying headers cannot be
                  used to evade the limit.
       test:      tests/integration/test_rate_limiter.py::test_spoofed_client_header_does_not_bypass_limit

  6. ? DECIDE needs_human BLOCKING
       The author must fix the request-rate threshold, the time window, and the
       dimension being limited; 'a reasonable number of requests' gives an
       implementer nothing to build or test against.
```

For an unpinned plan, Arbiter first extracts a complete baseline contract in
memory, then asks the reviewers to edit it. `--apply` writes that full revised
contract into the plan; it never pins only the reviewers' additions. The command
exits non-zero while blocking problems remain, so an agent can be told not to
start until it passes.

| Kind | Blocks? | Meaning |
|---|---|---|
| `contradiction` | ✓ | two requirements that cannot both hold |
| `ambiguous` | ✓ | an agent will pick an interpretation and call it done |
| `untestable` | ✓ | no test could settle whether it was met |
| `needs_human` | ✓ | a decision a model shouldn't make on your behalf |
| `missing` | — | an edge case, error path, migration, or constraint |
| `unnecessary` | — | scope that doesn't belong in this change |

**This is also what makes strict verification affordable.** Naming the test that
proves each criterion is tedious to retrofit — here it falls out of reviewing the
plan, and it happens *before the code exists*, which is the only moment at which
you can't pick whichever test happens to be green.

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

If the diff *rewrites the selected test node*, the approval is stale and Arbiter
refuses `met` — in every mode, not just strict. A new test is fine even when it
is appended to an existing test module: it was named in the contract before the
code existed, which is the normal workflow.

This staleness check is intentionally node-local. Rewriting a fixture or helper
called by the selected test does not automatically revoke the plan selection;
review those transitive test changes like any other change to trusted evidence.

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
it into a failure when you want the stricter reading. Strict mode also fails
closed when the base cannot be checked, the test cannot collect there, or the
baseline run times out.

The base revision is inferred from `--diff` (`origin/main...HEAD` → `origin/main`,
`staged` → `HEAD`) or set with `--base`. A diff read from a file or stdin has no
base, so `--require-discriminating-tests` requires an explicit `--base`; combining
that strict flag with `--no-run-tests` is also a usage error. Both are rejected
before any model calls. Baseline execution runs in a throwaway git worktree; your
working tree is never touched. For Python projects, the runner removes editable
paths for the active checkout and puts the worktree's `src/`, `lib/`, and
repository root first, so it cannot silently import HEAD for both runs.

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
| the base test cannot be checked, with `--require-discriminating-tests` | `fail` | `1` |
| any `unverifiable` | `unclear` | `3` |
| a reviewer failed or returned an incomplete review | `unclear` | `3` |
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

| Tool | Install | |
|------|---------|---|
| Claude Code | [code.claude.com](https://code.claude.com) | default |
| Codex CLI | `npm install -g @openai/codex` | default |
| Antigravity (`agy`) | [antigravity.google](https://antigravity.google) | default |
| Gemini CLI | [developers.google.com/gemini-cli](https://developers.google.com/gemini-cli) | opt-in |

You don't need all of them. With no `--models`, the panel is whichever **default**
providers are installed, and the ones that aren't are named rather than sent off
to fail one by one. Ask for one by name and it stays strict — a reviewer you
requested and didn't get is a missing opinion, so the verdict can't be a clean
pass.

**Gemini is opt-in, not unsupported.** It's the weakest panel member — no
structured output, no config isolation — and its CLI blocks on an interactive
login that a headless run can't answer, which turns a default run into a timeout
instead of a verdict. It's still fully wired up; name it and it works:

```bash
arbiter verify --plan plan.md --diff --models claude,codex,gemini
arbiter doctor --all          # include opt-in providers in the health check
```

`arbiter doctor` checks each is installed **and authenticated**, and reports what
it can actually do. Presence isn't enough: a CLI can sit on `PATH` and still
refuse to serve you — and one that needs a login will sit there asking a
question no headless run can answer. Doctor quotes the question back at you.
Opt-in providers aren't probed unless you pass `--all`: probing costs a real
call, and a provider you've chosen not to use shouldn't be what makes the health
check fail.

---

## Usage

```bash
# 1. Critique the plan, and apply the proposed criteria
arbiter plan review plan.md --apply

# 2. Or extract criteria from a plan you're happy with
arbiter plan pin plan.md

# 3. Gate a change against them
arbiter verify --plan plan.md --diff                      # staged
arbiter verify --plan plan.md --diff working              # unstaged
arbiter verify --plan plan.md --diff origin/main...HEAD   # a revision range
git diff | arbiter verify --plan plan.md --diff -         # stdin
```

Keep a versioned run record when you want an audit trail, a way to re-derive a
verdict for free, or an input for an eval corpus:

```bash
mkdir -p .arbiter/runs
arbiter plan review plan.md --record .arbiter/runs/plan-review.json
arbiter verify --plan plan.md --diff origin/main...HEAD \
  --record .arbiter/runs/verify.json
```

The record embeds the exact plan and diff with SHA-256 digests, normalized
configuration, every extractor/reviewer/consolidator final response or failure,
per-call token counts and cost where the provider reports them, evidence and
blinding observations, the final result, timing, and exit code. Verify records
also hash every Git-visible file in the working tree (or every file in a
non-Git tree except standard test/cache artifacts) so eval scoring can bind the
result to unchanged tests, fixtures, helpers, and imports as well as files named
by the diff. Files are hashed as raw bytes with bounded, streaming reads; file
contents are not copied into the record. `schema_version` makes the format an
explicit compatibility boundary.
Provider command lines, session IDs, environment variables, and the value of a
custom test command are not serialized.

Token counts are normalized across providers (`input_tokens`, `output_tokens`,
`cached_input_tokens`, `cache_write_tokens`, `reasoning_tokens`, `total_tokens`,
`cost_usd`) and every field is nullable, because no two CLIs report the same set.
A total is only ever the provider's own figure — summing the parts would invent
a number whose billing meaning is unknown, since cache reads and writes are
priced differently. A provider Arbiter can't read counts from records `null`,
which stays distinguishable from a call that consumed nothing.

Run records are opt-in because they are **sensitive**: plans, diffs, provider
responses, and error text can contain proprietary code or secrets. The files are
created with owner-only permissions and `.arbiter/runs/` is ignored in this repo;
add the same path to the target project's `.gitignore` before using it there.

A record is not signed and is not an authenticity proof. `replay --check`
means current deterministic logic reproduced the expected output from the
recorded inputs; someone who rewrites the inputs and expected output together
can forge that claim. Runtime provenance is also not yet bound: the record keeps
whether a custom runner and scrubbed environment were used, but not the runner
command, interpreter, installed dependencies, or operating-system state. Run
eval corpora in a controlled environment when comparing measurements.

### `arbiter replay` — the same verdict, for nothing

Everything downstream of a provider's response is deterministic: parsing,
merging, the evidence downgrade rules, the roll-up. Replay re-runs that half from
a record, so a change to how Arbiter reads a response can be checked against runs
that already happened — no providers installed, no tokens spent.

```bash
arbiter replay .arbiter/runs/verify.json           # exits with the replayed verdict
arbiter replay .arbiter/runs/verify.json --check   # exits 0 only if it still reproduces
```

`replay` takes one record. A corpus of them is a regression test for Arbiter's
own logic:

```bash
for r in .arbiter/runs/*.json; do
  arbiter replay "$r" --check >/dev/null || echo "drift: $r"
done
```

The comparison is **exhaustive**, not a summary: every field of the result is
compared, so a changed finding severity, a rewritten proposed criterion, or a
dropped citation is drift even when the verdict and every criterion status hold.
Evidence checks and the blinding map are stored separately as replay inputs, so
their corresponding result fields are covered by that comparison too.

What replay does **not** do is re-check evidence. Whether a cited line exists or
a cited test passes is a fact about a working tree. Those observations are
restored from the record and the downgrade rules applied to them again. The
workspace digest can bind eval scoring to the same tree, but it cannot recreate
the tree or re-run the test. Replay answers "does today's logic reach the
recorded verdict from the recorded responses?", not "is that verdict still true
of this repo today".

Record schemas v1 and v2 stored those replay inputs inside the result itself.
They remain readable for zero-cost replay, but cannot pass `--check`: only v3+
can certify that the independently restored inputs reproduce the expected
output exactly.

### `arbiter eval` — how often is it right?

An eval case is one run whose right answer a human wrote down: the plan, the
diff, and an `expected.json` naming the status each criterion should get and the
findings that must (or must not) appear. Scoring replays a record, so labelling a
case costs one paid run and every scoring after it is free.

```bash
arbiter eval validate evals/*/                        # is the corpus well formed?
arbiter eval score evals/<case> --record run.json     # exits 0 when the run matches
```

```
  ✗ totals-drops-first-element  FAIL  verify
       verdict:  expected fail, got pass
       criteria: 50%
       ✗ AC-1: expected unmet, got met
```

Format, workflow, and the starter cases: [`evals/README.md`](evals/README.md).

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
| `--apply` | off | (`plan review`) Write the proposed criteria into the plan |
| `--plan` | — | Plan file (or `-` for stdin). Required by `verify`. |
| `--diff` | `staged` | Bare, or `working`, a revision range, a path, or `-` |
| `--require-tests` | off | Treat `untested` as a failure |
| `--require-pinned-evidence` | off | Require plan-selected tests; implies `--require-tests` |
| `--require-discriminating-tests` | off | Reject cited tests that pass against the base too |
| `--base` | inferred | Base revision for the discrimination check |
| `--no-run-tests` | off | Check cited tests exist, don't execute them |
| `--no-scrub-test-env` | off | Run cited tests with the full environment |
| `--test-command` | autodetect | Test runner (only pytest is autodetected) |
| `--record` | off | (`plan review`, `verify`) Write a sensitive, versioned JSON run record |
| `--check` | off | (`replay`) Exit 0 only if the replay reproduces the record |
| `--models` | installed defaults | Comma-separated reviewers; naming one requires it |
| `--judge` | `claude` if installed | Who consolidates |
| `--all` | off | (`doctor`) Also check opt-in providers |
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

**3. Run-record disclosure.** `--record` intentionally copies the plan, diff,
model responses, and errors into one JSON file. Owner-only file permissions
reduce accidental local exposure, but do not make the contents safe to commit,
upload, or share. A model response or error can repeat sensitive input even when
Arbiter does not serialize credentials or environment variables directly.

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

| | Read-only | Ignores user config | Structured output | Session pinning | Token usage |
|---|---|---|---|---|---|
| `claude` | ✓ | ✓ `--safe-mode` | ✓ | pre-assigned UUID | ✓ + cost |
| `codex` | ✓ | ✓ partial | ✓ | provider-minted | ✓ |
| `antigravity` | ✓ | ~ skills only | ✓ | provider-minted | ✓ |
| `gemini` *(opt-in)* | ✓ | ✗ none | ✗ prose fallback | pre-assigned UUID | ✗ |

That bottom row is why gemini is off the default panel: two of the four
properties this tool leans on are simply absent there. It stays supported
because "we can't control it" is a reason to disclose, not to delete.

Codex is isolated with `--ignore-user-config` but deliberately *not*
`--ephemeral`, which would stop it persisting sessions and break resume.
Antigravity's `--disable-slash-commands` silences skills and slash commands but
leaves project instruction files in place, so `doctor` says "isolates slash
commands and skills only" rather than claiming parity.

**Codex does not stream.** It emits its whole message in one event; Claude and
Gemini stream incrementally. Arbiter renders one block at a time either way, so
output never interleaves.

**Antigravity can't take a prompt on stdin.** Its `--print` requires the value in
argv, and a bare `--print` swallows the next flag as the prompt. So its prompt
always goes in argv, and a plan plus a very large diff can exceed what `execve`
will carry — which is reported as exactly that rather than as a launch failure.

**Large prompts go on stdin.** A plan plus a real diff exceeds OS argument
limits — `ARG_MAX` is 1 MiB on macOS, and Linux caps any single argument at
128 KiB regardless — so past 32 KiB the prompt is written to the child's stdin.

---

## Project structure

```
src/arbiter/
├── cli.py           # subcommands, stdin, exit codes
├── plan_review.py   # critique a plan into proposed contract edits
├── criteria.py      # extract, pin, and read acceptance criteria
├── verify.py        # the gate: prompts, statuses, verdict derivation
├── evidence.py      # mechanical checking — read lines, run tests
├── debate.py        # debate rounds, judge parsing, follow-up loop
├── streaming.py     # parallel block-streamed display coordinator
├── run_records.py   # versioned, atomic audit/eval records
├── replay.py        # re-derive a recorded run's result, calling nothing
├── evals.py         # eval-case schema, loading, and scoring
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
make venv && make install
make check          # lint + format + the full suite, in CI's order
make test-unit      # ~5s, for iterating
make demo           # run the gate on a seeded defect (real provider calls)
```

`make` on its own lists every target. See **[DEVELOPMENT.md](DEVELOPMENT.md)**
for the test architecture, how to add a provider, and debugging.

Subprocess behaviour is tested against a **real fake CLI placed on `PATH`**, not
by mocking `asyncio.create_subprocess_exec`. That's deliberate: the 64 KiB line
limit that used to crash long answers lives inside the real `StreamReader`, so a
mocked seam would pass a test that production fails. Same for exit codes, SIGTERM
escalation, argv size limits, and stderr backpressure. No test calls a real
provider — `make check` costs nothing and needs no login.

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

- [x] Versioned records of inputs, provider outcomes, results, and timing
- [x] Replay recorded provider outcomes for deterministic, zero-cost reruns
- [x] An eval-case format, and scoring a run against a hand-labelled answer key
- [ ] Grow the corpus to 15–20 labelled cases from real agent implementations,
      and publish the numbers: missed requirements, false clean passes,
      actionable-vs-wrong plan findings, latency, cost
- [ ] An Arbiter agent skill, so you can say "review this plan before
      implementing" and "don't declare it done until the gate passes"
- [ ] Test-runner support beyond pytest (jest, cargo, go test)
- [x] Per-call token counts, and cost where the provider reports it
- [ ] Roll those up into a per-run cost summary and a corpus-wide one
- [ ] Pre-commit hook and GitHub Action packaging

---

## License

MIT

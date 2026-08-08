# Arbiter

[![CI](https://github.com/dsjohal/arbiter/actions/workflows/ci.yml/badge.svg)](https://github.com/dsjohal/arbiter/actions/workflows/ci.yml)

> A falsification-first verification gate for AI-generated plans and code changes.

An agent wrote the plan. An agent wrote the code. Who checks that the second one
did what the first one promised?

Arbiter runs several independent models against your diff and your approved plan,
asks each one to **falsify** the change rather than praise it, and derives a
`pass` / `fail` / `unclear` verdict mechanically from the findings that survive.
Every finding must cite a file, a line, a command, or a failing test. A finding
with no evidence is dropped before it can block anything.

The exit code is stable, so it drops straight into pre-commit or CI.

```bash
arbiter verify --plan plan.md --diff
```

```
━━━  INDEPENDENT REVIEW  ━━━━━━━━━━━━━━━━━━━━━━

  ▶ claude (sonnet)
  │ {"findings":[{"severity":"major","claim":"`_build_cmd` is not a pure
  │ function of (Session, prompt) as criterion 4 requires…

━━━  CONSOLIDATION  ━━━━━━━━━━━━━━━━━━━━━━━━━━━
  claude (opus) is merging and checking findings (blinded)...

╔══════════════════════════════════════════╗
║  ✗  VERDICT: FAIL                        ║
╚══════════════════════════════════════════╝

  blocking 1  major 1  minor 0  question 0

  1. BLOCKING  total() now skips the first item
     criterion: Sum every item
     evidence:  app.py:3
     validate:  pytest tests/test_total.py
```

---

## Why this and not "let a model review my PR"

Generic AI code review tells you what a model thinks of your diff. That is an
opinion, and opinions do not gate merges.

Arbiter answers a narrower, checkable question — *does this implementation
satisfy the plan that was approved?* — and answers it with structure:

| | Generic AI review | Arbiter |
|---|---|---|
| Question asked | "Is this code good?" | "Does this satisfy the approved plan?" |
| Reviewer stance | Assess | **Falsify** — assume it's wrong, try to prove it |
| Findings | Prose | Typed, with evidence and a validation command |
| Unevidenced claims | Reported | Dropped |
| Verdict | A model's judgement | **Derived** from surviving findings |
| Reviewer identity | Visible to the judge | Blinded and shuffled |
| Output | Text | Text + `--json` + stable exit codes |

The consolidating model does not get a vote on the outcome. It merges duplicate
findings and checks whether cited evidence actually holds; the verdict falls out
of the result.

---

## How the verdict is derived

| Condition | Verdict | Exit |
|---|---|---|
| A `blocking` finding whose evidence was confirmed | `fail` | `1` |
| A `blocking` finding nobody could confirm or refute | `unclear` | `3` |
| An acceptance criterion no reviewer could evaluate | `unclear` | `3` |
| Neither of the above | `pass` | `0` |

Also: `2` usage error, `4` runtime error. `unclear` gets its own code so a CI job
can tell "the reviewers were not confident" apart from "you called it wrong."

`unclear` is deliberate. A serious finding nobody could settle should not fail
the build unfairly, and should not quietly pass either.

---

## Install

```bash
git clone https://github.com/dsjohal/arbiter && cd arbiter
uv tool install .        # or: pip install -e .
arbiter doctor
```

No API keys and no Python dependencies — Arbiter drives the CLI tools you already
have installed and are already logged into.

| Tool | Install |
|------|---------|
| Claude Code | [code.claude.com](https://code.claude.com) |
| Codex CLI | `npm install -g @openai/codex` |
| Gemini CLI | [developers.google.com/gemini-cli](https://developers.google.com/gemini-cli) |

`arbiter doctor` checks each one is installed **and authenticated**, and reports
what it can actually be asked to do. Presence is not enough: a CLI can sit on
`PATH` and still refuse to serve you.

---

## Usage

```bash
# Gate the staged diff against an approved plan
arbiter verify --plan plan.md --diff

# From a pipe, as JSON, for CI
git diff main...HEAD | arbiter verify --plan plan.md --diff - --json

# Pick your reviewers and who consolidates
arbiter verify --plan plan.md --diff --models claude,codex --judge claude
```

In a pre-commit hook:

```bash
arbiter verify --plan plan.md --diff || exit 1
```

In GitHub Actions:

```yaml
- run: arbiter verify --plan plan.md --diff origin/main...HEAD --json > findings.json
```

### `arbiter debate`

The original mode is still here: several models answer, optionally refine across
rounds, and a blinded arbiter picks the best.

```bash
arbiter debate "Which sorting algorithm for mostly-sorted data?" --rounds 2
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--plan` | — | Approved plan (file, or `-` for stdin). Required by `verify`. |
| `--diff` | `staged` | `staged`, `working`, a path, or `-` for stdin |
| `--models` | all | Comma-separated reviewers |
| `--judge` | `claude` | Who consolidates |
| `--timeout` | `300` | Per-call timeout in seconds |
| `--seed` | random | Blinding seed; reproduces an earlier run exactly |
| `--allow-tools` | off | Let providers use their full toolset |
| `--json` | off | Machine-readable result on stdout, progress on stderr |
| `--rounds` | `1` | (`debate`) Number of refinement rounds |

---

## Design notes

**Reviewers are blinded.** An earlier version showed the judge
`Answer from claude [CLAUDE]` — an Anthropic model grading an Anthropic answer
against competitors', with the labels visible. Reports are now relabelled A/B/C
and shuffled. The shuffle seed and the label→model mapping are persisted in the
result, so a run stays reproducible and attributable without the judge ever
seeing a vendor name.

**Reviewers are isolated, not disabled.** These CLIs are agents with filesystem
access. Left alone, the Claude reviewer silently ingests your `CLAUDE.md`, Codex
ingests `AGENTS.md`, and each pulls in its own skills, hooks, and MCP servers — so
the reviewers answer under different, undisclosed system prompts and the
comparison isn't controlled. Arbiter defaults to read-only mode with user config
ignored, and passes an explicit manifest of the files in scope. It does *not*
blanket-disable tools: a reviewer that cannot read the repo cannot verify code.

**Providers are not equivalent, and the code says so.** Each provider declares
its capabilities and `arbiter doctor` reports them:

| | Read-only mode | Ignores user config | Structured output | Session pinning |
|---|---|---|---|---|
| `claude` | ✓ | ✓ | ✓ | pre-assigned UUID |
| `codex` | ✓ | ✓ | ✓ | provider-minted |
| `gemini` | ✓ | ✗ | ✗ (prose fallback) | pre-assigned UUID |

**Codex does not stream.** It emits its whole message in one event. Claude and
Gemini stream incrementally. Arbiter renders one model's block at a time either
way, so output never interleaves.

---

## Project structure

```
src/arbiter/
├── cli.py         # subcommands, stdin, exit codes
├── verify.py      # the gate: prompts, findings, verdict derivation
├── debate.py      # debate rounds, judge parsing, follow-up loop
├── streaming.py   # parallel block-streamed display coordinator
├── models.py      # provider capabilities, sessions, subprocess transport
├── prompts.py     # prompt construction and blinding
├── display.py     # terminal output only — zero logic
└── doctor.py      # install / auth / capability checks
```

Adding a provider is one `Provider` entry plus a branch in the event parser.

---

## Development

```bash
uv pip install -e . pytest pytest-asyncio pytest-cov ruff
pytest          # 109 tests
ruff check src/ tests/
```

Subprocess behaviour is tested against a **real fake CLI placed on `PATH`**, not
by mocking `asyncio.create_subprocess_exec`. That is deliberate: the 64 KiB line
limit that used to crash long answers lives inside the real `StreamReader`, so a
mocked seam would pass a test that production fails. Same for exit codes, SIGTERM
escalation, and stderr backpressure.

---

## Non-goals

- **Arbiter does not write code.** It adjudicates between agents that do.
- **No custom repo retrieval** (tree-sitter, embedding indexes). The underlying
  CLIs already do this, better.
- **No patch-format plumbing.** Three CLIs, three diff dialects, hunk offsets
  that rot.
- **No role pipeline** (Planner→Implementer→Reviewer→Critic). That's a prompt
  chain wearing an architecture costume.
- **No Rust rewrite.** The workload is three subprocesses blocked on network I/O;
  the process is idle ~99.9% of wall-clock. Rust's concurrency advantage would
  address a bottleneck that doesn't exist here.

---

## Roadmap

- [ ] Validate against 15–20 real AI-generated changes with seeded defects;
      measure confirmed findings, false positives, latency, and cost against a
      single-model review
- [ ] Record/replay of provider streams for deterministic, zero-cost reruns
- [ ] Per-run cost and token accounting (the data is already in the event stream)
- [ ] `--rubric criteria.yaml` for per-criterion scoring
- [ ] Pre-commit hook and GitHub Action packaging

---

## License

MIT

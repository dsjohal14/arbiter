# Arbiter

> Multiple AI agents debate. An Opus 4.8 arbiter picks the best answer — then keeps refining it with you.

Arbiter is a developer CLI tool that routes your question through **Claude Sonnet**, **Codex**, and **Gemini Pro** in structured debate rounds. Models refine each other's answers, then **Opus 4.8** arbitrates — picking the winner on reasoning quality, not a popularity vote. Output streams live, sessions persist, and after the verdict you can keep talking to the arbiter.

```
╔══════════════════════════════════════════╗
║       🤖  LLM DEBATE ARENA  🤖           ║
╚══════════════════════════════════════════╝

  Debaters:  Claude Sonnet  vs  Codex  vs  Gemini Pro
  Arbiter:   Opus 4.8
  Rounds:    1

  Your prompt: ▌
```

---

## How It Works

1. **Round 1** — all debaters answer your prompt in parallel, streamed live into their own blocks
2. **Round 2..N** *(optional)* — each model sees the others' answers and refines its position (push back, incorporate good points, or stand firm)
3. **Judgment** — the **Opus 4.8** arbiter reads the final answers and picks the best, with a written reason and an improved best answer
4. **Follow-up** — the arbiter keeps its session; reply with anything and it refines its answer. If your follow-up needs fresh debate, it re-polls the debaters and re-judges.

Each model runs as **one persistent CLI session** for the whole run, so context carries across rounds and follow-ups. No API keys needed — Arbiter shells out to the CLI tools you already have installed.

---

## Prerequisites

| Tool | Install | Used for |
|------|---------|----------|
| Claude Code | [code.claude.com](https://code.claude.com) | Sonnet debater + Opus 4.8 arbiter |
| Codex CLI | `npm install -g @openai/codex` | Codex debater |
| Gemini CLI | [developers.google.com/gemini-cli](https://developers.google.com/gemini-cli) | Gemini Pro debater |
| Python 3.12+ | [python.org](https://python.org) | Runtime |

Make sure you're authenticated in each CLI tool before running Arbiter. Run Arbiter from a directory the Gemini CLI trusts (or set `GEMINI_CLI_TRUST_WORKSPACE=true`).

---

## Setup

```bash
git clone https://github.com/dsjohal/arbiter
cd arbiter
```

No pip install needed. Arbiter has no external Python dependencies.

---

## Usage

```bash
# Default: 1 round, all 3 debaters, Opus 4.8 arbitrates
python main.py

# 2 rounds of refinement
python main.py --rounds 2

# Only Claude and Gemini, save the transcript, show raw CLI calls + timing
python main.py --models claude,gemini --save debate.md --verbose

# Use Gemini as the arbiter instead of Opus
python main.py --judge gemini
```

### Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--rounds` | `-r` | `1` | Number of debate rounds |
| `--judge` | `-j` | `claude` | Arbiter model: `claude` (→ Opus 4.8), `codex`, `gemini` |
| `--models` | `-m` | `claude,codex,gemini` | Comma-separated debaters |
| `--save` | | _(off)_ | Write the full transcript (markdown) to a file |
| `--verbose` | `-v` | _(off)_ | Show raw CLI commands and per-call timing |

---

## Project Structure

```
arbiter/
├── main.py       # CLI entry point, arg parsing, prompt input, follow-up loop wiring
├── debate.py     # Orchestration: rounds, streaming coordinator, judge, follow-up loop
├── models.py     # CLI adapters (model config, persistent sessions, streaming) + prompt builders
├── display.py    # All terminal output, ANSI colors, live streaming blocks — zero logic
└── README.md
```

**Design principle:** each file has one job. No display logic in orchestration. No debate logic in the UI.

---

## Architecture

```
User Prompt
    │
    ▼
Debate Orchestrator (debate.py)
    │
    ├── Round 1: asyncio.gather → [Sonnet, Codex, Gemini Pro] stream live in parallel
    │
    ├── Round 2..N: each model resumes its session, sees prior answers, refines
    │
    ├── Arbiter (Opus 4.8) → parse WINNER / REASON / BEST ANSWER
    │
    └── Follow-up loop: arbiter refines, or re-polls the debaters and re-judges
            │
            ▼
       Terminal Output (display.py)
```

Each model is described by a single config entry in `models.py` and driven through one interface:

```python
async def call_session(session: Session, prompt: str, on_delta=..., on_cmd=...) -> ModelResponse
```

The adapter knows how to build that model's first/resume command and parse its streaming events. Adding a new model (Ollama, local LLM, etc.) is a new config entry plus a parse branch — no orchestration changes.

---

## Roadmap

### v2.0 — Arbiter for Dev Work
- [ ] Role-based pipeline: `Planner → Implementer → Reviewer → Critic → Judge`
- [ ] `--context ./src/` flag to pass a file or directory as context
- [ ] Textual TUI with live panels per model
- [ ] SQLite event store for session history and replay
- [ ] Acceptance policy: judge evaluates against a checklist, not just rhetoric

### v3.0 — Full Coding Agent
- [ ] Models return structured patch proposals (not free text)
- [ ] Git worktree sandbox — patches applied in isolation, never to main branch
- [ ] Test runner: `pytest` / `npm test` / `cargo test` as acceptance gates
- [ ] Lint and typecheck gates before declaring a winner
- [ ] ripgrep + tree-sitter based repo context retrieval

### v3.5 — Rust Core
- [ ] Rewrite debate engine in Rust (`tokio` + `ratatui` + `clap`)
- [ ] Single binary distribution, no Python runtime needed

---

## The Insight Behind Arbiter

The idea came while working: I used Claude's responses inside Codex to verify, validate, and refine plans.

The same bet is now showing up at the platform layer. In June 2026, [OpenRouter launched **Fusion**](https://x.com/openrouter/status/2065856853989270011) — it routes a single prompt across 3–5 leading models in parallel, then uses *judge models* to synthesize the competing outputs into one answer. The lesson is the same one Arbiter is built on: **a panel of models that debate and a judge that selects beats any single model answering alone.**

Arbiter does this locally, over the CLI tools you already have — no extra API layer, full transcript in your terminal, and an arbiter you can keep talking to.

---

## License

MIT

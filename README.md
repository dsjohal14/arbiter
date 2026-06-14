# Arbiter

> Multiple AI agents debate. The best answer wins.

Arbiter is a developer CLI tool that routes your question through Claude, Codex, and Gemini in structured debate rounds. Models refine each other's answers, then a judge picks the winner based on reasoning quality — not a popularity vote.

```
╔══════════════════════════════════════════╗
║       🤖  LLM DEBATE ARENA  🤖           ║
╚══════════════════════════════════════════╝

  Debaters:  Claude  vs  Codex  vs  Gemini
  Judge:     Claude
  Rounds:    2

  Your prompt: ▌
```

---

## How It Works

1. **Round 1** — all models answer your prompt in parallel
2. **Round 2..N** — each model sees the others' answers and refines its position (can push back, incorporate good points, or stand firm)
3. **Judgment** — the judge model reads all final-round answers and picks the best, with a written reason

No API keys needed. Arbiter shells out to the CLI tools you already have installed.

---

## Prerequisites

| Tool | Install | Used for |
|------|---------|----------|
| Claude Code | [code.claude.com](https://code.claude.com) | `claude` model |
| Codex CLI | `npm install -g @openai/codex` | `codex` model |
| Gemini CLI | [developers.google.com/gemini-cli](https://developers.google.com/gemini-cli) | `gemini` model |
| Python 3.12+ | [python.org](https://python.org) | Runtime |

Make sure you're authenticated in each CLI tool before running Arbiter.

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
# Default: 2 rounds, all 3 models, Claude judges
python main.py

# 3 rounds, Gemini as judge
python main.py --rounds 3 --judge gemini

# Only Claude and Gemini, 1 round
python main.py --rounds 1 --models claude,gemini

# Only Claude and Codex
python main.py --models claude,codex
```

### Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--rounds` | `-r` | `2` | Number of debate rounds |
| `--judge` | `-j` | `claude` | Which model judges: `claude`, `codex`, `gemini` |
| `--models` | `-m` | `claude,codex,gemini` | Comma-separated list of debaters |

---

## Good Prompts to Try

Arbiter shines on questions with real tradeoffs:

- `Should startups build on microservices or a monolith first?`
- `What is the best way to handle errors in async Python code?`
- `Should I use REST or GraphQL for a new API?`
- `What makes a good code review?`
- `What's the most underrated concept in software engineering?`
- `How should a junior engineer spend their first 90 days at a new job?`

---

## Project Structure

```
arbiter/
├── main.py       # CLI entry point, arg parsing, prompt input
├── debate.py     # Orchestration: rounds, concurrency, judge call
├── models.py     # CLI callers (claude, codex, gemini) + message builders
├── display.py    # All terminal output and ANSI colors — zero logic
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
    ├── Round 1: asyncio.gather → [claude, codex, gemini] in parallel
    │
    ├── Round 2..N: each model sees all prior answers, refines
    │
    └── Judge call → parse WINNER / REASON / BEST ANSWER
            │
            ▼
       Terminal Output (display.py)
```

Each model adapter in `models.py` exposes one interface:

```python
async def call_model(model: str, messages: list[dict]) -> ModelResponse
```

Adding a new model (Ollama, local LLM, etc.) requires only a new function in `models.py` — no orchestration changes.

---

## Roadmap

### v1.1 — Polish (next)
- [ ] Stream output live as models respond (currently waits for full response)
- [ ] `--save debate.md` flag to write full transcript to file
- [ ] `--verbose` flag to show raw CLI timing and output

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

The idea came from [WAT321](https://github.com/WillyDrucker/WAT321) — a VS Code extension with an "Epic Handshake" feature that lets Claude route questions to other models mid-session.

Arbiter takes that idea further: instead of one model asking another, all models debate each other from the start, with structured roles and a judge that applies an acceptance policy rather than picking the most confident-sounding answer.

The architecture Arbiter uses to design its own v2 was itself generated by running Arbiter — Claude, Codex, and Gemini debating "what is the best architecture for a multi-agent coding debate tool?".

---

## License

MIT

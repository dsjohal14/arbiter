# Arbiter — developer tasks.
#
# Everything runs through `$(PY) -m <tool>` rather than the venv's console
# scripts. A venv copied or moved between directories keeps stale shebangs, so
# `venv/bin/pytest` can fail while `venv/bin/python -m pytest` still works.
#
# Override the interpreter if you keep your venv elsewhere:
#     make test VENV=.venv

VENV    ?= venv
PY      := $(VENV)/bin/python
PKG     := arbiter
SRC     := src tests
COV_MIN ?= 80

.DEFAULT_GOAL := help
.PHONY: help venv install test test-unit test-int test-live lint fmt fmt-check \
        typecheck cov cov-html check smoke evals doctor demo demo-plan clean distclean

help: ## Show this help
	@printf "\nArbiter developer tasks\n\n"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'
	@printf "\nInterpreter: $(PY)\n\n"

# ── Setup ────────────────────────────────────────────────────────────────────

venv: ## Create the virtualenv
	python3 -m venv $(VENV)
	@printf "\nCreated $(VENV). Now run: make install\n"

install: ## Install arbiter (editable) plus dev tooling
	@test -x $(PY) || { printf "No interpreter at $(PY). Run: make venv\n"; exit 1; }
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e . pytest pytest-asyncio pytest-cov ruff
	@printf "\nInstalled. Try: make check\n"

# ── Tests ────────────────────────────────────────────────────────────────────

test: ## Run the whole suite
	$(PY) -m pytest tests/

test-unit: ## Unit tests only (~5s — use this while iterating)
	$(PY) -m pytest tests/unit/

test-int: ## Integration tests only (~30s — spawns real subprocesses)
	$(PY) -m pytest tests/integration/

test-live: ## Tests that call the REAL provider CLIs (costs tokens; opt-in)
	@printf "No live-provider tests are checked in yet — the suite runs entirely\n"
	@printf "against the fake CLI in tests/fake_cli.py. Use 'make demo' to exercise\n"
	@printf "real providers by hand.\n"

cov: ## Run tests with a coverage report
	$(PY) -m pytest tests/ --cov=$(PKG) --cov-report=term-missing \
		--cov-fail-under=$(COV_MIN)

cov-html: ## Coverage as browsable HTML
	$(PY) -m pytest tests/ --cov=$(PKG) --cov-report=html
	@printf "\nOpen htmlcov/index.html\n"

# ── Quality ──────────────────────────────────────────────────────────────────

lint: ## Lint with ruff
	$(PY) -m ruff check $(SRC)

fmt: ## Format with ruff
	$(PY) -m ruff format $(SRC)
	$(PY) -m ruff check --fix $(SRC)

fmt-check: ## Verify formatting without changing anything
	$(PY) -m ruff format --check $(SRC)

typecheck: ## Run mypy (not enforced in CI yet)
	@$(PY) -m mypy --version >/dev/null 2>&1 \
		|| { printf "mypy is not installed. Run: $(PY) -m pip install mypy\n"; exit 1; }
	$(PY) -m mypy --strict src/$(PKG)

check: lint fmt-check test evals ## Everything CI runs, in CI's order

# ── Running it ───────────────────────────────────────────────────────────────

smoke: ## Prove the package imports and the CLI wires up (no provider calls)
	$(PY) -c "import $(PKG); print('import ok', $(PKG).__version__)"
	$(PY) -m $(PKG).cli --help >/dev/null && printf "cli ok\n"
	$(PY) -m $(PKG).cli plan review --help >/dev/null && printf "plan review ok\n"
	$(PY) -m $(PKG).cli verify --help >/dev/null && printf "verify ok\n"
	$(PY) -m $(PKG).cli replay --help >/dev/null && printf "replay ok\n"
	$(PY) -m $(PKG).cli eval score --help >/dev/null && printf "eval ok\n"

evals: ## Validate the evaluation corpus (no provider calls)
	$(PY) -m $(PKG).cli eval validate evals/*/

doctor: ## Check provider install, auth, and capabilities (makes real calls)
	$(PY) -m $(PKG).cli doctor

# A failing gate is the POINT of these demos, but make exits 2 on any failed
# recipe, which would read as a broken build. So report arbiter's own exit code
# and return success — the verdict is in the output, not in make's status.

demo: ## Run the gate on a seeded defect (REAL provider calls — costs tokens)
	@printf "This calls a real provider CLI. Ctrl-C now to bail.\n\n"
	@cd examples/totals && $(abspath $(PY)) -m $(PKG).cli verify \
		--plan plan.md --diff broken.diff --models claude \
		--test-command "$(abspath $(PY)) -m pytest"; \
		printf "\narbiter exit code: %s (1 = fail, as expected for a seeded defect)\n" "$$?"

demo-plan: ## Critique a deliberately vague plan (REAL provider calls)
	@printf "This calls a real provider CLI. Ctrl-C now to bail.\n\n"
	@$(PY) -m $(PKG).cli plan review examples/rate-limit/plan.md --models claude; \
		printf "\narbiter exit code: %s (1 = blocking problems, as expected)\n" "$$?"

# ── Housekeeping ─────────────────────────────────────────────────────────────

clean: ## Remove caches, coverage, and build artifacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@printf "Cleaned.\n"

distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV)

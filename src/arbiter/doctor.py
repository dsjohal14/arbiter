"""`arbiter doctor` — check that each provider CLI is installed, authenticated,
and report what it can actually be asked to do.

This is a separate subcommand rather than a preflight on every run. Probing
providers costs a real call, and paying that on each invocation would be worse
than the cryptic failure it prevents. Run it when something looks wrong, or once
after install.

Presence alone is not enough: a CLI can be on PATH and still refuse to serve you
(expired login, ineligible account tier), which only a real call surfaces.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field

from . import display
from .models import PROVIDERS, ModelCallError, Provider, make_session

INSTALL_HINTS = {
    "claude": "https://code.claude.com",
    "codex": "npm install -g @openai/codex",
    "gemini": "https://developers.google.com/gemini-cli",
    "antigravity": "https://antigravity.google (installs the `agy` CLI)",
}

PROBE_TIMEOUT = 60.0
PROBE_PROMPT = "Reply with the single word: ok"


@dataclass
class ProviderHealth:
    key: str
    installed: bool
    path: str | None = None
    authenticated: bool | None = None  # None when not probed
    error: str | None = None
    capabilities: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.installed and self.authenticated is not False

    def as_dict(self) -> dict:
        return {
            "provider": self.key,
            "installed": self.installed,
            "path": self.path,
            "authenticated": self.authenticated,
            "error": self.error,
            "capabilities": self.capabilities,
        }


def describe_capabilities(p: Provider) -> list[str]:
    """One line per thing this CLI can or cannot be asked to do.

    Read verbatim by users deciding which panel to trust, so each line states
    the narrowest true thing — a provider that only silences skills must not
    read like one that controls the whole system prompt.
    """
    caps: list[str] = []
    caps.append(
        "structured output" if p.output_schema_flag else "no structured output (prose fallback)"
    )
    caps.append("read-only mode" if p.readonly_flags else "no read-only mode")
    caps.append(
        f"isolates {p.isolate_scope}" if p.isolate_flags else "inherits user config (uncontrolled)"
    )
    caps.append("pre-assigned session id" if p.preassigns_session else "provider-minted session id")
    if not p.accepts_stdin_prompt:
        caps.append("prompt must fit in argv (no stdin); very large diffs may not fit")
    caps.append("reports token usage" if p.reports_usage else "no token usage reported")
    return caps


async def check_provider(key: str, probe: bool = True) -> ProviderHealth:
    provider = PROVIDERS[key]
    path = shutil.which(provider.cli)
    health = ProviderHealth(
        key=key,
        installed=path is not None,
        path=path,
        capabilities=describe_capabilities(provider),
    )
    if not health.installed:
        health.error = f"not on PATH — install: {INSTALL_HINTS.get(key, provider.cli)}"
        return health
    if not probe:
        return health

    try:
        sess = make_session(key, key)
        await asyncio.wait_for(_probe(sess), timeout=PROBE_TIMEOUT + 5)
        health.authenticated = True
    except (ModelCallError, TimeoutError) as e:
        health.authenticated = False
        health.error = str(e)[:300]
    return health


async def _probe(sess) -> None:
    from .models import call_session

    await call_session(sess, PROBE_PROMPT, timeout=PROBE_TIMEOUT)


async def run_doctor(keys: list[str] | None = None, probe: bool = True) -> list[ProviderHealth]:
    keys = keys or sorted(PROVIDERS)
    return list(await asyncio.gather(*[check_provider(k, probe) for k in keys]))


def render(results: list[ProviderHealth]) -> None:
    display.print_section("arbiter doctor")
    for h in results:
        if not h.installed or h.authenticated is False:
            mark, color = "✗", display.RED
        elif h.authenticated is None:
            mark, color = "?", display.YELLOW
        else:
            mark, color = "✓", display.GREEN

        display.print(
            f"  {display._c(mark, display.BOLD, color)}  {display._c(h.key, display.BOLD)}"
        )
        if h.path:
            display.print(f"     {display._c('path:  ', display.DIM)} {h.path}")
        if h.error:
            display.print(
                f"     {display._c('error: ', display.DIM)} {display._c(h.error, display.RED)}"
            )
        for cap in h.capabilities:
            display.print(f"     {display._c('·', display.DIM)} {display._c(cap, display.DIM)}")
        display.print()

    broken = [h for h in results if not h.ok]
    if broken:
        display.print_note(
            f"{len(broken)} provider(s) unusable: {', '.join(h.key for h in broken)}"
        )
    else:
        display.print_note("All providers ready.")
    display.print()

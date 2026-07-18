# ADR-0003 — Runtime & tooling choices

Status: Accepted · Date: 2026-07-18 · Updated: 2026-07-18 (moved to 3.14)

## Context

CLAUDE.md §3 locks Python 3.11+, `uv` if available else pip+venv, `ruff`,
`pytest`. At initial build only Python 3.10.19 was installed, so the first cut
targeted 3.10 (a documented, deliberately-two-way-door deviation). The user then
installed **Python 3.14.6** (homebrew `python@3.14`) and removed 3.10.

## Decision

- **Target Python 3.14** (`requires-python = ">=3.14"`), satisfying §3's 3.11+
  floor. The two-way door was walked through as planned: **no source changes were
  needed** to move 3.10 → 3.14; only `pyupgrade` modernizations applied
  automatically (`timezone.utc` → `datetime.UTC`, UP017). `ruff target-version`
  and `mypy python_version` are both `3.14`/`py314`.
- **pip + venv** (no `uv` present). Standard `.venv` built with
  `/opt/homebrew/bin/python3.14 -m venv .venv`, editable install
  `pip install -e ".[dev]"`.
- Shell housekeeping (outside the repo): a stale `alias python3="python3.10"` and
  a dead `python@3.10` PATH export in `~/.zshrc` were removed; `python3` now
  resolves via `/opt/homebrew/bin/python3` → `python@3.14`, and a
  `alias python="python3"` was added.
- **argparse** for the CLI, not click/typer — stdlib, zero deps, enough for a
  local single-user tool (avoids premature machinery, §9).
- **httpx** for the WHOOP client (async-capable, testable via its built-in
  `MockTransport` — no extra mocking dependency).
- **stdlib `sqlite3`** directly; no ORM. The schema is small and hand-tuned; an
  ORM would obscure the raw/canonical/view structure that is the whole point.
- Runtime deps kept minimal: `httpx`, `python-dotenv`, `tzdata`.

## Consequences

- Now fully §3-compliant (3.11+). Nothing further needed.
- `filterwarnings = ["error"]` in pytest turns deprecation warnings into failures
  early — verified clean on 3.14 (79 tests, no warnings).
- The floor is `>=3.14`; drop it to `>=3.11` only if the tool ever needs to run
  on an older interpreter. No code currently uses 3.12+-only features, so lowering
  it would just require reverting the `datetime.UTC` alias.

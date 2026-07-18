# ADR-0003 — Runtime & tooling choices

Status: Accepted · Date: 2026-07-18

## Context

CLAUDE.md §3 locks Python 3.11+, `uv` if available else pip+venv, `ruff`,
`pytest`. The build machine had **only Python 3.10.19** installed — no 3.11, no
`uv`, no `pyenv` 3.11. Installing a new interpreter unattended overnight risked
hanging on prompts, so a call was needed.

## Decision

- **Target Python 3.10** (`requires-python = ">=3.10"`), the only interpreter
  present. All code is kept 3.10-compatible: no `tomllib`, no `match` statements,
  `from __future__ import annotations` everywhere so `X | Y` hints are fine.
  This is a two-way door — bump the floor to 3.11 the moment a 3.11 interpreter
  is available; nothing in the code will need to change.
- **pip + venv** (no `uv` available). Standard `.venv`, editable install
  `pip install -e ".[dev]"`.
- **argparse** for the CLI, not click/typer — stdlib, zero deps, enough for a
  local single-user tool (avoids premature machinery, §9).
- **httpx** for the WHOOP client (async-capable, testable via its built-in
  `MockTransport` — no extra mocking dependency).
- **stdlib `sqlite3`** directly; no ORM. The schema is small and hand-tuned; an
  ORM would obscure the raw/canonical/view structure that is the whole point.
- Runtime deps kept minimal: `httpx`, `python-dotenv`, `tzdata`.

## Consequences

- If the human wants strict §3 compliance: `brew install python@3.11`, recreate
  the venv with it, bump `requires-python` and `target-version`/`python_version`
  in `pyproject.toml`. No source changes anticipated.
- `filterwarnings = ["error"]` in pytest turns deprecation warnings into failures
  early, which matters given the 3.10→3.11 gap.
- This deviation from a §3 "locked decision" is surfaced (not silent) here and in
  SESSION_LOG.md, per the CLAUDE.md preamble.

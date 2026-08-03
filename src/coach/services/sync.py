"""The daily-driver sync, as a callable that RETURNS results instead of printing.

`coach sync` is the lowest-friction path to current data (risk #8: sync
friction kills the tool). The orchestration — which sources run, how each
degrades, what order — is domain logic, not presentation, so it lives here and
the CLI only formats what comes back.

Degradation contract: an unconfigured or unauthorized source is **skipped with
a reason**, never fatal. One dead source must not cost you the others.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..adapters.myfitnesspal.auth import MfpAuthError
from ..adapters.myfitnesspal.client import MfpClient
from ..adapters.myfitnesspal.ingest import auto_since as mfp_auto_since
from ..adapters.myfitnesspal.ingest import ingest_mfp
from ..adapters.whoop.auth import ReauthRequired
from ..adapters.whoop.client import WhoopClient
from ..adapters.whoop.ingest import auto_since_by_type as whoop_auto_since_by_type
from ..adapters.whoop.ingest import ingest_whoop
from ..config import ConfigError, Settings
from ..normalize.runner import normalize_all


@dataclass(frozen=True)
class SourceResult:
    """One source's outcome. ``counts`` is empty whenever ``skipped`` is set."""

    name: str
    counts: dict = field(default_factory=dict)
    skipped: str | None = None  # human-readable reason, None when it ran
    since: str | None = None
    # Whether this outcome needs a human.
    #
    # ``skipped`` alone cannot answer that, and conflating the two was a real
    # bug once sync moved to cron: "not configured" (a source you don't use) and
    # "auth needed" (a token that expired last Tuesday) were the same string and
    # the same exit code, so a dead credential would quietly ingest nothing
    # forever while the nightly job reported success.
    needs_attention: bool = False


@dataclass(frozen=True)
class SyncResult:
    sources: list[SourceResult]
    normalized: dict[str, int]

    @property
    def ok(self) -> bool:
        """False if any source needs a human. Drives the process exit code."""
        return not any(s.needs_attention for s in self.sources)

    @property
    def attention(self) -> list[SourceResult]:
        return [s for s in self.sources if s.needs_attention]


def run_sync(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    whoop_client: Callable[[Settings], WhoopClient],
    mfp_client: Callable[[Settings], MfpClient],
    today: str,
    hk_file: Path | None = None,
) -> SyncResult:
    """Ingest every configured source, then normalize. Never raises on a
    source-level failure — each becomes a skipped :class:`SourceResult`.

    Clients are injected (not built here) so this stays testable without
    credentials and so a future caller can supply its own transport.
    """
    sources: list[SourceResult] = []

    # --- WHOOP ---
    try:
        settings.require_whoop()
        # Per-type windows: a quiet type (no workouts this week) must not drag
        # every other type's window back (see auto_since_by_type).
        by_type = whoop_auto_since_by_type(conn)
        since = min(by_type.values()) if by_type else None
        if since is None:
            sources.append(
                SourceResult(
                    "whoop",
                    skipped="no prior ingest — run `coach ingest whoop --since <date>` once first",
                    needs_attention=True,
                )
            )
        else:
            whoop_counts = ingest_whoop(
                conn, whoop_client(settings), since=by_type, user_id=settings.user_id
            )
            sources.append(SourceResult("whoop", counts=dict(whoop_counts), since=since))
    except ConfigError:
        sources.append(SourceResult("whoop", skipped="not configured"))
    except ReauthRequired as exc:
        sources.append(SourceResult("whoop", skipped=f"auth needed ({exc})", needs_attention=True))

    # --- MyFitnessPal (food + weight) ---
    try:
        settings.require_mfp()
        m_since = mfp_auto_since(conn)
        if m_since is None:
            sources.append(
                SourceResult(
                    "mfp",
                    skipped="no prior ingest — run `coach ingest mfp --since <date>` once first",
                    needs_attention=True,
                )
            )
        else:
            until = date.fromisoformat(today).isoformat()
            mfp_counts = ingest_mfp(
                conn,
                mfp_client(settings),
                since=m_since,
                until=until,
                user_id=settings.user_id,
            )
            sources.append(SourceResult("mfp", counts=dict(mfp_counts), since=m_since))
    except ConfigError:
        sources.append(SourceResult("mfp", skipped="not configured"))
    except MfpAuthError as exc:
        sources.append(SourceResult("mfp", skipped=f"auth needed ({exc})", needs_attention=True))

    # --- HealthKit: occasional backfill, only when explicitly requested ---
    if hk_file is not None:
        from ..adapters.healthkit.ingest import ingest_healthkit

        if hk_file.exists():
            hk_counts = ingest_healthkit(conn, hk_file, user_id=settings.user_id)
            sources.append(SourceResult("healthkit", counts=dict(hk_counts)))
        else:
            sources.append(
                SourceResult(
                    "healthkit",
                    skipped=f"export not found: {hk_file}",
                    # Explicitly requested via --hk-file and not there: the
                    # operator asked for something that did not happen.
                    needs_attention=True,
                )
            )

    return SyncResult(sources=sources, normalized=normalize_all(conn, user_id=settings.user_id))

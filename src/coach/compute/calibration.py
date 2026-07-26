"""Cross-source calibration statistics (§4/§5, ADR-0012).

When Adapter B (local BLE) lands, its recomputed objective measurements run
against WHOOP's official ones over the same days, and THESE numbers say whether
our textbook math is trustworthy before the membership dies: agreement
(correlation), systematic offset (mean bias), and typical error (MAE), each
over the days BOTH sources reported (§2.7 — a day either side is missing simply
isn't a pair; nothing is interpolated).

Pure math over day-keyed series; the DB assembler pairs sibling rows from the
`recovery` table (the whole point of storing sources side by side, §2.3).
Usable today for any sibling pair (e.g. healthkit-vs-myfitnesspal weight).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .hrv_validation import MIN_PAIRS, Correlation, pearson
from .trends import Insufficient


@dataclass(frozen=True)
class Agreement:
    """How closely series B tracks series A over their shared days."""

    n: int
    mean_bias: float  # mean(B - A): systematic offset, signed
    mae: float  # mean(|B - A|): typical magnitude of disagreement
    correlation: Correlation | Insufficient


def compare_series(
    a: dict[str, float], b: dict[str, float]
) -> Agreement | Insufficient:
    """Agreement stats over the days present in BOTH series.

    Insufficient below :data:`MIN_PAIRS` shared days — two sources that barely
    overlap can't be honestly compared (§2.2).
    """
    shared = sorted(set(a) & set(b))
    n = len(shared)
    if n < MIN_PAIRS:
        return Insufficient(have=n, needed=MIN_PAIRS)
    xs = [a[d] for d in shared]
    ys = [b[d] for d in shared]
    diffs = [y - x for x, y in zip(xs, ys, strict=True)]
    return Agreement(
        n=n,
        mean_bias=sum(diffs) / n,
        mae=sum(abs(d) for d in diffs) / n,
        correlation=pearson(xs, ys),
    )


# objective recovery columns that are comparable across sources (§5) — the
# composite `score` is deliberately NOT here (proprietary weighting)
OBJECTIVE_METRICS = (
    "hrv_rmssd_ms",
    "resting_hr_bpm",
    "spo2_pct",
    "skin_temp_c",
    "resp_rate_bpm",
)


def recovery_source_series(
    conn: sqlite3.Connection, *, source: str, metric: str, user_id: int = 1
) -> dict[str, float]:
    """One source's daily series for one objective recovery metric."""
    if metric not in OBJECTIVE_METRICS:
        raise ValueError(
            f"{metric!r} is not an objective metric (comparable set: {OBJECTIVE_METRICS})"
        )
    return {
        r["day_key"]: float(r["v"])
        for r in conn.execute(
            # metric is whitelisted against OBJECTIVE_METRICS above — never user input
            f"SELECT day_key, {metric} AS v FROM recovery "
            "WHERE user_id=? AND source=? AND day_key IS NOT NULL ORDER BY day_key",
            (user_id, source),
        )
        if r["v"] is not None
    }


def calibration_report(
    conn: sqlite3.Connection,
    *,
    source_a: str,
    source_b: str,
    user_id: int = 1,
) -> dict[str, Agreement | Insufficient]:
    """Per-metric agreement of ``source_b`` against ``source_a`` (the reference).

    During the paid window: source_a='whoop_api' (official ground truth),
    source_b='whoop_ble' (our recomputation). Every metric degrades to
    Insufficient independently.
    """
    out: dict[str, Agreement | Insufficient] = {}
    for metric in OBJECTIVE_METRICS:
        a = recovery_source_series(conn, source=source_a, metric=metric, user_id=user_id)
        b = recovery_source_series(conn, source=source_b, metric=metric, user_id=user_id)
        out[metric] = compare_series(a, b)
    return out

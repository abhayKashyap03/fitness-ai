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


def compare_series(a: dict[str, float], b: dict[str, float]) -> Agreement | Insufficient:
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


# weight/body-comp columns that are comparable across sources. Unlike recovery
# there is no proprietary composite here — a kilogram is a kilogram, which is why
# this domain can be calibrated TODAY while `whoop_ble` rows don't exist yet.
WEIGHT_METRICS = ("weight_kg", "body_fat_pct", "lean_mass_kg")


@dataclass(frozen=True)
class SourceSpec:
    """One writer of a metric: a ``source``, optionally narrowed by ``source_app``.

    Apple Health carries several apps writing the same metric under one
    ``source`` (ADR-0008), so the sub-source is part of the identity: comparing
    "healthkit" as a whole against MyFitnessPal would silently pool the OKOK
    scale with MFP's own mirrored rows and calibrate a source against itself.
    """

    source: str
    source_app: str | None = None

    @classmethod
    def parse(cls, spec: str) -> SourceSpec:
        """``"healthkit:okok"`` -> SourceSpec('healthkit', 'okok')."""
        source, _, app = spec.partition(":")
        source = source.strip()
        if not source:
            raise ValueError(f"source spec is empty: {spec!r}")
        return cls(source=source, source_app=app.strip() or None)

    def __str__(self) -> str:
        return f"{self.source}:{self.source_app}" if self.source_app else self.source


def weight_source_series(
    conn: sqlite3.Connection, *, spec: SourceSpec, metric: str, user_id: int = 1
) -> dict[str, float]:
    """One writer's daily series for one weight/body-comp metric.

    A source may report more than once in a day (a morning and an evening
    weigh-in), so the day's readings are averaged — the comparison is between
    "what source A said that day" and "what source B said that day", and picking
    an arbitrary one of two readings would make the result depend on insertion
    order. Days where this source reported nothing are simply absent, never zero
    (§2.7), so they form no pair.
    """
    if metric not in WEIGHT_METRICS:
        raise ValueError(f"{metric!r} is not a weight metric (comparable set: {WEIGHT_METRICS})")
    sql = (
        # metric is whitelisted against WEIGHT_METRICS above — never user input
        f"SELECT day_key, AVG({metric}) AS v FROM weight_measurement "
        "WHERE user_id = ? AND source = ? AND day_key IS NOT NULL "
        f"AND {metric} IS NOT NULL"
    )
    params: list[object] = [user_id, spec.source]
    if spec.source_app is not None:
        sql += " AND source_app = ?"
        params.append(spec.source_app)
    sql += " GROUP BY day_key ORDER BY day_key"
    return {r["day_key"]: float(r["v"]) for r in conn.execute(sql, params) if r["v"] is not None}


def weight_calibration_report(
    conn: sqlite3.Connection,
    *,
    spec_a: SourceSpec,
    spec_b: SourceSpec,
    user_id: int = 1,
) -> dict[str, Agreement | Insufficient]:
    """Per-metric agreement of weight writer ``b`` against reference ``a``.

    Runnable on today's data (e.g. a smart scale via Apple Health against
    MyFitnessPal's own figure), which is the point: it exercises the same
    machinery the BLE adapter will need long before that adapter exists
    (ADR-0012), so the calibration path is not first tried on the day the
    membership lapses.
    """
    out: dict[str, Agreement | Insufficient] = {}
    for metric in WEIGHT_METRICS:
        a = weight_source_series(conn, spec=spec_a, metric=metric, user_id=user_id)
        b = weight_source_series(conn, spec=spec_b, metric=metric, user_id=user_id)
        out[metric] = compare_series(a, b)
    return out


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

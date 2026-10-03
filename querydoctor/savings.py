"""
Monthly savings estimate.

Honesty rule: computed ONLY from measured runtimes (real validation
before/after EXPLAIN ANALYZE medians) — never from planner cost.

    saved_seconds_per_call = (before_ms - after_ms) / 1000
    monthly_cpu_hours      = saved_seconds_per_call × calls_per_day × 30 / 3600
    monthly_compute_usd    = monthly_cpu_hours × vcpu_hour_usd
    monthly_storage_usd    = index_size_GB × storage_gb_month_usd
    net                    = compute saved − storage added

Simple model: treats query time as one busy vCPU; ignores I/O pricing,
parallel workers, cache effects and index write overhead.
"""

from querydoctor.config import get_settings
from querydoctor.db import get_conn


DAYS_PER_MONTH = 30

# Below this stats window, a derived calls/day rate is flagged unreliable.
MIN_RELIABLE_WINDOW_HOURS = 24


def derive_calls_per_day(queryid) -> dict | None:
    """calls/day from pg_stat_statements since its last reset."""

    if queryid is None:
        return None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT sum(calls) FROM pg_stat_statements
                     WHERE toplevel AND queryid = %s),
                    extract(epoch FROM now() - stats_reset) / 3600.0
                FROM pg_stat_statements_info
            """, (int(queryid),))
            calls, window_hours = cur.fetchone()
    finally:
        conn.close()

    if not calls or not window_hours:
        return None

    window_hours = float(window_hours)

    return {
        "calls_per_day": float(calls) / window_hours * 24,
        "calls": int(calls),
        "window_hours": window_hours,
        "reliable": window_hours >= MIN_RELIABLE_WINDOW_HOURS,
    }


def estimate_monthly_savings(
    before_ms: float,
    after_ms: float,
    calls_per_day: float,
    index_size_bytes: int | None = None,
    vcpu_hour_usd: float | None = None,
    storage_gb_month_usd: float | None = None,
) -> dict:

    settings = get_settings()

    if vcpu_hour_usd is None:
        vcpu_hour_usd = settings.vcpu_hour_price_usd
    if storage_gb_month_usd is None:
        storage_gb_month_usd = settings.storage_gb_month_usd

    saved_s_per_call = max(0.0, (before_ms - after_ms) / 1000)
    monthly_calls = calls_per_day * DAYS_PER_MONTH
    monthly_cpu_hours = saved_s_per_call * monthly_calls / 3600

    compute_usd = monthly_cpu_hours * vcpu_hour_usd
    storage_usd = (index_size_bytes or 0) / 1024 ** 3 * storage_gb_month_usd

    return {
        "saved_ms_per_call": saved_s_per_call * 1000,
        "calls_per_day": calls_per_day,
        "monthly_calls": monthly_calls,
        "monthly_cpu_hours_saved": monthly_cpu_hours,
        "monthly_compute_savings_usd": compute_usd,
        "monthly_storage_cost_usd": storage_usd,
        "monthly_net_savings_usd": compute_usd - storage_usd,
        "vcpu_hour_usd": vcpu_hour_usd,
        "storage_gb_month_usd": storage_gb_month_usd,
    }


def savings_for_state(state: dict) -> dict | None:
    """Savings for an agent run, or None if no measured runtime exists."""

    best = state.get("best") or {}
    actual = best.get("actual")

    if not actual:
        return None

    calls_per_day = state.get("calls_per_day") or get_settings().calls_per_day
    source = "user-provided assumption"
    derived = None

    if not calls_per_day:
        derived = derive_calls_per_day(state.get("query_id"))
        if not derived:
            return None
        calls_per_day = derived["calls_per_day"]
        source = (
            f"derived from pg_stat_statements: {derived['calls']} calls in a "
            f"{derived['window_hours']:.2f} h window"
            + ("" if derived["reliable"] else
               " — UNRELIABLE (window < 24 h); set calls/day explicitly")
        )

    result = estimate_monthly_savings(
        actual["before_ms"],
        actual["after_ms"],
        calls_per_day,
        actual.get("actual_size_bytes"),
    )
    result["calls_per_day_source"] = source
    result["reliable"] = derived is None or derived["reliable"]

    return result

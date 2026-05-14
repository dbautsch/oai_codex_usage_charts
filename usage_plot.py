"""Render the weekly usage chart as a single PNG."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # headless backend, safe on a server
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np


# Extrapolation parameters. The slope is fit with exponentially-weighted
# least squares so the line reacts quickly when the user changes pace —
# e.g. heavy usage on days 1-2 then slow down on day 3+ should flatten the
# forecast within ~`LSQ_DECAY_HOURS`, not after the full window has rolled.
#
#   weight_i = exp(-age_hours_i / LSQ_DECAY_HOURS)
#
# The window cap (LSQ_WINDOW_HOURS) just bounds how far back we look; old
# points outside it are dropped before fitting (their weights would be
# negligible anyway, but excluding them keeps the math conditioning clean).
LSQ_WINDOW_HOURS = 48.0
LSQ_DECAY_HOURS = 12.0
LSQ_MIN_SPAN_HOURS = 2.0
LSQ_MIN_SAMPLES = 3


CSV_FIELDS = [
    "captured_at",
    "weekly_pct_used",
    "weekly_pct_left",
    "weekly_next_reset",
    "account",
    "model",
]


@dataclass
class Sample:
    captured_at: datetime
    pct_used: float
    next_reset: datetime


def append_sample(csv_path: Path, snapshot) -> None:
    new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(
            {
                "captured_at": snapshot.captured_at.isoformat(timespec="seconds"),
                "weekly_pct_used": snapshot.weekly_pct_used,
                "weekly_pct_left": snapshot.weekly_pct_left,
                "weekly_next_reset": snapshot.weekly_next_reset.isoformat(timespec="seconds"),
                "account": snapshot.account or "",
                "model": snapshot.model or "",
            }
        )


def load_samples(csv_path: Path) -> list[Sample]:
    if not csv_path.exists():
        return []
    out: list[Sample] = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                out.append(
                    Sample(
                        captured_at=datetime.fromisoformat(row["captured_at"]),
                        pct_used=float(row["weekly_pct_used"]),
                        next_reset=datetime.fromisoformat(row["weekly_next_reset"]),
                    )
                )
            except (KeyError, ValueError):
                continue
    out.sort(key=lambda s: s.captured_at)
    return out


def _current_cycle(samples: list[Sample]) -> tuple[list[Sample], datetime, datetime]:
    """Filter to the most recent reset cycle. Returns (samples_in_cycle, cycle_start, cycle_end)."""
    latest = samples[-1]
    cycle_end = latest.next_reset
    cycle_start = cycle_end - timedelta(days=7)
    in_cycle = [s for s in samples if s.captured_at >= cycle_start and s.next_reset == cycle_end]
    return in_cycle, cycle_start, cycle_end


def _extrapolate(
    in_cycle: list[Sample],
    cycle_start: datetime,
    cycle_end: datetime,
) -> Optional[tuple[datetime, float, float, str]]:
    """Estimate the linear extrapolation of usage from the latest sample to cycle_end.

    Strategy:
      * Prefer a slope computed over the last 24h of real samples (≥2 points
        inside the window with a non-zero time delta).
      * Otherwise fall back to a slope anchored at the cycle start (0% at
        cycle_start → latest pct at latest.captured_at). With only one sample
        this is the only signal we have, and it's better than nothing — the
        chart's title makes the source explicit so the reader knows it's coarse.

    Returns (anchor_t, anchor_pct, predicted_end_pct, source_label) or None.
    """
    if not in_cycle:
        return None
    latest = in_cycle[-1]

    rate: Optional[float] = None
    source = ""

    # Exponentially-weighted LSQ over recent in-cycle samples. xs are hours
    # relative to the latest sample (negative for past). Weights decay with age
    # so a few days of heavy usage at the start of the cycle stop dragging the
    # forecast once you slow down — the line flattens within ~LSQ_DECAY_HOURS.
    cutoff = latest.captured_at - timedelta(hours=LSQ_WINDOW_HOURS)
    window = [s for s in in_cycle if s.captured_at >= cutoff]
    if len(window) >= LSQ_MIN_SAMPLES:
        span_h = (window[-1].captured_at - window[0].captured_at).total_seconds() / 3600.0
        if span_h >= LSQ_MIN_SPAN_HOURS:
            xs = np.array(
                [(s.captured_at - latest.captured_at).total_seconds() / 3600.0 for s in window]
            )
            ys = np.array([s.pct_used for s in window], dtype=float)
            ages = -xs  # hours-before-now, ≥ 0
            weights = np.exp(-ages / LSQ_DECAY_HOURS)
            slope, _intercept = np.polyfit(xs, ys, 1, w=weights)
            rate = float(slope)  # %/hour
            source = (
                f"EW-LSQ, window {LSQ_WINDOW_HOURS:.0f}h, "
                f"τ={LSQ_DECAY_HOURS:.0f}h, n={len(window)}"
            )

    if rate is None:
        # Not enough recent data — anchor the slope at the cycle start (0%).
        delta_h = (latest.captured_at - cycle_start).total_seconds() / 3600.0
        if delta_h > 0:
            rate = (latest.pct_used - 0.0) / delta_h
            label_note = "only one sample" if len(in_cycle) < 2 else "window too short"
            source = f"since cycle start (coarse — {label_note})"

    if rate is None:
        return None

    hours_to_end = (cycle_end - latest.captured_at).total_seconds() / 3600.0
    predicted_end = latest.pct_used + rate * hours_to_end
    return latest.captured_at, latest.pct_used, predicted_end, source


def render_chart(
    png_path: Path,
    csv_path: Path,
    error_message: Optional[str] = None,
) -> None:
    samples = load_samples(csv_path)
    fig, ax = plt.subplots(figsize=(17, 10), dpi=100)

    if not samples:
        ax.set_axis_off()
        msg = "No usage data collected yet."
        if error_message:
            msg = f"{msg}\n\nLast error:\n{error_message}"
        ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=14, color="#b00020")
        fig.savefig(png_path, bbox_inches="tight")
        plt.close(fig)
        return

    in_cycle, cycle_start, cycle_end = _current_cycle(samples)
    now = datetime.now()
    latest = samples[-1]

    xs = [s.captured_at for s in in_cycle]
    ys = [s.pct_used for s in in_cycle]

    # Anchor the start of the cycle at 0% if we have no earlier datapoint.
    if not xs or xs[0] > cycle_start + timedelta(minutes=5):
        xs = [cycle_start] + xs
        ys = [0.0] + ys

    ax.plot(xs, ys, linewidth=2.5, color="#1f6feb", label="Actual usage")

    extra = _extrapolate(in_cycle, cycle_start, cycle_end)
    predicted_end_pct: Optional[float] = None
    extra_source: str = ""
    overshoot_t: Optional[datetime] = None
    if extra is not None:
        anchor_t, anchor_pct, predicted_end_pct, extra_source = extra
        ax.plot(
            [anchor_t, cycle_end],
            [anchor_pct, predicted_end_pct],
            linestyle="--",
            linewidth=2.5,
            color="#d97706",
            label=f"Extrapolation ({extra_source}) → {predicted_end_pct:.1f}% at reset",
        )
        # If the extrapolation crosses 100% before the reset, mark exactly where
        # so the reader can read the date off the X axis.
        if predicted_end_pct > 100 and anchor_pct < 100:
            hours_to_end = (cycle_end - anchor_t).total_seconds() / 3600.0
            rate = (predicted_end_pct - anchor_pct) / hours_to_end
            if rate > 0:
                hours_to_100 = (100 - anchor_pct) / rate
                overshoot_t = anchor_t + timedelta(hours=hours_to_100)
                ax.axvline(
                    overshoot_t,
                    color="#b00020",
                    linestyle="--",
                    linewidth=2,
                    alpha=0.75,
                )
                ax.text(
                    overshoot_t,
                    100,
                    f" 100% at {overshoot_t:%Y-%m-%d %H:%M}",
                    color="#b00020",
                    fontsize=15,
                    fontweight="bold",
                    va="bottom",
                    ha="left",
                )

    # 100% reference line
    ax.axhline(100, color="#b00020", linestyle=":", linewidth=1.5, label="Limit (100%)")
    # vertical "now"
    ax.axvline(now, color="#444", linestyle=":", linewidth=1.5, alpha=0.6)
    ax.text(now, 102, "now", ha="center", va="bottom", fontsize=13, color="#444")
    # vertical "reset" — distinct green dash-dot, drawn just inside the right edge
    # so the label has room and the line stays visible against the spine.
    ax.axvline(cycle_end, color="#16a34a", linestyle="-.", linewidth=2.2, alpha=0.85)
    ax.text(
        cycle_end,
        102,
        f" reset {cycle_end:%a %H:%M}",
        ha="left",
        va="bottom",
        fontsize=13,
        fontweight="bold",
        color="#16a34a",
    )

    # Pad an extra day past the reset so the green reset line doesn't merge
    # with the right spine of the chart.
    ax.set_xlim(cycle_start, cycle_end + timedelta(days=1))
    ax.set_ylim(0, max(110, (predicted_end_pct or 0) + 10))
    ax.set_ylabel("Weekly usage (%)", fontsize=15)
    ax.set_xlabel("Time", fontsize=15)
    ax.tick_params(axis="both", labelsize=13)

    will_overshoot = predicted_end_pct is not None and predicted_end_pct > 100
    headline = f"Codex weekly usage — {latest.pct_used:.0f}% used"
    sub = (
        f"Cycle: {cycle_start:%Y-%m-%d %H:%M} -> {cycle_end:%Y-%m-%d %H:%M}  *  "
        f"Reset in {_fmt_delta(cycle_end - now)}"
    )
    if predicted_end_pct is not None:
        verdict = "WILL EXCEED LIMIT" if will_overshoot else "within limit"
        sub += f"  *  Forecast at reset: {predicted_end_pct:.1f}% ({verdict})"
    if overshoot_t is not None:
        sub += f"  *  Hits 100% at {overshoot_t:%Y-%m-%d %H:%M}"

    fig.text(0.04, 0.955, headline, fontsize=22, fontweight="bold", color="#111")
    fig.text(0.04, 0.92, sub, fontsize=14, color="#333")

    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    formatter = mdates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_formatter(formatter)

    ax.grid(True, axis="y", alpha=0.25)
    ax.legend(loc="upper left", fontsize=13, framealpha=0.9)

    if error_message:
        fig.text(
            0.5,
            0.01,
            f"Last error: {error_message}",
            ha="center",
            fontsize=11,
            color="#b00020",
        )

    fig.text(
        0.985,
        0.01,
        f"Generated: {now:%Y-%m-%d %H:%M:%S}",
        ha="right",
        va="bottom",
        fontsize=10,
        color="#888",
    )

    fig.subplots_adjust(left=0.06, right=0.985, top=0.88, bottom=0.07)
    fig.savefig(png_path)
    plt.close(fig)


def render_error_png(png_path: Path, error_message: str) -> None:
    """Render a PNG that contains only an error message. Used when no data exists yet."""
    fig, ax = plt.subplots(figsize=(17, 10), dpi=100)
    ax.set_axis_off()
    ax.text(
        0.5,
        0.5,
        f"codex /status failed\n\n{error_message}\n\n{datetime.now():%Y-%m-%d %H:%M:%S}",
        ha="center",
        va="center",
        fontsize=13,
        color="#b00020",
    )
    fig.savefig(png_path)
    plt.close(fig)


def render_login_required_png(png_path: Path) -> None:
    """Big, centred 'Please login into codex account' banner — the only signal the
    user gets on the public-facing PNG when the codex session is no longer authenticated."""
    fig, ax = plt.subplots(figsize=(17, 10), dpi=100)
    ax.set_axis_off()
    ax.text(
        0.5,
        0.55,
        "Please login into codex account",
        ha="center",
        va="center",
        fontsize=34,
        fontweight="bold",
        color="#b00020",
    )
    ax.text(
        0.5,
        0.30,
        "Run:  codex login",
        ha="center",
        va="center",
        fontsize=18,
        color="#444",
    )
    ax.text(
        0.5,
        0.08,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ha="center",
        va="center",
        fontsize=10,
        color="#888",
    )
    fig.savefig(png_path)
    plt.close(fig)


def _fmt_delta(d: timedelta) -> str:
    total = int(d.total_seconds())
    if total < 0:
        return "0m"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

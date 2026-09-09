"""
make_plots.py — figures for the report
======================================
Reads the JSON produced by run_experiments.py and writes PNGs into
report/figures/.

Chart conventions used throughout: one measure per axis (never a second y-scale),
a fixed categorical hue order so a system keeps its colour in every figure,
recessive grid and axes, direct labels on the last point of each line so series
identity never depends on colour alone, and a legend whenever more than one
series is present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).parent
RESULTS = HERE / "results"
FIGURES = HERE.parent / "report" / "figures"

# Validated categorical palette, fixed slot order (blue, orange, aqua, yellow).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
NODE_COLOR = {"Sys1": SERIES[0], "Sys2": SERIES[1], "Sys3": SERIES[2], "Sys4": SERIES[3]}
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#8a8880"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.size": 10,
    "font.family": "DejaVu Sans",
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK2,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.titlecolor": INK,
    "xtick.color": INK2,
    "ytick.color": INK2,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "legend.frameon": False,
    "lines.linewidth": 2.0,
    "lines.markersize": 5,
})


def style(ax, xlabel: str = "", ylabel: str = "", title: str = "") -> None:
    ax.grid(True, axis="y", alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    if xlabel:
        ax.set_xlabel(xlabel)
    if ylabel:
        ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title, loc="left", pad=12)


def load(name: str) -> Optional[Dict[str, Any]]:
    path = RESULTS / name
    if not path.exists():
        print(f"  (skipping {name} — not found)")
        return None
    with open(path) as fh:
        return json.load(fh)


def save(fig, name: str) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(FIGURES / name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote report/figures/{name}")


def label_end(ax, x, y, text, color) -> None:
    """Direct label at the end of a line, so identity is not colour-only."""
    ax.annotate(f" {text}", xy=(x, y), xytext=(4, 0), textcoords="offset points",
                color=color, fontsize=9, fontweight="bold", va="center")


def rolling(values: List[float], window: int) -> np.ndarray:
    if len(values) < window or window < 2:
        return np.asarray(values, dtype=float)
    arr = np.asarray(values, dtype=float)
    kernel = np.ones(window) / window
    return np.convolve(arr, kernel, mode="valid")


# ── figures ──────────────────────────────────────────────────────────────────
def plot_capacity() -> None:
    data = load("capacity.json")
    if not data:
        return
    runs = data["runs"]
    users = [r["users"] for r in runs]
    rps = [r["throughput_rps"] for r in runs]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ax = axes[0]
    ax.plot(users, rps, color=SERIES[0], marker="o")
    label_end(ax, users[-1], rps[-1], f"{rps[-1]:.0f} rps", SERIES[0])
    peak = max(range(len(rps)), key=lambda i: rps[i])
    ax.annotate(f"peak {rps[peak]:.0f} rps @ {users[peak]} users",
                xy=(users[peak], rps[peak]), xytext=(0, 14),
                textcoords="offset points", ha="center", color=INK2, fontsize=9)
    style(ax, "Concurrent users", "Throughput (requests/s)", "Throughput vs offered load")
    ax.set_ylim(bottom=0)

    ax = axes[1]
    for i, key in enumerate(("p50_ms", "p95_ms", "p99_ms")):
        ys = [r["overall"][key] for r in runs]
        ax.plot(users, ys, color=SERIES[i], marker="o", label=key.replace("_ms", ""))
        label_end(ax, users[-1], ys[-1], key.replace("_ms", ""), SERIES[i])
    style(ax, "Concurrent users", "Response time (ms)", "Response time vs offered load")
    ax.legend(loc="upper left")
    ax.set_ylim(bottom=0)
    save(fig, "capacity.png")


def plot_threshold() -> None:
    data = load("threshold_sweep.json")
    if not data:
        return
    runs = sorted(data["runs"], key=lambda r: r["threshold"])
    thr = [r["threshold"] for r in runs]
    rps = [r["throughput_rps"] for r in runs]
    p95 = [r["overall"]["p95_ms"] for r in runs]
    p50 = [r["overall"]["p50_ms"] for r in runs]
    switches = [r.get("switch_count") or 0 for r in runs]

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2))

    ax = axes[0]
    ax.plot(thr, rps, color=SERIES[0], marker="o")
    best = max(range(len(rps)), key=lambda i: rps[i])
    ax.scatter([thr[best]], [rps[best]], s=120, facecolor="none",
               edgecolor=SERIES[0], linewidth=2, zorder=5)
    ax.annotate(f"best {rps[best]:.0f} rps\nat threshold {thr[best]:.2f}",
                xy=(thr[best], rps[best]), xytext=(0, -34),
                textcoords="offset points", ha="center", color=INK2, fontsize=9)
    style(ax, "Switching threshold", "Throughput (requests/s)", "Throughput vs threshold")

    ax = axes[1]
    ax.plot(thr, p50, color=SERIES[2], marker="o", label="p50")
    ax.plot(thr, p95, color=SERIES[1], marker="o", label="p95")
    label_end(ax, thr[-1], p50[-1], "p50", SERIES[2])
    label_end(ax, thr[-1], p95[-1], "p95", SERIES[1])
    style(ax, "Switching threshold", "Response time (ms)", "Latency vs threshold")
    ax.legend(loc="upper left")

    ax = axes[2]
    ax.bar([f"{t:.2f}" for t in thr], switches, color=SERIES[0], width=0.6)
    style(ax, "Switching threshold", "Backend switches during run",
          "Routing churn vs threshold")
    ax.tick_params(axis="x", rotation=45)
    save(fig, "threshold_sweep.png")


def plot_algorithms() -> None:
    data = load("algorithm_comparison.json")
    if not data:
        return
    runs = data["runs"]
    names = [r["algorithm"].replace("_", " ") for r in runs]
    rps = [r["throughput_rps"] for r in runs]
    p95 = [r["overall"]["p95_ms"] for r in runs]
    colors = [SERIES[0] if r["algorithm"] != "adaptive_threshold" else SERIES[1]
              for r in runs]

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))
    ax = axes[0]
    bars = ax.bar(names, rps, color=colors, width=0.6)
    for b, v in zip(bars, rps):
        ax.annotate(f"{v:.0f}", xy=(b.get_x() + b.get_width() / 2, v),
                    xytext=(0, 4), textcoords="offset points", ha="center",
                    fontsize=9, color=INK2)
    style(ax, "", "Throughput (requests/s)", "Throughput by routing policy")
    ax.tick_params(axis="x", rotation=20)

    ax = axes[1]
    bars = ax.bar(names, p95, color=colors, width=0.6)
    for b, v in zip(bars, p95):
        ax.annotate(f"{v:.0f}", xy=(b.get_x() + b.get_width() / 2, v),
                    xytext=(0, 4), textcoords="offset points", ha="center",
                    fontsize=9, color=INK2)
    style(ax, "", "p95 response time (ms)", "Tail latency by routing policy")
    ax.tick_params(axis="x", rotation=20)
    fig.text(0.01, -0.04, "Orange = the adaptive threshold policy submitted; "
                          "blue = comparison baselines.", color=MUTED, fontsize=9)
    save(fig, "algorithm_comparison.png")


def _timeline_axes(run: Dict[str, Any], title_suffix: str = "") -> None:
    samples = run["samples"]
    metrics = run["metrics"]

    fig, axes = plt.subplots(2, 1, figsize=(11, 7.6), sharex=True)

    # -- response time --
    ax = axes[0]
    ts = [s["t"] for s in samples if s["ok"]]
    lat = [s["latency_ms"] for s in samples if s["ok"]]
    order = np.argsort(ts)
    ts = np.asarray(ts)[order]
    lat = np.asarray(lat)[order]
    ax.scatter(ts, lat, s=3, color=SERIES[0], alpha=0.18, linewidths=0)
    w = max(2, len(lat) // 60)
    if len(lat) > w:
        smooth = rolling(list(lat), w)
        ax.plot(ts[w - 1:], smooth, color=SERIES[1], linewidth=2)
        label_end(ax, ts[-1], smooth[-1], "rolling mean", SERIES[1])
    style(ax, "", "Response time (ms)",
          f"Response time over the run{title_suffix}")
    ax.set_ylim(bottom=0)
    ax.scatter([], [], s=18, color=SERIES[0], alpha=0.5, label="individual request")
    ax.plot([], [], color=SERIES[1], label="rolling mean")
    ax.legend(loc="upper left")

    # -- utilisation of all four systems --
    ax = axes[1]
    if metrics:
        t = [m["t_rel"] for m in metrics]
        lb = [m["lb"]["cpu"] * 100 for m in metrics]
        ax.plot(t, lb, color=NODE_COLOR["Sys1"], label="Sys1 (load balancer)")
        label_end(ax, t[-1], lb[-1], "Sys1", NODE_COLOR["Sys1"])
        for node in ("Sys2", "Sys3", "Sys4"):
            ys = [m["backends"].get(node, {}).get("cpu", 0) * 100 for m in metrics]
            ax.plot(t, ys, color=NODE_COLOR[node], label=f"{node} (backend)")
            label_end(ax, t[-1], ys[-1], node, NODE_COLOR[node])
        ax.axhline(100, color=MUTED, linewidth=1, linestyle=":")
        ax.annotate("1 CPU = 100%", xy=(0, 100), xytext=(2, 4),
                    textcoords="offset points", color=MUTED, fontsize=9)
    style(ax, "Time (s)", "CPU utilisation (% of the container's 1 core)",
          "System utilisation of all four systems")
    ax.set_ylim(0, 130)
    ax.legend(loc="lower right", ncol=2)
    return fig


def plot_timeline() -> None:
    data = load("timeline.json")
    if not data:
        return
    fig = _timeline_axes(data["run"])
    save(fig, "timeline.png")

    # memory panel, separate figure (its own measure -> its own axis)
    metrics = data["run"]["metrics"]
    if metrics:
        fig, ax = plt.subplots(figsize=(11, 3.8))
        t = [m["t_rel"] for m in metrics]
        ax.plot(t, [m["lb"]["mem"] * 100 for m in metrics],
                color=NODE_COLOR["Sys1"], label="Sys1 (load balancer)")
        label_end(ax, t[-1], metrics[-1]["lb"]["mem"] * 100, "Sys1", NODE_COLOR["Sys1"])
        for node in ("Sys2", "Sys3", "Sys4"):
            ys = [m["backends"].get(node, {}).get("mem", 0) * 100 for m in metrics]
            ax.plot(t, ys, color=NODE_COLOR[node], label=f"{node} (backend)")
            label_end(ax, t[-1], ys[-1], node, NODE_COLOR[node])
        style(ax, "Time (s)", "Memory used (% of the container's 512 MB)",
              "Memory utilisation of all four systems")
        ax.set_ylim(0, 110)
        ax.legend(loc="lower right", ncol=2)
        save(fig, "memory.png")


def plot_routing_share() -> None:
    data = load("timeline.json")
    if not data:
        return
    metrics = data["run"]["metrics"]
    if not metrics:
        return
    fig, ax = plt.subplots(figsize=(11, 4.0))
    t = [m["t_rel"] for m in metrics]
    for node in ("Sys2", "Sys3", "Sys4"):
        ys = [m["backends"].get(node, {}).get("score", 0) for m in metrics]
        ax.plot(t, ys, color=NODE_COLOR[node], label=node)
        label_end(ax, t[-1], ys[-1], node, NODE_COLOR[node])
    thr = metrics[0].get("threshold")
    if thr:
        ax.axhline(thr, color=INK2, linewidth=1.4, linestyle="--")
        ax.annotate(f"switching threshold = {thr}", xy=(0, thr), xytext=(4, 5),
                    textcoords="offset points", color=INK2, fontsize=9)
    style(ax, "Time (s)", "Composite load score",
          "Per-backend load score against the switching threshold")
    ax.legend(loc="upper right", ncol=3)
    save(fig, "load_scores.png")


def plot_failover() -> None:
    data = load("failover.json")
    if not data:
        return
    run = data["run"]
    samples = run["samples"]
    metrics = run["metrics"]
    events = run["summary"].get("events", [])

    fig, axes = plt.subplots(2, 1, figsize=(11, 7.4), sharex=True)

    ax = axes[0]
    ok_t = [s["t"] for s in samples if s["ok"]]
    ok_l = [s["latency_ms"] for s in samples if s["ok"]]
    bad_t = [s["t"] for s in samples if not s["ok"]]
    ax.scatter(ok_t, ok_l, s=3, color=SERIES[0], alpha=0.2, linewidths=0,
               label="successful request")
    if bad_t:
        ax.scatter(bad_t, [max(ok_l or [1])] * len(bad_t), s=18, color="#e34948",
                   marker="x", label=f"failed request (n={len(bad_t)})")
    for ev in events:
        ax.axvline(ev["t"], color=INK2, linestyle="--", linewidth=1.2)
        ax.annotate(ev["event"], xy=(ev["t"], ax.get_ylim()[1]),
                    xytext=(4, -12), textcoords="offset points",
                    color=INK2, fontsize=9, rotation=0)
    style(ax, "", "Response time (ms)", "Behaviour when a backend is killed mid-run")
    ax.legend(loc="upper left")

    ax = axes[1]
    if metrics:
        t = [m["t_rel"] for m in metrics]
        for node in ("Sys2", "Sys3", "Sys4"):
            ys = [m["backends"].get(node, {}).get("cpu", 0) * 100 for m in metrics]
            ax.plot(t, ys, color=NODE_COLOR[node], label=node)
            label_end(ax, t[-1], ys[-1], node, NODE_COLOR[node])
        for ev in events:
            ax.axvline(ev["t"], color=INK2, linestyle="--", linewidth=1.2)
    style(ax, "Time (s)", "CPU utilisation (%)",
          "Backend CPU — surviving nodes absorb the load")
    ax.set_ylim(0, 130)
    ax.legend(loc="upper right", ncol=3)
    save(fig, "failover.png")


def plot_backend_share() -> None:
    data = load("timeline.json")
    if not data:
        return
    share = data["run"]["summary"]["requests_per_backend"]
    if not share:
        return
    nodes = sorted(share)
    counts = [share[n] for n in nodes]
    total = sum(counts) or 1
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    bars = ax.bar(nodes, counts, color=[NODE_COLOR.get(n, SERIES[0]) for n in nodes],
                  width=0.55)
    for b, c in zip(bars, counts):
        ax.annotate(f"{c}\n({100 * c / total:.0f}%)",
                    xy=(b.get_x() + b.get_width() / 2, c), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9, color=INK2)
    style(ax, "", "Requests served", "Work actually placed on each backend")
    ax.set_ylim(0, max(counts) * 1.2)
    save(fig, "backend_share.png")


def main() -> None:
    print("generating figures...")
    for fn in (plot_capacity, plot_threshold, plot_algorithms, plot_timeline,
               plot_routing_share, plot_failover, plot_backend_share):
        try:
            fn()
        except Exception as exc:
            print(f"  {fn.__name__} failed: {type(exc).__name__}: {exc}")
    print("done")


if __name__ == "__main__":
    main()

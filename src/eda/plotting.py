"""Purposeful Matplotlib figures from reduced arrays/tables, closed after saving."""

from __future__ import annotations

import math
import re
import textwrap
from pathlib import Path

import matplotlib

# Portable PNG rendering for CLI, tests, and notebooks; no GUI/Tcl dependency.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

COLORS = ("#276A9C", "#C77C26", "#8B8B38", "#AF6281", "#D4AD35")


def figure_title(title):
    """Keep single-panel titles inside the figure without shrinking all typography."""
    title = re.sub(r" \d\d:\d\d:\d\d", "", title)
    if " | " in title:
        label, period = title.rsplit(" | ", 1)
        return textwrap.fill(label, width=65) + "\n" + period
    return textwrap.fill(title, width=65)


def set_style():
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.titlesize": 12, "axes.labelsize": 11, "figure.facecolor": "white", "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": "#dddddd", "grid.alpha": .5, "savefig.dpi": 160})


def panels(n, title, cols=3):
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(rows, min(cols, n), figsize=(5.2 * min(cols, n), 3.7 * rows), squeeze=False, layout="constrained")
    fig.suptitle(figure_title(title), fontsize=14)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    return fig, list(axes.flat)


def save(fig, root: Path, filename: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    try:
        fig.savefig(target, facecolor="white")
    finally:
        plt.close(fig)
    return target


def ecdf(samples: dict, units: dict, period: str, root: Path, filename: str, title: str):
    if not samples:
        return None
    fig, axes = panels(len(samples), f"{title} | {period}")
    for i, (name, values) in enumerate(samples.items()):
        ax = axes[i]
        values = np.sort(values)
        ax.plot(values, np.arange(1, len(values) + 1) / max(len(values), 1), color=COLORS[i % len(COLORS)])
        ax.set(title=f"{name} (finite display sample n={len(values):,})", xlabel=f"{name} [{units[name]}]", ylabel="Cumulative probability")
        ax.set_ylim(0, 1)
        if len(values) and values.min() >= 0:
            p05, median, p99 = np.quantile(values, [.05, .5, .99])
            if median > 0 and p99 > 5 * median and values.max() > 10 * p99:
                positive = values[values > 0]
                threshold = max(float(p05), float(positive.min()))
                ax.set_xscale("symlog", linthresh=threshold)
                ax.set_xlim(0, float(values.max()) * 1.05)
                ax.set_xlabel(f"{name} [{units[name]}]; symlog, linear below {threshold:.2g}")
    # PM comparisons use a common unit/range and the same axis transformation.
    if len(samples) > 1 and len({units[name] for name in samples}) == 1:
        pooled = np.concatenate(list(samples.values()))
        if pooled.size:
            p05, median, p99 = np.quantile(pooled, [.05, .5, .99])
            use_log = pooled.min() >= 0 and median > 0 and p99 > 5 * median and pooled.max() > 10 * p99
            threshold = max(float(p05), float(pooled[pooled > 0].min())) if use_log else None
            for ax, name in zip(axes, samples):
                ax.set_xscale("symlog", linthresh=threshold) if use_log else ax.set_xscale("linear")
                ax.set_xlim(min(0, float(pooled.min())), float(pooled.max()) * 1.05 or 1)
                ax.set_xlabel(f"{name} [{units[name]}]" + (f"; symlog, linear below {threshold:.2g}" if use_log else ""))
    return save(fig, root, filename)


def temporal(aggregations: dict, names: list, field: str, period: str, root: Path, filename: str, title: str):
    names = [n for n in names if n in aggregations]
    if not names:
        return None
    fig, axes = panels(len(names), f"{title} | {period}")
    for i, name in enumerate(names):
        array = aggregations[name][field]
        dim = array.dims[0]
        x = pd.DatetimeIndex(array[dim].data).year if field == "annual" else array[dim].data
        ax = axes[i]
        ax.plot(x, array.data, color=COLORS[i % 5], marker="o", markersize=3)
        ax.set(title=name, xlabel="Year (UTC)" if field == "annual" else "Month (UTC)" if field == "monthly_climatology" else "Local hour (UTC+7)", ylabel=f"{name} [{array.attrs.get('units', '')}]")
        if field == "monthly_climatology":
            ax.set_xticks(np.arange(1, 13))
        elif field == "diurnal":
            ax.set_xticks(x)
        elif len(x) > 1:
            ax.set_xticks(np.unique(np.linspace(x[0], x[-1], min(6, len(x))).round().astype(int)))
    return save(fig, root, filename)


def year_month(aggregations, names, period, root, filename):
    names = [n for n in names if n in aggregations]
    if not names:
        return None
    fig, axes = panels(len(names), f"Year × month: area-weighted mean | {period}")
    for ax, name in zip(axes, names):
        array = aggregations[name]["monthly"]
        dates = pd.DatetimeIndex(array.time.data)
        table = pd.DataFrame({"year": dates.year, "month": dates.month, "value": array.data}).pivot(index="year", columns="month", values="value").reindex(columns=range(1, 13))
        im = ax.pcolormesh(np.arange(13) + .5, np.arange(len(table) + 1) - .5, np.ma.masked_invalid(table.to_numpy()), cmap="cividis", shading="flat")
        indices = np.unique(np.linspace(0, len(table) - 1, min(7, len(table))).round().astype(int))
        ax.set(title=name, xlabel="Month (UTC)", ylabel="Year (UTC)", xticks=range(1, 13), yticks=indices, yticklabels=table.index[indices])
        ax.grid(False)
        fig.colorbar(im, ax=ax, label=f"{name} [{array.attrs.get('units', '')}]")
    return save(fig, root, filename)


def maps(fields: list, period: str, root: Path, filename: str, title: str, diverging=False):
    if not fields:
        return None
    fig, axes = panels(len(fields), f"{title} | {period}")
    for ax, (label, array) in zip(axes, fields):
        values = np.ma.masked_invalid(array.data)
        options = {}
        if diverging and values.count():
            extent = float(np.max(np.abs(values))) or 1
            options = {"vmin": -extent, "vmax": extent}
        im = ax.pcolormesh(array.longitude.data, array.latitude.data, values, shading="nearest", cmap="PuOr_r" if diverging else "cividis", **options)
        ax.set(title=textwrap.fill(label, width=32), xlabel="Longitude [°E]", ylabel="Latitude [°N]")
        ax.set_aspect(1 / np.cos(np.deg2rad(float(array.latitude.mean()))))
        ax.grid(False)
        fig.colorbar(im, ax=ax, label=array.attrs.get("units", ""), shrink=.85)
    return save(fig, root, filename)


def correlations(pearson: pd.DataFrame, spearman: pd.DataFrame, period, root):
    if pearson.empty:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(16, 7.6), layout="constrained")
    fig.suptitle(figure_title(f"Daily area-weighted mean correlations | {period}"), fontsize=14)
    for ax, table, method in zip(axes, (pearson, spearman), ("Pearson", "Spearman")):
        im = ax.imshow(np.ma.masked_invalid(table.to_numpy()), vmin=-1, vmax=1, cmap="PuOr_r")
        ax.set(title=method, xlabel="Variable", ylabel="Variable", xticks=range(len(table)), yticks=range(len(table)), xticklabels=table.columns, yticklabels=table.index)
        ax.tick_params(axis="x", rotation=60)
        ax.grid(False)
        fig.colorbar(im, ax=ax, label="Correlation coefficient", shrink=.8)
    return save(fig, root, "12_daily_correlation_matrices.png")


def relationships(daily: pd.DataFrame, units: dict, names: list, period, root, filename, title):
    if "pm2p5" not in daily:
        return None
    names = [n for n in names if n in daily and len(daily[[n, "pm2p5"]].dropna()) >= 3]
    if not names:
        return None
    fig, axes = panels(len(names), f"{title}: daily area-weighted means | {period}")
    for ax, name in zip(axes, names):
        pair = daily[[name, "pm2p5"]].dropna()
        im = ax.hexbin(pair[name], pair.pm2p5, gridsize=30, mincnt=1, cmap="cividis")
        ax.set(title=f"{name} (paired days n={len(pair):,})", xlabel=f"{name} [{units[name]}]", ylabel=f"PM2.5 [{units['pm2p5']}]")
        fig.colorbar(im, ax=ax, label="Paired days per hexagon")
    return save(fig, root, filename)


def extremes(yearly: pd.DataFrame, monthly: pd.DataFrame, comparison: pd.DataFrame, period, root):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.3), layout="constrained")
    fig.suptitle(figure_title(f"PM2.5 exceedances: UTC timesteps with ≥1 affected cell | {period}"), fontsize=14)
    for ax, table, xlabel in zip(axes, (yearly, monthly), ("Year (UTC)", "Month (UTC)")):
        for i, field in enumerate(("p95_timestep_count", "p99_timestep_count")):
            ax.plot(table.index, table[field], marker="o", label=field.split("_")[0].upper(), color=COLORS[i])
        ax.set(xlabel=xlabel, ylabel="Exceeding 3-hour timesteps")
        ax.legend()
    outputs = [save(fig, root, "16_pm25_extreme_event_counts.png")]
    if not comparison.empty:
        fig, axes = panels(len(comparison), f"Co-located conditions during PM2.5 > pooled P95 | {period}")
        for ax, (_, row) in zip(axes, comparison.iterrows()):
            ax.bar(["Normal", ">P95"], [row.normal_mean, row.extreme_mean], color=COLORS[:2])
            ax.set(title=row.variable, xlabel="PM2.5 grid-time condition", ylabel=f"{row.variable} [{row.unit}]")
            ax.text(.02, .98, f"Paired finite n: {int(row.normal_count):,} / {int(row.extreme_count):,}", transform=ax.transAxes, va="top", fontsize=9)
        outputs.append(save(fig, root, "18_pm25_extreme_conditions.png"))
    return outputs

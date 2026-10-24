"""Xarray-first reductions at explicit temporal/spatial grains."""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr


def regional_mean(array: xr.DataArray) -> xr.DataArray:
    """Cos(latitude) area weighting over the requested bounding box (including sea)."""
    weights = np.cos(np.deg2rad(array.latitude))
    out = array.where(np.isfinite(array)).weighted(weights).mean(("latitude", "longitude"))
    out.attrs = array.attrs.copy()
    return out


def temporal_aggregations(array: xr.DataArray) -> dict:
    regional = regional_mean(array).compute(scheduler="synchronous")
    # Keep UTC as the canonical time; UTC+7 is an auxiliary coordinate.
    regional = regional.assign_coords(time_local=("time", regional.time.data + np.timedelta64(7, "h")))
    daily = regional.resample(time="1D").mean()
    monthly = regional.resample(time="MS").mean()
    annual = regional.resample(time="YS").mean()
    # These climatologies average actual 3-hour observations, not unequal monthly means.
    climatology = regional.groupby("time.month").mean().reindex(month=np.arange(1, 13))
    diurnal = regional.assign_coords(local_hour=regional.time_local.dt.hour).groupby("local_hour").mean()
    counts = regional.notnull().resample(time="YS").sum()
    daily_counts = regional.notnull().resample(time="1D").sum()
    return {"regional": regional, "daily": daily, "monthly": monthly, "annual": annual, "monthly_climatology": climatology, "diurnal": diurnal, "annual_count": counts, "daily_count": daily_counts}


def spatial_statistics(array: xr.DataArray) -> xr.Dataset:
    finite = array.where(np.isfinite(array))
    # Exact time quantiles: complete time axis for only 4 × 4 cells per task.
    tiled = finite.chunk({"time": -1, "latitude": 4, "longitude": 4})
    quantiles = tiled.quantile([.5, .95], dim="time", skipna=True)
    out = xr.Dataset({"mean": finite.mean("time"), "median": quantiles.sel(quantile=.5, drop=True), "std": finite.std("time", ddof=0), "p95": quantiles.sel(quantile=.95, drop=True), "finite_count": np.isfinite(array).sum("time")}).compute(scheduler="synchronous")
    for field in ("mean", "median", "std", "p95"):
        out[field].attrs = array.attrs.copy()
    return out


def annual_grid_trend(array: xr.DataArray, samples_per_day=8, minimum_coverage=.95) -> xr.Dataset:
    """OLS on adequately observed annual grid means, at least 3 eligible years.

    No significance claim: temporal dependence is not corrected by simple OLS.
    """
    finite = array.where(np.isfinite(array))
    annual = finite.resample(time="YS").mean().compute(scheduler="synchronous")
    counts = finite.notnull().resample(time="YS").sum().compute(scheduler="synchronous")
    years = annual.time.dt.year
    expected = xr.DataArray([samples_per_day * (366 if pd.Timestamp(int(y), 12, 31).is_leap_year else 365) for y in years.data], dims="time", coords={"time": annual.time})
    coverage = counts / expected
    eligible = annual.where(coverage >= minimum_coverage)
    x = years.astype(float)
    mask = np.isfinite(eligible)
    n = mask.sum("time")
    x_mean = x.where(mask).mean("time")
    y_mean = eligible.mean("time")
    covariance = ((x - x_mean) * (eligible - y_mean)).sum("time")
    variance = ((x - x_mean) ** 2).where(mask).sum("time")
    slope = (covariance / variance.where(variance > 0)).where(n >= 3)
    slope.attrs = {"units": f"{array.attrs.get('units', '')}/year", "long_name": f"Annual-mean linear trend: {array.name}", "minimum_annual_coverage": minimum_coverage, "minimum_years": 3}
    return xr.Dataset({"slope": slope, "eligible_years": n, "annual_mean": annual, "annual_coverage": coverage})


def reproducible_sample(array: xr.DataArray, size=50000, seed=42) -> np.ndarray:
    """Uniform grid-time sample for ECDF display only; summary percentiles are exact."""
    rng = np.random.default_rng(seed)
    indices = np.sort(rng.choice(array.size, min(size, array.size), replace=False))
    # Read only bounded time blocks and selected entries; do not stack a giant DataFrame.
    plane = array.sizes["latitude"] * array.sizes["longitude"]
    samples = []
    for begin in range(0, array.sizes["time"], 2048):
        selected = indices[(indices >= begin * plane) & (indices < min(begin + 2048, array.sizes["time"]) * plane)]
        if len(selected):
            block = array.isel(time=slice(begin, begin + 2048)).compute(scheduler="synchronous").data
            samples.append(block.reshape(-1)[selected - begin * plane])
    values = np.concatenate(samples) if samples else np.array([])
    return values[np.isfinite(values)]

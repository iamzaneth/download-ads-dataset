"""Coordinate audits, exact descriptive statistics, and physical checks."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .transformations import COLUMNS, PM, exact_align

QUANTILES = np.array([.01, .05, .25, .5, .75, .9, .95, .99])
QUANTILE_NAMES = ("p01", "p05", "p25", "median", "p75", "p90", "p95", "p99")


def resolution(values) -> str:
    diffs = np.abs(np.diff(np.asarray(values, dtype=float)))
    return ", ".join(f"{v:g}" for v in np.unique(np.round(diffs, 10))) if len(diffs) else "singleton"


def inventory_metadata(array: xr.DataArray) -> dict:
    time = pd.DatetimeIndex(array.time.data)
    lat, lon = array.latitude.data, array.longitude.data
    delta = np.diff(time.as_unit("ns").asi8)
    return {
        "grib_variable_name": array.attrs.get("GRIB_shortName", array.name),
        "long_name": array.attrs.get("long_name", array.attrs.get("GRIB_name", "")),
        "unit": array.attrs.get("units", array.attrs.get("GRIB_units", "")),
        "dimensions": str(dict(array.sizes)), "n_time": len(time), "n_lat": len(lat), "n_lon": len(lon),
        "start_time": str(time.min()), "end_time": str(time.max()),
        "time_frequency": ", ".join(str(pd.Timedelta(int(d), unit="ns")) for d in np.unique(delta)),
        "lat_min": float(np.min(lat)), "lat_max": float(np.max(lat)), "lon_min": float(np.min(lon)), "lon_max": float(np.max(lon)),
        "lat_resolution": resolution(lat), "lon_resolution": resolution(lon), "dtype": str(array.dtype),
    }


def consistency(array: xr.DataArray, reference: xr.DataArray, expected: pd.DatetimeIndex, audit: dict) -> dict:
    time = pd.DatetimeIndex(array.time.data).as_unit("ns")
    ref_time = pd.DatetimeIndex(reference.time.data).as_unit("ns")
    unique = time.unique().sort_values()
    internal = expected[(expected >= unique.min()) & (expected <= unique.max())]
    missing, missing_internal = expected.difference(unique), internal.difference(unique)
    missing_ranges = []
    if len(missing):
        split = np.flatnonzero(np.diff(missing.asi8) != pd.Timedelta(expected.freq).value) + 1
        for group in np.split(missing.asi8, split):
            missing_ranges.append(f"{pd.Timestamp(group[0])} to {pd.Timestamp(group[-1])} ({len(group)} timestamps)")
    delta = np.diff(time.asi8)
    notes = []
    for coord in ("latitude", "longitude"):
        actual, ref = np.asarray(array[coord].data), np.asarray(reference[coord].data)
        if not np.array_equal(actual, ref):
            mismatch = np.flatnonzero(actual != ref) if actual.shape == ref.shape else np.arange(min(len(actual), len(ref)))
            notes.append(f"{coord}: lengths {len(actual)}/{len(ref)}, first differing positions {mismatch[:5].tolist()}; actual {actual[:3].tolist()}, reference {ref[:3].tolist()}")
        if len(np.unique(actual)) != len(actual):
            notes.append(f"duplicate {coord} coordinates")
    if not time.equals(ref_time):
        notes.append(f"time: absent vs reference={len(ref_time.difference(time))}, extra={len(time.difference(ref_time))}, ordering equal={np.array_equal(time.asi8, ref_time.asi8)}")
    lat_diff = np.diff(array.latitude.data)
    return {
        "variable": array.name, "same_time_grid": time.equals(ref_time),
        "same_spatial_grid": all(np.array_equal(array[c].data, reference[c].data) for c in ("latitude", "longitude")),
        "missing_timestamps": len(missing), "internal_missing_timestamps": len(missing_internal),
        "missing_examples": "; ".join(map(str, missing[:5])),
        "missing_ranges": "; ".join(missing_ranges),
        "duplicate_timestamps": int(time.duplicated().sum()), "duplicate_messages": audit.get("duplicate_messages", 0),
        "identical_duplicate_messages": audit.get("identical_duplicate_messages", 0),
        "conflicting_duplicate_messages": audit.get("conflicting_duplicate_messages", 0),
        "duplicate_values_verified": audit.get("duplicate_values_verified", not audit.get("duplicate_messages", 0)),
        "duplicate_message_examples": "; ".join(audit.get("duplicate_message_examples", [])),
        "monotonic_time": time.is_monotonic_increasing,
        "irregular_intervals": int(np.count_nonzero(delta != pd.Timedelta(expected.freq).value)) if len(expected) > 1 else 0,
        "off_schedule_timestamps": len(unique.difference(expected)),
        "latitude_order": "ascending" if np.all(lat_diff > 0) else "descending" if np.all(lat_diff < 0) else "nonmonotonic/singleton",
        "lat_resolution": resolution(array.latitude.data), "lon_resolution": resolution(array.longitude.data),
        "latitude_values": str(array.latitude.data.tolist()), "longitude_values": str(array.longitude.data.tolist()),
        "notes": "; ".join(notes) or "matches reference coordinates",
    }


def exact_quantiles(array: xr.DataArray, cache_root: Path, chunk_time=2048) -> np.ndarray:
    """Exact global quantiles using one temporary disk buffer, never a raw DataFrame."""
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root, prefix="quantile-") as folder:
        scratch = np.memmap(Path(folder) / "finite.bin", mode="w+", dtype="float32", shape=(array.size,))
        count = 0
        try:
            for begin in range(0, array.sizes["time"], chunk_time):
                block = np.asarray(array.isel(time=slice(begin, begin + chunk_time)).compute(scheduler="synchronous").data)
                finite = block[np.isfinite(block)]
                scratch[count:count + finite.size] = finite
                count += finite.size
            return np.percentile(scratch[:count], QUANTILES * 100, overwrite_input=True) if count else np.full(len(QUANTILES), np.nan)
        finally:
            del scratch


def descriptive_statistics(array: xr.DataArray, cache_root: Path, chunk_time=2048) -> dict:
    finite_mask = np.isfinite(array)
    finite = array.where(finite_mask)
    reductions = xr.Dataset({"count": finite_mask.sum(), "nan_count": array.isnull().sum(), "inf_count": np.isinf(array).sum(), "mean": finite.mean(), "std": finite.std(ddof=0), "min": finite.min(), "max": finite.max()}).compute(scheduler="synchronous")
    result = {k: float(v.item()) for k, v in reductions.data_vars.items()}
    for k in ("count", "nan_count", "inf_count"):
        result[k] = int(result[k])
    result.update(variable=array.name, unit=array.attrs.get("units", ""), total_count=array.size, missing_count=result["nan_count"], missing_pct=100 * result["nan_count"] / array.size, nonfinite_pct=100 * (array.size - result["count"]) / array.size, quantile_method="exact finite pooled grid-time values; linear percentile", std_ddof=0)
    quantiles = exact_quantiles(array, cache_root, chunk_time)
    result.update(zip(QUANTILE_NAMES, map(float, quantiles)))
    lower, upper = result["p25"] - 3 * (result["p75"] - result["p25"]), result["p75"] + 3 * (result["p75"] - result["p25"])
    result["suspicious_3iqr_count"] = int(((finite < lower) | (finite > upper)).sum().compute(scheduler="synchronous").item())
    nonnegative = array.name in (*PM, *COLUMNS, "aod550", "sp", "mslp", "wind_speed", "relative_humidity")
    result["negative_count"] = int((finite < 0).sum().compute(scheduler="synchronous").item()) if nonnegative else np.nan
    result["suspicious_rule"] = "outside [Q1 - 3 IQR, Q3 + 3 IQR], review flag only; no removal"
    return result


def check_mask(name: str, eligible: xr.DataArray, violations: xr.DataArray, note="", max_examples=5) -> dict:
    totals = xr.Dataset({"eligible": eligible.sum(), "violations": (eligible & violations).sum()}).compute(scheduler="synchronous")
    denominator, count = int(totals.eligible.item()), int(totals.violations.item())
    examples = []
    if count:
        for begin in range(0, eligible.sizes["time"], 2048):
            mask = (eligible & violations).isel(time=slice(begin, begin + 2048)).compute(scheduler="synchronous")
            for ti, yi, xi in np.argwhere(mask.data)[:max_examples - len(examples)]:
                examples.append({"time_utc": str(pd.Timestamp(mask.time.data[ti])), "latitude": float(mask.latitude.data[yi]), "longitude": float(mask.longitude.data[xi])})
            if len(examples) >= max_examples:
                break
    return {"check": name, "status": "checked" if denominator else "skipped: no finite eligible observations", "eligible_count": denominator, "violation_count": count, "violation_pct": 100 * count / denominator if denominator else np.nan, "examples": str(examples), "notes": note}


def physical_checks(arrays: dict, unsafe: set, tolerance=.01) -> pd.DataFrame:
    rows = []
    specifications = [("PM1 <= PM2.5 <= PM10", PM), ("dewpoint <= temperature", ("d2m", "t2m")), ("0 <= RH <= 100", ("relative_humidity",))]
    for label, required in specifications:
        if not all(k in arrays for k in required):
            rows.append({"check": label, "status": "skipped: required variable not available"})
            continue
        try:
            if unsafe.intersection(required):
                raise ValueError("duplicate source GRIB messages/timestamps")
            aligned = exact_align(*(arrays[k] for k in required))
            if label.startswith("PM1"):
                if len({a.attrs.get("units") for a in aligned}) != 1 or aligned[0].attrs.get("units") != "µg/m³":
                    raise ValueError("PM units are not confirmed comparable mass concentration")
                eligible = np.isfinite(aligned[0]) & np.isfinite(aligned[1]) & np.isfinite(aligned[2])
                violations = (aligned[0] > aligned[1]) | (aligned[1] > aligned[2])
                note = "strict ordering; denominator = finite co-located triples"
            elif label.startswith("dewpoint"):
                if any(a.attrs.get("units") != "°C" for a in aligned):
                    raise ValueError("temperature units not confirmed")
                eligible = np.isfinite(aligned[0]) & np.isfinite(aligned[1])
                violations = aligned[0] > aligned[1] + tolerance
                note = f"floating-point tolerance {tolerance} °C"
            else:
                eligible = np.isfinite(aligned[0])
                violations = (aligned[0] < 0) | (aligned[0] > 100)
                note = "unclipped RH; finite denominator"
            rows.append(check_mask(label, eligible, violations, note))
        except ValueError as error:
            rows.append({"check": label, "status": f"skipped: {error}"})
    if all(k in arrays for k in ("t2m", "d2m")):
        try:
            t, td = exact_align(arrays["t2m"], arrays["d2m"])
            if t.attrs.get("units") == td.attrs.get("units") == "°C":
                rows.append(check_mask("Magnus formula range (-45 to 60 °C)", np.isfinite(t) & np.isfinite(td), (t < -45) | (t > 60) | (td < -45) | (td > 60), "flags approximation outside documented range; values retained"))
        except ValueError:
            pass
    return pd.DataFrame(rows)

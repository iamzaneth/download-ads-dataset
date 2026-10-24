"""Metadata-gated conversions and physical derived quantities."""

from __future__ import annotations

import re

import numpy as np
import xarray as xr

PM = ("pm1", "pm2p5", "pm10")
COLUMNS = ("co_column", "no2_column", "so2_column", "o3_column")
RH_FORMULA = (
    "RH = 100 exp[17.62 Td/(243.12 + Td) - 17.62 T/(243.12 + T)], "
    "T and Td in degrees Celsius; Magnus saturation vapour pressure over liquid "
    "water (WMO Guide No. 8). Approximation, no pressure enhancement correction; "
    "no clipping. Formula range checked separately: -45 to 60 degrees Celsius."
)
RH_SOURCE = "https://etrp.wmo.int/pluginfile.php/97653/mod_folder/content/0/Training%20Material/5_1_Humidity%20Part%201%20Introduction.pdf"


def unit_token(unit: str) -> str:
    """Recognise common equivalent GRIB/CF spellings, without guessing units."""
    unit = str(unit).lower().strip().replace("µ", "u").replace("μ", "u")
    unit = unit.replace("³", "3").replace("²", "2").replace("−", "-")
    return re.sub(r"[\s*^{}()]", "", unit)


def normalize_units(array: xr.DataArray, short_name: str) -> tuple[xr.DataArray, dict]:
    source = str(array.attrs.get("units", array.attrs.get("GRIB_units", "")))
    token = unit_token(source)
    factor, offset, target, action = 1.0, 0.0, source, "retained metadata unit"
    if short_name in PM:
        if token in {"kgm-3", "kg/m3"}:
            factor, target, action = 1e9, "µg/m³", "kg/m³ × 1e9"
        elif token in {"ug/m3", "ugm-3"}:
            target = "µg/m³"
        else:
            action = "unrecognised/missing PM unit: retained; unit-dependent checks skipped"
    elif short_name in {"t2m", "d2m"}:
        if token in {"k", "kelvin"}:
            offset, target, action = -273.15, "°C", "K - 273.15"
        elif token in {"c", "°c", "degc", "celsius", "degreecelsius", "degreescelsius"}:
            target = "°C"
        else:
            action = "unrecognised/missing temperature unit: retained; RH skipped"
    elif short_name in {"sp", "mslp"} and token in {"pa", "pascal", "pascals"}:
        factor, target, action = 0.01, "hPa", "Pa / 100"
    elif short_name == "aod550" and token in {"1", "dimensionless", "~", "(0-1)", "0-1"}:
        target, action = "1", "dimensionless optical depth retained"
    elif short_name in COLUMNS:
        action = "total column: metadata unit retained; not a surface concentration"
    # Operations remain lazy for Dask/backend arrays. Never modify the input.
    converted = array * factor + offset if factor != 1 or offset else array.copy(deep=False)
    converted = converted.rename(short_name)
    converted.attrs = {**array.attrs, "units": target, "source_units": source, "conversion": action}
    return converted, {"variable": short_name, "source_unit": source, "analysis_unit": target, "conversion": action}


def exact_align(*arrays: xr.DataArray) -> tuple[xr.DataArray, ...]:
    """Reject differing coordinates, order, dimensions, or duplicate timestamps."""
    for array in arrays:
        if array.dims != ("time", "latitude", "longitude"):
            raise ValueError(f"Unexpected dimensions: {array.dims}")
        if not array.get_index("time").is_unique:
            raise ValueError("Duplicate timestamps: cross-variable analysis is unsafe")
        for coordinate in ("latitude", "longitude"):
            if not array.get_index(coordinate).is_unique:
                raise ValueError(f"Duplicate {coordinate} coordinates: cross-variable analysis is unsafe")
    return xr.align(*arrays, join="exact", copy=False)


def wind_fields(u: xr.DataArray, v: xr.DataArray) -> dict[str, xr.DataArray]:
    if any(unit_token(a.attrs.get("units", "")) not in {"ms-1", "m/s"} for a in (u, v)):
        raise ValueError("Wind components must have confirmed m/s units")
    u, v = exact_align(u, v)
    speed = np.hypot(u, v).rename("wind_speed")
    speed.attrs = {"units": "m/s", "long_name": "10 m wind speed", "formula": "hypot(u10, v10)"}
    direction = ((270 - np.degrees(np.arctan2(v, u))) % 360).where(speed > 0).rename("wind_direction")
    direction.attrs = {"units": "degrees", "long_name": "Meteorological wind direction from north", "calm_policy": "NaN when speed = 0"}
    return {"wind_speed": speed, "wind_direction": direction}


def relative_humidity(t: xr.DataArray, td: xr.DataArray) -> xr.DataArray:
    if any(a.attrs.get("units") != "°C" for a in (t, td)):
        raise ValueError("RH requires temperatures converted from confirmed metadata to °C")
    t, td = exact_align(t, td)
    rh = (100 * np.exp(17.62 * td / (243.12 + td) - 17.62 * t / (243.12 + t))).rename("relative_humidity")
    rh.attrs = {"units": "%", "long_name": "Relative humidity over liquid water", "formula": RH_FORMULA, "source": RH_SOURCE}
    return rh


def safe_ratio(numerator: xr.DataArray, denominator: xr.DataArray, name: str) -> xr.DataArray:
    numerator, denominator = exact_align(numerator, denominator)
    if not numerator.attrs.get("units") or numerator.attrs.get("units") != denominator.attrs.get("units"):
        raise ValueError("PM ratios require matching confirmed units")
    # Non-positive or non-finite denominator is undefined, explicitly represented as NaN.
    ratio = (numerator / denominator.where((denominator > 0) & np.isfinite(denominator))).rename(name)
    ratio.attrs = {"units": "1", "long_name": name, "denominator_policy": "NaN for nonpositive/nonfinite denominator"}
    return ratio

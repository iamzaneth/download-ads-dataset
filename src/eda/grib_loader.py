"""GRIB discovery, lazy cfgrib loading, and rebuildable disk-backed analysis cache.

No files, indexes, or other sidecars are written beside raw GRIB inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path

import dask.array as da
import numpy as np
import pandas as pd
import xarray as xr

from src.download_cams_dataset import VARIABLES, MERGED_DIR_NAME, merged_file_path
from .transformations import normalize_units

CACHE_VERSION = 1


def discover_files(data_root: Path, start_year: int, end_year: int, selected=None):
    """One row per downloader variable; refuse ambiguous fallback matches."""
    selected = set(VARIABLES if selected is None else selected)
    unknown = selected.difference(VARIABLES)
    if unknown:
        raise ValueError(f"Unknown downloader variables: {sorted(unknown)}")
    folder = data_root / MERGED_DIR_NAME
    files = sorted(folder.glob("*.grib"))
    files_by_variable = {}
    for path in files:
        match = re.fullmatch(r"(.+)_[0-9]{4}_[0-9]{4}_vietnam\.grib", path.name)
        if match and match[1] in VARIABLES:
            files_by_variable.setdefault(match[1], []).append(path)
    used, paths, rows = set(), {}, []
    for name, source in VARIABLES.items():
        row = {"short_name": name, "source_variable_name": source, "status": "missing", "filename": "", "file_size": 0, "notes": ""}
        if name not in selected:
            row["status"] = "not_selected"
            rows.append(row)
            continue
        expected = merged_file_path(data_root, name, start_year, end_year)
        candidates = [expected] if expected.is_file() else files_by_variable.get(name, [])
        if len(candidates) > 1:
            row.update(status="ambiguous", notes="Multiple files: " + ", ".join(p.name for p in candidates))
            used.update(candidates)
        elif candidates:
            path = candidates[0]
            used.add(path)
            paths[name] = path
            row.update(status="available", filename=str(path.resolve()), file_size=path.stat().st_size)
        rows.append(row)
    # Known but excluded files are not mislabeled as unknown extras.
    known = {p for variable_files in files_by_variable.values() for p in variable_files}
    extras = pd.DataFrame([{"filename": str(p.resolve()), "file_size": p.stat().st_size, "reason": "unselected/alternate file" if p in known else "not declared in VARIABLES"} for p in files if p not in used], columns=["filename", "file_size", "reason"])
    return paths, pd.DataFrame(rows), extras


def fingerprint(path: Path, filters: dict) -> str:
    from importlib.metadata import version
    stat = path.stat()
    identity = {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "filter_by_keys": filters, "cache_version": CACHE_VERSION, "cfgrib": version("cfgrib"), "eccodes": version("eccodes")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def canonical_array(array: xr.DataArray) -> xr.DataArray:
    """Preserve actual grid order; use valid UTC time for scalar GRIB steps."""
    rename = {k: v for k, v in {"lat": "latitude", "lon": "longitude"}.items() if k in array.coords}
    array = array.rename(rename)
    if "time" not in array.coords:
        raise ValueError("No GRIB time coordinate")
    if "step" in array.dims and array.sizes["step"] > 1:
        raise ValueError("Multiple forecast steps require explicit filter_by_keys; not EAC4 analysis timestamps")
    for dim in list(array.dims):
        if dim not in {"time", "latitude", "longitude"}:
            if array.sizes[dim] != 1:
                raise ValueError(f"Non-singleton extra GRIB dimension {dim}; set filter_by_keys")
            array = array.isel({dim: 0}, drop=True)
    if "time" not in array.dims:
        array = array.expand_dims("time")
    if "valid_time" in array.coords:
        valid = array.valid_time
        if valid.ndim == 1 and valid.dims == ("time",):
            array = array.assign_coords(forecast_reference_time=array.time, time=valid.data)
    if set(array.dims) != {"time", "latitude", "longitude"}:
        raise ValueError(f"Only regular latitude/longitude grids supported: {array.dims}")
    if array.latitude.ndim != 1 or array.longitude.ndim != 1:
        raise ValueError("Curvilinear grid requires a different spatial workflow")
    return array.transpose("time", "latitude", "longitude")


@contextmanager
def open_grib(path: Path, name: str, cache_root: Path, chunk_time=2048, filters=None):
    """Use explicit selectors or cfgrib hypercube splitting; never suppress errors."""
    import cfgrib

    path, cache_root = Path(path).resolve(), Path(cache_root).resolve()
    filters = filters or {}
    cache_root.mkdir(parents=True, exist_ok=True)
    key = fingerprint(path, filters)
    kwargs = {"indexpath": str(cache_root / f"{key}.{{short_hash}}.idx"), "filter_by_keys": filters, "errors": "raise", "read_keys": ["name", "units", "shortName", "paramId", "typeOfLevel", "level", "stepType"], "cache_geo_coords": False}
    opened = []
    try:
        try:
            opened = [xr.open_dataset(path, engine="cfgrib", chunks={"time": chunk_time}, backend_kwargs=kwargs)]
        except cfgrib.dataset.DatasetBuildError:
            opened = cfgrib.open_datasets(str(path), chunks={"time": chunk_time}, backend_kwargs=kwargs)
        available = []
        for ds in opened:
            for grib_name, array in ds.data_vars.items():
                available.append((grib_name, array))
        matching = [array for grib_name, array in available if grib_name == name or array.attrs.get("GRIB_shortName") == name]
        candidates = [canonical_array(array) for array in (matching or [array for _, array in available])]
        if not candidates:
            raise ValueError(f"No unambiguous variable for {name}; configure filter_by_keys")
        reference = candidates[0]
        for other in candidates[1:]:
            for coord in ("latitude", "longitude"):
                if not reference[coord].identical(other[coord]):
                    raise ValueError("GRIB hypercubes have different spatial grids; configure filter_by_keys")
            for attr in ("GRIB_paramId", "GRIB_typeOfLevel", "GRIB_level", "units"):
                if reference.attrs.get(attr) != other.attrs.get(attr):
                    raise ValueError(f"Ambiguous hypercubes ({attr}); configure filter_by_keys")
        array = reference if len(candidates) == 1 else xr.concat(candidates, dim="time", join="exact", compat="equals", coords="minimal")
        yield array, key, len(opened)
    finally:
        for ds in opened:
            ds.close()


def audit_messages(path: Path, attrs: dict, filters: dict, cache_root: Path, key: str, rebuild=False) -> dict:
    """Scan ecCodes headers independently: cfgrib may collapse duplicate messages."""
    import eccodes

    target = cache_root / f"{key}.audit.json"
    if target.exists() and not rebuild:
        result = json.loads(target.read_text(encoding="utf-8"))
        if not result["duplicate_messages"]:
            result.update(identical_duplicate_messages=0, conflicting_duplicate_messages=0, duplicate_values_verified=True)
        elif not result.get("duplicate_values_verified"):
            result.update(verify_duplicate_values(path, attrs, filters))
        target.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    seen, duplicate_examples, duplicate_count, count = set(), [], 0, 0
    selections = {**filters}
    for k in ("paramId", "typeOfLevel", "level", "stepType"):
        value = attrs.get(f"GRIB_{k}")
        if value is not None:
            selections.setdefault(k, value)
    # A unique full message identity avoids confusing different levels/steps with duplicates.
    with path.open("rb") as stream:
        while True:
            handle = eccodes.codes_grib_new_from_file(stream, headers_only=True)
            if handle is None:
                break
            try:
                if any(eccodes.codes_get(handle, k) != v for k, v in selections.items()):
                    continue
                identity = tuple(eccodes.codes_get(handle, k) for k in ("validityDate", "validityTime", "paramId", "typeOfLevel", "level", "stepType"))
                count += 1
                if identity in seen:
                    duplicate_count += 1
                    if len(duplicate_examples) < 5:
                        duplicate_examples.append(str(identity))
                seen.add(identity)
            finally:
                eccodes.codes_release(handle)
    result = {"message_count": count, "duplicate_messages": duplicate_count, "duplicate_message_examples": duplicate_examples}
    if duplicate_count:
        result.update(verify_duplicate_values(path, attrs, filters))
    else:
        result.update(identical_duplicate_messages=0, conflicting_duplicate_messages=0, duplicate_values_verified=True)
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def verify_duplicate_values(path: Path, attrs: dict, filters: dict) -> dict:
    """Compare every duplicate with its first message, without changing the raw stream.

    Only duplicate grids are decoded. Identical values/geometry can safely use
    cfgrib's already documented first-message representation; conflicts cannot.
    """
    import eccodes

    selections = dict(filters)
    for k in ("paramId", "typeOfLevel", "level", "stepType"):
        if attrs.get(f"GRIB_{k}") is not None:
            selections.setdefault(k, attrs[f"GRIB_{k}"])
    first_offsets, identical, conflicting = {}, 0, 0
    geometry = ("gridType", "Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees", "latitudeOfLastGridPointInDegrees", "longitudeOfLastGridPointInDegrees", "iScansNegatively", "jScansPositively")
    with path.open("rb") as stream, path.open("rb") as reference_stream:
        while True:
            offset = stream.tell()
            handle = eccodes.codes_grib_new_from_file(stream, headers_only=True)
            if handle is None:
                break
            try:
                if any(eccodes.codes_get(handle, k) != v for k, v in selections.items()):
                    continue
                identity = tuple(eccodes.codes_get(handle, k) for k in ("validityDate", "validityTime", "paramId", "typeOfLevel", "level", "stepType"))
                if identity not in first_offsets:
                    first_offsets[identity] = offset
                    continue
                reference_stream.seek(first_offsets[identity])
                first = eccodes.codes_grib_new_from_file(reference_stream)
                try:
                    same_grid = all(eccodes.codes_get(first, k) == eccodes.codes_get(handle, k) for k in geometry)
                    same_values = np.array_equal(eccodes.codes_get_values(first), eccodes.codes_get_values(handle), equal_nan=True)
                    if same_grid and same_values:
                        identical += 1
                    else:
                        conflicting += 1
                finally:
                    eccodes.codes_release(first)
            finally:
                eccodes.codes_release(handle)
    return {"identical_duplicate_messages": identical, "conflicting_duplicate_messages": conflicting, "duplicate_values_verified": True}


def write_array_cache(array: xr.DataArray, cache_root: Path, key: str, chunk_time=2048, rebuild=False) -> xr.DataArray:
    """Decode once in bounded chunks; later calculations read memory-mapped data.

    This is disposable EDA working storage, not a new raw/Bronze dataset.
    Completion metadata is written last; partial caches cannot be reused.
    """
    target, metadata = cache_root / f"{key}.npy", cache_root / f"{key}.json"
    if rebuild or not target.exists() or not metadata.exists():
        metadata.unlink(missing_ok=True)
        mmap = np.lib.format.open_memmap(target, mode="w+", dtype="float32", shape=array.shape)
        try:
            for begin in range(0, array.sizes["time"], chunk_time):
                end = min(begin + chunk_time, array.sizes["time"])
                mmap[begin:end] = array.isel(time=slice(begin, end)).compute(scheduler="synchronous").data
            mmap.flush()
        finally:
            del mmap
        meta = {"attrs": array.attrs, "name": array.name, "shape": list(array.shape), "coordinates": {"time": pd.DatetimeIndex(array.time.data).as_unit("ns").asi8.tolist(), "latitude": array.latitude.data.tolist(), "longitude": array.longitude.data.tolist()}}
        metadata.write_text(json.dumps(meta, default=str), encoding="utf-8")
    return read_array_cache(target, chunk_time)


class DiskArray:
    """Slice-only source: prevents Dask from copying a whole NumPy memmap at construction."""

    def __init__(self, path: Path, shape):
        self.path, self.shape = str(path), tuple(shape)
        self.ndim, self.dtype = len(self.shape), np.dtype("float32")

    def __getitem__(self, index):
        mmap = np.load(self.path, mmap_mode="r")
        try:
            return np.array(mmap[index], copy=True)
        finally:
            mmap._mmap.close()


def read_array_cache(path: Path, chunk_time=2048) -> xr.DataArray:
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    coords = meta["coordinates"]
    coords["time"] = pd.to_datetime(coords["time"], unit="ns")
    data = da.from_array(DiskArray(path, meta["shape"]), chunks=(chunk_time, -1, -1), asarray=False, fancy=False, name=str(path))
    return xr.DataArray(data, dims=("time", "latitude", "longitude"), coords=coords, attrs=meta["attrs"], name=meta["name"])


def cache_normalized(raw: xr.DataArray, name: str, cache_root: Path, key: str, chunk_time: int, rebuild: bool):
    array, conversion = normalize_units(raw, name)
    return write_array_cache(array, cache_root, f"{key}.{name}", chunk_time, rebuild), conversion

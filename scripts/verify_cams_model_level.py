"""Small live ADS probe; writes a report, keeps production GRIB files untouched.

Run from project root: python -m scripts.verify_cams_model_level
Retrieves one timestamp per new variable, using the downloader's exact grid.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import cdsapi
import eccodes as ec
import numpy as np

from src import download_cams_dataset as downloader


def inspect(path: Path) -> tuple[dict, np.ndarray, np.ndarray]:
    with path.open("rb") as stream:
        handle = ec.codes_grib_new_from_file(stream)
        if handle is None:
            raise ValueError(f"No GRIB messages: {path}")
        try:
            keys = (
                "paramId", "typeOfLevel", "level", "Ni", "Nj", "gridType",
                "latitudeOfFirstGridPointInDegrees", "latitudeOfLastGridPointInDegrees",
                "longitudeOfFirstGridPointInDegrees", "longitudeOfLastGridPointInDegrees",
                "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees",
            )
            metadata = {key: ec.codes_get(handle, key) for key in keys}
            return metadata, ec.codes_get_array(handle, "latitudes"), ec.codes_get_array(handle, "longitudes")
        finally:
            ec.codes_release(handle)


def main() -> None:
    root = downloader.DEFAULT_DATA_ROOT
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "existing": {}, "live": {}}
    reference_lat = reference_lon = None
    # Read actual coordinates from all existing files, not a historical report.
    for path in sorted(root.rglob("*.grib")):
        metadata, lat, lon = inspect(path)
        np.testing.assert_array_equal(np.unique(lat), np.arange(6., 24.01, .75))
        np.testing.assert_array_equal(np.unique(lon), np.arange(102., 118.51, .75))
        if reference_lat is None:
            reference_lat, reference_lon = lat, lon
        np.testing.assert_array_equal(lat, reference_lat)
        np.testing.assert_array_equal(lon, reference_lon)
        report["existing"][str(path.resolve())] = {
            "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
            "grid_sha256": hashlib.sha256(lat.tobytes() + lon.tobytes()).hexdigest(),
        }
    if reference_lat is None:
        raise ValueError("Existing GRIB files required for coordinate comparison")
    print(f"Existing grids match: {len(report['existing'])} files", flush=True)
    client = cdsapi.Client(timeout=60, retry_max=0, quiet=True, progress=False)
    output = downloader.PROJECT_ROOT / "reports" / "cams_model_level_verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as directory:
            for name, variable in downloader.CAMS_MULTI_LEVEL_VARIABLES.items():
                request = downloader.build_request(variable, 2025)
                request["date"] = ["2025-12-31/2025-12-31"]
                request["time"] = ["21:00"]
                path = Path(directory) / f"{name}.grib"
                print(f"Probing {name}: {request}", flush=True)
                client.retrieve(downloader.DATASET, request, str(path))
                metadata, lat, lon = inspect(path)
                assert metadata["paramId"] == downloader.PARAM_IDS[name], metadata
                assert metadata["typeOfLevel"] == "hybrid" and metadata["level"] == 60, metadata
                np.testing.assert_array_equal(lat, reference_lat)
                np.testing.assert_array_equal(lon, reference_lon)
                with path.open("rb") as stream:
                    assert ec.codes_count_in_file(stream) == 1
                    handle = ec.codes_grib_new_from_file(stream)
                    try:
                        assert ec.codes_get(handle, "dataDate") == 20251231
                        assert ec.codes_get(handle, "dataTime") == 2100
                        assert ec.codes_get(handle, "step") == 0
                        assert ec.codes_get(handle, "numberOfMissing") == 0
                        assert np.isfinite(ec.codes_get_values(handle)).all()
                    finally:
                        ec.codes_release(handle)
                del stream
                report["live"][name] = {"request": request, "metadata": metadata, "grid_matches_existing": True}
                print(f"PASS {name}: {metadata}", flush=True)
    finally:
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report: {output}", flush=True)


if __name__ == "__main__":
    main()

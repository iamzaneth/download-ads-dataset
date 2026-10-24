"""Real GRIB fixtures verify full-region coverage and complete yearly schedules."""
from pathlib import Path
import tempfile
import unittest

import eccodes as ec
import numpy as np
import pandas as pd

from src.download_cams_dataset import validate_yearly_file, VIETNAM_AREA, CAMS_MULTI_LEVEL_VARIABLES, PARAM_IDS, download_year


def write_year(path, year=2004, *, mainland=False, parameter=165, duplicate=False, missing=False, model_level=None):
    lat = np.arange(24, 8 if mainland else 5.99, -.75)
    lon = np.arange(102, 109.51 if mainland else 118.51, .75)
    times = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="3h", inclusive="left")
    handle = ec.codes_grib_new_from_samples("regular_ll_sfc_grib1")
    try:
        for key, value in {
            "paramId": parameter, "Ni": len(lon), "Nj": len(lat),
            "latitudeOfFirstGridPointInDegrees": float(lat[0]),
            "latitudeOfLastGridPointInDegrees": float(lat[-1]),
            "longitudeOfFirstGridPointInDegrees": float(lon[0]),
            "longitudeOfLastGridPointInDegrees": float(lon[-1]),
            "iDirectionIncrementInDegrees": .75, "jDirectionIncrementInDegrees": .75,
            "iScansNegatively": 0, "jScansPositively": 0, "jPointsAreConsecutive": 0,
        }.items():
            ec.codes_set(handle, key, value)
        if model_level is not None:
            ec.codes_set(handle, "typeOfLevel", "hybrid")
            ec.codes_set(handle, "level", model_level)
        ec.codes_set_values(handle, np.ones(len(lat) * len(lon)))
        with path.open("wb") as stream:
            for index, timestamp in enumerate(times):
                if missing and index == len(times) - 1:
                    continue
                if duplicate and index == 1:
                    timestamp = times[0]
                ec.codes_set(handle, "dataDate", int(timestamp.strftime("%Y%m%d")))
                ec.codes_set(handle, "dataTime", int(timestamp.strftime("%H%M")))
                ec.codes_write(handle, stream)
    finally:
        ec.codes_release(handle)


class FullRegionTests(unittest.TestCase):
    def test_model_download_validates_and_publishes_real_grib(self):
        class Client:
            def retrieve(self, dataset, request, target):
                write_year(Path(target), parameter=210123, model_level=60)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "co.grib"
            self.assertEqual(download_year(
                Client(), "co", "carbon_monoxide", 2004, path,
                max_attempts=1, retry_backoff_seconds=0, overwrite=False,
            ), (True, True, 1))
            self.assertTrue(path.is_file())
            self.assertFalse(path.with_suffix(".grib.part").exists())

    def test_incomplete_model_download_cleans_partial_file(self):
        class Client:
            def retrieve(self, dataset, request, target):
                write_year(Path(target), parameter=210123, model_level=60, missing=True)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "co.grib"
            self.assertEqual(download_year(
                Client(), "co", "carbon_monoxide", 2004, path,
                max_attempts=1, retry_backoff_seconds=0, overwrite=False,
            ), (False, True, 1))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_all_multilevel_variables_pass_on_full_grid_at_level_60(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.grib"
            for name in CAMS_MULTI_LEVEL_VARIABLES:
                with self.subTest(variable=name):
                    write_year(path, parameter=PARAM_IDS[name], model_level=60)
                    validate_yearly_file(path, name, 2004)

    def test_rejects_surface_and_wrong_model_level(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wrong_level.grib"
            for level in (None, 59):
                with self.subTest(level=level):
                    write_year(path, parameter=130, model_level=level)
                    with self.assertRaisesRegex(ValueError, "Expected model level 60"):
                        validate_yearly_file(path, "t_model", 2004)

    def test_default_bounds_include_mainland_and_islands(self):
        self.assertEqual(VIETNAM_AREA, (24., 102., 6., 118.5))

    def test_complete_leap_year_passes_without_creating_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "u10.grib"
            write_year(path)
            validate_yearly_file(path, "u10", 2004)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_rejects_mainland_only_grid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.grib"
            write_year(path, mainland=True)
            with self.assertRaisesRegex(ValueError, "mainland and island grid"):
                validate_yearly_file(path, "u10", 2004)

    def test_rejects_wrong_parameter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wrong.grib"
            write_year(path, parameter=166)
            with self.assertRaisesRegex(ValueError, "Unexpected CAMS parameter"):
                validate_yearly_file(path, "u10", 2004)

    def test_rejects_missing_and_duplicate_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.grib"
            for flags in ({"missing": True}, {"duplicate": True}):
                with self.subTest(flags=flags):
                    write_year(path, **flags)
                    with self.assertRaises(ValueError):
                        validate_yearly_file(path, "u10", 2004)


if __name__ == "__main__":
    unittest.main()

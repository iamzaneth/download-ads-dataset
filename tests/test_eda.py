from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import xarray as xr

from src.download_cams_dataset import VARIABLES, MERGED_DIR_NAME
from src.eda import EDAConfig, EDARun
from src.eda.analysis import annual_grid_trend, regional_mean, temporal_aggregations
from src.eda.grib_loader import canonical_array, discover_files, write_array_cache
from src.eda.quality import consistency, descriptive_statistics, physical_checks
from src.eda.transformations import exact_align, normalize_units, relative_humidity, safe_ratio, wind_fields


def grid(values, name="pm1", units="kg m**-3", times=None):
    values = np.asarray(values, dtype="float32")
    if values.ndim == 1:
        values = values[:, None, None]
    return xr.DataArray(values, dims=("time", "latitude", "longitude"), coords={"time": pd.date_range("2003-01-01", periods=len(values), freq="3h") if times is None else times, "latitude": np.arange(values.shape[1], dtype=float) + 8.25, "longitude": np.arange(values.shape[2], dtype=float) + 102}, name=name, attrs={"units": units})


class TransformationTests(unittest.TestCase):
    def test_metadata_gates_and_preserves_raw(self):
        raw = grid([1e-9, 5e-9])
        converted, audit = normalize_units(raw, "pm1")
        np.testing.assert_allclose(converted.data[:, 0, 0], [1, 5])
        np.testing.assert_allclose(raw.data[:, 0, 0], [1e-9, 5e-9])
        self.assertEqual(audit["analysis_unit"], "µg/m³")
        unknown, _ = normalize_units(grid([2], units="unknown"), "pm1")
        self.assertEqual(unknown.item(), 2)
        column, _ = normalize_units(grid([.001], units="kg m**-2"), "co_column")
        self.assertAlmostEqual(column.item(), .001)
        self.assertEqual(column.attrs["units"], "kg m**-2")
        t, _ = normalize_units(grid([273.15], units="K"), "t2m")
        self.assertAlmostEqual(t.item(), 0, places=4)
        p, _ = normalize_units(grid([101325], units="Pa"), "sp")
        self.assertAlmostEqual(p.item(), 1013.25, places=2)

    def test_wind_meteorological_direction_and_calm(self):
        u, v = grid([0, -3, 0], units="m s**-1"), grid([-4, 0, 0], units="m/s")
        fields = wind_fields(u, v)
        np.testing.assert_allclose(fields["wind_speed"].data[:, 0, 0], [4, 3, 0])
        np.testing.assert_allclose(fields["wind_direction"].data[:2, 0, 0], [0, 90])
        self.assertTrue(np.isnan(fields["wind_direction"].data[2, 0, 0]))

    def test_rh_saturation_and_violation_not_clipped(self):
        t, td = grid([20, 20], "t2m", "°C"), grid([20, 21], "d2m", "°C")
        rh = relative_humidity(t, td)
        self.assertAlmostEqual(rh.data[0, 0, 0], 100)
        self.assertGreater(rh.data[1, 0, 0], 100)
        checks = physical_checks({"t2m": t, "d2m": td, "relative_humidity": rh}, set())
        row = checks.set_index("check").loc["0 <= RH <= 100"]
        self.assertEqual(row.violation_count, 1)
        self.assertIn("2003-01-01 03:00:00", row.examples)

    def test_ratio_nonpositive_denominator_and_exact_grid(self):
        a, b = grid([1, 2, 3], units="µg/m³"), grid([2, 0, -1], units="µg/m³")
        ratio = safe_ratio(a, b, "ratio")
        self.assertEqual(ratio.data[0, 0, 0], .5)
        self.assertTrue(np.isnan(ratio.data[1:, 0, 0]).all())
        with self.assertRaises(ValueError):
            exact_align(a, b.assign_coords(longitude=[103]))
        with self.assertRaises(ValueError):
            exact_align(a, b.assign_coords(time=[a.time.data[0]] * 3))


class QualityTests(unittest.TestCase):
    def test_exact_statistics_distinguish_nan_and_inf(self):
        array = grid([1, 2, 3, np.nan, np.inf, -1], units="µg/m³")
        with tempfile.TemporaryDirectory() as folder:
            stats = descriptive_statistics(array, Path(folder), chunk_time=2)
        self.assertEqual(stats["count"], 4)
        self.assertEqual(stats["nan_count"], 1)
        self.assertEqual(stats["inf_count"], 1)
        self.assertEqual(stats["negative_count"], 1)
        self.assertAlmostEqual(stats["mean"], 1.25)
        self.assertAlmostEqual(stats["median"], 1.5)
        self.assertAlmostEqual(stats["p99"], np.percentile([-1, 1, 2, 3], 99))

    def test_gaps_duplicate_times_and_grid_order(self):
        expected = pd.date_range("2003-01-01", periods=4, freq="3h")
        a = grid(np.ones((4, 2, 1)), times=expected)
        b = grid(np.ones((3, 2, 1)), times=[expected[0], expected[2], expected[2]]).isel(latitude=slice(None, None, -1))
        result = consistency(b, a, expected, {"duplicate_messages": 1})
        self.assertEqual(result["missing_timestamps"], 2)
        self.assertEqual(result["internal_missing_timestamps"], 1)
        self.assertEqual(result["duplicate_timestamps"], 1)
        self.assertFalse(result["same_spatial_grid"])
        self.assertFalse(result["same_time_grid"])
        self.assertEqual(result["latitude_order"], "descending")

    def test_pm_ordering_counts_finite_triples_only(self):
        arrays = {"pm1": grid([1, 4, np.nan], "pm1", "µg/m³"), "pm2p5": grid([2, 3, 3], "pm2p5", "µg/m³"), "pm10": grid([3, 5, 5], "pm10", "µg/m³")}
        row = physical_checks(arrays, set()).iloc[0]
        self.assertEqual(row.eligible_count, 2)
        self.assertEqual(row.violation_count, 1)
        self.assertEqual(row.violation_pct, 50)


class AggregationTests(unittest.TestCase):
    def test_local_day_boundary_keeps_utc(self):
        array = grid([1, 2], times=pd.to_datetime(["2003-01-01 18:00", "2003-01-01 21:00"]))
        agg = temporal_aggregations(array)
        np.testing.assert_array_equal(agg["regional"].time.data, array.time.data)
        self.assertEqual(agg["diurnal"].local_hour.data.tolist(), [1, 4])
        self.assertEqual(pd.Timestamp(agg["regional"].time_local.data[0]).day, 2)

    def test_area_weights(self):
        array = grid([[[0], [10]]]).assign_coords(latitude=[0, 60])
        self.assertAlmostEqual(regional_mean(array).item(), 10 / 3)

    def test_trend_uses_actual_year_and_excludes_low_coverage(self):
        times = pd.to_datetime(["2003-01-01", "2005-01-01", "2006-01-01", "2007-01-01"])
        array = grid([2, 6, 8, 10], "t2m", "°C", times)
        trend = annual_grid_trend(array, minimum_coverage=.0001)
        self.assertAlmostEqual(trend.slope.item(), 2)
        strict = annual_grid_trend(array, minimum_coverage=.95)
        self.assertTrue(np.isnan(strict.slope.item()))


class PipelineTests(unittest.TestCase):
    def test_schedule_derived_from_downloader_has_more_than_65536_times(self):
        self.assertEqual(len(EDAConfig().expected_time), 67208)
        self.assertGreater(len(EDAConfig().expected_time), 65536)

    def test_discovery_missing_extra_ambiguity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            merged = root / MERGED_DIR_NAME
            merged.mkdir(parents=True)
            (merged / "pm1_2003_2025_vietnam.grib").touch()
            (merged / "unknown.grib").touch()
            paths, inventory, extras = discover_files(root, 2003, 2025)
            self.assertEqual(set(paths), {"pm1"})
            self.assertEqual((inventory.status == "missing").sum(), len(VARIABLES) - 1)
            self.assertEqual(len(extras), 1)
            for year in (2004, 2005):
                (merged / f"pm10_{year}_{year}_vietnam.grib").touch()
            _, inventory, _ = discover_files(root, 2003, 2025)
            self.assertEqual(inventory.set_index("short_name").loc["pm10", "status"], "ambiguous")

    def test_cache_is_lazy_and_preserves_nan_utc_units(self):
        a = grid([1, np.nan, 3], units="µg/m³").chunk({"time": 1})
        with tempfile.TemporaryDirectory() as folder:
            cached = write_array_cache(a, Path(folder), "test", 1)
            np.testing.assert_allclose(cached.compute().data, a.compute().data, equal_nan=True)
            np.testing.assert_array_equal(cached.time.data, a.time.data)
            self.assertEqual(cached.attrs["units"], "µg/m³")
            self.assertTrue(hasattr(cached.data, "dask"))

    def test_all_missing_produces_report_and_explicit_skips(self):
        with tempfile.TemporaryDirectory() as folder:
            run = EDARun(EDAConfig(data_root=Path(folder) / "data", report_root=Path(folder) / "reports"))
            report = run.run_all()
            self.assertTrue(report.is_file())
            self.assertIn("skipped: required variable not available", report.read_text(encoding="utf-8"))
            self.assertEqual((run.inventory.status == "missing").sum(), len(VARIABLES))

    def test_pm1_only_all_stages_generate_real_figure_report(self):
        with tempfile.TemporaryDirectory() as folder:
            run = EDARun(EDAConfig(data_root=Path(folder) / "data", report_root=Path(folder) / "reports", sample_size=100))
            a, _ = normalize_units(grid(np.arange(1, 17) * 1e-9), "pm1")
            run.discover()
            run.inventory.loc[run.inventory.short_name == "pm1", "status"] = "available"
            for key, value in {"start_time": "2003-01-01", "end_time": "2003-01-02", "n_time": 16, "n_lat": 1, "n_lon": 1, "lat_resolution": "singleton", "lon_resolution": "singleton"}.items():
                run.inventory.loc[run.inventory.short_name == "pm1", key] = value
            run.arrays["pm1"] = a.chunk({"time": 4})
            run.check_quality()
            run.summarize()
            run.distributions()
            run.temporal()
            run.spatial_patterns()
            run.spatial_trends()
            run.relationships()
            run.extreme_pm25()
            report = run.write_report()
            self.assertTrue(report.is_file())
            self.assertTrue((run.config.report_root / "figures" / "01_pm_distribution.png").is_file())
            self.assertGreater((run.config.report_root / "figures" / "01_pm_distribution.png").stat().st_size, 1000)
            self.assertTrue(any(s["section"] == "extreme_pm25" and s["status"].startswith("skipped") for s in run.statuses))


if __name__ == "__main__":
    unittest.main()

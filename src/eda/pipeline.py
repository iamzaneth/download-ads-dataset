"""Notebook-facing staged orchestration; only compact summaries are exported."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from src.download_cams_dataset import DATASET, DEFAULT_DATA_ROOT, DEFAULT_END_YEAR, DEFAULT_START_YEAR, PROJECT_ROOT, TIMES, VARIABLES, VIETNAM_AREA
from . import analysis, grib_loader, plotting, quality, transformations

LOGGER = logging.getLogger("cams_eda")


@dataclass
class EDAConfig:
    data_root: Path = DEFAULT_DATA_ROOT
    report_root: Path = PROJECT_ROOT / "reports"
    cache_root: Path | None = None
    start_year: int = DEFAULT_START_YEAR
    end_year: int = DEFAULT_END_YEAR
    variables: tuple[str, ...] | None = None
    chunk_time: int = 2048
    sample_size: int = 50000
    seed: int = 42
    minimum_annual_coverage: float = .95
    rebuild_cache: bool = False
    filter_by_keys: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        self.data_root, self.report_root = Path(self.data_root).resolve(), Path(self.report_root).resolve()
        self.cache_root = Path(self.cache_root).resolve() if self.cache_root else self.data_root / "eda_cache"
        if self.end_year < self.start_year or self.chunk_time < 1 or self.sample_size < 1:
            raise ValueError("Invalid EDA years, chunk_time, or sample_size")
        if not 0 < self.minimum_annual_coverage <= 1:
            raise ValueError("minimum_annual_coverage must be in (0, 1]")
        if self.variables is not None and set(self.variables).difference(VARIABLES):
            raise ValueError("variables must come from downloader VARIABLES")

    @property
    def cadence(self):
        offsets = pd.to_timedelta([f"{t}:00" for t in TIMES]).as_unit("ns")
        differences = np.diff(np.r_[offsets.asi8, pd.Timedelta(days=1).value])
        if len(set(differences)) != 1:
            raise ValueError("Downloader TIMES no longer has uniform cadence; update expected schedule logic")
        return pd.Timedelta(int(differences[0]), unit="ns")

    @property
    def expected_time(self):
        return pd.date_range(f"{self.start_year}-01-01", f"{self.end_year + 1}-01-01", freq=self.cadence, inclusive="left")


class EDARun:
    def __init__(self, config: EDAConfig | None = None):
        self.config = config or EDAConfig()
        self.arrays, self.audits, self.keys, self.assets, self.statuses = {}, {}, {}, {}, []
        self.aggregations, self.spatial, self.trends = {}, {}, {}
        self.inventory = self.consistency = self.conversions = self.statistics = self.physical = pd.DataFrame()
        self.daily = self.pearson = self.spearman = self.pair_counts = self.seasonal = pd.DataFrame()
        self.extreme_yearly = self.extreme_monthly = self.extreme_comparison = pd.DataFrame()
        self.extreme_thresholds = {}
        self.unsafe = set()
        plotting.set_style()

    @property
    def period(self):
        if self.inventory.empty or "start_time" not in self.inventory:
            return "no readable observations"
        dates = self.inventory.loc[self.inventory.status == "available"]
        return f"{dates.start_time.min()} — {dates.end_time.max()} UTC" if len(dates) else "no readable observations"

    @property
    def units(self):
        return {k: str(a.attrs.get("units", "unknown")) or "unknown" for k, a in self.arrays.items()}

    def _status(self, section, status):
        self.statuses.append({"section": section, "status": status})
        LOGGER.info("%s: %s", section, status)

    def _save_table(self, name, table):
        folder = self.config.report_root / "tables"
        folder.mkdir(parents=True, exist_ok=True)
        table.to_csv(folder / f"{name}.csv", index=False)

    def _asset(self, section, path):
        if path:
            self.assets.setdefault(section, []).append(Path(path))

    def discover(self):
        self.paths, self.inventory, self.extras = grib_loader.discover_files(self.config.data_root, self.config.start_year, self.config.end_year, self.config.variables)
        self._save_table("extra_files", self.extras)
        return self.inventory

    def inspect(self):
        if self.inventory.empty:
            self.discover()
        consistency_rows, reference = [], None
        for name, path in self.paths.items():
            LOGGER.info("Inspect GRIB metadata: %s", name)
            row_index = self.inventory.index[self.inventory.short_name == name][0]
            filters = self.config.filter_by_keys.get(name, {})
            try:
                if self.config.rebuild_cache:
                    key = grib_loader.fingerprint(path, filters)
                    for index_path in self.config.cache_root.glob(f"{key}.*.idx"):
                        index_path.unlink()
                with grib_loader.open_grib(path, name, self.config.cache_root, self.config.chunk_time, filters) as (raw, key, n_hypercubes):
                    raw = raw.rename(name)
                    metadata = quality.inventory_metadata(raw)
                    for k, v in metadata.items():
                        self.inventory.loc[row_index, k] = v
                    audit = grib_loader.audit_messages(path, raw.attrs, filters, self.config.cache_root, key, self.config.rebuild_cache)
                    if reference is None:
                        reference = raw.copy(deep=False)
                    row = quality.consistency(raw, reference, self.config.expected_time, audit)
                    row["reference_variable"] = reference.name
                    row["hypercubes_opened"] = n_hypercubes
                    consistency_rows.append(row)
                    self.audits[name], self.keys[name] = audit, key
                    if row["duplicate_timestamps"] or row["conflicting_duplicate_messages"] or not row["duplicate_values_verified"]:
                        self.unsafe.add(name)
                    self.inventory.loc[row_index, "notes"] = f"Selected GRIB messages={audit['message_count']}; hypercubes={n_hypercubes}"
            except Exception as error:
                self.inventory.loc[row_index, "status"] = "error"
                self.inventory.loc[row_index, "notes"] = f"{type(error).__name__}: {error}"
                self._status(f"inventory/{name}", f"error: {error}")
        self.consistency = pd.DataFrame(consistency_rows)
        for column in ("file_size", "n_time", "n_lat", "n_lon"):
            if column in self.inventory:
                self.inventory[column] = self.inventory[column].astype("Int64")
        self._save_table("inventory", self.inventory)
        self._save_table("consistency", self.consistency)
        return self.inventory, self.consistency

    def normalize(self):
        conversions = []
        for name, path in self.paths.items():
            if name not in self.keys:
                continue
            LOGGER.info("Decode/cache analysis array: %s", name)
            with grib_loader.open_grib(path, name, self.config.cache_root, self.config.chunk_time, self.config.filter_by_keys.get(name, {})) as (raw, key, _):
                self.arrays[name], conversion = grib_loader.cache_normalized(raw, name, self.config.cache_root, key, self.config.chunk_time, self.config.rebuild_cache)
                conversions.append(conversion)
        derived = [(("u10", "v10"), "wind", transformations.wind_fields), (("t2m", "d2m"), "relative_humidity", lambda t, td: {"relative_humidity": transformations.relative_humidity(t, td)})]
        for required, label, function in derived:
            if not all(k in self.arrays for k in required):
                self._status(label, "skipped: required variable not available")
                continue
            try:
                if self.unsafe.intersection(required):
                    raise ValueError("duplicate GRIB messages/timestamps in source")
                fields = function(*(self.arrays[k] for k in required))
                for name, array in fields.items():
                    key = "derived." + ".".join(self.keys[k] for k in required) + f".{name}"
                    self.arrays[name] = grib_loader.write_array_cache(array, self.config.cache_root, key, self.config.chunk_time, self.config.rebuild_cache)
                    conversions.append({"variable": name, "source_unit": "derived", "analysis_unit": array.attrs["units"], "conversion": array.attrs.get("formula", array.attrs.get("long_name"))})
                self._status(label, "computed on exactly matching grids and time axes")
            except ValueError as error:
                self._status(label, f"skipped: {error}")
        for numerator, denominator, name in (("pm1", "pm2p5", "pm1_pm2p5_ratio"), ("pm2p5", "pm10", "pm2p5_pm10_ratio")):
            if not all(k in self.arrays for k in (numerator, denominator)):
                self._status(name, "skipped: required variable not available")
                continue
            try:
                if self.unsafe.intersection((numerator, denominator)):
                    raise ValueError("duplicate GRIB source messages")
                array = transformations.safe_ratio(self.arrays[numerator], self.arrays[denominator], name)
                self.arrays[name] = grib_loader.write_array_cache(array, self.config.cache_root, f"derived.{self.keys[numerator]}.{self.keys[denominator]}.{name}", self.config.chunk_time, self.config.rebuild_cache)
            except ValueError as error:
                self._status(name, f"skipped: {error}")
        self.conversions = pd.DataFrame(conversions)
        self._save_table("unit_conversions", self.conversions)
        return self.conversions

    def check_quality(self):
        self.physical = quality.physical_checks(self.arrays, self.unsafe)
        self._save_table("physical_checks", self.physical)
        return self.physical

    def summarize(self):
        rows = []
        for name, array in self.arrays.items():
            if name == "wind_direction":
                continue  # Circular variable: arithmetic mean/percentiles are misleading.
            LOGGER.info("Exact descriptive statistics: %s", name)
            row = quality.descriptive_statistics(array, self.config.cache_root, self.config.chunk_time)
            row["source_duplicate_messages"] = self.audits.get(name, {}).get("duplicate_messages", 0)
            duplicates = self.audits.get(name, {}).get("duplicate_messages", 0)
            row["duplicate_policy"] = "cfgrib first message retained; conflicting/unverified duplicates flagged, cross-variable checks skipped" if name in self.unsafe else "identical duplicates verified grid-by-grid; cfgrib first message used explicitly" if duplicates else "no source duplicate collapse detected"
            rows.append(row)
        self.statistics = pd.DataFrame(rows)
        self._save_table("descriptive_statistics", self.statistics)
        return self.statistics

    def distributions(self):
        groups = [("PM", transformations.PM, "01_pm_distribution.png"), ("Meteorology", ("t2m", "relative_humidity", "wind_speed", "sp", "mslp"), "02_meteorology_distribution.png"), ("Aerosol and total columns", ("aod550", *transformations.COLUMNS), "03_aerosol_column_distribution.png")]
        for title, names, filename in groups:
            samples = {n: analysis.reproducible_sample(self.arrays[n], self.config.sample_size, self.config.seed) for n in names if n in self.arrays}
            self._asset("distribution", plotting.ecdf(samples, self.units, self.period, self.config.report_root / "figures", filename, title))
            if not samples:
                self._status(f"distribution/{title}", "skipped: required variable not available")
        return self.assets.get("distribution", [])

    def temporal(self):
        for name, array in self.arrays.items():
            if name == "wind_direction":
                continue
            if not array.get_index("time").is_unique or not array.get_index("time").is_monotonic_increasing:
                self._status(f"temporal/{name}", "skipped: duplicate or unordered time coordinates")
                continue
            self.aggregations[name] = analysis.temporal_aggregations(array)
        root = self.config.report_root / "figures"
        annual = ["pm1", "pm2p5", "pm10", "aod550", "t2m", "relative_humidity", "wind_speed", "no2_column"]
        monthly = ["pm1", "pm2p5", "pm10", "aod550", "t2m", "relative_humidity", "wind_speed", *transformations.COLUMNS]
        diurnal = ["pm1", "pm2p5", "pm10", "t2m", "relative_humidity", "wind_speed"]
        for names, field, filename, title in [(annual, "annual", "04_annual_area_mean.png", "Annual means (partial-year coverage in tables)"), (monthly, "monthly_climatology", "05_monthly_climatology.png", "Monthly climatology"), (diurnal, "diurnal", "07_local_diurnal_cycle.png", "3-hour local diurnal climatology")]:
            self._asset("temporal", plotting.temporal(self.aggregations, names, field, self.period, root, filename, title))
        self._asset("temporal", plotting.year_month(self.aggregations, ["pm2p5", "aod550", "t2m"], self.period, root, "06_year_month_heatmaps.png"))
        if not any(n in self.aggregations for n in ("pm2p5", "aod550", "t2m")):
            self._status("year_month_heatmaps", "skipped: required variable not available")
        for field in ("daily", "monthly", "annual", "monthly_climatology", "diurnal", "annual_count", "daily_count"):
            table = pd.concat([agg[field].to_series().rename(name) for name, agg in self.aggregations.items()], axis=1) if self.aggregations else pd.DataFrame()
            self._save_table(field, table.reset_index())
        return self.assets.get("temporal", [])

    def spatial_patterns(self):
        for name, array in self.arrays.items():
            if name == "wind_direction":
                continue
            LOGGER.info("Exact spatial summaries: %s", name)
            self.spatial[name] = analysis.spatial_statistics(array)
            # This is a tiny reduced spatial grid (four summaries, no raw time axis).
            self._save_table(f"spatial_{name}", self.spatial[name].to_dataframe().reset_index())
        specs = [("pm2p5", "mean"), ("pm2p5", "p95"), ("pm10", "mean"), ("aod550", "mean"), ("t2m", "mean"), ("wind_speed", "mean"), ("no2_column", "mean")]
        fields = [(f"{stat.upper()} {name}", self.spatial[name][stat]) for name, stat in specs if name in self.spatial]
        if not fields and "pm1" in self.spatial:
            fields = [("Mean PM1", self.spatial["pm1"]["mean"]), ("P95 PM1", self.spatial["pm1"]["p95"])]
        self._asset("spatial", plotting.maps(fields, self.period, self.config.report_root / "figures", "08_spatial_mean_and_p95.png", "Native grid spatial patterns"))
        return self.assets.get("spatial", [])

    def spatial_trends(self):
        for name in ("pm2p5", "aod550", "t2m", "no2_column"):
            if name not in self.arrays:
                self._status(f"trend/{name}", "skipped: required variable not available")
                continue
            if name in self.unsafe:
                self._status(f"trend/{name}", "skipped: duplicate source GRIB messages")
                continue
            self.trends[name] = analysis.annual_grid_trend(self.arrays[name], len(TIMES), self.config.minimum_annual_coverage)
            self._save_table(f"trend_{name}", self.trends[name][["slope", "eligible_years"]].to_dataframe().reset_index())
            self._save_table(f"annual_grid_coverage_{name}", self.trends[name][["annual_mean", "annual_coverage"]].to_dataframe().reset_index())
            if not np.isfinite(self.trends[name].slope.data).any():
                self._status(f"trend/{name}", "skipped: fewer than 3 sufficiently observed years")
        fields = [(f"{n} slope (≥3 years, ≥{self.config.minimum_annual_coverage:.0%} annual coverage)", result.slope) for n, result in self.trends.items() if np.isfinite(result.slope.data).any()]
        self._asset("trend", plotting.maps(fields, self.period, self.config.report_root / "figures", "09_annual_grid_trend_slopes.png", "OLS trend of eligible annual grid means", diverging=True))
        return self.assets.get("trend", [])

    def relationships(self):
        if not self.aggregations:
            self._status("relationships", "skipped: required variable not available")
            return []
        reference_name = "pm2p5" if "pm2p5" in self.arrays else next(iter(self.aggregations))
        reference = self.arrays[reference_name]
        compatible = []
        for name in self.aggregations:
            try:
                if name in self.unsafe or reference_name in self.unsafe:
                    raise ValueError("duplicate source messages")
                transformations.exact_align(reference, self.arrays[name])
                compatible.append(name)
            except ValueError as error:
                self._status(f"relationships/{name}", f"skipped: grid/time inconsistency: {error}")
        self.daily = pd.concat([self.aggregations[n]["daily"].to_series().rename(n) for n in compatible], axis=1) if compatible else pd.DataFrame()
        self.daily = self.daily.replace([np.inf, -np.inf], np.nan)
        if len(compatible) < 2:
            self._status("relationships", "skipped: required variable not available (at least 2 compatible variables)")
            return []
        self.pearson = self.daily.corr(method="pearson", min_periods=3)
        self.spearman = self.daily.corr(method="spearman", min_periods=3)
        observed = self.daily.notnull().astype("int64")
        self.pair_counts = observed.T @ observed
        for name, table in (("correlation_daily_input", self.daily), ("pearson", self.pearson), ("spearman", self.spearman), ("correlation_paired_days", self.pair_counts)):
            self._save_table(name, table.reset_index())
        root = self.config.report_root / "figures"
        # Keep matrices readable: ratios remain inspectable in CSV, avoid crowded axes.
        main = [n for n in self.daily if "ratio" not in n and n not in {"u10", "v10", "d2m", "mslp"}]
        self._asset("relationships", plotting.correlations(self.pearson.loc[main, main], self.spearman.loc[main, main], self.period, root))
        for names, filename, title in [(["pm1", "pm10"], "10_pm_fraction_relationships.png", "PM fractions"), (["wind_speed", "relative_humidity", "t2m"], "11_pm25_meteorology_relationships.png", "PM2.5 and meteorology"), (["aod550", *transformations.COLUMNS], "13_pm25_aerosol_column_relationships.png", "PM2.5, optical depth and total columns")]:
            self._asset("relationships", plotting.relationships(self.daily, self.units, names, self.period, root, filename, title))
        seasonal = []
        if all(n in self.daily for n in ("pm2p5", "aod550")):
            season_map = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM", 6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}
            seasons = pd.Index(self.daily.index.month).map(season_map)
            for season in ("DJF", "MAM", "JJA", "SON"):
                pair = self.daily.loc[seasons == season, ["pm2p5", "aod550"]].dropna()
                seasonal.append({"season": season, "paired_days": len(pair), "pearson": pair.corr(method="pearson").iloc[0, 1] if len(pair) >= 3 else np.nan, "spearman": pair.corr(method="spearman").iloc[0, 1] if len(pair) >= 3 else np.nan})
        self.seasonal = pd.DataFrame(seasonal)
        self._save_table("pm25_aod_seasonal_correlations", self.seasonal)
        self._status("relationships", "daily area-weighted means; pairwise finite days; descriptive correlations, no causal inference")
        return self.assets.get("relationships", [])

    def extreme_pm25(self):
        if "pm2p5" not in self.arrays or self.statistics.empty:
            self._status("extreme_pm25", "skipped: required variable not available")
            return []
        if "pm2p5" in self.unsafe:
            self._status("extreme_pm25", "skipped: duplicate source GRIB messages")
            return []
        pm = self.arrays["pm2p5"]
        summary = self.statistics.set_index("variable").loc["pm2p5"]
        self.extreme_thresholds = {"p95": float(summary.p95), "p99": float(summary.p99)}
        if not all(np.isfinite(v) for v in self.extreme_thresholds.values()):
            self._status("extreme_pm25", "skipped: no finite PM2.5 observations")
            return []
        finite = np.isfinite(pm)
        indicators = {}
        fields = []
        for label, threshold in self.extreme_thresholds.items():
            mask = finite & (pm > threshold)
            event = mask.any(("latitude", "longitude")).compute(scheduler="synchronous")
            indicators[f"{label}_timestep_count"] = event
            frequency = (100 * mask.sum("time") / finite.sum("time").where(finite.sum("time") > 0)).compute(scheduler="synchronous")
            frequency.attrs = {"units": "% of finite grid-cell observations"}
            fields.append((f"PM2.5 > pooled {label.upper()} ({threshold:.3g} {self.units['pm2p5']})", frequency))
            self._save_table(f"pm25_{label}_spatial_frequency", frequency.to_dataframe(name="frequency_pct").reset_index())
        event_table = pd.DataFrame({name: a.to_series() for name, a in indicators.items()})
        event_table["finite_timestep_count"] = finite.any(("latitude", "longitude")).compute(scheduler="synchronous").to_series().astype(int)
        self.extreme_yearly = event_table.groupby(event_table.index.year).sum()
        self.extreme_yearly.index.name = "year"
        self.extreme_monthly = event_table.groupby(event_table.index.month).sum().reindex(range(1, 13), fill_value=0)
        self.extreme_monthly.index.name = "month"
        for table in (self.extreme_yearly, self.extreme_monthly):
            for label in ("p95", "p99"):
                table[f"{label}_timestep_pct"] = 100 * table[f"{label}_timestep_count"] / table.finite_timestep_count.replace(0, np.nan)
        rows = []
        for name in ("wind_speed", "relative_humidity", "t2m", "aod550"):
            if name not in self.arrays:
                self._status(f"extreme_conditions/{name}", "skipped: required variable not available")
                continue
            try:
                if name in self.unsafe:
                    raise ValueError("duplicate source messages")
                _, variable = transformations.exact_align(pm, self.arrays[name])
                eligible = finite & np.isfinite(variable)
                normal, extreme = eligible & (pm <= summary.p95), eligible & (pm > summary.p95)
                reduced = xr.Dataset({"normal_mean": variable.where(normal).mean(), "extreme_mean": variable.where(extreme).mean(), "normal_count": normal.sum(), "extreme_count": extreme.sum()}).compute(scheduler="synchronous")
                rows.append({"variable": name, "unit": self.units[name], **{k: float(v.item()) for k, v in reduced.data_vars.items()}})
            except ValueError as error:
                self._status(f"extreme_conditions/{name}", f"skipped: {error}")
        self.extreme_comparison = pd.DataFrame(rows)
        self._save_table("pm25_extreme_yearly", self.extreme_yearly.reset_index())
        self._save_table("pm25_extreme_monthly", self.extreme_monthly.reset_index())
        self._save_table("pm25_extreme_conditions", self.extreme_comparison)
        self._save_table("pm25_extreme_thresholds", pd.DataFrame([self.extreme_thresholds]))
        root = self.config.report_root / "figures"
        self._asset("extreme", plotting.maps(fields, self.period, root, "17_pm25_extreme_spatial_frequency.png", "Extreme PM2.5 frequency on the native grid"))
        for path in plotting.extremes(self.extreme_yearly, self.extreme_monthly, self.extreme_comparison, self.period, root):
            self._asset("extreme", path)
        self._status("extreme_pm25", "pooled finite grid-time P95/P99; strict >; a timestep event means at least one exceeding cell; conditions compare co-located finite grid-time points")
        return self.assets.get("extreme", [])

    def findings(self, section=None):
        from .reporting import findings
        return findings(self, section)

    def write_report(self):
        from .reporting import write_report
        self._save_table("section_status", pd.DataFrame(self.statuses))
        return write_report(self)

    def run_all(self):
        self.discover()
        self.inspect()
        self.normalize()
        self.check_quality()
        self.summarize()
        self.distributions()
        self.temporal()
        self.spatial_patterns()
        self.spatial_trends()
        self.relationships()
        self.extreme_pm25()
        return self.write_report()

    def close(self):
        self.arrays.clear()  # Releases disk-backed array handles when graphs are no longer used.


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run read-only CAMS EAC4 EDA and generate Markdown report")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--report-root", type=Path, default=PROJECT_ROOT / "reports")
    parser.add_argument("--variables", nargs="+", choices=tuple(VARIABLES))
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR)
    parser.add_argument("--rebuild-cache", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    run = EDARun(EDAConfig(data_root=args.data_root, report_root=args.report_root, variables=tuple(args.variables) if args.variables else None, start_year=args.start_year, end_year=args.end_year, rebuild_cache=args.rebuild_cache))
    try:
        path = run.run_all()
        print(f"Report: {path}")
        return 1 if (run.inventory.status == "error").any() else 0
    finally:
        run.close()

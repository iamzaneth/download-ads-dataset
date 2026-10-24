"""Evidence-backed findings and the requested ten-section Markdown report."""

from __future__ import annotations

import json
import platform
from importlib.metadata import version

import numpy as np
import pandas as pd

from .transformations import COLUMNS, RH_FORMULA, RH_SOURCE
from src.download_cams_dataset import DATASET, TIMES, VARIABLES, VIETNAM_AREA


def _fmt(value):
    if pd.isna(value):
        return "—"
    if isinstance(value, (float, np.floating)):
        return f"{value:.5g}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown_table(table, columns=None, limit=None):
    if table.empty:
        return "Không có dữ liệu đủ điều kiện cho bảng này."
    view = table[[c for c in columns if c in table.columns]] if columns else table
    if limit:
        view = view.head(limit)
    lines = ["| " + " | ".join(view.columns) + " |", "| " + " | ".join("---" for _ in view.columns) + " |"]
    lines.extend("| " + " | ".join(_fmt(v) for v in row) + " |" for row in view.itertuples(index=False, name=None))
    return "\n".join(lines)


def findings(run, section=None):
    found = {k: [] for k in ("inventory", "quality", "statistics", "distribution", "temporal", "spatial", "trend", "relationships", "extreme")}
    inventory = run.inventory
    if not inventory.empty:
        available = inventory.loc[inventory.status == "available", "short_name"].tolist()
        missing = inventory.loc[inventory.status == "missing", "short_name"].tolist()
        found["inventory"].append(f"Đọc được {len(available)}/{len(VARIABLES)} biến khai báo: {', '.join(available) or 'không có'}. Thiếu: {', '.join(missing) or 'không có'}. Giai đoạn quan sát: {run.period}.")
        if "n_time" in inventory and available:
            row = inventory[inventory.short_name == available[0]].iloc[0]
            found["inventory"].append(f"Grid của {row.short_name}: {int(row.n_lat)} × {int(row.n_lon)} điểm, bước latitude {row.lat_resolution}°, longitude {row.lon_resolution}°; {int(row.n_time):,} timestamp.")
    if not run.consistency.empty:
        c = run.consistency
        found["quality"].append(f"So với {c.reference_variable.iloc[0]}, {int(c.same_time_grid.sum())}/{len(c)} biến cùng trục thời gian và {int(c.same_spatial_grid.sum())}/{len(c)} biến cùng grid theo đúng thứ tự tọa độ. Tổng timestamp thiếu so với giai đoạn cấu hình (cộng qua biến): {int(c.missing_timestamps.sum()):,}; message trùng: {int(c.duplicate_messages.sum()):,}.")
        if "identical_duplicate_messages" in c and c.duplicate_messages.sum():
            found["quality"].append(f"Các message trùng: {int(c.identical_duplicate_messages.sum()):,} bản có cùng decoded values/grid với bản đầu, {int(c.conflicting_duplicate_messages.sum()):,} bản xung đột. Bản giống nhau dùng representation đầu của cfgrib có audit rõ ràng; bản xung đột không được dùng cho analysis liên biến.")
    if not run.statistics.empty:
        s = run.statistics
        found["quality"].append(f"Các trường được thống kê có {int(s.nan_count.sum()):,} NaN và {int(s.inf_count.sum()):,} infinity; bao gồm biến dẫn xuất và ratio, nên không xem tổng này là số missing của raw riêng biệt.")
        for name in ("pm2p5", "pm1"):
            if name in s.variable.tolist():
                row = s.set_index("variable").loc[name]
                found["statistics"].append(f"{name}: mean={row['mean']:.4g}, median={row['median']:.4g}, P95={row.p95:.4g}, P99={row.p99:.4g}, max={row['max']:.4g} {row.unit}; {int(row['count']):,} giá trị hữu hạn.")
                found["distribution"].append(f"{name}: khoảng P5–P95 [{row.p05:.4g}, {row.p95:.4g}] {row.unit}; thống kê percentile chính xác trên các điểm grid × time hữu hạn, ECDF chỉ dùng mẫu có seed={run.config.seed}.")
                break
    if not run.physical.empty:
        checked = run.physical[run.physical.status == "checked"]
        for _, row in checked.iterrows():
            found["quality"].append(f"{row['check']}: {int(row.violation_count):,}/{int(row.eligible_count):,} điểm vi phạm ({row.violation_pct:.4g}%).")
    for name in ("pm2p5", "pm1", "aod550", "t2m"):
        if name not in run.aggregations:
            continue
        agg = run.aggregations[name]
        monthly = agg["monthly_climatology"].to_series().dropna()
        hourly = agg["diurnal"].to_series().dropna()
        if len(monthly):
            found["temporal"].append(f"{name}: climatology trung bình vùng cao nhất tháng {int(monthly.idxmax())} ({monthly.max():.4g} {run.units[name]}), thấp nhất tháng {int(monthly.idxmin())} ({monthly.min():.4g} {run.units[name]}).")
        if len(hourly):
            found["temporal"].append(f"{name}: climatology tại các giờ được quan sát cao nhất lúc {int(hourly.idxmax()):02d}:00 UTC+7 ({hourly.max():.4g} {run.units[name]}). Dữ liệu 3 giờ không xác định được peak giữa các giờ này.")
        break
    name = "pm2p5" if "pm2p5" in run.spatial else "pm1" if "pm1" in run.spatial else None
    if name:
        grid = run.spatial[name]["mean"]
        if np.isfinite(grid.data).any():
            index = np.unravel_index(np.nanargmax(grid.data), grid.shape)
            found["spatial"].append(f"Ô có mean {name} cao nhất trong bounding box: {float(grid.latitude.data[index[0]]):.3f}°N, {float(grid.longitude.data[index[1]]):.3f}°E ({float(grid.data[index]):.4g} {run.units[name]}). Đây là ô grid, chưa được xác định là địa điểm hay hotspot trong ranh giới Việt Nam.")
    for name, result in run.trends.items():
        values = result.slope.data
        if np.isfinite(values).any():
            found["trend"].append(f"{name}: slope annual-grid trong khoảng [{np.nanmin(values):.4g}, {np.nanmax(values):.4g}] {run.units[name]}/year, ít nhất 3 năm có coverage ≥{run.config.minimum_annual_coverage:.0%}; chưa kiểm định ý nghĩa thống kê.")
    if not run.pearson.empty and "pm2p5" in run.pearson:
        targets = [n for n in ("pm1", "pm10", "wind_speed", "relative_humidity", "t2m", "aod550", *COLUMNS) if n in run.pearson and np.isfinite(run.pearson.loc["pm2p5", n])]
        for name in targets:
            found["relationships"].append(f"Daily mean PM2.5 với {name}: Pearson={run.pearson.loc['pm2p5', name]:.3f}, Spearman={run.spearman.loc['pm2p5', name]:.3f}, {int(run.pair_counts.loc['pm2p5', name]):,} ngày ghép hữu hạn; association mô tả.")
    if not run.statistics.empty:
        for name in ("pm1_pm2p5_ratio", "pm2p5_pm10_ratio"):
            rows = run.statistics[run.statistics.variable == name]
            if not rows.empty:
                row = rows.iloc[0]
                found["relationships"].append(f"{name}: mean={row['mean']:.4g}, median={row['median']:.4g}; mẫu số ≤0 hoặc không hữu hạn tạo NaN ({int(row.missing_count):,} missing ratio, bao gồm missing đầu vào).")
    if run.extreme_thresholds:
        found["extreme"].append(f"Ngưỡng pooled PM2.5: P95={run.extreme_thresholds['p95']:.4g}, P99={run.extreme_thresholds['p99']:.4g} {run.units['pm2p5']}. Tổng timestamp có ít nhất một ô vượt ngưỡng: {int(run.extreme_yearly.p95_timestep_count.sum()):,} (>P95) và {int(run.extreme_yearly.p99_timestep_count.sum()):,} (>P99).")
        for _, row in run.extreme_comparison.iterrows():
            found["extreme"].append(f"{row.variable}: mean tại điểm normal={row.normal_mean:.4g}, tại điểm PM2.5 >P95={row.extreme_mean:.4g} {row.unit}; so sánh đồng vị trí/thời điểm, chưa điều chỉnh mùa hoặc địa lý.")
    if section:
        result = found.get(section, [])
        return result or ["skipped: required variable not available, hoặc chưa có output đủ điều kiện; xem bảng section_status."]
    # Preserve coverage across main questions in 5–10 selected actual findings.
    key = []
    for category in ("inventory", "quality", "statistics", "temporal", "spatial", "trend", "relationships", "extreme"):
        if found[category]:
            key.append(found[category][0])
    if not run.consistency.empty and run.consistency.duplicate_messages.sum() and len(found['quality']) > 1:
        key.insert(2, found['quality'][1])
    if not run.physical.empty and 'violation_count' in run.physical:
        violations = run.physical[(run.physical.status == 'checked') & (run.physical.violation_count.fillna(0) > 0)]
        if not violations.empty:
            row = violations.loc[violations.violation_pct.idxmax()]
            key.append(f"{row['check']}: {int(row.violation_count):,}/{int(row.eligible_count):,} điểm vi phạm ({row.violation_pct:.4g}%), dữ liệu giữ nguyên để xem xét.")
    return key[:10]


def write_report(run):
    root = run.config.report_root
    root.mkdir(parents=True, exist_ok=True)

    def paragraphs(section):
        return "\n\n".join(findings(run, section))

    def figures(section):
        return "\n\n".join(f"![{path.stem.replace('_', ' ')}](figures/{path.name})\n\n[Biểu đồ PNG](figures/{path.name})" for path in run.assets.get(section, []))

    summary_cols = ["variable", "unit", "count", "missing_count", "missing_pct", "mean", "std", "min", "p01", "p05", "p25", "median", "p75", "p90", "p95", "p99", "max"]
    available = run.inventory.loc[run.inventory.status == "available", "short_name"].tolist()
    missing = run.inventory.loc[run.inventory.status == "missing", "short_name"].tolist()
    excluded = run.inventory.loc[run.inventory.status == "not_selected", "short_name"].tolist()
    problematic = run.inventory[run.inventory.status.isin(["error", "ambiguous"])]
    negatives = run.statistics[[c for c in ("variable", "nan_count", "inf_count", "negative_count", "suspicious_3iqr_count", "source_duplicate_messages") if c in run.statistics]]
    relationships_table = pd.DataFrame()
    if "pm2p5" in run.pearson:
        names = [n for n in ("pm1", "pm10", "wind_speed", "relative_humidity", "t2m", "aod550", *COLUMNS) if n in run.pearson]
        relationships_table = pd.DataFrame([{"variable": n, "pearson_with_pm2p5": run.pearson.loc["pm2p5", n], "spearman_with_pm2p5": run.spearman.loc["pm2p5", n], "paired_days": run.pair_counts.loc["pm2p5", n]} for n in names])
    text = f"""# CAMS EAC4 Exploratory Data Analysis

## 1. Dataset overview

Source: `{DATASET}`, các file GRIB tải bằng `src/download_cams_dataset.py`.
[Dataset chính thức CAMS EAC4](https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4).
Giai đoạn cấu hình: {run.config.start_year}–{run.config.end_year}; giai đoạn quan sát thực tế: {run.period}.
Lịch downloader: {', '.join(TIMES)} UTC, cadence {run.config.cadence}.
Area [North, West, South, East]: {list(VIETNAM_AREA)}. Phạm vi phân tích là bounding box, gồm đất liền ngoài Việt Nam và biển; không có country mask.

Available: {', '.join(available) or 'không có'}.
Missing: {', '.join(missing) or 'không có'}.
Không chọn trong lần chạy này: {', '.join(excluded) or 'không có'}.

{paragraphs('inventory')}

[Inventory đầy đủ, metadata và trạng thái từng biến](tables/inventory.csv).

File thừa hoặc bản thay thế:

{markdown_table(run.extras)}

File lỗi/không rõ bản cần dùng:

{markdown_table(problematic, ['short_name', 'status', 'notes'])}

## 2. Data quality

{paragraphs('quality')}

{markdown_table(run.consistency, ['variable', 'same_time_grid', 'same_spatial_grid', 'missing_timestamps', 'internal_missing_timestamps', 'duplicate_timestamps', 'duplicate_messages', 'irregular_intervals', 'latitude_order'])}

[Chi tiết khác biệt tọa độ, cadence, missing và duplicate examples](tables/consistency.csv).
Missing timestamp so với toàn giai đoạn cấu hình bao gồm các khoảng thiếu ở đầu/cuối; internal missing chỉ xét trong giai đoạn quan sát của từng file.
Duplicate GRIB message được quét bằng ecCodes trước khi dùng output cfgrib. Mỗi bản trùng được so sánh toàn bộ decoded values và grid geometry với bản đầu. Bản giống nhau có thể dùng representation đầu của cfgrib, được ghi rõ trong audit; nếu xung đột hoặc chưa kiểm chứng thì analysis liên biến bị bỏ qua. Thống kê đơn biến ghi rõ chính sách bản đầu; raw không được sửa và không xóa message nguồn.
[Tài liệu cfgrib về lazy loading và hypercube filtering](https://github.com/ecmwf/cfgrib).

{markdown_table(negatives)}

NaN là missing giá trị; infinity được báo riêng và chỉ loại khỏi các phép thống kê cần số hữu hạn.
Suspicious extremes dùng Q1−3IQR/Q3+3IQR như cờ cần xem xét, không xem max mặc nhiên là outlier, không xóa hoặc impute dữ liệu. Không dùng giá trị lặp phổ biến của một trường lượng tử hóa để kết luận duplicate; duplicate được định nghĩa theo message/timestamp.

{markdown_table(run.physical, ['check', 'status', 'eligible_count', 'violation_count', 'violation_pct', 'examples', 'notes'])}

Unit normalization chỉ áp dụng khi metadata xác nhận; [audit đổi đơn vị](tables/unit_conversions.csv).
RH: {RH_FORMULA}
[Nguồn công thức Magnus/WMO](<{RH_SOURCE}>).
Wind speed = hypot(u10, v10); hướng gió meteorological from-north = (270−degrees(atan2(v10,u10))) mod 360; gió lặng có direction NaN. Hướng là đại lượng vòng tròn, không đưa vào mean/correlation tuyến tính.

## 3. Descriptive statistics

{paragraphs('statistics')}

{markdown_table(run.statistics, summary_cols)}

[Bảng thống kê đầy đủ](tables/descriptive_statistics.csv). Pooled grid-time statistics không area-weighted; std dùng ddof=0; percentile chính xác kiểu linear trên số hữu hạn.

{figures('distribution')}

ECDF dùng tối đa {run.config.sample_size:,} điểm grid-time được chọn uniform không thay thế trên từng biến, seed={run.config.seed}; chỉ loại số không hữu hạn khỏi mẫu hiển thị. Summary và ngưỡng extreme không dùng mẫu này. Với mẫu không âm có P99 >5×median và max >10×P99, trục x dùng symlog (linear gần zero, ngưỡng ghi ngay trên axis) để thấy cả thân phân phối và upper tail. Giá trị và thống kê giữ nguyên; mẫu có giá trị âm dùng trục linear.
Nhóm PM cùng unit dùng chung range và axis transformation; các nhóm khác unit có thang riêng ghi trên từng panel, không so sánh độ lớn chỉ qua hình học.

## 4. Temporal patterns

{paragraphs('temporal')}

{figures('temporal')}

Regional mean dùng cos(latitude) trên số hữu hạn trong bounding box; aggregate daily/monthly/annual từ chuỗi 3-hourly để tránh trung bình các tháng có trọng số không đúng. UTC giữ nguyên; local time = UTC+7 là tọa độ phụ. Climatology dùng quan sát hữu hạn thực tế.
Annual line hiển thị cả năm quan sát thiếu và không được tự xem là trend đã kiểm định. [Annual counts](tables/annual_count.csv), [daily counts](tables/daily_count.csv), [monthly means](tables/monthly.csv), [climatology](tables/monthly_climatology.csv), [local-hour climatology](tables/diurnal.csv).

## 5. Spatial patterns

{paragraphs('spatial')}

{figures('spatial')}

Mean/median/std/P95 tính dọc time trên từng ô nguyên bản; tọa độ giữ đúng GRIB, không interpolate hay regrid. Các bảng `tables/spatial_<variable>.csv` chứa grid đã rút gọn, không có raw time axis.

{paragraphs('trend')}

{figures('trend')}

Spatial slope dùng OLS theo calendar year của annual grid mean; một năm tại một ô phải có ít nhất {run.config.minimum_annual_coverage:.0%} số giá trị 3-hourly kỳ vọng; cần ≥3 năm đủ điều kiện. Các bảng annual_grid_coverage ghi rõ năm bị thiếu. Chưa hiệu chỉnh autocorrelation hay tính p-value, không khẳng định trend có ý nghĩa thống kê.

## 6. Relationships

{markdown_table(relationships_table)}

{paragraphs('relationships')}

{figures('relationships')}

{markdown_table(run.seasonal)}

Pearson/Spearman dùng daily area-weighted means của các biến có cùng time và spatial grid, pairwise finite với ít nhất 3 ngày; pair-count matrix được lưu để audit. Daily aggregation giảm số điểm nhưng không loại bỏ autocorrelation/seasonality. [Pearson](tables/pearson.csv), [Spearman](tables/spearman.csv), [paired days](tables/correlation_paired_days.csv).
PM ratio tính ở grid/time trước khi aggregate; mẫu số ≤0/nonfinite cho NaN và không impute.
PM2.5 là near-surface mass concentration; AOD550 là column-integrated optical property, dimensionless khi metadata xác nhận. Association không chứng minh quan hệ 1:1 hay phép đổi AOD thành PM2.5.
CO, NO2, SO2, O3 trong project là **total column**; unit giữ theo metadata, không phải surface concentration và không đổi tùy tiện sang µg/m³.

## 7. Extreme PM2.5

{paragraphs('extreme')}

{figures('extreme')}

{markdown_table(run.extreme_comparison)}

Ngưỡng P95/P99 là percentile pooled trên các điểm PM2.5 grid × time hữu hạn toàn giai đoạn. So sánh strict `>`; một extreme timestep là timestamp UTC có **ít nhất một ô** vượt ngưỡng, khác với ngưỡng trên regional mean. Spatial frequency chia số vượt ngưỡng cho số quan sát hữu hạn tại chính ô đó. Counts theo năm/tháng có tỷ lệ và finite timestep denominator trong bảng CSV.
Normal = PM2.5 ≤P95; conditions so sánh co-located grid-time finite observations, trọng số mỗi điểm bằng nhau, chưa điều chỉnh mùa/địa lý. Không suy luận causal relationship hay dùng đây như ngưỡng tiêu chuẩn chất lượng không khí.

## 8. Modeling implications

Kết quả association, climatology và spatial slope ở trên là gợi ý chuẩn bị feature, chưa chứng minh sức mạnh dự báo. Kiểm tra thêm local-hour/month/season, các đặc trưng meteorology/AOD có sẵn và PM fraction redundancy bằng validation theo thời gian.
Daily autocorrelation và spatial effects cần temporal holdout, geographic holdout hoặc blocked validation; random split grid-time có thể đánh giá quá lạc quan.
Ngưỡng percentile, climatology, normalization thống kê và feature selection phải fit chỉ trên training period. Không dùng PM contemporaneous fractions hoặc future rolling statistics khi target là PM2.5 nếu thông tin đó không có tại thời điểm dự báo. EAC4 là reanalysis hồi cứu: kiểm tra độ trễ/khả dụng của feature trong deployment thực tế. Không train model trong pipeline này.

## 9. Data limitations

EAC4 là reanalysis, không thay thế quan trắc tại trạm. Grid resolution giới hạn diễn giải ở cấp địa điểm; ô có giá trị cao nhất bounding box chưa được xác nhận nằm trong ranh giới Việt Nam. Không có boundary overlay tải từ Internet.
Total-column pollutants không phải surface concentrations; AOD không phải PM2.5. Correlation không đồng nghĩa causation. RH dùng xấp xỉ saturation over liquid water không clipping; đọc check range và RH violations trước khi sử dụng.
Thiếu file, time gaps, nonfinite data hoặc mismatched grids làm giảm phạm vi phân tích; các phần phụ thuộc hiển thị skipped. Không regrid, interpolate, xóa outlier, chỉnh raw hay export toàn bộ raw grid sang CSV.

## 10. Key findings

"""
    text += "\n".join(f"{i}. {line}" for i, line in enumerate(findings(run), 1))
    text += "\n\nTrạng thái các phần phụ thuộc dữ liệu:\n\n" + markdown_table(pd.DataFrame(run.statuses))
    text += "\n\n[Trạng thái CSV](tables/section_status.csv). Reproduce: mở `notebooks/01_cams_eac4_eda.ipynb` và Run All hoặc `python -m src.eda`.\n"
    # Do not link unavailable outputs in reduced/all-missing runs.
    import re
    def prune(match):
        label, target = match.group(1), match.group(2)
        return match.group(0) if (root / target).is_file() else f"{label} (skipped: required variable not available)"
    text = re.sub(r"\[([^\]]+)\]\((tables/[^)]+)\)", prune, text)
    target = root / "cams_eac4_eda_report.md"
    target.write_text(text, encoding="utf-8")
    provenance = {"source": DATASET, "config": {"data_root": str(run.config.data_root), "start_year": run.config.start_year, "end_year": run.config.end_year, "variables": list(run.config.variables or VARIABLES), "seed": run.config.seed, "sample_size": run.config.sample_size, "minimum_annual_coverage": run.config.minimum_annual_coverage, "filter_by_keys": run.config.filter_by_keys}, "observed_period": run.period, "python": platform.python_version(), "packages": {p: version(p) for p in ("numpy", "pandas", "xarray", "cfgrib", "eccodes", "dask", "scipy", "matplotlib", "nbformat", "nbclient", "ipykernel")}, "source_fingerprints": run.keys, "cache_policy": "Disposable normalized grid .npy working buffers and header/index caches; rebuild via rebuild_cache=True; never raw/Bronze; no raw sidecars", "figure_files": [str(p.relative_to(root)) for paths in run.assets.values() for p in paths]}
    (root / "eda_run_manifest.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return target

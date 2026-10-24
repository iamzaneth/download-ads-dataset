"""Build the staged notebook with nbformat (no hand-edited notebook JSON)."""

from pathlib import Path

import nbformat as nbf

ROOT = Path(__file__).resolve().parents[1]
cells = []


def markdown(text):
    cells.append(nbf.v4.new_markdown_cell(text))


def code(text):
    cells.append(nbf.v4.new_code_cell(text))


def section(number, title, question, why):
    markdown(f"## {number}. {title}\n\n**Question:** {question}\n\n**Why this analysis:** {why}")


def finding(name):
    code(f"display(Markdown('**Finding:**\\n\\n' + '\\n\\n'.join(run.findings({name!r}))))")


markdown("""# CAMS EAC4 EDA — Việt Nam, native GRIB grid

Pipeline dùng `VARIABLES`, lịch giờ và phạm vi từ downloader. Raw GRIB chỉ được đọc.
Các kết quả và finding được tính khi chạy notebook; không có kết luận mẫu hard-code.

**Chạy:** `python -m pip install -r requirements.txt`, chọn kernel của môi trường
đó và Run All. Không cần API token để EDA dữ liệu đã có. CLI tương đương: `python -m src.eda`.

**Key assumptions:** tọa độ time là UTC; regional mean dùng cos(latitude) trên
bounding box gồm cả biển/đất ngoài Việt Nam, không có country mask. Không regrid,
interpolate, impute hay xóa outlier. Percentile summary chính xác; ECDF dùng mẫu uniform
seed cố định. Trend chỉ dùng annual grid mean đủ coverage. Xem unit audit và chất lượng trước
khi diễn giải các biến dẫn xuất. Cache là working storage EDA có thể dựng lại, không phải raw/Bronze.
""")
section(0, "Setup", "Notebook đang chạy từ project nào và môi trường nào?", "Giữ đường dẫn và package versions rõ ràng để có thể chạy lại.")
code("""import os
import sys
from pathlib import Path
from importlib.metadata import version

import pandas as pd
from IPython.display import Image, Markdown, display

PROJECT = next((p for p in (Path.cwd(), *Path.cwd().parents)
                if (p / 'src/download_cams_dataset.py').is_file()), None)
if PROJECT is None:
    raise RuntimeError('Run this notebook from the project root or notebooks directory')
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from src.download_cams_dataset import VARIABLES, TIMES, VIETNAM_AREA, DATASET, DEFAULT_DATA_ROOT
from src.eda import EDAConfig, EDARun

def show_figures(paths):
    if not paths:
        display(Markdown('skipped: required variable not available, hoặc không đủ quan sát hợp lệ'))
    for path in paths:
        display(Image(filename=str(path)))

display(pd.DataFrame({'package': [p for p in ('xarray', 'cfgrib', 'eccodes', 'numpy', 'pandas', 'matplotlib', 'scipy', 'dask')],
                      'version': [version(p) for p in ('xarray', 'cfgrib', 'eccodes', 'numpy', 'pandas', 'matplotlib', 'scipy', 'dask')]}))
display(Markdown(f'**Finding:** Project: `{PROJECT}`; Python: `{sys.version.split()[0]}`.'))
""")
section(1, "Project configuration", "Giai đoạn, biến và sampling rule nào sẽ được dùng?", "Cấu hình tập trung để notebook tự kích hoạt khi đủ các file downloader.")
code("""# Default: all downloader variables, 2003–2025. Environment overrides support validation.
config = EDAConfig(
    data_root=Path(os.environ.get('CAMS_EDA_DATA_ROOT', str(DEFAULT_DATA_ROOT))),
    report_root=Path(os.environ.get('CAMS_EDA_REPORT_ROOT', str(PROJECT / 'reports'))),
    cache_root=Path(os.environ['CAMS_EDA_CACHE_ROOT']) if os.environ.get('CAMS_EDA_CACHE_ROOT') else None,
    chunk_time=2048,
    sample_size=50_000,
    seed=42,
    minimum_annual_coverage=0.95,
    rebuild_cache=False,  # True rebuilds disposable EDA buffers/index/header summaries.
    filter_by_keys={},  # e.g. {'pm1': {'paramId': ...}} if metadata exposes ambiguous hypercubes.
)
run = EDARun(config)
import logging
config.report_root.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=config.report_root / 'execution.log', encoding='utf-8',
                    level=logging.INFO, format='%(asctime)s | %(message)s', force=True)
display(Markdown(f'**Finding:** `{DATASET}`, {config.start_year}–{config.end_year}; '
                 f'{len(config.expected_time):,} expected timestamps, cadence {config.cadence}; '
                 f'area {VIETNAM_AREA}; seed={config.seed}.'))
display(pd.DataFrame(VARIABLES.items(), columns=['short_name', 'source_variable_name']))
""")
section(2, "Discover GRIB files", "Biến nào có file, thiếu file hoặc có file không thuộc VARIABLES?", "Không làm crash bản mẫu chỉ có PM1; không tự chọn giữa các file không rõ ràng.")
code("""display(run.discover()[['short_name', 'source_variable_name', 'status', 'filename', 'file_size']])
display(run.extras)
display(Markdown('**Finding:** ' + ', '.join(
    f'{status}={count}' for status, count in run.inventory.status.value_counts().items())))
""")
section(3, "Dataset inventory", "Metadata thực tế nói gì về unit, time axis và native grid?", "Đọc GRIB bằng cfgrib/ecCodes; file trên 65.536 timestamp vẫn được kiểm tra toàn bộ.")
code("""inventory, consistency_report = run.inspect()
display(inventory)
""")
finding("inventory")
section(4, "Grid/time consistency", "Các biến có cùng tọa độ, thứ tự grid và timestamp hay không?", "Tránh align/regrid ngầm làm sai kiểm tra vật lý hoặc relationship.")
code("""display(consistency_report)
display(Markdown('Tham khảo `reports/tables/consistency.csv` để xem missing examples, '
                 'duplicate GRIB examples, extent/resolution và khác biệt với reference. '
                 'Missing count gồm đầu/cuối giai đoạn cấu hình; internal missing chỉ trong giai đoạn quan sát.'))
""")
finding("quality")
section(5, "Unit normalization", "Có thể chuyển unit nào và derive wind/RH nào từ metadata xác nhận?", "Không coi AOD là PM hoặc total-column variables là surface concentration.")
code("""display(run.normalize())
display(pd.DataFrame(run.statuses))
from src.eda.transformations import RH_FORMULA, RH_SOURCE
display(Markdown(f'RH formula: {RH_FORMULA}\\n\\n[WMO reference]({RH_SOURCE})'))
display(Markdown('**Finding:** Các conversion ở bảng trên áp dụng trên analysis cache; '
                 'raw GRIB giữ nguyên. Wind direction dùng meteorological from-north; calm = NaN. '
                 'RH chưa clipping. Unit không nhận diện được giữ nguyên và check phụ thuộc unit bị skipped.'))
""")
section(6, "Data quality", "PM ordering, dewpoint và RH có vi phạm cơ sở vật lý không?", "Chỉ flag/đếm vi phạm và lưu ví dụ đồng vị trí/thời điểm, không sửa dữ liệu ngầm.")
code("""display(run.check_quality())
display(Markdown('**Finding:** ' + '\\n\\n'.join(
    f"{r['check']}: {r['status']}" +
    (f"; violations={int(r['violation_count']):,}/{int(r['eligible_count']):,} "
     f"({r['violation_pct']:.4g}%)" if r['status'] == 'checked' else '')
    for _, r in run.physical.iterrows())))
""")
section(7, "Descriptive statistics", "Phân phối hữu hạn, missing, infinity và suspicious extremes có quy mô nào?", "Exact P1–P99 và cờ 3IQR đặt max vào bối cảnh; không xóa outlier.")
code("""statistics = run.summarize()
summary_columns = ['variable', 'unit', 'count', 'missing_count', 'missing_pct',
                   'mean', 'std', 'min', 'p01', 'p05', 'p25', 'median', 'p75', 'p90', 'p95', 'p99', 'max']
display(statistics[[c for c in summary_columns if c in statistics]])
display(statistics[[c for c in ('variable', 'nan_count', 'inf_count', 'negative_count',
                               'suspicious_3iqr_count', 'source_duplicate_messages') if c in statistics]])
""")
finding("statistics")
section(8, "Distribution analysis", "Nhóm PM, meteorology và aerosol/column có shape và spread nào?", "Một ECDF mỗi biến trong figure nhóm; chỉ sample phần hiển thị, không sample summary.")
code("show_figures(run.distributions())")
finding("distribution")
section(9, "Temporal patterns", "Annual means, monthly seasonality và local-hour cycle khác nhau thế nào?", "Aggregate trước để tránh raw plot 67k timestamp; UTC gốc không bị đổi.")
code("show_figures(run.temporal())")
code("""# Annual finite regional timestep counts identify incomplete years; no zero-filling observations.
display(pd.concat({name: agg['annual_count'].to_series()
                   for name, agg in run.aggregations.items()}, axis=1)
        if run.aggregations else pd.DataFrame())
""")
finding("temporal")
section(10, "Spatial patterns", "Mean và P95 tập trung ở ô nào của native grid?", "Tính mean/median/std/P95 dọc time từng ô; không flatten trước aggregation.")
code("show_figures(run.spatial_patterns())")
finding("spatial")
section(11, "Spatial trends", "Annual grid mean thay đổi tuyến tính theo năm như thế nào?", "Dùng ≥3 năm có coverage ≥95%, tránh fit regression raw 3-hourly hoặc diễn giải năm thiếu như năm đầy đủ.")
code("show_figures(run.spatial_trends())")
finding("trend")
section(12, "Cross-variable relationships", "PM fractions, meteorology, AOD và total columns liên hệ ra sao?", "Dùng daily area-weighted means, exact grid/time alignment và paired-day denominator; không train model.")
code("show_figures(run.relationships())")
code("""display(run.seasonal)
display(run.pair_counts)
display(Markdown('PM2.5 = near-surface mass concentration; AOD = column-integrated optical property. '
                 'CO/NO2/SO2/O3 = total columns với unit từ GRIB. '
                 'Correlation không phải quan hệ 1:1 hoặc causal relationship. '
                 'PM ratios dùng NaN khi mẫu số ≤0/nonfinite.'))
""")
finding("relationships")
section(13, "Extreme PM2.5", "Pooled P95/P99 bị vượt khi nào, ở đâu và đi kèm điều kiện nào?", "Định nghĩa extreme data-driven, đếm timestep có ≥1 ô vượt ngưỡng và so sánh co-located finite points.")
code("""show_figures(run.extreme_pm25())
display(run.extreme_comparison)
display(run.extreme_yearly)
""")
finding("extreme")
section(14, "Modeling implications", "Những dependency, redundancy và leakage risk nào cần kiểm tra trước modeling?", "EDA chỉ đề xuất feature để validation, không khẳng định feature hữu ích qua association đơn giản.")
code("""autocorrelation = pd.DataFrame([
    {'variable': name, 'daily_lag1_autocorrelation': series.autocorr(1),
     'daily_lag7_autocorrelation': series.autocorr(7), 'finite_days': series.notna().sum()}
    for name, series in run.daily.items()])
display(autocorrelation)
run._save_table('daily_autocorrelation', autocorrelation)
display(Markdown('**Finding:** Autocorrelation trong bảng trên được tính từ chuỗi daily mean; '
                 'dùng temporal/geographic holdout để kiểm tra ảnh hưởng phụ thuộc thời gian và không gian. '
                 'Fit ngưỡng percentile/climatology chỉ từ training period; không dùng target PM fractions '
                 'đồng thời hoặc future rolling statistics nếu chúng chưa có lúc dự báo. '
                 'Feature meteorology/AOD cần validation ngoài mẫu; chưa train model.'))
""")
section(15, "Key findings and report", "Các kết quả thực tế quan trọng nhất và artifact tái lập nằm ở đâu?", "Report lấy finding từ output đã tính, kèm missing/skipped và limitation.")
code("""report_path = run.write_report()
display(Markdown('**Finding:**\\n\\n' + '\\n'.join(
    f'{i}. {text}' for i, text in enumerate(run.findings(), 1))))
display(pd.DataFrame(run.statuses))
display(Markdown(f'Report: `{report_path}`; figures: `{config.report_root / "figures"}`; '
                 f'compact tables: `{config.report_root / "tables"}`; '
                 'provenance/package versions: `eda_run_manifest.json`.'))
run.close()
""")

notebook = nbf.v4.new_notebook(cells=cells, metadata={
    "kernelspec": {"display_name": "Python 3 (CAMS EDA)", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.12"},
})
nbf.validate(notebook)
destination = ROOT / "notebooks" / "01_cams_eac4_eda.ipynb"
destination.parent.mkdir(parents=True, exist_ok=True)
nbf.write(notebook, destination)
print(destination)

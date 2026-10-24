# Pipeline tải dữ liệu CAMS EAC4

Script duy nhất: [`src/download_cams_dataset.py`](src/download_cams_dataset.py).
Tải trực tiếp toàn vùng **6–24°B, 102–118,5°Đ**, gồm đất liền, Hoàng Sa,
Trường Sa và vùng biển giữa các khu vực: grid 25 × 23, bước 0,75°.
Đây là hộp lấy dữ liệu, không phải ranh giới hành chính.

Pipeline tải dữ liệu CAMS EAC4 cho khu vực Việt Nam theo nguyên tắc: mỗi request
chỉ chứa một biến và một năm. Với giai đoạn mặc định 2003-2025, mỗi biến có 23
request tuần tự. Khi đủ dữ liệu, 23 file năm được nối theo thứ tự thời gian thành
một file GRIB duy nhất cho từng biến, sau đó mới chuyển sang biến tiếp theo.
Toàn bộ 20 biến tương ứng 460 request năm, chưa tính retry. Không cần tải riêng
đất liền/đảo, không chia request theo tháng và không cần ghép không gian nữa.

14 biến single level giữ nguyên: `pm1 pm2p5 pm10 u10 v10 t2m d2m sp mslp aod550
co_column no2_column so2_column o3_column`.
Thêm 6 biến multi level: `co no2 so2 o3 t_model q_model`, luôn chọn
`model_level=["60"]` (tầng model thấp nhất của EAC4). Chỉ request cho 6 biến này
có khóa `model_level`; các request single level không thay đổi.
Theo [hướng dẫn ADS](https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4?tab=download),
level 60 dùng để lấy giá trị gần bề mặt của các biến ba chiều.
`co/no2/so2/o3` là tỷ lệ trộn khối lượng (kg/kg), khác với biến `*_column`;
`t_model` có đơn vị K, `q_model` có đơn vị kg/kg. Script giữ nguyên dữ liệu gốc.

## Cài đặt

1. Tạo/kích hoạt môi trường Python và cài dependency:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Cấu hình CDS/ADS API token trong `.cdsapirc` và chấp nhận điều khoản của
   dataset [`cams-global-reanalysis-eac4`](https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4)
   trên ADS.

3. Xem trước toàn bộ kế hoạch mà không tải dữ liệu:

   ```powershell
   python src/download_cams_dataset.py --dry-run
   ```

4. Tải tất cả biến và nối các file năm:

   ```powershell
   python src/download_cams_dataset.py
   ```

Script gửi request tuần tự, tự retry lỗi tạm thời và có thể chạy tiếp sau khi bị
gián đoạn bằng cách bỏ qua các file GRIB năm đã vượt qua kiểm tra: đúng biến,
đủ lịch UTC 3 giờ (2.920/2.928 mốc mỗi năm), không trùng message, đúng toàn bộ
grid đất liền và biển đảo, không thiếu giá trị. Không tạo cfgrib index cạnh raw.
Các biến multi level còn phải đúng loại tầng `hybrid` và level `60`; file sai
tầng không được dùng để resume hoặc ghép. Tất cả biến dùng chung phạm vi grid
và 8 mốc giờ UTC/ngày. File thiếu/sai grid bị từ chối trước khi công bố file năm.

Khi chạy lại cùng lệnh (không có `--overwrite`), script kiểm tra các file có sẵn
và in `[RESUME]`: số năm được bỏ qua và danh sách chính xác những năm cần tải.
`[CHECK]` là đọc kiểm tra file trên máy; chỉ `[DOWNLOAD]` mới gửi request ADS.
File năm hợp lệ không bị tải lại. `--overwrite` mới buộc tải lại toàn bộ năm.

ADS đôi khi trả message trùng trong cùng request năm. Với `t_model` 2019,
phản hồi đã kiểm tra có 3.077 message, gồm 2.920 mốc giờ và 157 message trùng
hoàn toàn từng byte trong tháng 8. Script chỉ loại message giống hệt từng byte
trên file tải tạm, sau đó kiểm tra lại đầy đủ lịch năm, biến, tầng, grid và giá
trị. Message cùng mốc giờ nhưng khác dữ liệu không được tự chọn hoặc bỏ qua.

## Theo dõi log trên terminal

Mặc định terminal hiển thị log có timestamp, trạng thái từng biến, tiến độ tổng
`[REQUEST hiện_tại/tổng]`, năm đang tải, retry, thời gian tải, dung lượng file,
tiến độ ghép và thống kê cuối phiên. Ví dụ:

```text
2026-09-11 19:20:01 | INFO    | [REQUEST 1/322][DOWNLOAD] biến=pm1 | năm=2003 | lần=1/3
2026-09-11 19:24:18 | INFO    | [REQUEST 1/322][DONE] biến=pm1 | năm=2003 | dung lượng=18.4 MB | thời gian=4m 17s
2026-09-11 19:24:20 | INFO    | [REQUEST 2/322][DOWNLOAD] biến=pm1 | năm=2004 | lần=1/3
```

Dùng log chi tiết để xem cả nội dung request và đường dẫn file đích:

```powershell
python src/download_cams_dataset.py --log-level DEBUG
```

Lưu log để có thể kiểm tra lỗi và tiến độ sau khi đóng terminal:

```powershell
.\.venv\Scripts\python.exe src/download_cams_dataset.py --variables t_model --log-file reports/t_model_resume.log
```

Log được ghi nối tiếp bằng UTF-8. Lệnh trên tự bỏ qua các năm hợp lệ, tải những
năm còn thiếu và ghép `t_model_2003_2025_vietnam.grib` khi đủ dữ liệu.

## Cấu trúc dữ liệu

```text
data/CAMS/EAC4/
|-- yearly/
|   |-- pm2p5/
|   |   |-- pm2p5_2003_vietnam.grib
|   |   `-- ... pm2p5_2025_vietnam.grib
|   `-- <variable>/
`-- merged/
    |-- pm2p5_2003_2025_vietnam.grib
    `-- <variable>_2003_2025_vietnam.grib
```

Các file năm được giữ lại để có thể resume hoặc build lại file ghép. File đang
tải dùng hậu tố `.grib.part` và không bao giờ được xem là dữ liệu hoàn chỉnh.
Hậu tố `_vietnam` giờ chỉ toàn bộ rectangle đất liền và biển đảo ở trên.

Sau đợt dọn thư mục, giữ 322 file năm từ bộ toàn vùng đã kiểm chứng và giữ nguyên
14 file trong `merged/`. Đã bỏ các bản đất liền cũ, các bản tải trung gian
trong `islands/` (kể cả `_monthly/`) và script ghép không gian cũ. Nội dung
file năm được đối chiếu từng byte với phần tương ứng trong file tổng trước khi
dọn. Bằng chứng hash, dung lượng và mtime trước/sau nằm trong
[`reports/cams_cleanup_audit.json`](reports/cams_cleanup_audit.json).
Kiểm tra lại bằng `python scripts/audit_cams_cleanup.py --after` ngay sau dọn;
báo cáo này ghi nhận snapshot, không phải cấu hình cho các lần tải mới.

## Các lệnh thường dùng

Chỉ tải và nối một số biến:

```powershell
python src/download_cams_dataset.py --variables pm2p5 pm10 aod550
```

Chỉ bổ sung 6 biến model level 60, mỗi biến ghép riêng giai đoạn 2003–2025:

```powershell
.\.venv\Scripts\python.exe src/download_cams_dataset.py --variables co no2 so2 o3 t_model q_model
```

Chọn một biến, hoặc kết hợp biến single level và multi level:

```powershell
.\.venv\Scripts\python.exe src/download_cams_dataset.py --variables no2
.\.venv\Scripts\python.exe src/download_cams_dataset.py --variables pm2p5 no2 t_model --dry-run
```

Không truyền `--variables` sẽ xử lý cả 20 biến; file năm hợp lệ có sẵn được bỏ
qua. Các biến model level có thư mục và tên riêng, ví dụ
`yearly/no2/no2_2003_vietnam.grib` và `merged/no2_2003_2025_vietnam.grib`.
`--dry-run` in cả request mẫu để kiểm tra biến, grid, thời gian và tầng model.

Kiểm tra kết nối ADS bằng 6 mẫu nhỏ (mỗi biến mới một mốc 21:00 UTC ngày
31/12/2025), đối chiếu tọa độ từng điểm với các file GRIB hiện có:

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_cams_model_level
```

Lệnh này cần token ADS; mẫu tải nằm trong thư mục tạm, không ghi vào `yearly/`
hay `merged/`. Báo cáo lưu tại `reports/cams_model_level_verification.json`.
Kiểm tra mẫu xác nhận kết nối, biến/tầng, mốc giờ, giá trị và grid; việc đủ
toàn bộ năm vẫn được kiểm tra bởi downloader sau mỗi request năm.

Chỉ nối các file đã có trong `yearly/` (không gọi ADS):

```powershell
python src/download_cams_dataset.py --stage merge
```

Tải lại và build lại một biến:

```powershell
python src/download_cams_dataset.py --variables pm2p5 --overwrite
```

Dùng thư mục dữ liệu khác (đường dẫn tương đối được tính từ project root):

```powershell
python src/download_cams_dataset.py --data-root data/CAMS/EAC4
```

## EDA dữ liệu GRIB đã tải

Notebook chính: [`notebooks/01_cams_eac4_eda.ipynb`](notebooks/01_cams_eac4_eda.ipynb).
Cài `requirements.txt`, chọn kernel Python của môi trường đã cài và chạy **Run All**.
EDA chỉ đọc file trong `data/CAMS/EAC4/merged/`; không cần API token để phân tích.
Danh sách biến, mặc định giai đoạn, lịch UTC và area lấy từ downloader hiện tại.

Có thể chạy cùng pipeline trên terminal:

```powershell
python -m src.eda
```

Chạy EDA một số biến, lưu report ở thư mục khác:

```powershell
python -m src.eda --variables pm1 --report-root reports/pm1_only
```

Notebook tự phân tích các file có sẵn. Với bộ mẫu chỉ chứa PM1, inventory ghi
13 biến còn lại là `missing`; distribution, thống kê, temporal và spatial PM1
vẫn chạy. Các kiểm tra PM fractions, RH/wind, PM2.5 relationships/extremes và
trend của các biến chưa có hiển thị `skipped: required variable not available`.
Khi đủ 14 file, các phần này tự kích hoạt mà không sửa code.

Đầu ra chính:

* [`reports/cams_eac4_eda_report.md`](reports/cams_eac4_eda_report.md): report gồm
  10 phần với finding lấy từ kết quả và link tới PNG đã lưu.
* `reports/figures/`: ECDF theo nhóm, annual/monthly/local-hour climatology,
  year–month heatmap, native grid maps, annual-grid slope, daily correlations,
  PM fractions/meteorology/column relationships và extreme PM2.5.
* `reports/tables/`: inventory, consistency, unit/physical audit, exact
  descriptive statistics và bảng aggregation nhỏ; không export raw grid-time
  thành CSV.
* `reports/eda_run_manifest.json`: cấu hình, observed period, package versions,
  source fingerprints và danh sách figure của lần chạy.

GRIB đọc lazy bằng **xarray + cfgrib + ecCodes**; không dùng Rasterio/GDAL hay
giới hạn giả 65.536 bands. Nếu có nhiều GRIB hypercube, loader dùng
`cfgrib.open_datasets()` hoặc selector `filter_by_keys` trong notebook. Hypercube
khác grid/level/parameter không được merge ngầm; inventory báo lỗi để chọn
metadata đúng. File không rõ bản cần dùng được đánh dấu `ambiguous`.

Duplicate messages được đếm bằng ecCodes vì cfgrib có thể gộp chúng. Mỗi bản
trùng được so sánh toàn bộ decoded values và geometry với bản đầu. Khi giống
nhau, analysis dùng representation đầu của cfgrib và ghi rõ audit; khi xung
đột/chưa kiểm chứng, phần liên biến phụ thuộc nguồn đó bị skipped. Không xóa
message khỏi raw. Duplicate timestamp còn tồn tại trong analysis axis luôn
làm kiểm tra liên biến bị skipped.

PM chỉ đổi kg/m³ → µg/m³ khi metadata xác nhận; Kelvin → °C và Pa → hPa tương
tự. AOD giữ bản chất optical depth; CO/NO2/SO2/O3 giữ unit total column và không
được diễn giải là surface concentration. RH dùng công thức Magnus/WMO được
ghi trong report và không clipping. Raw không chỉnh sửa, không impute, xóa
outlier, regrid hay interpolate. Regional mean dùng cos(latitude) trong bounding
box **gồm biển và phần ngoài ranh giới Việt Nam**, không phải country mean.
Spatial trend cần ≥3 năm có annual coverage ≥95%; correlation là mô tả, chưa
hiệu chỉnh autocorrelation hoặc chứng minh causation.

### Cache và mức sử dụng tài nguyên

`data/CAMS/EAC4/eda_cache/` là working storage EDA, **không phải raw/Bronze**.
Cache chứa cfgrib indexes, ecCodes header audits và các mảng analysis đã chuẩn
hóa `.npy` để chỉ decode GRIB một lần theo block 2.048 timestamp; các phép tính
đọc block từ disk và dùng scheduler synchronous. Dask không sao chép toàn bộ
memmap khi dựng graph. Global percentile chính xác dùng một scratch buffer trên
disk cho từng biến; spatial quantile dùng tile 4×4 ô với toàn time axis. Summary
không phụ thuộc mẫu ECDF uniform 50.000 điểm có seed=42.

Cache được bỏ qua bởi `.gitignore`, có thể xóa riêng khi không có phiên EDA đang
chạy hoặc rebuild bằng:

```powershell
python -m src.eda --rebuild-cache
```

Cache key phụ thuộc đường dẫn/size/mtime GRIB, selector, cache schema và version
cfgrib/ecCodes; raw sidecars không được tạo. Bộ toàn vùng 67.208 × 25 × 23 dùng
khoảng 147 MiB mỗi cached float32 field, thêm derived fields và indexes; chuẩn bị
vài GiB disk trống. Không commit cache hoặc dataset khổng lồ.

### Chạy và kiểm tra notebook bằng đúng môi trường

```powershell
python scripts/execute_eda_notebook.py
python scripts/execute_eda_notebook.py --pm1-only
python -m unittest discover -s tests -v
```

Script execution dùng đúng interpreter đang gọi, lưu notebook đã execute với
figure/table outputs và không đăng ký kernel toàn cục. Kiểm tra `--pm1-only`
dùng hard link tạm tới PM1 thật, không sao chép hay sửa raw; report và notebook
kiểm tra nằm ở `reports/validation_pm1/`. Cấu trúc notebook có thể được scaffold
lại bằng `scripts/build_eda_notebook.py` (lệnh này tạo lại notebook chưa execute).

Trong notebook, cấu hình `EDAConfig` tập trung cho data/report/cache roots,
giai đoạn, chunk, seed, coverage và selectors. Các biến môi trường
`CAMS_EDA_DATA_ROOT`, `CAMS_EDA_REPORT_ROOT`, `CAMS_EDA_CACHE_ROOT` hỗ trợ chạy
kiểm tra độc lập mà không sửa notebook. Artifact của bộ PM1 và bộ đủ biến ghi
rõ scope trong inventory và report.

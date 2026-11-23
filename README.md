# Elearning TDTU Deadline Bot

Bot Telegram nhắc deadline **bài tập** và **bài kiểm tra (quiz)** trên elearning TDTU (Moodle) mà bạn **chưa làm**.

Bot chạy mỗi 1 giờ, chỉ gửi tin khi có deadline vừa chạm một mốc nhắc, nếu không có gì thì im lặng.

| Mốc | Khi nào nhắc |
|---|---|
| 7 ngày | còn ≤ 168 giờ |
| 3 ngày | còn ≤ 72 giờ |
| 1 ngày | còn ≤ 24 giờ |
| 3 giờ | còn ≤ 3 giờ |
| 1 giờ | còn ≤ 1 giờ |

Tin nhắn ghi thời gian còn lại thực tế (ví dụ "Còn 2 ngày 5 giờ"), kèm tên bài, môn, hạn nộp và link.

## Cách hoạt động

1. Bot dùng Playwright đăng nhập elearning, mở trang Calendar.
2. Bot gọi web service của Moodle để lấy sự kiện của tháng này và tháng sau (cùng dữ liệu bạn thấy trên Calendar).
3. Với mỗi bài có hạn trong 7 ngày tới, nếu sắp nhắc thì bot mở trang bài để kiểm tra:
   - **Bài tập:** trạng thái "Submitted for grading" là đã làm.
   - **Quiz:** có lần làm bài ở trạng thái "Finished" là đã làm.
4. Bài chưa làm được gom vào một tin Telegram. Bài đã làm bị bỏ qua và được ghi nhớ.
5. Mốc đã nhắc được lưu trong `state.json`, workflow tự commit file này lại repo để không nhắc trùng.

Bot chỉ theo dõi bài tập (`assign`) và quiz (`quiz`), loại sự kiện "đến hạn/đóng". Các sự kiện khác (diễn đàn, ngày mở bài...) bị bỏ qua.

## Cấu trúc

```
.
├── bote.py
├── requirements.txt
├── .gitignore
├── state.json                       # tự tạo sau lần chạy đầu
└── .github/workflows/deadline.yml
```

## Biến môi trường (Repository secrets)

| Biến | Ý nghĩa |
|---|---|
| `ELEARNING_ID` | MSSV |
| `ELEARNING_PASSWORD` | Mật khẩu |
| `TELEGRAM_TOKEN` | Token bot Telegram (từ @BotFather) |
| `TELEGRAM_CHAT_ID` | ID chat nhận tin |

## Chạy thử trên máy (Windows)

```powershell
pip install -r requirements.txt
playwright install chromium

$env:ELEARNING_ID='MSSV'
$env:ELEARNING_PASSWORD='mat_khau'
$env:DRY_RUN='1'

py bote.py
```

Với `DRY_RUN=1`, bot chỉ in tin nhắn ra màn hình, không gửi Telegram và không ghi `state.json`. Bỏ `DRY_RUN` và đặt thêm `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` để gửi thật.

## Chạy tự động

### 1. GitHub Actions

Đẩy code lên repo Private, thêm 4 secret ở **Settings → Secrets and variables → Actions**. Workflow `deadline.yml` chỉ có trigger `workflow_dispatch`, bạn có thể bấm **Run workflow** trong tab Actions để chạy thử.

### 2. cron-job.org (mỗi 1 giờ)

Tạo token GitHub (fine-grained) có quyền **Actions: Read and write** trên repo này, hoặc thêm repo này vào token đang dùng cho hai bot lịch học. Sau đó tạo cronjob:

| Mục | Giá trị |
|---|---|
| URL | `https://api.github.com/repos/<user>/<repo>/actions/workflows/deadline.yml/dispatches` |
| Lịch | Mỗi 1 giờ (`0 * * * *`) |
| Time zone | `Asia/Ho_Chi_Minh` |
| Method | `POST` |
| Body | `{"ref":"main"}` |

Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`.

Test run đúng là **HTTP 204**.

## Xử lý lỗi

| Hiện tượng | Cách xử lý |
|---|---|
| `Thiếu hoặc rỗng biến môi trường` | Kiểm tra lại 4 secret (tên, loại *Repository secrets*) |
| `Đăng nhập thất bại` | Sai MSSV/mật khẩu hoặc trang đăng nhập đổi cách hoạt động. Xem `debug_elearning.png` |
| `AJAX ... lỗi` / `HTTP ...` | Moodle đổi API hoặc phiên đăng nhập hết hạn |
| Không nhận tin dù có deadline | Xem log của bước `python bote.py`, kiểm tra dòng `Tổng ... sự kiện` |
| Nhắc cả bài đã làm | Trang bài đổi giao diện, tin sẽ có dòng "Chưa xác định được trạng thái" |

Bot báo lỗi về Telegram tối đa 6 giờ một lần để không spam.

## Bảo mật

- Giữ repo ở chế độ **Private** (log Actions có tên môn và bài của bạn).
- Không commit mật khẩu, token hay ảnh debug.
- Nếu lỡ lộ, hãy đổi mật khẩu và tạo lại token.

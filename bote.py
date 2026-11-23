import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# --- THÔNG TIN LẤY TỪ BIẾN MÔI TRƯỜNG (không ghi trực tiếp vào code) ---
ELEARNING_ID = os.environ.get("ELEARNING_ID", "")
ELEARNING_PASSWORD = os.environ.get("ELEARNING_PASSWORD", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # chạy thử: chỉ in ra, không gửi Telegram, không ghi state

BASE = "https://elearning.tdtu.edu.vn"
CALENDAR_URL = f"{BASE}/calendar/view.php?view=month"
VN_TZ = timezone(timedelta(hours=7))
WEEKDAYS = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]

# Các mốc nhắc (đơn vị: giờ còn lại): 7 ngày, 3 ngày, 1 ngày, 3 giờ, 1 giờ
MILESTONES_H = [168, 72, 24, 3, 1]
# Chỉ theo dõi bài tập (assign) và bài kiểm tra (quiz), loại sự kiện "đến hạn" / "đóng"
MONITORED_MODULES = {"assign", "quiz"}
MONITORED_EVENT_TYPES = {"due", "close"}
KIND_LABEL = {"assign": "Bài tập", "quiz": "Bài kiểm tra"}

STATE_FILE = Path("state.json")
ERROR_COOLDOWN_S = 6 * 3600  # báo lỗi về Telegram tối đa 6 giờ một lần

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Gọi web service của Moodle ngay trong trang (đã đăng nhập, dùng sesskey của phiên)
JS_FETCH = """async ({sesskey, method, args}) => {
    const url = '/lib/ajax/service.php?sesskey=' + encodeURIComponent(sesskey) + '&info=' + method;
    const res = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify([{index: 0, methodname: method, args: args}]),
    });
    return {status: res.status, text: await res.text()};
}"""


# ----------------------------------------------------------------- Telegram
def send_telegram(message):
    """Trả về True nếu gửi thành công."""
    if DRY_RUN:
        print("----- [DRY_RUN] tin nhắn sẽ gửi -----")
        print(message)
        print("-------------------------------------")
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code != 200:
            print(f"⚠️ Lỗi Telegram API ({res.status_code}): {res.text}")
            return False
        return True
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")
        return False


# --------------------------------------------------------------------- State
def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, ValueError):
        state = {}
    if not isinstance(state.get("events"), dict):
        state["events"] = {}
    return state


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------- Logic nhắc
def milestone_reached(hours_left):
    """Mốc nhỏ nhất đã chạm (đơn vị giờ), hoặc None nếu còn hơn 7 ngày."""
    reached = [m for m in MILESTONES_H if hours_left <= m]
    return min(reached) if reached else None


def plan_reminders(events, state_events, now, check_fn):
    """
    Quyết định deadline nào cần nhắc lần này.
    - Chỉ nhắc khi vừa chạm một mốc mới nhỏ hơn mốc đã nhắc trước đó.
    - Chỉ mở trang bài để kiểm tra "đã làm chưa" khi sắp nhắc.
    - Bài đã làm xong được ghi nhớ (done) và không kiểm tra lại.
    Trả về (danh sách cần nhắc, state mới chỉ ghi sau khi gửi Telegram thành công).
    """
    reminders, pending = [], {}
    horizon = now + MILESTONES_H[0] * 3600
    for ev_id, ev in events.items():
        if ev["module"] not in MONITORED_MODULES or ev["etype"] not in MONITORED_EVENT_TYPES:
            continue
        ts = ev["ts"]
        if ts <= now or ts > horizon:
            continue
        milestone = milestone_reached((ts - now) / 3600)
        if milestone is None:
            continue
        entry = state_events.get(ev_id)
        if entry and entry.get("ts") != ts:  # hạn nộp bị đổi -> nhắc lại từ đầu
            entry = None
        if entry and entry.get("done"):
            continue
        if entry and milestone >= entry.get("m", 10**9):
            continue
        done = check_fn(ev)
        if done is True:
            state_events[ev_id] = {"ts": ts, "m": milestone, "done": True}
            continue
        reminders.append({"ev": ev, "left": ts - now, "milestone": milestone, "unknown": done is None})
        pending[ev_id] = {"ts": ts, "m": milestone}
    reminders.sort(key=lambda r: r["ev"]["ts"])
    return reminders, pending


def fmt_left(seconds):
    minutes = max(int(seconds // 60), 0)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} ngày {hours} giờ"
    if hours:
        return f"{hours} giờ {mins} phút"
    return f"{mins} phút"


def fmt_deadline(ts):
    dt = datetime.fromtimestamp(ts, VN_TZ)
    return f"{dt:%H:%M} {WEEKDAYS[dt.weekday()]}, {dt:%d/%m/%Y}"


def clean_name(name):
    name = html.unescape(name or "").strip()
    return re.sub(r"\s+(is due|closes|đến hạn|đóng)$", "", name, flags=re.I).strip() or "Bài không tên"


def urgency_icon(left_seconds):
    hours = left_seconds / 3600
    if hours <= 3:
        return "🔴"
    if hours <= 24:
        return "🟠"
    if hours <= 72:
        return "🟡"
    return "🟢"


def build_messages(reminders, max_len=3500):
    header = f"⏰ <b>NHẮC DEADLINE ELEARNING</b> ({len(reminders)} bài chưa làm)\n\n"
    blocks = []
    for r in reminders:
        ev = r["ev"]
        lines = [
            f"{urgency_icon(r['left'])} <b>{html.escape(clean_name(ev['name']))}</b> "
            f"({KIND_LABEL.get(ev['module'], ev['module'])})"
        ]
        if ev["course"]:
            lines.append(f"📚 {html.escape(html.unescape(ev['course']))}")
        lines.append(f"🕐 Hạn: {fmt_deadline(ev['ts'])}")
        lines.append(f"⏳ Còn {fmt_left(r['left'])}")
        if r["unknown"]:
            lines.append("❓ Chưa xác định được trạng thái nộp bài")
        if ev["url"]:
            lines.append(f"🔗 <a href=\"{html.escape(ev['url'], quote=True)}\">Mở bài</a>")
        blocks.append("\n".join(lines))

    messages, current = [], header
    for block in blocks:
        if len(current) + len(block) + 2 > max_len and current != header:
            messages.append(current.rstrip())
            current = ""
        current += block + "\n\n"
    if current.strip():
        messages.append(current.rstrip())
    return messages


# ------------------------------------------------------------------ Trình duyệt
def settle(page, timeout=15000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def has_password_field(page):
    return page.locator("input[type='password']:visible").count() > 0


def is_logged_in(page):
    try:
        return bool(page.evaluate(
            "() => !!(window.M && M.cfg && M.cfg.sesskey) && !document.body.classList.contains('notloggedin')"
        ))
    except Exception:
        return False


def login(page):
    print("1. Đang mở trang elearning...")
    page.goto(CALENDAR_URL, timeout=45000, wait_until="domcontentloaded")
    settle(page)

    if has_password_field(page):
        print("2. Đang đăng nhập...")
        user = page.locator(
            "input[type='text']:visible, input[type='email']:visible, input:not([type]):visible"
        ).first
        pw = page.locator("input[type='password']:visible").first
        user.click()
        user.fill(ELEARNING_ID)
        pw.click()
        pw.fill(ELEARNING_PASSWORD)
        pw.press("Enter")
        settle(page, 20000)
        page.wait_for_timeout(2000)
        if has_password_field(page):
            raise RuntimeError("Đăng nhập thất bại: kiểm tra lại ELEARNING_ID/ELEARNING_PASSWORD")

    if "/calendar/view.php" not in page.url or not is_logged_in(page):
        page.goto(CALENDAR_URL, timeout=45000, wait_until="domcontentloaded")
        settle(page)
    if not is_logged_in(page):
        raise RuntimeError(f"Không vào được trang Calendar sau khi đăng nhập (đang ở {page.url})")
    print(f"   Đăng nhập OK, đang ở: {page.url}")


def call_ajax(page, sesskey, method, args):
    res = page.evaluate(JS_FETCH, {"sesskey": sesskey, "method": method, "args": args})
    if res["status"] != 200:
        raise RuntimeError(f"AJAX {method} trả về HTTP {res['status']}")
    try:
        data = json.loads(res["text"])
    except ValueError:
        raise RuntimeError(f"AJAX {method} trả về dữ liệu không phải JSON (có thể bị đăng xuất)")
    item = data[0] if isinstance(data, list) and data else data
    if isinstance(item, dict) and item.get("error"):
        message = (item.get("exception") or {}).get("message") if isinstance(item.get("error"), bool) else item.get("error")
        raise RuntimeError(f"AJAX {method} lỗi: {message or str(item)[:200]}")
    return item["data"]


def extract_events(month_data):
    events = {}
    for week in month_data.get("weeks", []):
        for day in week.get("days", []):
            for ev in day.get("events", []):
                ev_id = str(ev.get("id"))
                if ev_id in events:
                    continue
                url = ev.get("url") or ev.get("viewurl") or ""
                module = ev.get("modulename") or ""
                if not module:
                    m = re.search(r"/mod/([a-z_]+)/", url)
                    module = m.group(1) if m else ""
                events[ev_id] = {
                    "name": ev.get("name") or "",
                    "ts": int(ev.get("timestart") or 0),
                    "module": module,
                    "etype": ev.get("eventtype") or "",
                    "url": url,
                    "course": (ev.get("course") or {}).get("fullname") or "",
                }
    return events


def check_done(page, ev):
    """True = đã làm xong, False = chưa làm, None = không xác định được."""
    try:
        page.goto(ev["url"], timeout=30000, wait_until="domcontentloaded")
        settle(page, 10000)
        if has_password_field(page):
            return None
        if ev["module"] == "assign":
            if page.locator("body#page-mod-assign-view").count() == 0:
                return None
            text = page.inner_text("body").lower()
            return any(k in text for k in ("submitted for grading", "đã nộp để chấm"))
        if ev["module"] == "quiz":
            if page.locator("body#page-mod-quiz-view").count() == 0:
                return None
            table = page.locator("table.quizattemptsummary")
            if table.count() == 0:
                return False  # chưa có lần làm bài nào
            text = table.first.inner_text().lower()
            return any(k in text for k in ("finished", "đã hoàn thành", "hoàn thành"))
    except Exception as e:
        print(f"   Không kiểm tra được trạng thái '{clean_name(ev['name'])}': {e}")
    return None


def run(state):
    now = int(time.time())
    local = datetime.fromtimestamp(now, VN_TZ)
    next_month = (local.replace(day=1) + timedelta(days=32)).replace(day=1)
    months = [(local.year, local.month), (next_month.year, next_month.month)]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=USER_AGENT)
            page = context.new_page()
            try:
                login(page)
                sesskey = page.evaluate("() => M.cfg.sesskey")
                course_id = page.evaluate("() => (M.cfg.courseId) || 1")

                print("3. Đang lấy sự kiện trong Calendar...")
                events = {}
                for year, month in months:
                    data = call_ajax(page, sesskey, "core_calendar_get_calendar_monthly_view", {
                        "year": year, "month": month, "courseid": course_id,
                        "categoryid": 0, "includenavigation": False, "mini": False, "day": 1,
                    })
                    found = extract_events(data)
                    print(f"   Tháng {month}/{year}: {len(found)} sự kiện")
                    events.update(found)

                watched = [e for e in events.values()
                           if e["module"] in MONITORED_MODULES and e["etype"] in MONITORED_EVENT_TYPES]
                print(f"   Tổng {len(events)} sự kiện, {len(watched)} là bài tập/bài kiểm tra có hạn")
                if not events:
                    print("   ⚠️ Không có sự kiện nào, có thể API đổi hoặc Calendar trống")

                print("4. Kiểm tra deadline sắp tới...")
                reminders, pending = plan_reminders(
                    events, state["events"], now, lambda ev: check_done(page, ev)
                )
                for r in reminders:
                    print(f"   + {clean_name(r['ev']['name'])}: còn {fmt_left(r['left'])} (mốc {r['milestone']}h)")

                if reminders:
                    sent = [send_telegram(m) for m in build_messages(reminders)]
                    if all(sent):
                        state["events"].update(pending)
                        print(f"Đã gửi nhắc {len(reminders)} deadline về Telegram.")
                    else:
                        raise RuntimeError("Gửi Telegram thất bại, sẽ thử lại ở lần chạy sau")
                else:
                    print("Không có deadline nào cần nhắc lúc này, không gửi tin.")
            except Exception:
                try:
                    page.screenshot(path="debug_elearning.png", full_page=True)
                except Exception:
                    pass
                raise
        finally:
            browser.close()

    state["events"] = {k: v for k, v in state["events"].items() if v.get("ts", 0) > now - 2 * 86400}
    state.pop("last_error", None)


def main():
    required = {"ELEARNING_ID": ELEARNING_ID, "ELEARNING_PASSWORD": ELEARNING_PASSWORD}
    if not DRY_RUN:
        required.update({"TELEGRAM_TOKEN": TELEGRAM_TOKEN, "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID})
    missing = [name for name, value in required.items() if not value]
    if missing:
        print(f"❌ Thiếu hoặc rỗng biến môi trường: {', '.join(missing)}")
        sys.exit(1)

    state = load_state()
    exit_code = 0
    try:
        run(state)
    except Exception as e:
        print(f"Lỗi: {e}")
        now = int(time.time())
        if now - state.get("last_error", 0) > ERROR_COOLDOWN_S:
            send_telegram(f"⚠️ <b>Bot deadline elearning lỗi</b>: <code>{html.escape(str(e)[:500])}</code>")
            state["last_error"] = now
        exit_code = 1
    finally:
        save_state(state)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

"""TableCheck 空席チェック → LINE通知(通知のみ・予約はしない)

環境変数:
  SHOP_URL      監視する店の予約ページURL(未設定なら entraide-kagurazaka)
  DAYS_AHEAD    今日から何日先まで監視するか(既定 30)
  TARGET_DATES  特定の日付だけ監視する場合(カンマ区切り, YYYY-MM-DD)。設定時はこちらが優先
  PAX           人数(カンマ区切り可, 既定 2)  例: 2,4
  LINE_CHANNEL_TOKEN / LINE_USER_ID  LINE Messaging API
  DEBUG=1       画面・HTML・通信ログを debug/ に保存
  TEST_LINE=1   LINEテスト通知だけ送って終了
"""
import json
import os
import pathlib
import re
import sys
from datetime import date as Date, datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

SHOP_URL = os.environ.get("SHOP_URL") or "https://www.tablecheck.com/ja/shops/entraide-kagurazaka/reserve"
_m = re.search(r"/shops/([^/?#]+)", SHOP_URL)
SHOP_SLUG = _m.group(1) if _m else "shop"
PAX_LIST = [x.strip() for x in (os.environ.get("PAX") or "2").split(",") if x.strip()]
TARGET_DATES = [d.strip() for d in (os.environ.get("TARGET_DATES") or "").split(",") if d.strip()]
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD") or "30")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
DEBUG = os.environ.get("DEBUG") == "1"
TEST_LINE = os.environ.get("TEST_LINE") == "1"

STATE_FILE = pathlib.Path("state/last.json")
DEBUG_DIR = pathlib.Path("debug")

sys.stdout.reconfigure(line_buffering=True)  # ログを即時表示


def line_push(text: str) -> None:
    if not (LINE_TOKEN and LINE_USER_ID):
        print("[LINE未設定] " + text)
        return
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text[:4900]}]},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"LINE送信失敗 {r.status_code}: {r.text}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def dump(page, name: str) -> None:
    if not DEBUG:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    (DEBUG_DIR / f"{name}.html").write_text(page.content())


def select_by_placeholder(page, placeholder: str):
    return page.locator(f"select:has(option:has-text('{placeholder}'))").first


def options_of(sel) -> list:
    return [(o.get_attribute("value") or "", o.inner_text().strip()) for o in sel.locator("option").all()]


def pick(sel, want: str, what: str) -> None:
    """value一致、または表示名が want で始まる選択肢を選ぶ(例: 2 → 「2名」)"""
    sel.locator("option").nth(1).wait_for(state="attached", timeout=15000)
    opts = options_of(sel)
    for v, t in opts:
        if v == want or t == want or re.match(rf"^{re.escape(want)}(\D|$)", t):
            sel.select_option(value=v)
            return
    raise RuntimeError(f"{what}の選択肢に {want} がありません: {opts}")


def set_date(page, date: str) -> None:
    """日付入力欄に値を入れて change を発火させる(要: 初回DEBUGで確認)"""
    el = page.locator("input[name*='date']").first.element_handle()
    page.evaluate(
        """([el, v]) => { el.value = v;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true})); }""",
        [el, date],
    )
    page.wait_for_timeout(1500)


def jp_date(date: str) -> str:
    y, m, d = (int(x) for x in date.split("-"))
    return f"{y}年{m}月{d}日"


def read_availability(page) -> tuple:
    """空席表示の枠(#availability)の読み込み完了を待ち、(class, 文言)を返す"""
    page.locator("#availability-loader.hidden").wait_for(state="attached", timeout=10000)
    tam = page.locator("#availability-tam")
    tam.filter(has_text=re.compile(r"\S")).wait_for(state="visible", timeout=10000)
    cls = page.locator("#availability").get_attribute("class") or ""
    return cls, tam.inner_text().strip()


def slot_status(page, date: str, label: str) -> str:
    """選んだ日時が available / full / unknown かを判定する
    満席: 「(日付)(時刻)には○名様用の空席がありません」など赤い表示
    空き: 満席表示が出ず、「コース・プランを選択してください」等が出る
    """
    this_slot = re.compile(rf"{re.escape(jp_date(date))}.*{re.escape(label)}")
    try:
        page.wait_for_timeout(1000)
        cls, text = read_availability(page)
        # 直前の時間帯の満席表示が残っている場合は、表示の切り替わりを待つ
        if "空席がありません" in text and not this_slot.search(text):
            page.wait_for_timeout(2000)
            cls, text = read_availability(page)
        if "空席がありません" in text or "tam-danger" in cls:
            return "full" if this_slot.search(text) or "空席がありません" not in text else "unknown"
        # 空きと判定する前に、表示が安定しているかもう一度確認
        page.wait_for_timeout(1500)
        cls2, text2 = read_availability(page)
        if "空席がありません" in text2 or "tam-danger" in cls2:
            return "full"
    except Exception:
        print(f"  判定不能 {date} {label}: 空席表示が読み込まれませんでした")
        return "unknown"
    print(f"  空き {date} {label}: [{cls2}] {text2}")
    return "available"


def parse_date(d: str) -> Date:
    y, m, dd = (int(x) for x in d.split("-"))
    return Date(y, m, dd)


def choose(page, date: str, pax: str) -> None:
    set_date(page, date)
    pick(page.locator("#reservation_num_people_adult"), pax, "人数")
    page.wait_for_timeout(2500)


def week_open_cells(page) -> tuple:
    """予約状況表(1週間分)で ✕ でも - でもないセルを数える"""
    days = [t.strip() for t in page.locator("#timetable-body .date-num").all_inner_texts()]
    open_cells = []
    for c in page.locator("tr.timetable-row td").all():
        cls = c.get_attribute("class") or ""
        if "not_available" in cls or "closed" in cls:
            continue
        open_cells.append(cls or c.inner_text().strip())
    return days, open_cells


def resolve_days(days: list, center: Date) -> list:
    """表の見出しの「日」の数字を、centerに一番近い実際の日付に直す"""
    out = []
    for n in days:
        if not n.isdigit():
            continue
        cands = [center + timedelta(days=k) for k in range(-10, 11)]
        cands = [c for c in cands if c.day == int(n)]
        if cands:
            out.append(min(cands, key=lambda c: abs((c - center).days)))
    return out


def check_slots(page, date: str, pax: str) -> list:
    """1日分の時間帯を1つずつ選んで空きを確認する"""
    choose(page, date, pax)
    time_sel = select_by_placeholder(page, "-- 時間 --")
    available = []
    for value, label in [(v, t) for v, t in options_of(time_sel) if v]:
        time_sel.select_option(value)
        if slot_status(page, date, label) == "available":
            available.append(label)
        page.wait_for_timeout(1500)
    dump(page, f"after_{date}_{pax}")
    return available


def open_page(page) -> None:
    page.goto(SHOP_URL, wait_until="networkidle")
    dump(page, "loaded")
    agree = page.get_by_label("「お店からのお知らせ」を読み、内容を理解して同意する").first
    if agree.count() and not agree.is_checked():
        agree.check()


def main() -> int:
    if TEST_LINE:
        if not (LINE_TOKEN and LINE_USER_ID):
            print("LINE_CHANNEL_TOKEN / LINE_USER_ID が読み込めていません(Secretsを確認)")
            return 1
        try:
            line_push(f"✅ テスト通知です({SHOP_SLUG})。空席通知の設定は正常です。\n" + SHOP_URL)
        except Exception as e:
            print(f"テスト通知に失敗しました: {e}")
            return 1
        print("テスト通知を送信しました")
        return 0

    today = datetime.now(timezone(timedelta(hours=9))).date()
    if TARGET_DATES:
        targets = sorted({parse_date(d) for d in TARGET_DATES})
    else:
        targets = [today + timedelta(days=i) for i in range(DAYS_AHEAD + 1)]
    targets = [t for t in targets if t >= today]
    if not targets:
        print("監視対象の日付がありません")
        return 1
    state, new_state, hits = load_state(), {}, []
    xhr_log = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ja-JP")
        if DEBUG:
            page.on("response", lambda r: xhr_log.append(
                {"url": r.url, "status": r.status, "type": r.headers.get("content-type", "")}
            ) if r.request.resource_type in ("xhr", "fetch") else None)
        try:
            open_page(page)
            for pax in PAX_LIST:
                cursor, end, done = targets[0], targets[-1], set()
                while cursor <= end:
                    # 表は選んだ日を中心に7日分出るので、cursorから始まる7日を狙う
                    center = cursor + timedelta(days=3)
                    choose(page, center.isoformat(), pax)
                    days, open_cells = week_open_cells(page)
                    shown = resolve_days(days, center)
                    dump(page, f"week_{center}_{pax}")
                    if not shown:
                        raise RuntimeError(f"予約状況表の日付を読めませんでした: {days}")
                    print(f"{pax}名 {shown[0]}〜{shown[-1]}: 空きらしきセル {len(open_cells)} {open_cells[:5]}")
                    if center not in shown:
                        print(f"  注意: {center} が表に出ません(受付期間外の可能性)")
                    for t in targets:
                        if not (shown[0] <= t <= shown[-1]) or t in done:
                            continue
                        done.add(t)
                        key = f"{SHOP_SLUG}_{t.isoformat()}_{pax}"
                        times = check_slots(page, t.isoformat(), pax) if open_cells else []
                        new_state[key] = times
                        if open_cells:
                            print(f"  {t} {pax}名: 空き {times or 'なし'}")
                        fresh = sorted(set(times) - set(state.get(key, [])))
                        if fresh:
                            hits.append(f"{t} {pax}名: {', '.join(fresh)}")
                    cursor = max(shown[-1], cursor) + timedelta(days=1)
                missed = [str(t) for t in targets if t not in done]
                if missed:
                    print(f"  {pax}名: 表で確認できなかった日 {missed}")
        except Exception as e:
            dump(page, "error")
            print(f"チェック失敗: {e}")
            return 1
        finally:
            if DEBUG:
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / "xhr.json").write_text(json.dumps(xhr_log, ensure_ascii=False, indent=1))
            browser.close()

    if hits:
        line_push(f"🍽 {SHOP_SLUG} に空きが出ました\n" + "\n".join(hits) + "\n" + SHOP_URL)
    save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

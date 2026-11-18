"""
嵯峨野トロッコ 11/25「嵯峨駅／嵐山駅 → 亀岡駅」の空き監視スクリプト(Triplabo)

使い方:
  python monitor_saga.py --debug --headed   # 動作確認(1回だけ。ブラウザを表示)
  python monitor_saga.py --test-notify      # LINE通知のテスト

※このサイトの画面は事前に確認できていないため、プラン選択の部分は推測で書いています。
  --debug で動かすと debug_saga.png / debug_saga.html が保存されるので、うまく動かなければ送ってください。

通知: LINE_CHANNEL_TOKEN と LINE_USER_ID を環境変数に設定してください。
"""
import argparse
import os
import random
import re
import time
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ===== 設定 =====
URL = "https://ars-saganokanko.triplabo.jp/activity/ja/LINKTIVITY-YRBTL"
TARGET_YEAR, TARGET_MONTH, TARGET_DAY = 2026, 11, 25
LABEL = f"嵯峨野トロッコ {TARGET_MONTH}/{TARGET_DAY} 嵯峨→亀岡"
# 選びたいプラン名にマッチする正規表現(全角スラッシュ/矢印の表記ゆれに対応)
PLAN_PATTERN = re.compile(r"嵯峨駅.{0,6}嵐山駅.{0,12}亀岡駅", re.S)
INTERVAL_SEC = 300
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
LINE_TOKEN = os.getenv("LINE_CHANNEL_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

HEADER_SEL = "p[class*='dateTableCurrent']"
NEXT_SEL = "button[class*='dateTableNext']:not([class*='hidden'])"
DAY_BTN_SEL = "button[class*='dateTableDayButton']"


def notify(title: str, msg: str):
    print(f"[NOTIFY] {title}: {msg}")
    if LINE_TOKEN and LINE_USER_ID:
        try:
            r = requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {LINE_TOKEN}"},
                json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": f"{title}\n{msg}\n{URL}"}]},
                timeout=10,
            )
            if r.status_code != 200:
                print("LINE失敗:", r.status_code, r.text[:200])
        except Exception as e:
            print("LINE失敗:", e)
    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=msg.encode("utf-8"),
                headers={"Title": title.encode("utf-8"), "Priority": "urgent", "Click": URL},
                timeout=10,
            )
        except Exception as e:
            print("ntfy失敗:", e)
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": f"**{title}**\n{msg}\n{URL}"}, timeout=10)
        except Exception as e:
            print("discord失敗:", e)


def dump(page, note=""):
    """画面の状態を保存(デバッグ用)"""
    page.screenshot(path="debug_saga.png", full_page=True)
    open("debug_saga.html", "w", encoding="utf-8").write(page.content())
    print(f"[debug] 画面を保存しました {note}")


def looks_disabled(btn):
    """ボタンが押せない(グレー)状態か。disabled属性・aria-disabled・クラス名で判定。"""
    if btn.is_disabled():
        return True
    return btn.evaluate(
        "el => el.getAttribute('aria-disabled') === 'true' "
        "|| /(^|[\\s_-])disabled([\\s_-]|$)/i.test(el.className || '')"
    )


def find_month(page):
    pat = re.compile(rf"{TARGET_MONTH}月\s*{TARGET_YEAR}")
    for _ in range(6):
        header = page.locator(HEADER_SEL).filter(has_text=pat)
        if header.count():
            return header.first.locator("xpath=../..")
        nxt = page.locator(NEXT_SEL)
        if not nxt.count():
            return None
        nxt.last.click(timeout=3000)
        page.wait_for_timeout(800)
    return None


class DirectionCheckFailed(Exception):
    """日付は押せる(○/△)のに、進行方向ボタンの確認に失敗したときの例外。"""

    def __init__(self, day_detail, reason):
        super().__init__(reason)
        self.day_detail = day_detail
        self.reason = reason


def check_once(page, debug=False):
    """1回分の確認。戻り値: (status, detail)。日付は押せるのに方向が確認できなければ DirectionCheckFailed。"""
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector(HEADER_SEL, timeout=30000)
    except PWTimeout:
        if debug:
            dump(page, "(カレンダーが出ません)")
        raise RuntimeError("カレンダーが表示されません")
    page.wait_for_timeout(1500)  # 日付ごとの空き状況が反映されるのを待つ

    month = find_month(page)
    if month is None:
        if debug:
            dump(page, "(対象月なし)")
        raise RuntimeError(f"{TARGET_YEAR}年{TARGET_MONTH}月のカレンダーが見つかりません")

    day_btn = None
    buttons = month.locator(DAY_BTN_SEL)
    for i in range(buttons.count()):
        b = buttons.nth(i)
        if b.locator("span").first.inner_text().strip() == str(TARGET_DAY):
            day_btn = b
            break
    if day_btn is None:
        raise RuntimeError(f"{TARGET_DAY}日のボタンが見つかりません")

    disabled = day_btn.is_disabled()
    icon_cls = day_btn.locator("svg").first.get_attribute("class") or ""
    mark = "×(満席)" if "sold-out" in icon_cls else ("△(残りわずか)" if "few" in icon_cls else "○(空きあり)")
    day_detail = f"日付: disabled={disabled}, mark={mark}"
    if debug:
        print(day_detail)

    # 日付自体が満席なら、どの方向も空いていない
    if disabled or "sold-out" in icon_cls:
        if debug:
            dump(page)
        return "unavailable", day_detail

    # 日付を選ぶと、進行方向ボタンが出て、有効/無効が切り替わる
    try:
        day_btn.click(timeout=5000)
        plan_btn = page.get_by_role("button", name=PLAN_PATTERN)
        plan_btn.first.wait_for(state="visible", timeout=15000)  # 固定の秒数ではなく、出るまで待つ
        page.wait_for_timeout(1500)  # 押せる/押せないの状態が確定するのを待つ
        plan = plan_btn.first
        plan_disabled = looks_disabled(plan)
    except Exception as e:
        if debug:
            dump(page, "(進行方向ボタンの確認に失敗)")
        raise DirectionCheckFailed(day_detail, f"{type(e).__name__}: {str(e)[:120]}")

    detail = f"{day_detail} / 嵯峨・嵐山→亀岡ボタン: {'押せない(満席)' if plan_disabled else '押せる(空きあり)'}"
    if debug:
        dump(page)
        print(detail)
        print("ボタンのHTML:", plan.evaluate("el => el.outerHTML")[:400])
        reverse = page.get_by_role("button", name=re.compile(r"亀岡駅.{0,6}嵯峨駅"))
        if reverse.count():
            print("(参考)逆方向のボタンのHTML:", reverse.first.evaluate("el => el.outerHTML")[:400])
    return ("unavailable" if plan_disabled else "available"), detail


def check(page, debug=False):
    """戻り値: (status, detail)  status = 'available' | 'unavailable' | 'error'

    一時的な失敗に備えて、最大3回やり直す。
    それでも「日付は押せる(○/△)のに方向が確認できない」場合は、見逃さないよう
    『空きの可能性あり(要確認)』として通知する。
    """
    last_error = ""
    direction_failed = None
    for attempt in range(1, 4):
        try:
            return check_once(page, debug=debug)
        except DirectionCheckFailed as e:
            direction_failed = e
            last_error = e.reason
            print(f"[retry {attempt}/3] 進行方向の確認に失敗: {e.reason}")
        except Exception as e:
            direction_failed = None
            last_error = str(e)[:150]
            print(f"[retry {attempt}/3] 失敗: {last_error}")
        page.wait_for_timeout(3000)

    if direction_failed is not None:
        # 日付が○/△なのに方向を確認できなかった → 空きを見逃さないため通知する
        return "available", f"(要確認)進行方向の確認に失敗しましたが、{direction_failed.day_detail}。予約ページを確認してください"
    return "error", last_error


# ===== 空きが出たとき: 時間・座席の取得 =====
# 予約画面(座席選択)は別のアドレスにあり、ログインなしで直接開ける(動作確認済み)。
# 大人1名(unitsCount=1)、嵯峨発 → 亀岡着、で空いているすべての列車と座席を調べる。
SEAT_URL = (f"https://file.sagano.linktivity.io/seat/51/down?lang=ja"
            f"&date={TARGET_YEAR}-{TARGET_MONTH:02d}-{TARGET_DAY:02d}&unitsCount=1")
BOARDING_STATIONS = ["トロッコ嵯峨"]                  # 乗車駅(嵯峨発のみ。嵐山発も見たければ "トロッコ嵐山" を足す)
ALIGHT_STATION = "トロッコ亀岡"                       # 降車駅
MAX_TRAINS = 12                                       # 座席まで調べる列車の最大数(1日10本ほどなので、全部入る)
MAX_MESSAGE_CHARS = 3500                              # LINE1通の上限(5000字)に収まるようにする
COMBO_SEL = "div[role='combobox'][aria-haspopup='listbox']"
TRAIN_SEL = "button[class*='_train_']"

# 号車ごとに座席画面を開き、押せる(空いている)座席を「行+席」(例: 1C)で集める
READ_CARS_JS = r"""
async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const heads = () => [...document.querySelectorAll("button, div[role='button'], summary")]
      .filter(e => /^\s*[1-9]\d*号車/.test(e.innerText));
  const out = {};
  const total = heads().length;
  for (let c = 1; c <= total; c++) {
    const h = heads().find(e => e.innerText.trim().startsWith(c + '号車'));
    if (!h) continue;
    h.click();
    await sleep(1200);
    const free = [];
    for (const b of document.querySelectorAll('button')) {
      if (!/^[A-D]$/.test(b.innerText.trim()) || b.disabled) continue;
      let n = b.parentElement, hops = 0;
      while (n && !/^\s*\d+\s/.test(n.innerText) && hops < 6) { n = n.parentElement; hops++; }
      const row = n ? n.innerText.trim().split(/\s+/)[0] : '?';
      free.push(row + b.innerText.trim());
    }
    out[c] = free;
  }
  return out;
}
"""


def pick_station(page, index, name):
    """乗車駅(index=0)/降車駅(index=1)のプルダウンから駅を選ぶ。"""
    page.locator(COMBO_SEL).nth(index).click(timeout=8000)
    page.get_by_role("option", name=name, exact=True).click(timeout=8000)
    page.wait_for_timeout(800)


def open_train_list(page, boarding):
    """座席画面を開き、乗車駅→降車駅を選んで、列車の一覧を出す。"""
    page.goto(SEAT_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(COMBO_SEL, timeout=30000)
    page.wait_for_timeout(1000)
    pick_station(page, 0, boarding)
    pick_station(page, 1, ALIGHT_STATION)
    page.wait_for_selector(TRAIN_SEL, timeout=20000)
    page.wait_for_timeout(1000)


def list_trains(page):
    """列車の一覧。[{index, dep, arr, name, free}]  (free=押せる=空席あり)"""
    trains = []
    btns = page.locator(TRAIN_SEL)
    for i in range(btns.count()):
        b = btns.nth(i)
        text = b.inner_text()
        times = re.findall(r"\d{1,2}:\d{2}", text)
        name = re.search(r"\S+号", text)
        trains.append({
            "index": i,
            "dep": times[0] if times else "?",
            "arr": times[1] if len(times) > 1 else "?",
            "name": name.group(0) if name else "?",
            "free": not b.is_disabled(),
        })
    return trains


def fetch_seat_details(ctx):
    """空いているすべての列車(発車時刻)と、号車ごとの空席番号を調べる。
    戻り値: (state, 文章)
      state = "free"    : 空きのある列車がある
              "none"    : 列車の一覧は確認できたが、空きのある列車はない
              "unknown" : 確認に失敗した(見逃さないよう、呼び出し側では「空きあり」として扱う)
    """
    lines = [f"【{TARGET_MONTH}/{TARGET_DAY} 大人1名・嵯峨発→亀岡着】"]
    found_free = False
    failed = False
    page = ctx.new_page()
    page.set_viewport_size({"width": 480, "height": 900})
    try:
        for boarding in BOARDING_STATIONS:
            try:
                open_train_list(page, boarding)
                trains = list_trains(page)
            except Exception as e:
                failed = True
                lines.append(f"{boarding}発: 列車の一覧の取得に失敗({type(e).__name__})")
                continue
            all_free = [t for t in trains if t["free"]]
            if not all_free:
                lines.append(f"{boarding}発: 空きのある列車はありません")
                continue
            found_free = True
            if len(BOARDING_STATIONS) > 1:
                lines.append(f"■{boarding}発")
            if len(all_free) > MAX_TRAINS:
                lines.append(f"(空きのある列車が{len(all_free)}本あります。先頭の{MAX_TRAINS}本を表示)")
            for t in all_free[:MAX_TRAINS]:
                head = f"■{t['dep']}発 {t['name']}(→{t['arr']}着)"
                try:
                    open_train_list(page, boarding)
                    page.locator(TRAIN_SEL).nth(t["index"]).click(timeout=8000)
                    page.get_by_role("button", name="次へ", exact=True).click(timeout=8000)
                    page.get_by_text("1号車").first.wait_for(timeout=20000)
                    cars = page.evaluate(READ_CARS_JS)
                except Exception as e:
                    lines.append(f"{head}: 座席の取得に失敗({type(e).__name__})")
                    continue
                total = sum(len(s) for s in cars.values())
                lines.append(f"{head} 空席{total}席")
                seat_lines = [f"  {car}号車: {' '.join(seats)}"
                              for car, seats in cars.items() if seats]
                lines.extend(seat_lines or ["  (空いている座席を確認できませんでした)"])
    finally:
        page.close()

    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "\n…(長いため以降は省略。予約ページで確認してください)"
    state = "free" if found_free else ("unknown" if failed else "none")
    return state, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat-info", action="store_true",
                    help="時間・座席の取得だけ試して表示(通知はしない。動作確認用)")
    ap.add_argument("--debug", action="store_true", help="1回だけ実行し、画面を保存")
    ap.add_argument("--headed", action="store_true", help="ブラウザを表示して動作確認")
    ap.add_argument("--test-notify", action="store_true", help="通知のテスト送信だけして終了")
    ap.add_argument("--once", action="store_true",
                    help="1回だけ確認して終了(GitHub Actions用)。空きあり=終了コード10、エラー=1")
    ap.add_argument("--send", nargs=2, metavar=("TITLE", "MSG"), help="指定の文章を通知して終了")
    args = ap.parse_args()

    if args.send:
        notify(args.send[0], args.send[1])
        return
    if args.test_notify:
        notify("通知テスト", f"{LABEL} の監視の通知テストです")
        return

    if args.seat_info:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(locale="ja-JP")
            state, text = fetch_seat_details(ctx)
            print(text)
            print(f"(判定: {state})")
            browser.close()
        return

    has_line = bool(LINE_TOKEN and LINE_USER_ID)
    if not (has_line or NTFY_TOPIC or DISCORD_WEBHOOK) and not args.debug:
        print("通知先(LINE_CHANNEL_TOKEN と LINE_USER_ID)を環境変数に設定してください。")
        return

    errors = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(locale="ja-JP", viewport={"width": 1200, "height": 900})
        page = ctx.new_page()
        while True:
            ts = datetime.now().strftime("%m/%d %H:%M:%S")
            try:
                status, detail = check(page, debug=args.debug)
            except PWTimeout:
                status, detail = "error", "タイムアウト"
                if args.debug:
                    dump(page, "(タイムアウト)")
            except Exception as e:
                status, detail = "error", str(e)[:200]
                if args.debug:
                    dump(page, "(例外)")
            seat_text = ""
            if status == "available":
                # 空きが出たら、何時の列車のどの座席が空いているかも調べる(失敗しても通知は送る)
                try:
                    seat_state, seat_text = fetch_seat_details(ctx)
                except Exception as e:
                    seat_state, seat_text = "unknown", f"(時間・座席の取得に失敗しました: {type(e).__name__})"
                if seat_state == "none":
                    # 日付・方向としては空きでも、嵯峨発に空きの列車がなければ通知しない
                    # (空きが嵐山発だけの場合など)
                    status = "unavailable"
                    detail = f"{detail} / ただし嵯峨発の空きなし"
            print(f"[{ts}] {status}  {detail}")
            if seat_text:
                print(seat_text)

            if status == "available":
                if args.once:
                    # GitHub Actions側で、回数を数えながら繰り返し通知する(文面は seat_info.txt 経由で渡す)
                    with open("seat_info.txt", "w", encoding="utf-8") as f:
                        f.write(seat_text)
                    browser.close()
                    raise SystemExit(10)
                notify(f"🎉 {LABEL} に空きが出ました",
                       f"今すぐ予約ページを開いてください!\n({detail})\n{seat_text}")
                break
            if args.once:
                browser.close()
                raise SystemExit(1 if status == "error" else 0)
            if status == "error":
                errors += 1
                if errors == 5:
                    notify("監視エラー", f"5回連続で失敗: {detail}")
            else:
                errors = 0

            if args.debug:
                break
            time.sleep(INTERVAL_SEC + random.randint(0, 60))
        browser.close()


if __name__ == "__main__":
    main()

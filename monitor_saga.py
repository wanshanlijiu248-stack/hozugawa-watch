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
LABEL = f"嵯峨野トロッコ {TARGET_MONTH}/{TARGET_DAY} 嵯峨・嵐山→亀岡"
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


def main():
    ap = argparse.ArgumentParser()
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
            print(f"[{ts}] {status}  {detail}")

            if status == "available":
                if args.once:
                    browser.close()
                    raise SystemExit(10)
                notify(f"🎉 {LABEL} に空きが出ました", f"今すぐ予約ページを開いてください! ({detail})")
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

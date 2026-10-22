"""
11/25 の空き監視スクリプト(Triplabo / 保津川下り)

使い方:
  python monitor.py --debug --headed   # 動作確認(1回だけ。ブラウザを表示)
  python monitor.py                    # 本番。空きが出るまで定期チェック

通知: ntfy.sh(スマホアプリ)と Discord Webhook に対応。どちらか片方でOK。
  LINE: 環境変数 LINE_CHANNEL_TOKEN と LINE_USER_ID を設定してください。
  (ほかに NTFY_TOPIC / DISCORD_WEBHOOK も使えます)

サイトの仕組み(確認済み):
  カレンダーは「10月 2026」「11月 2026」の2か月が並んで表示される。
  日付は button 要素。満席は disabled 属性 + ×アイコン、
  空きありは disabled なし + ○(多い)/△(残りわずか)アイコン。
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
URL = "https://ars-hozugawa.triplabo.jp/activity/ja/LINKTIVITY-9SUUZ"
TARGET_YEAR, TARGET_MONTH, TARGET_DAY = 2026, 11, 25
INTERVAL_SEC = 300          # チェック間隔(5分)。短くしすぎないこと
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")          # 例: hozugawa-1125-xxxx(推測されにくい名前に)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
LINE_TOKEN = os.getenv("LINE_CHANNEL_TOKEN", "")   # LINE Messaging API のチャネルアクセストークン
LINE_USER_ID = os.getenv("LINE_USER_ID", "")       # 自分のユーザーID(Uから始まる文字列)

HEADER_SEL = "p[class*='dateTableCurrent']"        # 「11月 2026」の見出し
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


def find_month(page):
    """対象月(11月 2026)のカレンダー部分を返す。表示されていなければ次月ボタンで進める。"""
    pat = re.compile(rf"{TARGET_MONTH}月\s*{TARGET_YEAR}")
    for _ in range(6):
        header = page.locator(HEADER_SEL).filter(has_text=pat)
        if header.count():
            return header.first.locator("xpath=../..")  # 見出しの親の親 = 月ごとのカレンダー
        nxt = page.locator(NEXT_SEL)
        if not nxt.count():
            return None
        nxt.last.click(timeout=3000)
        page.wait_for_timeout(800)
    return None


def check(page, debug=False):
    """戻り値: (status, detail)  status = 'available' | 'unavailable' | 'error'"""
    page.goto(URL, wait_until="networkidle", timeout=60000)
    page.wait_for_selector(HEADER_SEL, timeout=30000)

    month = find_month(page)
    if month is None:
        if debug:
            page.screenshot(path="debug_calendar.png", full_page=True)
        return "error", f"{TARGET_YEAR}年{TARGET_MONTH}月のカレンダーが見つかりません"

    buttons = month.locator(DAY_BTN_SEL)
    for i in range(buttons.count()):
        btn = buttons.nth(i)
        if btn.locator("span").first.inner_text().strip() == str(TARGET_DAY):
            disabled = btn.is_disabled()
            icon_cls = btn.locator("svg").first.get_attribute("class") or ""
            mark = "×(満席)" if "sold-out" in icon_cls else ("△(残りわずか)" if "few" in icon_cls else "○(空きあり)")
            detail = f"disabled={disabled}, mark={mark}"
            if debug:
                page.screenshot(path="debug_calendar.png", full_page=True)
                print("25日:", detail)
            # 押せない or 満席アイコンなら空きなし
            if disabled or "sold-out" in icon_cls:
                return "unavailable", detail
            return "available", detail
    return "error", f"{TARGET_DAY}日のボタンが見つかりません"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="1回だけ実行しスクショを保存")
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
        notify("通知テスト", "このメッセージが届けば通知設定はOKです")
        return

    has_line = bool(LINE_TOKEN and LINE_USER_ID)
    if not (has_line or NTFY_TOPIC or DISCORD_WEBHOOK) and not args.debug:
        print("通知先(LINE_CHANNEL_TOKEN と LINE_USER_ID、または NTFY_TOPIC、DISCORD_WEBHOOK)を環境変数に設定してください。")
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
            except Exception as e:
                status, detail = "error", str(e)[:200]
            print(f"[{ts}] {status}  {detail}")

            if status == "available":
                notify("🎉 11/25 に空きが出ました", f"今すぐ予約ページを開いてください! ({detail})")
                if args.once:
                    browser.close()
                    raise SystemExit(10)
                break
            if args.once:
                browser.close()
                raise SystemExit(1 if status == "error" else 0)
            if status == "error":
                errors += 1
                if errors == 5:  # 壊れたまま放置されないよう通知
                    notify("監視エラー", f"5回連続で失敗: {detail}")
            else:
                errors = 0

            if args.debug:
                break
            time.sleep(INTERVAL_SEC + random.randint(0, 60))
        browser.close()


if __name__ == "__main__":
    main()

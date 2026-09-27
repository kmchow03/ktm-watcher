#!/usr/bin/env python3
"""
KTM Shuttle Tebrau seat watcher
Checks KTMB's Shuttle Tebrau booking page for open seats (Woodlands CIQ <-> JB Sentral)
on the dates/times in config.toml and sends a Telegram alert when seats appear.

It only CHECKS. It never logs in or buys; you book yourself on the KTMB site / KITS app.

Usage:
  python ktm_watch.py                 # keep watching (Ctrl+C to stop)
  python ktm_watch.py --once          # check once and print results
  python ktm_watch.py --show-browser  # watch with the browser window visible
  python ktm_watch.py --get-chat-id   # find your Telegram chat id
  python ktm_watch.py --test-telegram # send a test message
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import requests
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
SHUTTLE_URL = "https://shuttleonline.ktmb.com.my/Home/Shuttle"
STATIONS = {"SG_TO_JB": ("WOODLANDS", "JB SENTRAL"), "JB_TO_SG": ("JB SENTRAL", "WOODLANDS")}
PRETTY = {"SG_TO_JB": "Woodlands → JB Sentral", "JB_TO_SG": "JB Sentral → Woodlands"}
MIN_INTERVAL_MIN = 3  # be polite to KTMB's servers


class SetupError(Exception):
    """The page didn't look the way the script expects."""


# ----------------------------------------------------------------------------- config
def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Can't find {path.name}. Put it in the same folder as this script.")
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("settings", {})
    cfg.setdefault("telegram", {})
    # Secrets can come from environment variables (used when running on GitHub)
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram"]["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"].strip()
    watches = cfg.get("watch", [])
    if not watches:
        sys.exit("No [[watch]] entries in config.toml — add at least one trip to watch.")
    for i, w in enumerate(watches, 1):
        d = str(w.get("direction", "")).upper()
        if d not in STATIONS:
            sys.exit(f"Watch #{i}: direction must be SG_TO_JB or JB_TO_SG (got {w.get('direction')!r}).")
        w["direction"] = d
        try:
            w["date"] = w["date"] if isinstance(w["date"], date) else date.fromisoformat(str(w["date"]))
        except Exception:
            sys.exit(f"Watch #{i}: date must look like 2026-10-03.")
        w["earliest"] = str(w.get("earliest", "00:00"))
        w["latest"] = str(w.get("latest", "23:59"))
    return cfg


# ----------------------------------------------------------------------------- telegram
def tg_send(cfg: dict, text: str) -> bool:
    tok, chat = cfg["telegram"].get("bot_token"), cfg["telegram"].get("chat_id")
    if not tok or not chat:
        print("  (Telegram not set up yet — message not sent)")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            print(f"  Telegram error: {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        print(f"  Telegram error: {e}")
        return False


def tg_get_chat_id(cfg: dict) -> None:
    tok = cfg["telegram"].get("bot_token")
    if not tok:
        sys.exit("Put your bot_token in config.toml first.")
    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=15).json()
    chats = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat")
        if c:
            chats[c["id"]] = c.get("first_name") or c.get("title") or c.get("username") or ""
    if not chats:
        print("No messages found. Open your bot in Telegram, send it any message (e.g. 'hi'), then run this again.")
        return
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")
    print("\nCopy the number into chat_id in config.toml.")


# ----------------------------------------------------------------------------- page helpers
JS_ORIGIN = r"""
() => {
  // Walk the page in order, collecting visible text and input/select values,
  // then return the first station name that appears after the word "Origin".
  const toks = [];
  const walk = (n) => {
    if (n.nodeType === 3) { const t = n.textContent.trim(); if (t) toks.push(t); return; }
    if (n.nodeType !== 1) return;
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden') return;
    if (n.tagName === 'SCRIPT' || n.tagName === 'STYLE') return;
    if (n.tagName === 'INPUT' && n.type !== 'hidden' && n.value) toks.push(n.value);
    if (n.tagName === 'SELECT') { const o = n.options[n.selectedIndex]; if (o) toks.push(o.text); return; }
    for (const c of n.childNodes) walk(c);
  };
  walk(document.body);
  const i = toks.findIndex(t => /^origin$/i.test(t));
  if (i < 0) return null;
  for (const t of toks.slice(i + 1, i + 40)) {
    if (/woodlands/i.test(t)) return 'WOODLANDS';
    if (/jb\s*sentral|johor/i.test(t)) return 'JB SENTRAL';
  }
  return null;
}
"""

SWAP_SELECTORS = [
    '[id*="swap" i]', '[class*="swap" i]', '[id*="switch" i]', '[class*="switch" i]',
    '[id*="exchange" i]', '[class*="exchange" i]', '[id*="reverse" i]', '[class*="reverse" i]',
    '.fa-exchange', '.fa-exchange-alt', '.fa-arrows-alt-h', '.fa-retweet', '.fa-sync',
]

JS_SET_DATE = r"""
([iso, text]) => {
  const all = [...document.querySelectorAll('input')];
  const want = (el) => {
    const k = ((el.id || '') + ' ' + (el.name || '') + ' ' + (el.placeholder || '')).toLowerCase();
    return /(onward|depart)/.test(k) && !/return/.test(k);
  };
  let targets = all.filter(want);
  if (!targets.length) {
    // fall back: first date-ish text input on the page
    targets = all.filter(el => el.type !== 'hidden' && /date/i.test((el.id||'') + (el.name||'') + (el.className||''))).slice(0, 1);
  }
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  let done = 0;
  for (const el of targets) {
    try {
      const $ = window.jQuery;
      if ($ && $(el).datepicker && el.type !== 'hidden') { $(el).datepicker('setDate', dt); }
    } catch (e) {}
    if (el.type === 'hidden') { continue; }
    if (!el.value || el.value.trim() === '') {
      el.removeAttribute('readonly');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(el, el.type === 'date' ? iso : text);
    }
    for (const ev of ['input', 'change', 'blur']) el.dispatchEvent(new Event(ev, { bubbles: true }));
    done++;
  }
  return targets.filter(t => t.type !== 'hidden').map(t => t.value);
}
"""

JS_PARSE = r"""
() => {
  const out = [];
  const timeRe = /\b([01]?\d|2[0-3]):[0-5]\d\b/;
  // 1) Tables with a header row that mentions seats
  for (const tbl of document.querySelectorAll('table')) {
    const heads = [...tbl.querySelectorAll('thead th, thead td, tr:first-child th')].map(h => h.innerText.trim().toLowerCase());
    const seatCol = heads.findIndex(h => /seat|avail|kekosongan|tempat/.test(h));
    const depCol = heads.findIndex(h => /depart|bertolak/.test(h));
    if (seatCol < 0) continue;
    const rows = tbl.querySelectorAll('tbody tr').length ? tbl.querySelectorAll('tbody tr') : [...tbl.querySelectorAll('tr')].slice(1);
    for (const tr of rows) {
      const cells = [...tr.querySelectorAll('td, th')].map(c => c.innerText.trim());
      if (!cells.length) continue;
      const depTxt = depCol >= 0 && cells[depCol] ? cells[depCol] : cells.join(' ');
      const tm = depTxt.match(timeRe);
      const sm = (cells[seatCol] || '').match(/\d+/);
      if (tm) out.push({ time: tm[0], seats: sm ? parseInt(sm[0]) : 0, raw: cells.join(' | ') });
    }
  }
  if (out.length) return { mode: 'table', trips: out };
  // 2) Card/row layouts: any block with a time and "N seat(s)"
  const seen = new Set();
  for (const el of document.querySelectorAll('tr, li, [class*="trip" i], [class*="card" i], [class*="row" i]')) {
    const t = el.innerText || '';
    if (t.length > 400) continue;
    const tm = t.match(timeRe);
    const sm = t.match(/(\d+)\s*(seats?|tempat|available)/i) || t.match(/(?:seats?|available)\D{0,15}(\d+)/i);
    if (tm && sm) {
      const key = tm[0];
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ time: tm[0], seats: parseInt(sm[1]), raw: t.replace(/\s+/g, ' ').slice(0, 160) });
    }
  }
  if (out.length) return { mode: 'cards', trips: out };
  const body = document.body.innerText;
  return { mode: 'none', trips: [], noTrips: /no (trip|train|result)|tiada/i.test(body) };
}
"""


def dismiss_popups(page: Page) -> None:
    for name in ("OK", "Close", "Accept", "I agree", "Got it"):
        try:
            btn = page.get_by_role("button", name=name, exact=True)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=1500)
        except Exception:
            pass


def current_origin(page: Page) -> str | None:
    return page.evaluate(JS_ORIGIN)


def ensure_direction(page: Page, direction: str) -> None:
    want = STATIONS[direction][0]
    if current_origin(page) == want:
        return
    # try any <select> that lists stations
    for sel in page.locator("select").all():
        try:
            opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            match = next((o for o in opts if want.split()[0] in o.upper()), None)
            if match and sel.is_visible():
                sel.select_option(label=match)
                page.wait_for_timeout(500)
                if current_origin(page) == want:
                    return
        except Exception:
            pass
    # try a swap / switch button
    for css in SWAP_SELECTORS:
        loc = page.locator(css)
        for i in range(min(loc.count(), 3)):
            try:
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=1500)
                    page.wait_for_timeout(600)
                    if current_origin(page) == want:
                        return
            except Exception:
                pass
    raise SetupError(
        f"Couldn't set origin to {want}. Run with --show-browser to watch what happens, "
        "and check the debug folder for a screenshot."
    )


def set_pax(page: Page, pax: int) -> None:
    label = f"{pax} Pax"
    for sel in page.locator("select").all():
        try:
            if label in sel.locator("option").all_inner_texts():
                sel.select_option(label=label)
                return
        except Exception:
            pass


def click_search(page: Page) -> None:
    for loc in (
        page.get_by_role("button", name=re.compile(r"^\s*search\s*$", re.I)),
        page.locator("input[type=submit][value*='SEARCH' i]"),
        page.locator("text=/^\\s*SEARCH\\s*$/"),
    ):
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=5000)
                return
        except Exception:
            pass
    raise SetupError("Couldn't find the SEARCH button.")


def save_debug(page: Page, tag: str) -> Path:
    d = HERE / "debug"
    d.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shot = d / f"{stamp}-{tag}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        (d / f"{stamp}-{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return shot


def check_trip(page: Page, w: dict, pax: int, date_fmt: str) -> list[dict]:
    page.goto(SHUTTLE_URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    dismiss_popups(page)
    ensure_direction(page, w["direction"])
    values = page.evaluate(JS_SET_DATE, [w["date"].isoformat(), w["date"].strftime(date_fmt)])
    if not values or not any(values):
        raise SetupError("Couldn't fill in the departure date.")
    set_pax(page, pax)
    click_search(page)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)
    dismiss_popups(page)
    res = page.evaluate(JS_PARSE)
    if not res["trips"] and not res.get("noTrips"):
        shot = save_debug(page, f"{w['direction']}-{w['date']}")
        raise SetupError(f"Searched, but couldn't read the results. Screenshot saved: {shot.name}")
    lo, hi = w["earliest"], w["latest"]
    trips = [t for t in res["trips"] if lo <= t["time"].zfill(5) <= hi]
    for t in trips:
        t["time"] = t["time"].zfill(5)
    return sorted(trips, key=lambda t: t["time"])


# ----------------------------------------------------------------------------- main loop
def describe(w: dict) -> str:
    return f"{PRETTY[w['direction']]} on {w['date']:%a %d %b} ({w['earliest']}–{w['latest']})"


def run(cfg: dict, once: bool, show_browser: bool, state_path: Path | None = None) -> None:
    s = cfg["settings"]
    pax = int(s.get("passengers", 1))
    every = max(float(s.get("check_every_minutes", 5)), MIN_INTERVAL_MIN)
    date_fmt = s.get("date_format", "%d %b %Y")
    headless = not show_browser and bool(s.get("headless", True))
    last: dict[str, int] = {}   # key -> seats last time
    fails = 0
    alerted_broken = False
    if state_path and state_path.exists():
        try:
            st = json.loads(state_path.read_text())
            last, fails, alerted_broken = st.get("last", {}), st.get("fails", 0), st.get("alerted_broken", False)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(locale="en-GB", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        if not once:
            tg_send(cfg, "🚆 KTM seat watcher started. Watching:\n" +
                    "\n".join("• " + describe(w) for w in cfg["watch"]))
        while True:
            today = date.today()
            active = [w for w in cfg["watch"] if w["date"] >= today]
            if not active:
                print("All watched dates have passed. Stopping.")
                tg_send(cfg, "🚆 KTM seat watcher stopped: all watched dates have passed.")
                break
            round_ok = True
            for w in active:
                print(f"[{datetime.now():%H:%M:%S}] Checking {describe(w)} ...")
                try:
                    trips = check_trip(page, w, pax, date_fmt)
                except (SetupError, PWTimeout) as e:
                    round_ok = False
                    print(f"  ⚠ {e}")
                    continue
                except Exception as e:  # network blips etc.
                    round_ok = False
                    print(f"  ⚠ Unexpected error: {e}")
                    continue
                if not trips:
                    print("  No trains in that time window.")
                newly_open = []
                for t in trips:
                    key = f"{w['direction']}|{w['date']}|{t['time']}"
                    seats = t["seats"]
                    print(f"  {t['time']}  seats: {seats}")
                    if seats >= pax and last.get(key, -1) < pax:
                        newly_open.append(t)
                    last[key] = seats
                if newly_open:
                    lines = "\n".join(f"• {t['time']} — {t['seats']} seat(s)" for t in newly_open)
                    tg_send(cfg, f"✅ Seats available!\n{PRETTY[w['direction']]}, {w['date']:%a %d %b %Y}\n"
                                 f"{lines}\n\nBook now: {SHUTTLE_URL}")
                time.sleep(random.uniform(4, 9))  # small gap between searches
            fails = 0 if round_ok else fails + 1
            if fails >= 3 and not alerted_broken:
                tg_send(cfg, "⚠ KTM seat watcher keeps failing to read the KTMB site. "
                             "Check the window on your computer / the debug folder.")
                alerted_broken = True
            if round_ok:
                alerted_broken = False
            if state_path:
                state_path.write_text(json.dumps(
                    {"last": last, "fails": fails, "alerted_broken": alerted_broken}, indent=1, sort_keys=True))
            if once:
                break
            wait = every * 60 * random.uniform(0.85, 1.15)
            print(f"Next check in {wait/60:.1f} min. (Ctrl+C to stop)\n")
            time.sleep(wait)
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="KTM Shuttle Tebrau seat watcher")
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--once", action="store_true", help="check once, print, and exit")
    ap.add_argument("--show-browser", action="store_true", help="show the browser window")
    ap.add_argument("--get-chat-id", action="store_true", help="find your Telegram chat id")
    ap.add_argument("--state", help="remember results between runs in this JSON file")
    ap.add_argument("--test-telegram", action="store_true", help="send a test Telegram message")
    a = ap.parse_args()
    cfg = load_config(Path(a.config))
    if a.get_chat_id:
        return tg_get_chat_id(cfg)
    if a.test_telegram:
        ok = tg_send(cfg, "👋 Test from your KTM seat watcher. Alerts will arrive here.")
        print("Sent!" if ok else "Not sent — check bot_token and chat_id.")
        return
    try:
        run(cfg, a.once, a.show_browser, Path(a.state) if a.state else None)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

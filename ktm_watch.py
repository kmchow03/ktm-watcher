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
try:
    from zoneinfo import ZoneInfo
    SGT = ZoneInfo("Asia/Singapore")
except Exception:  # pragma: no cover
    from datetime import timezone, timedelta
    SGT = timezone(timedelta(hours=8))


def now_sg() -> datetime:
    return datetime.now(SGT)


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
    watches = cfg.setdefault("watch", [])
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


JS_KTMB_SEARCH = r"""
([want, dateText]) => {
  // Uses KTMB's own page functions (SwapFromToTerminal, SearchTrip).
  const from = document.getElementById('FromStationId');
  const date = document.getElementById('OnwardDate');
  if (!from || !date || typeof SearchTrip !== 'function') return 'no-form';
  const norm = v => /woodlands/i.test(v) ? 'WOODLANDS' : (/jb|johor/i.test(v) ? 'JB SENTRAL' : v);
  if (norm(from.value) !== want && typeof SwapFromToTerminal === 'function') SwapFromToTerminal();
  if (norm(from.value) !== want) return 'no-swap:' + from.value;
  date.value = dateText;
  const pax = document.getElementById('PassengerCount'); if (pax) pax.value = '1';
  SearchTrip();
  return 'ok';
}
"""

JS_KTMB_STATUS = r"""
() => {
  const rows = document.querySelectorAll('tbody.depart-trips tr');
  // Any KTMB pop-up message that's showing
  const msgs = [...document.querySelectorAll('.modal.show, .modal[style*="block"], .jconfirm, .swal2-popup, .alert-danger, [role=alertdialog]')]
    .map(e => e.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean);
  return { rows: rows.length, msgs, url: location.href, title: document.title,
           text: document.body ? document.body.innerText.replace(/\s+/g, ' ').slice(0, 400) : '' };
}
"""


def check_trip(page: Page, w: dict, pax: int, date_fmt: str) -> list[dict]:
    page.goto(SHUTTLE_URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_function("typeof SearchTrip === 'function' && !!document.getElementById('OnwardDate')", timeout=25000)
    except PWTimeout:
        shot = save_debug(page, f"{w['direction']}-{w['date']}-form")
        st = page.evaluate(JS_KTMB_STATUS)
        raise SetupError(f"KTMB search form didn't load. Page title: {st['title']!r}. Text: {st['text'][:200]!r}")
    date_text = f"{w['date'].day} {w['date']:%b %Y}"   # KTMB uses e.g. "3 Oct 2026"
    res = page.evaluate(JS_KTMB_SEARCH, [STATIONS[w["direction"]][0], date_text])
    if res != "ok":
        raise SetupError(f"Couldn't fill in the KTMB search form ({res}).")
    page.wait_for_url("**/ShuttleTrip**", timeout=30000)
    st = None
    for _ in range(40):  # up to ~30s for the train list to load
        page.wait_for_timeout(750)
        st = page.evaluate(JS_KTMB_STATUS)
        if st["rows"] or st["msgs"]:
            break
    if not st["rows"]:
        save_debug(page, f"{w['direction']}-{w['date']}")
        if st["msgs"]:
            raise SetupError("KTMB said: " + " | ".join(st["msgs"])[:300])
        raise SetupError(f"No train list appeared. Page: {st['url']} Text: {st['text'][:250]!r}")
    trips = page.evaluate(JS_PARSE)["trips"]
    lo, hi = w["earliest"], w["latest"]
    out = []
    for t in trips:
        t["time"] = t["time"].zfill(5)
        if lo <= t["time"] <= hi:
            out.append(t)
    return sorted(out, key=lambda t: t["time"])


def describe(w: dict) -> str:
    return f"{PRETTY[w['direction']]} on {w['date']:%a %d %b} ({w['earliest']}–{w['latest']})"


# ----------------------------------------------------------------------------- telegram commands
HELP = """🚆 KTM seat bot commands
(I check about every 10–15 minutes, so replies can take a little while.)

/check 3 Oct SG — seats on every train that day
/watch 3 Oct SG — alert me when any train that day gets seats
/watch 3 Oct JB 17:00-22:00 — only trains in that time window
/list — what I'm watching
/stop 2 — stop watching number 2 from /list

SG = Woodlands → JB Sentral
JB = JB Sentral → Woodlands
Dates: 3 Oct, 3/10, 2026-10-03, today, tomorrow"""

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(tokens: list[str]) -> tuple[date | None, list[str]]:
    """Read a date from the start of tokens. Returns (date, remaining tokens)."""
    today = now_sg().date()
    if not tokens:
        return None, tokens
    t0 = tokens[0].lower()
    if t0 == "today":
        return today, tokens[1:]
    if t0 in ("tomorrow", "tmr", "tml"):
        return date.fromordinal(today.toordinal() + 1), tokens[1:]
    try:
        return date.fromisoformat(t0), tokens[1:]
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", t0)
    if m:
        d, mo, y = int(m[1]), int(m[2]), m[3]
        rest = tokens[1:]
    elif re.fullmatch(r"\d{1,2}", t0) and len(tokens) > 1 and tokens[1][:3].lower() in MONTHS:
        d, mo = int(t0), MONTHS[tokens[1][:3].lower()]
        y, rest = None, tokens[2:]
        if rest and re.fullmatch(r"\d{4}", rest[0]):
            y, rest = rest[0], rest[1:]
    else:
        return None, tokens
    try:
        if y:
            yy = int(y) + (2000 if len(y) == 2 else 0)
            return date(yy, mo, d), rest
        cand = date(today.year, mo, d)
        if cand < today:
            cand = date(today.year + 1, mo, d)
        return cand, rest
    except ValueError:
        return None, tokens


def parse_dir(tok: str | None) -> str | None:
    if not tok:
        return None
    t = tok.lower()
    if t in ("sg", "sin", "singapore", "woodlands", "wdl", "sg>jb", "sg-jb", "sg_to_jb"):
        return "SG_TO_JB"
    if t in ("jb", "jbs", "johor", "sentral", "jb>sg", "jb-sg", "jb_to_sg"):
        return "JB_TO_SG"
    return None


def parse_window(tok: str | None) -> tuple[str, str] | None:
    if not tok:
        return ("00:00", "23:59")
    m = re.fullmatch(r"(\d{1,2})[:.]?(\d{2})?\s*-\s*(\d{1,2})[:.]?(\d{2})?", tok)
    if not m:
        return None
    a = f"{int(m[1]):02d}:{m[2] or '00'}"
    b = f"{int(m[3]):02d}:{m[4] or '00'}"
    if b == "24:00":
        b = "23:59"
    return (a, b)


def watch_id(w: dict) -> str:
    return f"{w['direction']}|{w['date']}|{w['earliest']}|{w['latest']}"


def all_watches(cfg: dict, state: dict) -> list[dict]:
    """Trips from config.toml (minus ones stopped via Telegram) plus trips added via Telegram."""
    muted = set(state.get("muted", []))
    out = [dict(w, source="GitHub") for w in cfg["watch"] if watch_id(w) not in muted]
    for tw in state.get("tg_watches", []):
        w = dict(tw, date=date.fromisoformat(tw["date"]), source="Telegram")
        if watch_id(w) not in {watch_id(x) for x in out}:
            out.append(w)
    today = now_sg().date()
    return sorted([w for w in out if w["date"] >= today], key=lambda w: (w["date"], w["direction"], w["earliest"]))


def tg_get_commands(cfg: dict, state: dict) -> list[str]:
    tok, chat = cfg["telegram"].get("bot_token"), str(cfg["telegram"].get("chat_id") or "")
    if not tok or not chat:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates",
                         params={"offset": state.get("tg_offset", 0), "timeout": 0}, timeout=15).json()
    except Exception as e:
        print(f"  Couldn't read Telegram messages: {e}")
        return []
    cmds = []
    for u in r.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat:
            continue  # ignore anyone who isn't you
        text = (msg.get("text") or "").strip()
        if text:
            cmds.append(text)
    return cmds


def format_seats(w: dict, trips: list[dict]) -> str:
    head = f"🚆 {PRETTY[w['direction']]}, {w['date']:%a %d %b %Y}"
    if not trips:
        return head + "\nNo trains found for that day."
    rows = [f"{t['time']}  {'✅ ' + str(t['seats']) + ' seats' if t['seats'] else '❌ full'}" for t in trips]
    free = sum(1 for t in trips if t["seats"])
    return f"{head}\n" + "\n".join(rows) + f"\n\n{free} of {len(trips)} trains have seats.\nBook: {SHUTTLE_URL}"


def handle_commands(cfg: dict, state: dict, page: Page, pax: int, date_fmt: str) -> None:
    for text in tg_get_commands(cfg, state):
        print(f"  Telegram: {text!r}")
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        if cmd in ("/start", "/help", "help"):
            tg_send(cfg, HELP)
        elif cmd == "/list":
            ws = all_watches(cfg, state)
            if not ws:
                tg_send(cfg, "I'm not watching any trips. Add one with e.g. /watch 3 Oct SG")
            else:
                tg_send(cfg, "👀 Watching:\n" + "\n".join(f"{i}. {describe(w)}" for i, w in enumerate(ws, 1))
                        + "\n\nStop one with /stop <number>")
        elif cmd == "/stop":
            ws = all_watches(cfg, state)
            if not args or not args[0].isdigit() or not (1 <= int(args[0]) <= len(ws)):
                tg_send(cfg, "Send /list first, then e.g. /stop 2")
                continue
            w = ws[int(args[0]) - 1]
            wid = watch_id(w)
            state["tg_watches"] = [x for x in state.get("tg_watches", [])
                                   if watch_id(dict(x, date=date.fromisoformat(x["date"]))) != wid]
            if w["source"] == "GitHub":
                state.setdefault("muted", []).append(wid)
            tg_send(cfg, f"🛑 Stopped watching {describe(w)}")
        elif cmd in ("/watch", "/check"):
            d, rest = parse_date(args)
            direction = parse_dir(rest[0] if rest else None)
            window = parse_window(rest[1] if len(rest) > 1 else None)
            if not d or not direction or not window:
                example = "/watch 3 Oct JB 17:00-22:00" if cmd == "/watch" else "/check 3 Oct SG"
                tg_send(cfg, f"I didn't understand that. Try e.g. {example}\nSend /help for all commands.")
                continue
            if d < now_sg().date():
                tg_send(cfg, f"{d:%d %b %Y} has already passed.")
                continue
            w = {"direction": direction, "date": d, "earliest": window[0], "latest": window[1]}
            if cmd == "/check":
                try:
                    trips = check_trip(page, dict(w, earliest="00:00", latest="23:59"), pax, date_fmt)
                    tg_send(cfg, format_seats(w, trips))
                except Exception as e:
                    tg_send(cfg, f"⚠ Couldn't check KTMB just now ({e}). I'll try again if you resend.")
            else:
                if watch_id(w) in {watch_id(x) for x in all_watches(cfg, state)}:
                    tg_send(cfg, f"I'm already watching {describe(w)}")
                    continue
                state.setdefault("tg_watches", []).append(dict(w, date=d.isoformat()))
                state["muted"] = [m for m in state.get("muted", []) if m != watch_id(w)]
                tg_send(cfg, f"👀 Now watching {describe(w)}\nI'll message you whenever a train in that window gets seats.")
        else:
            tg_send(cfg, "Send /help to see what I can do.")
        time.sleep(random.uniform(2, 4))
    # tidy up trips whose date has passed
    today = now_sg().date()
    state["tg_watches"] = [x for x in state.get("tg_watches", []) if date.fromisoformat(x["date"]) >= today]


# ----------------------------------------------------------------------------- main loop
def run(cfg: dict, once: bool, show_browser: bool, state_path: Path | None = None) -> None:
    s = cfg["settings"]
    pax = int(s.get("passengers", 1))
    every = max(float(s.get("check_every_minutes", 5)), MIN_INTERVAL_MIN)
    date_fmt = s.get("date_format", "%d %b %Y")
    headless = not show_browser and bool(s.get("headless", True))
    state: dict = {}
    if state_path and state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            state = {}
    last: dict[str, int] = state.setdefault("last", {})   # key -> seats last time

    def save() -> None:
        if state_path:
            today = now_sg().date().isoformat()
            # forget seat history for dates that have passed
            state["last"] = {k: v for k, v in last.items() if k.split("|")[1] >= today}
            state_path.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(locale="en-GB", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        if not once:
            ws = all_watches(cfg, state)
            tg_send(cfg, "🚆 KTM seat watcher started. Watching:\n" +
                    ("\n".join("• " + describe(w) for w in ws) or "nothing yet — send /help"))
        while True:
            handle_commands(cfg, state, page, pax, date_fmt)
            active = all_watches(cfg, state)
            if not active:
                print("Nothing to watch right now. Send /watch to the bot on Telegram, or add a trip to config.toml.")
            round_ok = True
            for w in active:
                print(f"[{now_sg():%H:%M:%S} SGT] Checking {describe(w)} ...")
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
                    print(f"  {t['time']}  seats: {t['seats']}")
                    if t["seats"] >= pax and last.get(key, -1) < pax:
                        newly_open.append(t)
                    else:
                        last[key] = t["seats"]
                if newly_open:
                    lines = "\n".join(f"• {t['time']} — {t['seats']} seat(s)" for t in newly_open)
                    sent = tg_send(cfg, f"✅ Seats available!\n{PRETTY[w['direction']]}, {w['date']:%a %d %b %Y}\n"
                                        f"{lines}\n\nBook now: {SHUTTLE_URL}")
                    if sent:  # only remember these once you've actually been told
                        for t in newly_open:
                            last[f"{w['direction']}|{w['date']}|{t['time']}"] = t["seats"]
                time.sleep(random.uniform(4, 9))  # small gap between searches
            fails = 0 if round_ok else state.get("fails", 0) + 1
            state["fails"] = fails
            if fails >= 3 and not state.get("alerted_broken"):
                tg_send(cfg, "⚠ KTM seat watcher keeps failing to read the KTMB site. "
                             "Open the latest run on GitHub (Actions tab) to see why.")
                state["alerted_broken"] = True
            if round_ok:
                state["alerted_broken"] = False
            save()
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

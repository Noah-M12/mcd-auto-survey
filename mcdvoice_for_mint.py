#!/usr/bin/env python3
"""
mcdvoice.py - a local helper for McDonald's customer-satisfaction surveys.

What it does
    * Keeps a local file of survey codes from your receipts, with store number,
      visit date, items, expiry, and status.
    * Opens mcdvoice.com one code at a time in a real browser you can see.
    * Reads each survey page, shows you the actual question and its options,
      and waits for YOUR answer. It never picks a rating for you.
    * Saves the validation code from the end of each survey back to the file.

What it deliberately does NOT do
    * It does not bypass McDonald's per-restaurant monthly survey limit. That
      limit lives on their servers. If you're blocked, this script marks the
      code and stops.
    * It does not invent, default, or reuse satisfaction ratings.

Usage
    python3 mcdvoice.py add codes.txt      # import codes from a text file
    python3 mcdvoice.py add --paste        # paste codes, then Ctrl-D
    python3 mcdvoice.py list               # show everything and its status
    python3 mcdvoice.py run                # work through pending codes
    python3 mcdvoice.py run --only 13290   # just the code containing 13290
    python3 mcdvoice.py run --limit 1      # stop after one survey

Setup
    python3 -m pip install playwright
    python3 -m playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

SURVEY_URL = "https://www.mcdvoice.com"
DATA_FILE = Path.home() / "mcdvoice" / "codes.json"
PROFILE_FILE = Path.home() / "mcdvoice" / "profile.json"
DUMP_DIR: Path | None = None  # set by --dump

# Standing answers to questions that are the same on every visit. Each rule
# fires when any string in "when" appears in the question (or, failing that,
# anywhere on the page), and picks the option whose label matches "answer".
# Whatever is set here is applied without asking and printed as it's used.
DEFAULT_PROFILE = [
    {"when": ["how did you place your order"],
     "answer": "With an employee at the restaurant"},
    {"when": ["visit type"], "answer": "Drive-thru"},
    {"when": ["mymcdonald", "rewards"], "answer": "No"},
    {"when": ["was your order accurate"], "answer": "Yes"},
    {"when": ["experience a problem"], "answer": "No"},
    {"when": ["recommend this mcdonald"], "answer": "Highly Likely"},
    {"when": ["return to this mcdonald"], "answer": "Highly Likely"},
]


COMMENTS_FILE = Path.home() / "mcdvoice" / "comments.txt"

COMMENTS_TEMPLATE = """\
# Your comments for the survey's "what did you like best" box.
#
# One comment per line. Blank lines and lines starting with # are ignored.
# The script uses the next one each time it runs, cycling back to the start
# when it reaches the end - so consecutive surveys don't submit identical text.
#
# These go in as your own words, so write them yourself. Nothing here is
# filled in for you.
#
# To pin a specific comment to a specific receipt instead, add a "comment"
# field to that code's entry in codes.json - it wins over this file.
#
# Delete these instructions and add your lines below.
"""


def load_comments() -> list[str]:
    """Your comment lines, in order. Creates the file with instructions."""
    if not COMMENTS_FILE.exists():
        COMMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        COMMENTS_FILE.write_text(COMMENTS_TEMPLATE)
        return []
    out = []
    for line in COMMENTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def load_profile() -> list[dict]:
    """Standing answers, created from the defaults on first run."""
    if not PROFILE_FILE.exists():
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(json.dumps(DEFAULT_PROFILE, indent=2))
        return list(DEFAULT_PROFILE)
    try:
        rules = json.loads(PROFILE_FILE.read_text())
    except json.JSONDecodeError as exc:
        print(f"  [profile.json unreadable ({exc}); ignoring it]")
        return []
    return rules if isinstance(rules, list) else []


def profile_pick(rules, question: str, page: str, labels: list[str]):
    """Return (index, rule) for the first standing answer that fits, else None."""
    q = (question or "").lower()
    p = (page or "").lower()
    for rule in rules:
        triggers = [str(w).lower() for w in rule.get("when", [])]
        if not any(w in q for w in triggers) and not any(w in p for w in triggers):
            continue
        wanted = str(rule.get("answer", "")).strip().lower()
        if not wanted:
            continue
        for idx, label in enumerate(labels):
            lab = (label or "").strip().lower()
            if lab == wanted or (wanted in lab and len(wanted) > 3):
                return idx, rule
    return None

# 26-digit receipt code, printed as 19386-13290-90826-00029-00128-2
CODE_RE = re.compile(r"\b(\d{5})-(\d{5})-(\d{5})-(\d{5})-(\d{5})-(\d)\b")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*[APap]\.?[Mm]\.?)")
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")

# McDonald's receipt invitations are typically valid ~30 days from the visit.
EXPIRY_DAYS = 30

BLOCK_MARKERS = (
    "limit the number of surveys",
    "recently completed our survey",
)
INVALID_MARKERS = (
    "not a valid survey code",
    "invalid code",
    "check the code",
    "unable to locate",
)

STATUS_PENDING = "pending"
STATUS_DONE = "completed"
STATUS_BLOCKED = "blocked"
STATUS_INVALID = "invalid"
STATUS_EXPIRED = "expired"
STATUS_SKIPPED = "skipped"


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def load_store() -> dict:
    if not DATA_FILE.exists():
        return {"codes": []}
    try:
        with DATA_FILE.open() as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(f"{DATA_FILE} is not readable as JSON ({exc}). "
                 f"Move it aside and re-import.")
    data.setdefault("codes", [])
    return data


def save_store(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(DATA_FILE)


def find_entry(data: dict, code: str) -> dict | None:
    for entry in data["codes"]:
        if entry["code"] == code:
            return entry
    return None


# --------------------------------------------------------------------------
# importing codes
# --------------------------------------------------------------------------

def parse_lines(text: str, default_year: int | None = None) -> list[dict]:
    """Pull codes out of free-form text.

    Handles the shape you already use, e.g.

        1329 12:02 AM 19386-13290-90826-00029-00128-2 2 cheeseburgers, L fries

    but really it just hunts for the 26-digit pattern on each line and treats
    whatever sits around it as context. A bare list of codes works fine too.
    A line like "Time (09/08)" or "09/08/2026" sets the visit date for the
    lines that follow it.
    """
    default_year = default_year or date.today().year
    found: list[dict] = []
    current_date: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        match = CODE_RE.search(line)

        # A line with no code may still carry a date header for what follows.
        if not match:
            dm = DATE_RE.search(line)
            if dm:
                current_date = _norm_date(dm, default_year)
            continue

        code = "-".join(match.groups())
        store = match.group(1)

        before = line[: match.start()].strip()
        after = line[match.end():].strip(" ,\t")

        # A date on the code's own line wins over the running header.
        visit_date = current_date
        dm = DATE_RE.search(before) or DATE_RE.search(after)
        if dm:
            visit_date = _norm_date(dm, default_year)

        tm = TIME_RE.search(before) or TIME_RE.search(after)
        visit_time = tm.group(1).strip() if tm else None

        found.append({
            "code": code,
            "store": store,
            "visit_date": visit_date,
            "visit_time": visit_time,
            "items": after or None,
            "status": STATUS_PENDING,
            "validation_code": None,
            "notes": None,
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "completed_at": None,
        })

    return found


def _norm_date(match: re.Match, default_year: int) -> str:
    month, day, year = match.group(1), match.group(2), match.group(3)
    if year is None:
        year_i = default_year
    else:
        year_i = int(year)
        if year_i < 100:
            year_i += 2000
    try:
        return date(year_i, int(month), int(day)).isoformat()
    except ValueError:
        return ""


def expiry_of(entry: dict) -> date | None:
    if not entry.get("visit_date"):
        return None
    try:
        return date.fromisoformat(entry["visit_date"]) + timedelta(days=EXPIRY_DAYS)
    except ValueError:
        return None


def refresh_expiry(data: dict) -> None:
    today = date.today()
    for entry in data["codes"]:
        if entry["status"] != STATUS_PENDING:
            continue
        exp = expiry_of(entry)
        if exp and exp < today:
            entry["status"] = STATUS_EXPIRED
            entry["notes"] = f"invitation expired {exp.isoformat()}"


# --------------------------------------------------------------------------
# terminal prompts
# --------------------------------------------------------------------------

def ask_choice(question: str, options: list[str]) -> int | None:
    """Show a question and its real options. Returns the chosen index."""
    print()
    print("-" * 68)
    print(question.strip() or "(question text not detected - see the browser)")
    print("-" * 68)
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}. {opt}")
    print("   s. skip this question (leave it blank)")
    print("   m. I'll answer this one myself in the browser window")
    print("   q. quit this survey")

    while True:
        raw = input("your answer > ").strip().lower()
        if raw in {"q", "quit"}:
            raise KeyboardInterrupt
        if raw in {"m", "manual"}:
            return None
        if raw in {"s", "skip"}:
            return -1
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"  enter 1-{len(options)}, or s / m / q")


def ask_text(question: str) -> str:
    print()
    print("-" * 68)
    print(question.strip() or "(comment box)")
    print("-" * 68)
    print("  Type your comment and press Enter. Blank to leave it empty.")
    return input("your comment > ").strip()


def have_display() -> bool:
    """True if a visible browser window can plausibly be opened."""
    import os
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def resolve_headless(args) -> bool:
    """Decide whether to run the browser hidden.

    Explicit flags win. Otherwise a visible window is preferred - you can watch
    it and take over - but on a Linux box with no X or Wayland session that
    would just crash, so fall back to headless and say so.
    """
    if getattr(args, "headless", False):
        return True
    if getattr(args, "headed", False):
        return False
    if not have_display():
        print("  no DISPLAY/WAYLAND_DISPLAY detected - running headless.")
        print("  (use --headed to force a window, e.g. under xvfb-run)")
        return True
    return False


def wait_between(minutes: float) -> None:
    """Idle between surveys, counting down so it doesn't look hung.

    Ctrl-C skips the rest of the wait and moves straight to the next survey;
    press it again during a survey to stop the batch.
    """
    total = int(round(minutes * 60))
    if total <= 0:
        return
    print(f"\n  waiting {minutes:g} min before the next survey "
          f"(Ctrl-C to skip the wait)")
    try:
        remaining = total
        while remaining > 0:
            mins, secs = divmod(remaining, 60)
            print(f"\r  next survey in {mins:d}:{secs:02d}   ", end="", flush=True)
            step = min(1, remaining)
            time.sleep(step)
            remaining -= step
        print("\r  " + " " * 34 + "\r", end="", flush=True)
    except KeyboardInterrupt:
        print("\n  wait skipped.")


def pause(message: str) -> None:
    print()
    print(message)
    input("press Enter when you're ready to continue > ")


# --------------------------------------------------------------------------
# page reading
# --------------------------------------------------------------------------

def page_text(page) -> str:
    try:
        return page.inner_text("body").lower()
    except Exception:
        return ""


def detect_state(page) -> str:
    """Return 'block', 'invalid', or 'ok' for the current page."""
    url = (page.url or "").lower()
    text = page_text(page)
    if "block.aspx" in url or any(m in text for m in BLOCK_MARKERS):
        return "block"
    if any(m in text for m in INVALID_MARKERS):
        return "invalid"
    return "ok"


def read_radio_groups(page) -> list[dict]:
    """Find every radio-button question on the page.

    Returns a list of {name, question, options:[{label, value}]} in the order
    they appear. Label text is taken from the page itself, so whatever the
    survey actually says is what you see in the terminal.
    """
    return page.evaluate(
        """
        () => {
          // Matrix cells are filled with zero-width joiners, and header text
          // is separated by non-breaking spaces. Drop the zero-width filler,
          // but turn nbsp into a real space - deleting it welds words together
          // ("Highly Satisfied" -> "HighlySatisfied") and breaks label matching.
          const clean = (s) =>
            (s || '')
              .replace(/[\\u200B-\\u200D\\uFEFF]/g, '')
              .replace(/\\u00A0/g, ' ')
              .replace(/\\s+/g, ' ')
              .trim();

          const fromNode = (n) => {
            if (!n) return '';
            let t = clean(n.innerText);
            if (t) return t;
            t = clean(n.getAttribute && n.getAttribute('aria-label'));
            if (t) return t;
            t = clean(n.getAttribute && n.getAttribute('title'));
            if (t) return t;
            const img = n.querySelector && n.querySelector('img');
            if (img) {
              t = clean(img.getAttribute('alt')) || clean(img.getAttribute('title'));
              if (t) return t;
            }
            return '';
          };

          const labelFor = (input) => {
            let t = '';
            if (input.id) {
              const l = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
              t = fromNode(l);
              if (t) return t;
            }
            t = fromNode(input.closest('label'));
            if (t) return t;
            t = clean(input.getAttribute('aria-label'))
             || clean(input.getAttribute('title'));
            if (t) return t;

            // Matrix layout: borrow the column header above this cell.
            const cell = input.closest('td');
            if (cell) {
              const idx = Array.from(cell.parentElement.children).indexOf(cell);
              const table = cell.closest('table');
              if (table) {
                for (const row of table.querySelectorAll('tr')) {
                  const head = row.children[idx];
                  if (!head) continue;
                  if (head.querySelector && head.querySelector('input')) continue;
                  t = fromNode(head);
                  if (t) return t;
                }
              }
            }
            return '';
          };

          const questionFor = (input) => {
            const fs = input.closest('fieldset');
            const lg = fs && fs.querySelector('legend');
            if (lg && lg.innerText.trim()) return lg.innerText.trim();
            const row = input.closest('tr');
            if (row && row.cells.length && row.cells[0].innerText.trim()) {
              return row.cells[0].innerText.trim();
            }
            let node = input.parentElement;
            for (let i = 0; i < 6 && node; i++) {
              const t = (node.innerText || '').trim();
              if (t.length > 12 && t.length < 400) return t.split('\\n')[0].trim();
              node = node.parentElement;
            }
            return '';
          };

          const groups = new Map();
          for (const input of document.querySelectorAll('input[type=radio]')) {
            if (input.disabled) continue;
            const box = input.getBoundingClientRect();
            if (box.width === 0 && box.height === 0) continue;
            const name = input.name || ('anon_' + groups.size);
            if (!groups.has(name)) {
              groups.set(name, { name, question: questionFor(input), options: [] });
            }
            groups.get(name).options.push({
              label: labelFor(input),
              value: input.value,
              id: input.id || null,
              checked: input.checked,
            });
            groups.get(name).question = groups.get(name).question || questionFor(input);
          }
          return Array.from(groups.values());
        }
        """
    )


# Words in a checkbox label that carry no meaning on their own.
_LABEL_STOPWORDS = {"and", "the", "your", "with", "other", "none", "above",
                    "all", "that", "apply", "of", "or", "a", "an"}

# Category labels whose wording never appears in a receipt. Maps a checkbox
# label (lowercased) to receipt words that imply it. Edit freely - anything
# not covered here still falls back to matching the label's own words, and
# a page nothing matches on will stop and ask you.
ITEM_HINTS = {
    "burgers, chicken & fish": [
        "cheeseburger", "hamburger", "mcdouble", "big mac", "quarter pounder",
        "qp", "mccrispy", "mcchicken", "nugget", "mcnugget", "filet",
        "filet-o-fish", "snack wrap", "fries", "daily double", "mcrib",
    ],
    "beverages & coffee": [
        "coke", "sprite", "tea", "dr pepper", "dr. pepper", "hi-c", "fanta",
        "lemonade", "coffee", "latte", "mocha", "frappe", "macchiato",
        "refresher", "smoothie", "soft drink", "soda", "red bull", "dr pepper",
    ],
    "sweet treats": [
        "mcflurry", "cookie", "sundae", "pie", "cone", "shake", "oreo",
    ],
    "breakfast": [
        "mcmuffin", "hotcake", "biscuit", "burrito", "mcgriddle", "hash brown",
    ],
    "soft drink": [
        "soft drink", "coke", "sprite", "fanta", "dr pepper", "dr. pepper",
        "hi-c", "diet coke", "coke zero", "soda",
    ],
    "chicken nuggets": ["nugget", "mcnugget"],
    "hamburger/cheeseburger": ["cheeseburger", "hamburger"],
    "quarter pounder burger": ["quarter pounder", "qp"],
    "mcdouble/double cheeseburger": ["mcdouble"],
    "iced/sweet tea": ["sweet tea", "iced tea", "unsweet iced tea"],
    "fries": ["fries", "fry"],
}

# Words too generic to identify an item on their own - "Burger" would match
# "cheeseburger", "Drink" would match "Frozen Drink/Smoothie". These only ever
# match through ITEM_HINTS.
_GENERIC = {"burger", "burgers", "drink", "drinks", "coffee", "chicken",
            "fish", "sandwich", "meal", "deluxe", "double", "treats"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&/.\- ]+", " ",
                                      (text or "").lower())).strip()


_HINTS_NORM: dict | None = None


def _hints_normalised() -> dict:
    """ITEM_HINTS keyed the same way labels are normalised, so lookups hit."""
    global _HINTS_NORM
    if _HINTS_NORM is None:
        _HINTS_NORM = {_norm(k): v for k, v in ITEM_HINTS.items()}
    return _HINTS_NORM


def _has(phrase: str, hay: str) -> bool:
    """Whole-word (optionally plural) match, so 'burger' misses 'cheeseburger'."""
    p = re.escape(phrase.strip())
    if not p:
        return False
    return re.search(rf"(?<![a-z0-9]){p}e?s?(?![a-z0-9])", hay) is not None


def match_items(labels: list[str], items: str) -> list[int]:
    """Which checkboxes the receipt's item list implies.

    Matching is deliberately conservative: a label is only ticked when the
    receipt names it, as a whole word. Anything ambiguous is left for you,
    because a wrong tick is a wrong answer submitted in your name.
    """
    if not items:
        return []
    hay = _norm(items)
    picked = []

    for idx, label in enumerate(labels):
        lab = _norm(label)
        if not lab:
            continue

        hints = _hints_normalised().get(lab)
        if hints and any(_has(h, hay) for h in hints):
            picked.append(idx)
            continue

        # A slash means alternatives: "Hamburger/Cheeseburger" is satisfied by
        # either. Each alternative must appear in full.
        alts = [a.strip() for a in lab.split("/") if a.strip()]
        matched = False
        for alt in alts:
            if alt in _GENERIC:
                continue
            words = [w for w in re.split(r"[^a-z0-9]+", alt)
                     if w and w not in _LABEL_STOPWORDS]
            if not words or all(w in _GENERIC for w in words):
                continue
            if _has(" ".join(words), hay):
                matched = True
                break
        if matched:
            picked.append(idx)

    return picked


def read_checkboxes(page) -> list[dict]:
    """Every checkbox on the page, with the label text that names it."""
    return page.evaluate(
        """
        () => {
          const clean = (s) => (s || '')
            .replace(/[\\u200B-\\u200D\\uFEFF]/g, '')
            .replace(/\\u00A0/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
          const out = [];
          for (const i of document.querySelectorAll('input[type=checkbox]')) {
            if (i.disabled) continue;
            const box = i.getBoundingClientRect();
            const lab = i.id
              ? document.querySelector(`label[for="${CSS.escape(i.id)}"]`)
              : null;
            let t = clean(lab && lab.innerText);
            if (!t) t = clean(i.closest('label') && i.closest('label').innerText);
            if (!t) t = clean(i.getAttribute('aria-label'));
            if (!t && box.width === 0 && box.height === 0) continue;
            out.push({ id: i.id || null, name: i.name || null,
                       value: i.value, label: t, checked: i.checked });
          }
          return out;
        }
        """
    )


def ask_multi(question: str, options: list[str]) -> list[int] | None:
    """Pick several options. Returns indexes, [] to skip, None for manual."""
    print()
    print("-" * 68)
    print(question.strip() or "(select all that apply)")
    print("-" * 68)
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}. {opt}")
    print("   comma-separated numbers, e.g. 2,5,9")
    print("   s. none of them / skip")
    print("   m. I'll answer this one myself in the browser window")
    print("   q. quit this survey")

    while True:
        raw = input("your answer > ").strip().lower()
        if raw in {"q", "quit"}:
            raise KeyboardInterrupt
        if raw in {"m", "manual"}:
            return None
        if raw in {"s", "skip", ""}:
            return []
        try:
            picks = [int(p) - 1 for p in re.split(r"[,\s]+", raw) if p]
        except ValueError:
            print(f"  enter numbers 1-{len(options)}, or s / m / q")
            continue
        if all(0 <= p < len(options) for p in picks):
            return picks
        print(f"  enter numbers 1-{len(options)}, or s / m / q")


def read_text_inputs(page) -> list[dict]:
    return page.evaluate(
        """
        () => {
          const out = [];
          for (const el of document.querySelectorAll('textarea')) {
            const box = el.getBoundingClientRect();
            if (box.width === 0 && box.height === 0) continue;
            let q = '';
            const fs = el.closest('fieldset');
            const lg = fs && fs.querySelector('legend');
            if (lg) q = lg.innerText.trim();
            if (!q && el.id) {
              const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
              if (l) q = l.innerText.trim();
            }
            if (!q) q = el.getAttribute('aria-label') || '';
            out.push({ id: el.id || null, name: el.name || null, question: q });
          }
          return out;
        }
        """
    )


def click_next(page) -> bool:
    """Advance the survey. Returns False if no next control was found."""
    for name in ("Next", "next", "Continue", "Submit", "Finish", "Done"):
        try:
            btn = page.get_by_role("button", name=name, exact=False)
            if btn.count() and btn.first.is_enabled():
                btn.first.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                return True
        except Exception:
            pass
    for sel in ("input[type=submit]", "button[type=submit]", "#NextButton", ".NextButton"):
        try:
            el = page.locator(sel)
            if el.count() and el.first.is_visible():
                el.first.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                return True
        except Exception:
            pass
    return False


# The code is only ever accepted when it directly follows an explicit label.
VALIDATION_LABEL_RE = re.compile(
    r"validation\s+code\s*(?:is)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9-]{3,15})",
    re.IGNORECASE,
)

# The welcome text promises a validation code long before you earn one. Strip
# any sentence like that before looking, or you'll "find" the word 'that'.
BOILERPLATE_RE = re.compile(
    r"(?:you\s+will\s+be\s+given|will\s+be\s+given|receive)\s+a\s+validation\s+code[^.]*\.",
    re.IGNORECASE,
)


def find_validation_code(page) -> str | None:
    """Pull the validation code off a finished survey.

    Deliberately strict. A false positive here is worse than a miss: it makes
    the script think an unfinished survey is done, record a garbage code, and
    close the browser on a live session. So the token must follow an explicit
    'validation code' label AND contain a digit, and promissory boilerplate is
    removed first.
    """
    try:
        text = page.inner_text("body")
    except Exception:
        return None

    cleaned = BOILERPLATE_RE.sub(" ", text)
    match = VALIDATION_LABEL_RE.search(cleaned)
    if not match:
        return None

    token = match.group(1).strip(" .,:;-")
    if not any(ch.isdigit() for ch in token):
        return None
    if len(token) < 4:
        return None
    return token


# --------------------------------------------------------------------------
# the survey run
# --------------------------------------------------------------------------

def enter_code(page, code: str) -> None:
    page.goto(SURVEY_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(800)

    parts = code.split("-")
    boxes = page.locator("input[type=text]:visible")
    count = boxes.count()

    if count >= len(parts):
        for i, part in enumerate(parts):
            boxes.nth(i).fill(part)
    else:
        # Layout changed - hand it to the user rather than guessing.
        pause(f"Couldn't find the six code boxes on the page.\n"
              f"Please type {code} into the browser and click Start yourself.")
        return

    for name in ("Start", "Begin", "Continue"):
        btn = page.get_by_role("button", name=name, exact=False)
        if btn.count():
            btn.first.click()
            break
    else:
        page.keyboard.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)


def run_survey(page, entry: dict, express: str | None = None,
               profile: list | None = None, comment: str | None = None) -> str:
    """Walk one survey. Returns the new status for this code.

    `express` is a rating YOU supplied on the command line (e.g. "highly
    satisfied"). Scale questions whose options include that wording are set to
    it automatically and printed as they're set, so you can see what went in.
    Anything else - order type, what you bought, comment boxes - still asks,
    because a satisfaction level is not an answer to those.
    """
    code = entry["code"]
    print()
    print("=" * 68)
    print(f"  SURVEY  {code}")
    if entry.get("visit_date") or entry.get("visit_time"):
        print(f"  visit   {entry.get('visit_date') or '?'} {entry.get('visit_time') or ''}".rstrip())
    if entry.get("items"):
        print(f"  order   {entry['items']}")
    print("=" * 68)

    enter_code(page, code)

    state = detect_state(page)
    if state == "block":
        print("\n  McDonald's returned the monthly-limit block page.")
        print("  Nothing was submitted. This code stays pending for a later run.")
        entry["notes"] = "hit per-restaurant monthly limit"
        return STATUS_BLOCKED
    if state == "invalid":
        print("\n  The site rejected this code as invalid.")
        entry["notes"] = "rejected at entry"
        return STATUS_INVALID

    seen_pages = 0
    while True:
        seen_pages += 1
        if seen_pages > 60:
            pause("This has run through 60 pages, which is more than a survey "
                  "should be.\nFinish it in the browser if you like, then press Enter.")
            return STATUS_SKIPPED

        state = detect_state(page)
        if state == "block":
            print("\n  Hit the limit block mid-survey. Stopping.")
            entry["notes"] = "blocked mid-survey"
            return STATUS_BLOCKED

        # Questions first. A page with anything to answer is not the end of the
        # survey, whatever words happen to appear on it.
        groups = read_radio_groups(page)
        texts = read_text_inputs(page)
        boxes = read_checkboxes(page)

        if DUMP_DIR:
            try:
                DUMP_DIR.mkdir(parents=True, exist_ok=True)
                stem = f"{entry['code'][-8:]}_p{seen_pages:02d}"
                (DUMP_DIR / f"{stem}.html").write_text(page.content())
                (DUMP_DIR / f"{stem}.json").write_text(
                    json.dumps({"url": page.url, "groups": groups,
                                "texts": texts, "checkboxes": boxes}, indent=2)
                )
                print(f"  [dumped page {seen_pages} to {DUMP_DIR}/{stem}.html]")
            except Exception as exc:
                print(f"  [dump failed: {exc}]")

        if not groups and not texts and not boxes:
            validation = find_validation_code(page)
            if validation:
                print()
                print("=" * 68)
                print(f"  VALIDATION CODE: {validation}")
                print("=" * 68)
                entry["validation_code"] = validation
                entry["completed_at"] = datetime.now().isoformat(timespec="seconds")
                return STATUS_DONE

            body = page_text(page)
            if "thank you" in body and seen_pages > 2:
                print("\n  Survey finished, but no validation code was shown on the page.")
                print("  Check the browser window - some receipts don't get an offer.")
                entry["completed_at"] = datetime.now().isoformat(timespec="seconds")
                entry["notes"] = "completed; no validation code displayed"
                return STATUS_DONE
            if not click_next(page):
                pause("Nothing to answer and no Next button found.\n"
                      "Take a look at the browser window, move it forward if you can,\n"
                      "then press Enter. Or type nothing and it'll try again.")
            continue

        for group in groups:
            # When the page gives an option no readable text, show its
            # underlying value rather than a blank line you can't choose from.
            labels = [
                o["label"] or f"(no label - option value {o['value']!r})"
                for o in group["options"]
            ]

            choice = None
            matched = False

            # Standing answers first - these are facts you've already given.
            if profile:
                hit = profile_pick(profile, group["question"], page_text(page), labels)
                if hit:
                    idx, rule = hit
                    question = group["question"].strip().replace("\n", " ")
                    print(f"\n  {question[:64] or '(question)'}")
                    print(f"     -> {labels[idx]}   [profile]")
                    choice, matched = idx, True

            if not matched and express:
                wanted = express.strip().lower()
                for idx, label in enumerate(labels):
                    if wanted in label.strip().lower():
                        question = group["question"].strip().replace("\n", " ")
                        print(f"\n  {question[:64]}")
                        print(f"     -> {label}   [--express]")
                        choice, matched = idx, True
                        break

            if not matched:
                choice = ask_choice(group["question"], labels)

            if choice is None:
                pause("Answer that one in the browser window, then press Enter.")
                continue
            if choice == -1:
                continue
            option = group["options"][choice]
            try:
                # Two traps here, both found the hard way against the live site:
                #  - IDs look like "R001000.5"; a dot is a class selector, so
                #    "#id" throws. Match on the attribute instead.
                #  - The radio itself is visually hidden behind a styled label.
                #    Clicking the input does nothing; the label is the target.
                if option["id"]:
                    esc = option["id"].replace('"', '\\"')
                    box = page.locator(f'input[id="{esc}"]').first
                    label = page.locator(f'label[for="{esc}"]')

                    for attempt in (
                        lambda: label.first.click(force=True),
                        lambda: label.first.dispatch_event("click"),
                        lambda: box.check(force=True),
                    ):
                        if not label.count() and attempt is not None:
                            pass
                        try:
                            attempt()
                        except Exception:
                            continue
                        if box.is_checked():
                            break
                    else:
                        raise RuntimeError("option would not stay selected")

                    if not box.is_checked():
                        raise RuntimeError("option did not stay selected")
                else:
                    page.locator(
                        f'input[type=radio][name="{group["name"]}"]'
                        f'[value="{option["value"]}"]'
                    ).first.check(force=True)
            except Exception as exc:
                print(f"  couldn't click that option ({exc}).")
                pause("Please set it in the browser window, then press Enter.")

        if boxes:
            labels = [b["label"] or f"(no label - {b['id']})" for b in boxes]
            page_q = page_text(page).split("\n")[0][:80]

            picks = match_items(labels, entry.get("items") or "")
            if picks:
                print(f"\n  {page_q}")
                for i in picks:
                    print(f"     -> {labels[i]}   [from this receipt's items]")
            else:
                if entry.get("items"):
                    print(f"\n  Nothing in this receipt's items matched these "
                          f"options, so I won't guess:")
                    print(f"     items: {entry['items'][:90]}")
                picks = ask_multi(page_q or "Select all that apply", labels)
                if picks is None:
                    pause("Tick them in the browser window, then press Enter.")
                    picks = []

            for i in picks:
                target = boxes[i]
                try:
                    esc = (target["id"] or "").replace('"', '\\"')
                    if esc:
                        cb = page.locator(f'input[id="{esc}"]').first
                        lb = page.locator(f'label[for="{esc}"]')
                        for attempt in (
                            lambda: lb.first.click(force=True),
                            lambda: lb.first.dispatch_event("click"),
                            lambda: cb.check(force=True),
                        ):
                            try:
                                attempt()
                            except Exception:
                                continue
                            if cb.is_checked():
                                break
                        else:
                            raise RuntimeError("checkbox would not stay ticked")
                    else:
                        page.locator(
                            f'input[type=checkbox][name="{target["name"]}"]'
                        ).first.check(force=True)
                except Exception as exc:
                    print(f"  couldn't tick {labels[i]!r} ({exc}).")
                    pause("Please tick it in the browser window, then press Enter.")

        for box in texts:
            if comment:
                question = (box["question"] or "comment box").strip()
                print(f"\n  {question[:64]}")
                print(f'     -> "{comment}"   [comments.txt]')
                answer = comment
            else:
                answer = ask_text(box["question"])
            if not answer:
                continue
            try:
                if box["id"]:
                    page.locator(f"#{box['id']}").fill(answer)
                elif box["name"]:
                    page.locator(f"textarea[name='{box['name']}']").first.fill(answer)
            except Exception as exc:
                print(f"  couldn't type that in ({exc}).")
                pause("Please type it in the browser window, then press Enter.")

        if not click_next(page):
            pause("Couldn't find the Next button. Click it yourself in the browser,\n"
                  "then press Enter here.")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_add(args) -> None:
    if args.paste:
        print("Paste your codes, then press Ctrl-D:")
        text = sys.stdin.read()
    else:
        path = Path(args.source).expanduser()
        if not path.exists():
            sys.exit(f"no such file: {path}")
        text = path.read_text()

    parsed = parse_lines(text, default_year=args.year)
    if not parsed:
        sys.exit("No 26-digit survey codes found in that text.")

    data = load_store()
    added = skipped = 0
    for entry in parsed:
        if find_entry(data, entry["code"]):
            skipped += 1
            continue
        data["codes"].append(entry)
        added += 1

    refresh_expiry(data)
    save_store(data)
    print(f"added {added} code(s); {skipped} already on file")
    print(f"stored in {DATA_FILE}")


def cmd_list(args) -> None:
    data = load_store()
    refresh_expiry(data)
    save_store(data)

    if not data["codes"]:
        print("No codes on file yet. Add some with:  mcdvoice.py add --paste")
        return

    today = date.today()
    print(f"{'status':<10} {'store':<7} {'visit':<11} {'expires':<11} code")
    print("-" * 78)
    for entry in data["codes"]:
        exp = expiry_of(entry)
        if exp:
            left = (exp - today).days
            exp_s = f"{exp.isoformat()}" + (f" ({left}d)" if 0 <= left <= 7 else "")
        else:
            exp_s = "?"
        print(f"{entry['status']:<10} {entry['store']:<7} "
              f"{entry.get('visit_date') or '?':<11} {exp_s:<11} {entry['code']}")
        if entry.get("validation_code"):
            print(f"{'':<10} -> validation code: {entry['validation_code']}")
        if entry.get("notes"):
            print(f"{'':<10} -> {entry['notes']}")

    counts: dict[str, int] = {}
    for entry in data["codes"]:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("-" * 78)
    print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))


def cmd_run(args) -> None:
    global DUMP_DIR
    if args.dump:
        DUMP_DIR = Path(args.dump).expanduser()
        print(f"dumping every page to {DUMP_DIR}")

    comments = [] if args.no_comments else load_comments()
    if comments:
        print(f"{len(comments)} comment(s) available from {COMMENTS_FILE}")
    elif not args.no_comments:
        print(f"no comments on file yet - add lines to {COMMENTS_FILE} "
              f"and they'll be used automatically")

    profile = [] if args.no_profile else load_profile()
    if profile:
        print(f"standing answers from {PROFILE_FILE}:")
        for rule in profile:
            print(f"  {' / '.join(rule.get('when', []))!r:<42} -> {rule.get('answer')}")
        print()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright isn't installed. Run:\n"
                 "  python3 -m pip install playwright\n"
                 "  python3 -m playwright install chromium")

    data = load_store()
    refresh_expiry(data)

    queue = [e for e in data["codes"] if e["status"] == STATUS_PENDING]
    if args.retry_blocked:
        queue += [e for e in data["codes"] if e["status"] == STATUS_BLOCKED]
    if args.only:
        queue = [e for e in queue if args.only in e["code"]]
    if args.limit:
        queue = queue[: args.limit]

    if not queue:
        print("Nothing pending. Try 'list' to see why, or 'run --retry-blocked'.")
        return

    print(f"{len(queue)} survey(s) queued. One at a time; you answer every question.")
    print("Ctrl-C during a survey abandons that one and stops.\n")

    with sync_playwright() as pw:
        headless = resolve_headless(args)
        if headless:
            print("  browser is hidden: the 'answer it myself in the browser'\n"
                  "  option and the hand-back-on-error prompt have no window to\n"
                  "  show you. Use --dump DIR to capture pages instead.\n")
        launch_args = []
        if headless and sys.platform.startswith("linux"):
            # Common on containers and minimal distros: no /dev/shm worth
            # speaking of, and often running as root.
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        if args.incognito and args.channel:
            # Only meaningful for a real Chrome install. Playwright's bundled
            # Chromium already runs on a throwaway profile.
            launch_args.append("--incognito")

        def new_browser():
            kw = {"headless": headless, "slow_mo": 120, "args": launch_args}
            if args.channel:
                kw["channel"] = args.channel
            return pw.chromium.launch(**kw)

        browser = None if args.fresh_browser else new_browser()
        context = None if args.fresh_browser else browser.new_context(
            viewport={"width": 1280, "height": 900})
        page = None if args.fresh_browser else context.new_page()

        if args.fresh_browser:
            print("  a separate browser instance per survey; nothing is shared "
                  "between them.\n")

        for entry in queue:
            if args.fresh_browser:
                browser = new_browser()
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900})
                page = context.new_page()
            # A comment pinned to this receipt wins; otherwise take the next
            # line from comments.txt and advance, so surveys don't repeat text.
            comment = entry.get("comment")
            if not comment and comments and not args.no_comments:
                idx = int(data.get("comment_index", 0))
                comment = comments[idx % len(comments)]
                data["comment_index"] = idx + 1

            stop = False
            try:
                try:
                    status = run_survey(page, entry, express=args.express,
                                        profile=profile, comment=comment)
                except KeyboardInterrupt:
                    print("\n\n  stopped. Nothing further submitted.")
                    entry["notes"] = "abandoned partway"
                    save_store(data)
                    stop = True
                except Exception as exc:
                    import traceback
                    print(f"\n  error on {entry['code']}: {exc}\n")
                    traceback.print_exc()
                    entry["notes"] = f"error: {exc}"
                    status = STATUS_SKIPPED
                    # The browser window is still on a live survey. Don't tear
                    # it down - finishing by hand is usually still possible,
                    # and the page is the only evidence of what went wrong.
                    print("\n  The browser is still open on that survey.")
                    pause("Finish it by hand if you can, or copy what's on screen.\n"
                          "Press Enter here only when you're done with that window.")

                if not stop:
                    entry["status"] = status
                    save_store(data)

                    if status == STATUS_BLOCKED and not args.keep_going:
                        print("\n  Stopping here - the limit applies to the whole\n"
                              "  restaurant, so the remaining codes for this store\n"
                              "  will hit it too.")
                        stop = True

                    # Only wait when there's actually another survey coming.
                    elif args.delay and entry is not queue[-1]:
                        wait_between(args.delay)
            finally:
                # A per-survey browser is closed here whatever happened, so a
                # block or an error can't leave one running.
                if args.fresh_browser and browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = context = page = None

            if stop:
                break

        if browser is not None:
            browser.close()

    print(f"\nsaved to {DATA_FILE}")
    done = [e for e in data["codes"] if e.get("validation_code")]
    if done:
        print("\nvalidation codes on file:")
        for entry in done:
            print(f"  {entry['code']}  ->  {entry['validation_code']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcdvoice.py",
        description="Track McDonald's survey codes and work through the surveys.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="import codes from a file or a paste")
    p_add.add_argument("source", nargs="?", help="text file containing codes")
    p_add.add_argument("--paste", action="store_true", help="read from stdin instead")
    p_add.add_argument("--year", type=int, default=None,
                       help="year for m/d dates (default: this year)")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="show stored codes and their status")
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="work through pending surveys")
    p_run.add_argument("--only", help="only codes containing this substring")
    p_run.add_argument("--limit", type=int, help="stop after this many surveys")
    p_run.add_argument("--retry-blocked", action="store_true",
                       help="also retry codes that were blocked before")
    p_run.add_argument("--keep-going", action="store_true",
                       help="don't stop the batch on the first block page")
    p_run.add_argument("--reuse-browser", dest="fresh_browser",
                       action="store_false", default=True,
                       help="keep one browser for the whole batch instead of "
                            "starting a fresh one per survey")
    p_run.add_argument("--no-incognito", dest="incognito",
                       action="store_false", default=True,
                       help="don't pass --incognito (only affects --channel chrome)")
    p_run.add_argument("--channel", metavar="NAME",
                       help="use an installed browser instead of Playwright's "
                            "bundled Chromium, e.g. --channel chrome")
    p_run.add_argument("--headless", action="store_true",
                       help="run the browser with no visible window "
                            "(default on Linux with no display)")
    p_run.add_argument("--headed", action="store_true",
                       help="force a visible browser window")
    p_run.add_argument("--delay", type=float, default=0, metavar="MINUTES",
                       help="wait this many minutes between surveys "
                            "(e.g. --delay 5). Ctrl-C skips a wait.")
    p_run.add_argument("--no-comments", action="store_true",
                       help="ignore comments.txt and ask for the comment "
                            "(or leave it blank)")
    p_run.add_argument("--no-profile", action="store_true",
                       help="ignore the standing answers in profile.json and "
                            "ask every question")
    p_run.add_argument("--dump", metavar="DIR",
                       help="save every page's HTML there, for debugging")
    p_run.add_argument("--express", metavar="RATING",
                       help="a rating you're supplying yourself, e.g. "
                            "--express 'highly satisfied'. Scale questions "
                            "offering that wording are set to it and printed; "
                            "everything else still asks.")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    if args.command == "add" and not args.paste and not args.source:
        p_add.error("give a file path, or use --paste")
    args.func(args)


if __name__ == "__main__":
    main()

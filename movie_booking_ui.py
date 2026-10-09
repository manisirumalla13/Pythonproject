#!/usr/bin/env python3
"""Movie Ticket Booking System - Web UI launcher (Course End Project A9513).

Starts a small local web server on top of movie_ticket_booking.py (same folder)
and opens a fuller cinema-style UI in its own Chrome window: seat types and
tiered pricing, live seat holds with a countdown, concessions, a mock payment
step and an enforced cancellation policy. Payment is a demo only -- no card
details are collected or stored, and nothing is charged anywhere.

Run:
    python3 movie_booking_ui.py
Options:
    --db PATH        SQLite database path (default: movie_booking.db beside the script)
    --port N         Port to use (default: any free port)
    --no-browser     Only start the server, don't open Chrome
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from movie_ticket_booking import DB_FILE, MovieTicketBookingSystem  # noqa: E402

SYSTEM: MovieTicketBookingSystem | None = None
HOLD_MINUTES = 5
CONVENIENCE_FEE = 20.0
MAX_SEATS = 8

# Promo codes (demo). type: flat|pct, on: seats|conc|all, optional cap / min order.
PROMOS = {
    "FIRST50":   {"type": "flat", "value": 50, "on": "all",   "min": 200,
                  "label": "Rs 50 off on orders of Rs 200+"},
    "PRABHAS10": {"type": "pct",  "value": 10, "on": "seats", "cap": 150,
                  "label": "10% off tickets (max Rs 150)"},
    "SNACK20":   {"type": "pct",  "value": 20, "on": "conc",  "cap": 100,
                  "label": "20% off snacks (max Rs 100) - add snacks first"},
}


def promo_discount(code: str, seat_total: float, conc_total: float) -> float:
    r = PROMOS.get((code or "").strip().upper())
    if not r:
        return 0.0
    base = {"seats": seat_total, "conc": conc_total, "all": seat_total + conc_total}[r["on"]]
    if base <= 0 or base < r.get("min", 0):
        return 0.0
    d = r["value"] if r["type"] == "flat" else base * r["value"] / 100
    if r.get("cap"):
        d = min(d, r["cap"])
    return float(round(min(d, seat_total + conc_total)))


def api_promo(code: str) -> dict:
    code = code.strip().upper()
    r = PROMOS.get(code)
    if not r:
        return {"valid": False, "message": "That promo code isn't valid."}
    return {"valid": True, "rule": r, "message": "Applied: " + r["label"]}


def api_notify(movie_id: int, sid: str) -> dict:
    c = SYSTEM.conn
    had = c.execute("SELECT 1 FROM movie_interest WHERE movie_id=? AND session_id=?", (movie_id, sid)).fetchone()
    if had:
        c.execute("DELETE FROM movie_interest WHERE movie_id=? AND session_id=?", (movie_id, sid))
    else:
        c.execute("INSERT INTO movie_interest VALUES (?,?)", (movie_id, sid))
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM movie_interest WHERE movie_id=?", (movie_id,)).fetchone()[0]
    return {"interested": not had, "count": n}


# ------------------------------------------------------------- Poster images
POSTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posters")
POSTER_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
TMDB_POSTER_PATHS = {
  "pushpa2therule": "/1T21FblunT0y8fz7YaW8JMYgUKm.jpg",
  "kalki2898ad": "/rstcAnBeCkxNQjNp3YXrF6IP1tW.jpg",
  "devarapart1": "/lQfuaXjANoTsdx5iS0gCXlK9D2L.jpg",
  "salaarpart1ceasefire": "/nlu9WbcetNFRGXXPWITr30ob7W6.jpg",
  "stree2": "/nfnhwfUEFuSOxxf4jDdBlY6Lccw.jpg",
  "bhoolbhulaiyaa3": "/3AfHD1HoaQpQwKH8kxRdBKVmzeU.jpg",
  "maharaja": "/s0m4TM1XRAftQStgKpw024RvkJo.jpg",
  "vettaiyan": "/1q0dAC3OJZVKQcV2dG5sGvdUGqN.jpg",
}


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def find_poster(movie_id: int, title: str = "") -> str | None:
    """Find the title-matched TMDB poster, then an exact-title local image."""
    tmdb_path = TMDB_POSTER_PATHS.get(_norm(title))
    if tmdb_path:
      return "https://image.tmdb.org/t/p/w500" + tmdb_path
    if not os.path.isdir(POSTER_DIR):
      return None
    for fname in sorted(os.listdir(POSTER_DIR)):
      stem, ext = os.path.splitext(fname)
      if ext.lower() not in POSTER_EXT:
        continue
      while os.path.splitext(stem)[1].lower() in POSTER_EXT:
        stem = os.path.splitext(stem)[0]
      key = _norm(stem)
      if title and key == _norm(title):
        return "/posters/" + quote(fname)
    return None


# ---------------------------------------------------------- Seats & pricing
SEAT_ROWS = ["A", "B", "C", "D"]
SEAT_TYPE = {
    "A": ("Recliner", 1.8),
    "B": ("Premium", 1.4),
    "C": ("Premium", 1.4),
    "D": ("Standard", 1.0),
}


def seat_row(n: int) -> str:
    return SEAT_ROWS[(n - 1) // 10]


def seat_type_name(n: int) -> str:
    return SEAT_TYPE[seat_row(n)][0]


def seat_price(base: float, n: int) -> float:
    mult = SEAT_TYPE[seat_row(n)][1]
    return round(base * mult / 10) * 10


def seat_label(n: int) -> str:
    return seat_row(n) + str((n - 1) % 10 + 1)


# -------------------------------------------------------- Extra demo tables
# Short, original blurbs written for this demo -- not copied from any source.
# Ratings are sample/demo numbers only, not real audience or critic scores.
MOVIE_DETAILS = {
    1: ("A fearless smuggler rises to power in the red-soil heartland, as a brutal power struggle turns personal.", "Allu Arjun (lead)", "Action drama, intense violence", 4.7),
    2: ("In a distant future, a mysterious prophecy and a dangerous new world pull a chosen few into a cosmic conflict.", "Prabhas, Amitabh Bachchan, Kamal Haasan", "Sci-fi action thriller", 4.5),
    3: ("A determined coastal hero faces a ruthless empire, as loyalty, revenge and destiny collide in a high-stakes showdown.", "Jr. NTR, Janhvi Kapoor", "Action thriller, stylised violence", 4.4),
    4: ("A quiet man becomes the unlikely defender of his city when old enemies test his loyalties and strength.", "Prabhas (lead)", "Contains intense violence", 4.1),
    5: ("A prankster in the city gets pulled back into a chaotic, funny and frightening battle against a monster of the past.", "Shraddha Kapoor, Rajkummar Rao", "Comedy horror", 4.3),
    6: ("A family returns to their ancestral home, only to awaken a centuries-old curse with a very theatrical twist.", "Kartik Aaryan, Vidya Balan", "Comedy horror", 4.2),
    7: ("A retired king confronts a dangerous underworld and a deadly personal reckoning in a gritty vigilante thriller.", "Vijay Sethupathi", "Action thriller", 4.0),
    8: ("A hard-nosed officer uncovers a criminal network while protecting a family in a dangerous city landscape.", "Rajinikanth (lead)", "Action drama", 4.0),
    9: ("A fearless smuggler rises to power in the red-soil heartland, as a brutal power struggle turns personal.", "Allu Arjun (lead)", "Action drama, intense violence", 4.7),
    10: ("A fearless smuggler rises to power in the red-soil heartland, as a brutal power struggle turns personal.", "Allu Arjun (lead)", "Action drama, intense violence", 4.7),
    11: ("In a distant future, a mysterious prophecy and a dangerous new world pull a chosen few into a cosmic conflict.", "Prabhas, Amitabh Bachchan, Kamal Haasan", "Sci-fi action thriller", 4.5),
    12: ("In a distant future, a mysterious prophecy and a dangerous new world pull a chosen few into a cosmic conflict.", "Prabhas, Amitabh Bachchan, Kamal Haasan", "Sci-fi action thriller", 4.5),
    13: ("A determined coastal hero faces a ruthless empire, as loyalty, revenge and destiny collide in a high-stakes showdown.", "Jr. NTR, Janhvi Kapoor", "Action thriller, stylised violence", 4.4),
    14: ("A determined coastal hero faces a ruthless empire, as loyalty, revenge and destiny collide in a high-stakes showdown.", "Jr. NTR, Janhvi Kapoor", "Action thriller, stylised violence", 4.4),
    15: ("A quiet man becomes the unlikely defender of his city when old enemies test his loyalties and strength.", "Prabhas (lead)", "Contains intense violence", 4.1),
}

CONCESSION_ITEMS = [
    (1, "Popcorn (Regular)", 150.0, "Snacks"),
    (2, "Popcorn (Large)", 220.0, "Snacks"),
    (3, "Nachos & Cheese", 210.0, "Snacks"),
    (4, "Soft Drink (Regular)", 120.0, "Beverages"),
    (5, "Soft Drink (Large)", 160.0, "Beverages"),
    (6, "Combo: Popcorn + 2 Drinks", 380.0, "Combos"),
]


def ensure_extra_schema() -> None:
    conn = SYSTEM.conn
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS concession_items (
        item_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        price REAL NOT NULL, category TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS movie_details (
        movie_id INTEGER PRIMARY KEY, synopsis TEXT, "cast" TEXT,
        content_note TEXT, rating REAL
    );
    CREATE TABLE IF NOT EXISTS seat_holds (
        show_id INTEGER NOT NULL, seat INTEGER NOT NULL,
        session_id TEXT NOT NULL, expires_at TEXT NOT NULL,
        PRIMARY KEY (show_id, seat)
    );
    CREATE TABLE IF NOT EXISTS booking_extras (
        booking_id TEXT PRIMARY KEY, extras TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
      username TEXT PRIMARY KEY COLLATE NOCASE,
      password_hash TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS auth_sessions (
      session_id TEXT PRIMARY KEY,
      username TEXT NOT NULL COLLATE NOCASE
    );
    CREATE TABLE IF NOT EXISTS booking_owners (
      booking_id TEXT PRIMARY KEY,
      username TEXT NOT NULL COLLATE NOCASE
    );
    CREATE TABLE IF NOT EXISTS movie_interest (
      movie_id INTEGER NOT NULL, session_id TEXT NOT NULL,
      PRIMARY KEY (movie_id, session_id)
    );
    """)
    if not conn.execute("SELECT COUNT(*) FROM concession_items").fetchone()[0]:
        conn.executemany("INSERT INTO concession_items VALUES (?,?,?,?)", CONCESSION_ITEMS)
    conn.executemany(
        "INSERT OR REPLACE INTO movie_details VALUES (?,?,?,?,?)",
        [(mid, syn, cast, note, rating) for mid, (syn, cast, note, rating) in MOVIE_DETAILS.items()],
    )
    conn.commit()


def now() -> datetime:
    return datetime.now()


def parse_show_dt(show_date: str, show_time: str) -> datetime:
    return datetime.strptime(f"{show_date} {show_time}", "%Y-%m-%d %I:%M %p")


def cleanup_holds(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM seat_holds WHERE expires_at < ?", (now().isoformat(),))
    conn.commit()


def _password_hash(password: str, salt: bytes | None = None) -> str:
  salt = salt or secrets.token_bytes(16)
  digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
  return f"{salt.hex()}${digest.hex()}"


def _password_matches(password: str, stored_hash: str) -> bool:
  try:
    salt_hex, digest_hex = stored_hash.split("$", 1)
    salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
  except (ValueError, TypeError):
    return False
  actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
  return hmac.compare_digest(actual, expected)


def api_register(data: dict) -> dict:
  username = str(data.get("username", "")).strip()
  password = str(data.get("password", ""))
  if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
    raise ValueError("Username must be 3-32 characters using letters, numbers, ., _ or -.")
  if len(password) < 8:
    raise ValueError("Password must be at least 8 characters.")
  if SYSTEM.conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
    raise ValueError("That username is already taken.")
  SYSTEM.conn.execute(
    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
    (username, _password_hash(password)),
  )
  SYSTEM.conn.commit()
  return {"created": True, "username": username}


def api_login(data: dict, sid: str) -> dict:
  username = str(data.get("username", "")).strip()
  password = str(data.get("password", ""))
  row = SYSTEM.conn.execute(
    "SELECT username, password_hash FROM users WHERE username=?", (username,)
  ).fetchone()
  if not row or not _password_matches(password, row["password_hash"]):
    raise ValueError("Incorrect username or password.")
  SYSTEM.conn.execute(
    "INSERT OR REPLACE INTO auth_sessions (session_id, username) VALUES (?, ?)",
    (sid, row["username"]),
  )
  SYSTEM.conn.commit()
  return {"authenticated": True, "username": row["username"]}


def authenticated_username(sid: str) -> str | None:
  row = SYSTEM.conn.execute(
    "SELECT username FROM auth_sessions WHERE session_id=?", (sid,)
  ).fetchone()
  return row["username"] if row else None


# ----------------------------------------------------------------- API layer
def api_shows(sid: str = "", city: str | None = None) -> list[dict]:
    cleanup_holds(SYSTEM.conn)
    q = """
        SELECT s.show_id, s.movie_id, m.title, m.language, m.genre, m.certificate,
               m.duration_min, s.show_date, s.show_time,
               COALESCE(s.city, 'Hyderabad') AS city,
               COALESCE(s.theatre, 'Prabhas Cinemas') AS theatre,
               s.screen, s.ticket_price, s.total_seats
        FROM shows s JOIN movies m ON s.movie_id = m.movie_id
    """
    params: list[object] = []
    if city:
        q += " AND s.city = ?"
        params.append(city)
    q += " ORDER BY s.show_date, s.show_id"
    rows = SYSTEM.conn.execute(q, params).fetchall()
    today = now()
    out = []
    for r in rows:
        d = dict(r)
        d["city"] = d.get("city") or "Hyderabad"
        d["theatre"] = d.get("theatre") or "Prabhas Cinemas"
        d["screen_name"] = d.get("screen") or "Screen 1"
        d["screen_label"] = f"{d['theatre']} • {d['screen_name']}"
        d["format"] = "IMAX" if "IMAX" in (d["theatre"] + " " + d["screen_name"]).upper() or "Screen 3" in d["screen_name"] else "2D"
        f = find_poster(d["movie_id"], d["title"])
        d["poster"] = f

        det = SYSTEM.conn.execute(
            'SELECT synopsis, "cast", content_note, rating FROM movie_details WHERE movie_id=?',
            (d["movie_id"],),
        ).fetchone()
        if not det:
          det = SYSTEM.conn.execute("""
            SELECT md.synopsis, md."cast", md.content_note, md.rating
            FROM movie_details md
            JOIN movies original ON original.movie_id=md.movie_id
            WHERE original.title=? AND original.language='Telugu'
            LIMIT 1
          """, (d["title"],)).fetchone()
        d.update(dict(det) if det else {"synopsis": "", "cast": "", "content_note": "", "rating": None})

        show_dt = parse_show_dt(d["show_date"], d["show_time"])
        d["status"] = "now_showing" if show_dt.date() <= (today + timedelta(days=3)).date() else "coming_soon"

        booked = SYSTEM.conn.execute(
            "SELECT seats FROM bookings WHERE show_id=? AND status='CONFIRMED'", (d["show_id"],)
        ).fetchall()
        booked_count = sum(len(b["seats"].split(",")) for b in booked if b["seats"])
        total_seats = max(int(d["total_seats"] or 40), 1)
        held_count = SYSTEM.conn.execute(
          "SELECT COUNT(*) FROM seat_holds WHERE show_id=? AND expires_at>?",
          (d["show_id"], now().isoformat()),
        ).fetchone()[0]
        d["total_seats"] = total_seats
        d["available_seats"] = max(total_seats - booked_count - held_count, 0)
        d["fast_filling"] = d["status"] == "now_showing" and booked_count / total_seats >= 0.5
        d["interest_count"] = SYSTEM.conn.execute(
            "SELECT COUNT(*) FROM movie_interest WHERE movie_id=?", (d["movie_id"],)).fetchone()[0]
        d["interested"] = bool(SYSTEM.conn.execute(
            "SELECT 1 FROM movie_interest WHERE movie_id=? AND session_id=?", (d["movie_id"], sid)).fetchone())
        out.append(d)

    return sorted(out, key=lambda r: (parse_show_dt(r["show_date"], r["show_time"]), r["show_id"]))


def api_concessions() -> list[dict]:
    rows = SYSTEM.conn.execute(
        "SELECT item_id, name, price, category FROM concession_items ORDER BY item_id"
    ).fetchall()
    return [dict(r) for r in rows]


def _seat_rows(show_id: int, sid: str):
    row = SYSTEM.conn.execute("SELECT ticket_price, total_seats FROM shows WHERE show_id=?", (show_id,)).fetchone()
    if not row:
        raise ValueError("Show ID not found")
    base, total = row["ticket_price"], row["total_seats"]

    booked = set()
    for b in SYSTEM.conn.execute("SELECT seats FROM bookings WHERE show_id=? AND status='CONFIRMED'", (show_id,)):
        booked.update(int(x) for x in b["seats"].split(",") if x)

    holds = {
        h["seat"]: h["session_id"]
        for h in SYSTEM.conn.execute("SELECT seat, session_id FROM seat_holds WHERE show_id=?", (show_id,))
    }

    seats = []
    for n in range(1, total + 1):
        if n in booked:
            status = "booked"
        elif n in holds:
            status = "held_you" if holds[n] == sid else "held_other"
        else:
            status = "free"
        seats.append({
            "n": n, "label": seat_label(n), "type": seat_type_name(n),
            "price": seat_price(base, n), "status": status,
        })
    return seats, base


def api_seats(show_id: int, sid: str) -> dict:
    cleanup_holds(SYSTEM.conn)
    seats, base = _seat_rows(show_id, sid)
    mine = SYSTEM.conn.execute(
        "SELECT MAX(expires_at) AS e FROM seat_holds WHERE show_id=? AND session_id=?", (show_id, sid)
    ).fetchone()["e"]
    return {"base_price": base, "seats": seats, "hold_expires_at": mine, "fee": CONVENIENCE_FEE}


def api_hold(show_id: int, wanted: list[int], sid: str) -> dict:
    cleanup_holds(SYSTEM.conn)
    seats, _ = _seat_rows(show_id, sid)
    by_n = {s["n"]: s for s in seats}
    wanted = sorted(set(int(x) for x in wanted))
    if len(wanted) > MAX_SEATS:
        raise ValueError(f"You can hold up to {MAX_SEATS} seats at a time.")
    conflicts = [n for n in wanted if by_n.get(n, {}).get("status") not in ("free", "held_you")]
    if conflicts:
        return {"ok": False, "conflicts": conflicts}

    conn = SYSTEM.conn
    conn.execute("DELETE FROM seat_holds WHERE show_id=? AND session_id=?", (show_id, sid))
    expires = (now() + timedelta(minutes=HOLD_MINUTES)).isoformat()
    conn.executemany(
        "INSERT INTO seat_holds VALUES (?,?,?,?)",
        [(show_id, n, sid, expires) for n in wanted],
    )
    conn.commit()
    return {"ok": True, "expires_at": expires}


def api_release(show_id: int, sid: str) -> None:
    SYSTEM.conn.execute("DELETE FROM seat_holds WHERE show_id=? AND session_id=?", (show_id, sid))
    SYSTEM.conn.commit()


def api_book(data: dict, sid: str) -> dict:
    username = authenticated_username(sid)
    if not username:
        raise ValueError("Sign in before booking tickets.")
    show_id = int(data["show_id"])
    customer, phone = data.get("customer", "").strip(), data.get("phone", "").strip()
    if not customer:
        raise ValueError("Enter the customer name.")
    if not (phone.isdigit() and len(phone) == 10):
        raise ValueError("Enter a 10-digit phone number.")
    payment_method = data.get("payment_method", "UPI")
    if payment_method not in ("UPI", "Card", "Pay at Counter"):
        payment_method = "UPI"

    cleanup_holds(SYSTEM.conn)
    seats, base = _seat_rows(show_id, sid)
    by_n = {s["n"]: s for s in seats}
    wanted = sorted(set(int(x) for x in data.get("seats", [])))
    if not wanted:
        raise ValueError("Choose at least one seat.")
    if len(wanted) > MAX_SEATS:
        raise ValueError(f"You can book up to {MAX_SEATS} seats at a time.")
    unavailable = [n for n in wanted if by_n.get(n, {}).get("status") not in ("free", "held_you")]
    if unavailable:
        raise ValueError(f"Seat(s) no longer available: {[seat_label(n) for n in unavailable]}")

    seat_total = sum(by_n[n]["price"] for n in wanted)
    conc_lines, conc_total = [], 0.0
    items_by_id = {i["item_id"]: i for i in api_concessions()}
    for line in data.get("concessions", []):
        item = items_by_id.get(int(line.get("item_id", -1)))
        qty = int(line.get("qty", 0))
        if item and qty > 0:
            subtotal = item["price"] * qty
            conc_lines.append({"item_id": item["item_id"], "name": item["name"], "price": item["price"], "qty": qty, "subtotal": subtotal})
            conc_total += subtotal

    promo_code = str(data.get("promo", "")).strip().upper()
    discount = promo_discount(promo_code, seat_total, conc_total)
    total = seat_total + conc_total + CONVENIENCE_FEE - discount

    # SYSTEM.book_tickets does the real seat/range/double-booking checks and
    # creates the row; we then correct the amount for tiered pricing + extras.
    result = SYSTEM.book_tickets(show_id, customer, phone, wanted)
    extras = {"concessions": conc_lines, "fee": CONVENIENCE_FEE, "payment_method": payment_method, "seat_total": seat_total,
              "promo": promo_code if discount else "", "discount": discount}
    SYSTEM.conn.execute("UPDATE bookings SET amount=? WHERE booking_id=?", (total, result["booking_id"]))
    SYSTEM.conn.execute("INSERT INTO booking_owners VALUES (?,?)", (result["booking_id"], username))
    SYSTEM.conn.execute(
        "INSERT INTO booking_extras VALUES (?,?)", (result["booking_id"], json.dumps(extras))
    )
    SYSTEM.conn.execute("DELETE FROM seat_holds WHERE show_id=? AND session_id=?", (show_id, sid))
    SYSTEM.conn.commit()

    result["amount"] = total
    result["extras"] = extras
    result["seat_labels"] = [seat_label(n) for n in wanted]
    return result


def _booking_dict(row) -> dict:
    d = dict(row)
    ex = SYSTEM.conn.execute(
        "SELECT extras FROM booking_extras WHERE booking_id=?", (d["booking_id"],)
    ).fetchone()
    try:
        d["extras"] = json.loads(ex["extras"]) if ex else {}
    except (TypeError, ValueError):
        d["extras"] = {}
    d["seat_labels"] = [seat_label(int(n)) for n in d["seats"].split(",") if n]
    return d


def api_booking(booking_id: str):
    row = SYSTEM.find_booking(booking_id.strip())
    return _booking_dict(row) if row else None


def api_my_bookings(username: str) -> list[dict]:
    rows = SYSTEM.conn.execute("""
        SELECT b.*, m.title, s.show_date, s.show_time, s.screen
        FROM bookings b
        JOIN shows s ON b.show_id=s.show_id
        JOIN movies m ON s.movie_id=m.movie_id
        JOIN booking_owners o ON o.booking_id=b.booking_id
        WHERE o.username=?
        ORDER BY b.booked_at DESC, b.booking_id DESC
    """, (username,)).fetchall()
    return [_booking_dict(row) for row in rows]


def api_cancel(booking_id: str) -> None:
    row = SYSTEM.find_booking(booking_id.strip())
    if not row:
        raise ValueError("Booking not found.")
    if row["status"] != "CONFIRMED":
        raise ValueError("This booking is already cancelled.")
    show_dt = parse_show_dt(row["show_date"], row["show_time"])
    if now() >= show_dt - timedelta(hours=2):
        raise ValueError("Cancellations aren't allowed within 2 hours of the showtime.")
    SYSTEM.cancel_booking(booking_id.strip())


# --------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep the console quiet
        pass

    def _session(self) -> tuple[str, bool]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "sid" in cookie:
            return cookie["sid"].value, False
        return secrets.token_hex(16), True

    def _send(self, code: int, body: bytes, ctype: str, new_sid: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if new_sid:
          self.send_header("Set-Cookie", f"sid={new_sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200, new_sid: str | None = None) -> None:
        self._send(code, json.dumps(data).encode(), "application/json", new_sid)

    def do_GET(self):
        sid, is_new = self._session()
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8", sid if is_new else None)
            elif url.path.startswith("/posters/"):
                name = os.path.basename(unquote(url.path))
                ext = os.path.splitext(name)[1].lower()
                full = os.path.join(POSTER_DIR, name)
                if ext in POSTER_EXT and os.path.isfile(full):
                    with open(full, "rb") as fh:
                        self._send(200, fh.read(), POSTER_EXT[ext])
                else:
                    self._json({"error": "Not found"}, 404)
            elif url.path == "/api/shows":
                city = q.get("city", [None])[0]
                self._json(api_shows(sid, city), 200, sid if is_new else None)
            elif url.path == "/api/promo":
                self._json(api_promo(q.get("code", [""])[0]))
            elif url.path == "/api/concessions":
                self._json(api_concessions())
            elif url.path == "/api/auth":
                username = authenticated_username(sid)
                self._json({"authenticated": bool(username), "username": username})
            elif url.path == "/api/seats":
                self._json(api_seats(int(q["show_id"][0]), sid), 200, sid if is_new else None)
            elif url.path == "/api/booking":
                b = api_booking(q["id"][0])
                self._json(b if b else {"error": "No booking found with that ID."}, 200 if b else 404)
            elif url.path == "/api/my-bookings":
              username = authenticated_username(sid)
              if not username:
                self._json({"error": "Log in to see your bookings."}, 401)
              else:
                self._json(api_my_bookings(username))
            else:
                self._json({"error": "Not found"}, 404)
        except (ValueError, KeyError):
            self._json({"error": "Invalid request."}, 400)
        except sqlite3.Error:
            self._json({"error": "Database error. Please try again."}, 500)

    def do_POST(self):
        sid, is_new = self._session()
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/hold":
                r = api_hold(int(data["show_id"]), data.get("seats", []), sid)
                self._json(r, 200 if r["ok"] else 409, sid if is_new else None)
            elif self.path == "/api/notify":
                self._json(api_notify(int(data["movie_id"]), sid), 200, sid if is_new else None)
            elif self.path == "/api/release":
                api_release(int(data["show_id"]), sid)
                self._json({"ok": True})
            elif self.path == "/api/register":
                self._json(api_register(data), 201, sid if is_new else None)
            elif self.path == "/api/login":
                self._json(api_login(data, sid), 200, sid if is_new else None)
            elif self.path == "/api/logout":
                SYSTEM.conn.execute("DELETE FROM auth_sessions WHERE session_id=?", (sid,))
                SYSTEM.conn.commit()
                self._json({"authenticated": False})
            elif self.path == "/api/book":
                if not authenticated_username(sid):
                    self._json({"error": "Sign in before booking tickets."}, 401, sid if is_new else None)
                    return
                self._json(api_book(data, sid), 200, sid if is_new else None)
            elif self.path == "/api/cancel":
                api_cancel(str(data.get("booking_id", "")))
                self._json({"cancelled": True})
            else:
                self._json({"error": "Not found"}, 404)
        except sqlite3.IntegrityError:
            self._json({"error": "Could not create the booking ID. Please try again."}, 409)
        except sqlite3.Error:
            self._json({"error": "Database error. Please try again."}, 500)
        except (KeyError, json.JSONDecodeError):
            self._json({"error": "Invalid request."}, 400)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)


# ------------------------------------------------------------ Chrome launcher
def find_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def open_window(url: str) -> None:
    chrome = find_chrome()
    if chrome:
        subprocess.Popen([chrome, f"--app={url}", "--window-size=1320,880"])
    else:
        webbrowser.open_new(url)


def main() -> None:
    global SYSTEM
    p = argparse.ArgumentParser(description="Movie Ticket Booking - Web UI")
    p.add_argument("--db", default=DB_FILE)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    SYSTEM = MovieTicketBookingSystem(args.db)
    ensure_extra_schema()

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Booking UI running at {url}  (Ctrl+C to stop)")
    os.makedirs(POSTER_DIR, exist_ok=True)
    print(f"Poster images folder: {POSTER_DIR}")
    for m in SYSTEM.conn.execute("SELECT movie_id, title FROM movies ORDER BY movie_id"):
        state = find_poster(m["movie_id"], m["title"]) or "NOT FOUND -> save as posters/%d.jpg" % m["movie_id"]
        print(f"  {m['movie_id']}: {m['title']:<40} {state}")

    if not args.no_browser:
        threading.Timer(0.6, open_window, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        SYSTEM.close()


# ------------------------------------------------------------------ Front end
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TicketMaster &middot; Book tickets</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Hind:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --night:#0E1330; --night2:#171E48; --paper:#FBEFD3; --ink:#1A1530;
  --kumkum:#D62839; --haldi:#F6B21B; --jade:#1F9D8B; --mute:#9AA3D1;
  --violet:#8B5CF6; --teal:#14B8A6;
  --display:'Anton','Impact','Arial Narrow',sans-serif;
  --body:'Hind','Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{font-family:var(--body);background:#09090D;color:#EEF0FF;
  background-image:
    radial-gradient(ellipse 72% 42% at 50% 0%,rgba(157,35,48,.32),transparent 72%),
    radial-gradient(ellipse 38% 32% at 14% 38%,rgba(191,111,42,.13),transparent 76%),
    radial-gradient(ellipse 42% 36% at 88% 46%,rgba(126,30,52,.17),transparent 78%),
    linear-gradient(180deg,#211219 0%,#100D12 48%,#09090D 100%);
  background-attachment:fixed;min-height:100vh}
button{font:inherit;cursor:pointer}
:focus-visible{outline:3px solid var(--haldi);outline-offset:2px}

header{display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding:14px 28px;border-bottom:3px solid var(--haldi);background:var(--kumkum)}
.brand{font-family:var(--display);font-size:34px;letter-spacing:1.5px;color:var(--paper);line-height:1}
.brand small{display:block;font-family:var(--body);font-size:13px;font-weight:600;letter-spacing:.3px;color:#FFD9DD;margin-top:4px}
nav{display:flex;gap:6px;background:rgba(0,0,0,.25);padding:4px;border-radius:999px}
nav button{border:0;background:transparent;color:var(--paper);padding:8px 18px;border-radius:999px;font-weight:600}
nav button[aria-selected=true]{background:var(--paper);color:var(--kumkum)}

main{max-width:1240px;margin:0 auto;padding:26px 28px 60px}
.view{display:none}.view.on{display:block}
h2{font-family:var(--display);font-weight:400;font-size:44px;letter-spacing:1px;color:var(--paper)}
.sub{color:var(--mute);margin:6px 0 20px}

.layout{display:grid;grid-template-columns:minmax(0,1fr) 440px;gap:28px;align-items:start}
@media(max-width:980px){.layout{grid-template-columns:1fr}}

.location-note{margin:0 0 10px;padding:8px 10px;border-radius:10px;background:rgba(82,127,255,.12);border:1px solid rgba(142,184,255,.35);color:#DEE7FF;font-size:12.5px;line-height:1.4}
.location-note strong{color:#FFF4D3}
.demo-inventory{margin:0 0 14px;padding:8px 10px;border-left:3px solid var(--haldi);background:rgba(246,178,27,.1);font-size:12px;color:#FFE7A6}
.shows{display:grid;gap:14px}
.show{display:grid;grid-template-columns:118px 1fr auto;border-radius:14px;overflow:hidden;
  background:var(--night2);border:2px solid transparent;text-align:left;color:inherit;padding:0;width:100%}
.show:hover{border-color:#3A4499}
.show[aria-pressed=true]{border-color:var(--haldi);box-shadow:0 0 0 4px rgba(246,178,27,.18)}
.poster{position:relative;display:flex;align-items:flex-end;padding:10px;min-height:128px;
  font-family:var(--display);font-size:22px;line-height:1;color:#fff;letter-spacing:.5px;
  text-shadow:0 2px 0 rgba(0,0,0,.35);background-size:cover;background-position:center top}
.poster::after{content:"";position:absolute;inset:0;
  background:repeating-linear-gradient(115deg,rgba(255,255,255,.07) 0 6px,transparent 6px 14px);pointer-events:none}
.poster.photo::after{background:linear-gradient(to top,rgba(8,10,30,.92) 0,rgba(8,10,30,.25) 65%,transparent 100%)}
.poster span{position:relative;z-index:1}
.info{padding:14px 16px;display:flex;flex-direction:column;gap:6px;justify-content:center;min-width:0}
.info h3{font-size:19px;font-weight:700;color:#fff}
.tags{display:flex;flex-wrap:wrap;gap:6px}
.tag{font-size:12.5px;padding:2px 9px;border-radius:999px;background:rgba(255,255,255,.09);color:#D5DAFF}
.tag.availability{background:rgba(31,157,139,.2);color:#74D5C4;font-weight:700}
.tag.availability.low{background:rgba(246,178,27,.2);color:var(--haldi)}
.tag.availability.sold{background:var(--kumkum);color:#fff}
.tag.cert{background:var(--haldi);color:var(--ink);font-weight:700}
.tag.soon{background:transparent;border:1.5px solid var(--mute);color:var(--mute)}
.tag.fast{background:var(--kumkum);color:#fff;font-weight:700}
.tag.rating{background:rgba(246,178,27,.18);color:var(--haldi);font-weight:700}
.when{font-size:14px;color:var(--mute)}
.when b{color:#fff;font-weight:600}
.price{padding:14px 18px;display:flex;flex-direction:column;justify-content:center;align-items:flex-end}
.price strong{font-family:var(--display);font-weight:400;font-size:30px;color:var(--haldi)}
.price span{font-size:12.5px;color:var(--mute)}
.synopsis{font-size:13.5px;color:var(--mute);margin-top:2px;line-height:1.4}
.synopsis b{color:#D5DAFF}

.panel{position:sticky;top:18px;background:var(--paper);color:var(--ink);border-radius:18px;padding:22px;max-height:calc(100vh - 36px);overflow-y:auto}
@media(max-width:980px){.panel{position:static;max-height:none}}
.panel h3{font-family:var(--display);font-weight:400;font-size:26px;letter-spacing:.5px}
.panel .meta{font-size:14px;color:#5B5670;margin:2px 0 14px}
.empty{padding:34px 8px;text-align:center;color:#5B5670}
.empty b{display:block;font-family:var(--display);font-weight:400;font-size:26px;color:var(--ink);margin-bottom:6px}
.banner{height:120px;border-radius:12px;margin-bottom:12px;background-size:cover;background-position:center 20%;position:relative;overflow:hidden}
.banner::after{content:"";position:absolute;inset:0;background:linear-gradient(to top,rgba(8,10,30,.85),transparent 70%)}
.banner h3{position:absolute;left:14px;bottom:10px;z-index:1;color:#fff;font-size:26px}

.notice{background:#FCE4C8;border:1.5px solid #E3B36B;color:#6B4A16;border-radius:10px;padding:12px 14px;font-size:13.5px;margin:10px 0}
.notice.warn{background:#FBD7D9;border-color:var(--kumkum);color:#7A1420}

.screen{height:34px;margin:6px 26px 18px;border-radius:50% 50% 0 0/100% 100% 0 0;
  background:linear-gradient(#fff,#F2D999);border-top:4px solid var(--haldi);
  box-shadow:0 -14px 26px -6px rgba(246,178,27,.55);text-align:center;font-size:11px;
  font-weight:700;letter-spacing:3px;color:#8C6A16;padding-top:10px}
.rows{display:grid;gap:8px;justify-content:center}
.row{display:grid;grid-template-columns:18px repeat(10,1fr);gap:5px;align-items:center}
.row i{font-style:normal;font-weight:700;font-size:12px;color:#8A849C}
.seat{aspect-ratio:1;min-width:0;border:0;border-radius:7px 7px 3px 3px;color:#fff;font-size:11px;font-weight:600;
  padding:0;box-shadow:inset 0 -3px 0 rgba(0,0,0,.2);background:var(--jade)}
.seat.type-Recliner{background:var(--violet)}
.seat.type-Premium{background:var(--teal)}
.seat:hover:not(:disabled){transform:translateY(-2px)}
.seat[aria-pressed=true]{background:var(--kumkum)!important}
.seat.other{background:#E7A8AE;color:#7A1420;cursor:not-allowed;box-shadow:none}
.seat:disabled{background:#CFC7B5;color:#A39B87;cursor:not-allowed;box-shadow:none}
.legend{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;font-size:12px;color:#5B5670;margin:14px 0}
.legend span::before{content:"";display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px;background:var(--c)}

.timerbar{background:#fff;border:1.5px solid #E3D6B4;border-radius:10px;padding:9px 12px;font-size:13px;font-weight:600;
  display:flex;justify-content:space-between;align-items:center;margin:10px 0}
.timerbar .bar{position:relative;height:5px;background:#EFE3C4;border-radius:99px;overflow:hidden;flex:1;margin-left:12px}
.timerbar .bar i{position:absolute;inset:0;background:var(--kumkum);transform-origin:left;border-radius:99px}

.section-label{font-size:12.5px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#8C6A16;margin:16px 0 6px}
.concessions{display:grid;gap:8px}
.conc-row{display:flex;align-items:center;gap:10px;background:#fff;border:1.5px solid #E3D6B4;border-radius:10px;padding:8px 10px}
.conc-row .name{flex:1;font-weight:600;font-size:13.5px}
.conc-row .cat{display:block;font-weight:400;color:#8C6A16;font-size:11.5px}
.conc-row .price{font-size:12.5px;color:#5B5670}
.stepper{display:flex;align-items:center;gap:6px}
.stepper button{width:24px;height:24px;border-radius:6px;border:1.5px solid #CDBE98;background:#fff;font-weight:700}
.stepper span{min-width:16px;text-align:center;font-weight:700}

form{display:grid;gap:10px;margin-top:4px}
label{font-size:13px;font-weight:600;color:#5B5670;display:grid;gap:4px}
input{font:inherit;padding:10px 12px;border:2px solid #E3D6B4;background:#fff;border-radius:9px;color:var(--ink)}
input:focus{border-color:var(--kumkum);outline:0}
.account-bar{display:flex;align-items:center;gap:10px;color:#fff;font-size:13px;font-weight:600}
.account-bar button{background:transparent;border:1px solid rgba(255,255,255,.65);border-radius:7px;color:#fff;padding:5px 9px;font-size:12px}
.paymethods{display:grid;gap:8px}
.paymethods label{flex-direction:row;align-items:center;gap:8px;background:#fff;border:1.5px solid #E3D6B4;border-radius:10px;padding:10px 12px;font-weight:600;color:var(--ink);display:flex}
.paymethods input{width:auto;padding:0}
.breakdown{display:grid;gap:4px;font-size:13.5px;color:#5B5670;margin:6px 0}
.breakdown .line{display:flex;justify-content:space-between}
.total{display:flex;justify-content:space-between;align-items:baseline;border-top:2px dashed #CDBE98;padding-top:12px;margin-top:4px}
.total b{font-family:var(--display);font-weight:400;font-size:30px}
.btn{border:0;border-radius:10px;padding:13px 18px;font-weight:700;font-size:16px;background:var(--kumkum);color:#fff}
.btn:hover:not(:disabled){filter:brightness(1.08)}
.btn:disabled{background:#CFC7B5;color:#8E8672;cursor:not-allowed}
.btn.ghost{background:transparent;color:var(--kumkum);border:2px solid var(--kumkum)}
.btn.dark{background:var(--night)}
.btn.link{background:none;border:0;color:#7A5A16;font-weight:600;text-decoration:underline;padding:4px 0;font-size:13px}
.err{color:#B3121F;font-weight:600;font-size:14px;min-height:20px}

.lookup{display:flex;gap:10px;max-width:560px;margin-bottom:10px}
.lookup input{flex:1;background:var(--night2);border-color:#3A4499;color:#fff;text-transform:uppercase}
.booking-list{display:grid;gap:20px;max-width:760px}
.booking-entry{padding-bottom:20px;border-bottom:1px solid rgba(255,255,255,.18)}
.booking-entry .actions{margin-top:12px}
.policylink{background:none;border:0;color:var(--mute);text-decoration:underline;font-size:13px;margin-bottom:26px;padding:0}

.ticket{display:grid;grid-template-columns:1fr 150px;max-width:660px;background:var(--paper);color:var(--ink);
  border-radius:16px;position:relative;filter:drop-shadow(0 12px 24px rgba(0,0,0,.4))}
.ticket::before,.ticket::after{content:"";position:absolute;right:139px;width:22px;height:22px;border-radius:50%;background:var(--night)}
.ticket::before{top:-11px}.ticket::after{bottom:-11px}
.t-main{padding:20px 22px}
.t-main .lab{font-size:12px;color:#7A7490;font-weight:600}
.t-main h4{font-family:var(--display);font-weight:400;font-size:30px;line-height:1.05;margin:2px 0 12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px}
.grid2 b{display:block;font-size:16px}
.t-extras{border-top:1.5px dashed #CDBE98;margin-top:12px;padding-top:10px;font-size:12.5px;color:#5B5670}
.t-extras .line{display:flex;justify-content:space-between}
.t-stub{border-left:3px dashed #CDBE98;padding:22px 14px;display:flex;flex-direction:column;justify-content:space-between;align-items:center;text-align:center}
.status{font-family:var(--display);font-size:20px;letter-spacing:1px;padding:2px 10px;border:3px solid var(--jade);color:var(--jade);border-radius:6px;transform:rotate(-8deg)}
.status.x{border-color:var(--kumkum);color:var(--kumkum)}
.bars{width:100%;height:54px;background:repeating-linear-gradient(90deg,var(--ink) 0 2px,transparent 2px 4px,var(--ink) 4px 5px,transparent 5px 9px,var(--ink) 9px 12px,transparent 12px 14px)}
.stub-cap{font-size:10px;color:#7A7490;letter-spacing:1px;margin-top:4px}
.stub-id{font-size:11.5px;font-weight:700;word-break:break-all}
.actions{display:flex;gap:10px;margin-top:18px}

dialog{border:0;background:transparent;padding:16px;max-width:none;overflow:visible}
dialog::backdrop{background:rgba(6,8,26,.78)}
dialog[open] .ticket{animation:drop .55s cubic-bezier(.2,.9,.3,1.2)}
@keyframes drop{from{transform:translateY(-60px) rotate(-3deg);opacity:0}}
dialog .actions{justify-content:center}
.policybox{background:var(--paper);color:var(--ink);border-radius:16px;padding:24px;max-width:440px}
.policybox h3{font-family:var(--display);font-weight:400;font-size:26px;margin-bottom:12px}
.policybox ul{padding-left:18px;font-size:14px;line-height:1.6;display:grid;gap:6px}
.authbox{background:var(--paper);color:var(--ink);border-radius:14px;padding:24px;width:min(420px,calc(100vw - 40px))}
.authbox h3{font-family:var(--display);font-weight:400;font-size:30px;margin-bottom:4px}
.authbox .sub{margin:0 0 18px;color:#5B5670}
.auth-tabs{display:grid;grid-template-columns:1fr 1fr;background:#E8DDBF;padding:4px;border-radius:8px;margin-bottom:16px}
.auth-tabs button{border:0;border-radius:6px;padding:8px;background:transparent;color:var(--ink);font-weight:600}
.auth-tabs button[aria-selected=true]{background:#fff}
.authbox .actions{justify-content:flex-end}
@media(max-width:640px){header{flex-wrap:wrap;padding:12px 16px}.brand{font-size:28px}nav{order:3;width:100%;justify-content:center}
  .ticket{grid-template-columns:1fr}.ticket::before,.ticket::after{display:none}
  .t-stub{border-left:0;border-top:3px dashed #CDBE98;flex-direction:row;gap:10px}.bars{height:40px}
  .show{grid-template-columns:86px 1fr}.price{grid-column:1/-1;flex-direction:row;justify-content:space-between;padding-top:0}}
.filters{display:grid;grid-template-columns:minmax(160px,2fr) repeat(7,minmax(110px,1fr));gap:10px;margin:0 0 18px}
.filters input,.filters select{background:var(--night2);border:2px solid #3A4499;color:#fff;padding:10px 12px;border-radius:9px;font:inherit;min-width:0}
.filters select option{color:#000}
@media(max-width:760px){.filters{grid-template-columns:1fr 1fr}.filters input{grid-column:1/-1}}
.promo{display:flex;gap:8px}.promo input{flex:1;text-transform:uppercase}
.promo-msg{font-size:12.5px;color:#1F806E;min-height:18px;margin-top:4px;font-weight:600}
.btn.light{background:var(--paper);color:var(--ink)}
@media print{header,main{display:none!important}body{background:#fff!important}
  dialog::backdrop{background:none}dialog .actions{display:none}.ticket{filter:none;border:2px solid #000}
  dialog h2{color:#000}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<header>
  <div class="brand">TICKETMASTER<small>Big-screen Telugu action. Pick a show, pick your seats.</small></div>
  <nav role="tablist">
    <button role="tab" aria-selected="true" data-view="book">Book tickets</button>
    <button role="tab" aria-selected="false" data-view="find">My bookings</button>
  </nav>
  <div class="account-bar" id="accountBar"></div>
</header>

<main>
  <section id="book" class="view on">
    <h2>Now booking</h2>
    <p class="sub">Choose a movie and show from the listings below.</p>
    <p class="demo-inventory">Theatre schedules and seat availability are demo listings. Movie poster artwork is from <a href="https://www.themoviedb.org/" target="_blank" rel="noreferrer">TMDB</a>.</p>
    <div class="filters" role="search">
      <input id="fSearch" type="search" placeholder="Search movies or genres" aria-label="Search movies">
      <select id="fCity" aria-label="City"><option value="">Select a city</option></select>
      <select id="fDate" aria-label="Show date" disabled><option value="">Select a city first</option></select>
      <select id="fTheatre" aria-label="Theatre"><option value="">All theatres</option></select>
      <select id="fLang" aria-label="Language"><option value="">All languages</option></select>
      <select id="fFormat" aria-label="Format"><option value="">All formats</option><option>2D</option><option>IMAX</option></select>
      <select id="fScreen" aria-label="Screen"><option value="">All screens</option></select>
      <select id="fTime" aria-label="Time"><option value="">All times</option></select>
      <select id="fWhen" aria-label="Availability"><option value="">Now &amp; upcoming</option><option value="now_showing">Now showing</option><option value="coming_soon">Coming soon</option></select>
      <select id="fSort" aria-label="Sort"><option value="date">Sort: Date</option><option value="rating">Sort: Rating</option><option value="price">Sort: Price (low-high)</option></select>
    </div>
    <div class="layout">
      <div class="shows" id="shows" aria-label="Shows"></div>
      <aside class="panel" id="panel"></aside>
    </div>
  </section>

  <section id="find" class="view">
    <h2>My bookings</h2>
    <p class="sub">Tickets booked with your account.</p>
    <button class="policylink" id="policyLinkFind" type="button">Cancellation &amp; refund policy</button>
    <div class="booking-list" id="myBookings" aria-live="polite"></div>
  </section>
</main>

<dialog id="dlg"><div id="dlgBody"></div></dialog>
<dialog id="authDlg">
  <div class="authbox">
    <h3 id="authTitle">Sign in to book</h3>
    <p class="sub" id="authSubtitle">Use your account to continue booking.</p>
    <div class="auth-tabs" role="tablist">
      <button type="button" id="loginTab" aria-selected="true">Log in</button>
      <button type="button" id="registerTab" aria-selected="false">Create account</button>
    </div>
    <form id="authForm" novalidate>
      <label>Username<input id="authUsername" autocomplete="username" minlength="3" maxlength="32" required></label>
      <label>Password<input id="authPassword" type="password" autocomplete="current-password" minlength="8" required></label>
      <div class="err" id="authErr" aria-live="polite"></div>
      <button class="btn" id="authSubmit" type="submit">Log in</button>
    </form>
  </div>
</dialog>
<dialog id="policyDlg">
  <div class="policybox">
    <h3>Cancellation policy</h3>
    <ul>
      <li>Free cancellation up to 2 hours before the showtime.</li>
      <li>No cancellations once a show is within 2 hours of starting, or after it has started.</li>
      <li>Refunds go back to the original payment method (demo only -- nothing is actually charged).</li>
    </ul>
    <div class="actions"><button class="btn dark" id="closePolicy">Got it</button></div>
  </div>
</dialog>

<script>
const $ = (s, r = document) => r.querySelector(s);
const inr = n => '\u20b9' + Number(n).toLocaleString('en-IN', {maximumFractionDigits: 0});
const fmtDate = d => new Date(d + 'T00:00').toLocaleDateString('en-IN', {weekday:'short', day:'numeric', month:'short', year:'numeric'});
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const posterBg = s => { const h = (s.show_id * 47 + 350) % 360;
  const grad = `linear-gradient(160deg,hsl(${h} 70% 42%),hsl(${(h+40)%360} 65% 18%))`;
  return s.poster ? `url('${s.poster}') center top/cover, ${grad}` : grad; };

async function api(url, body) {
  const res = await fetch(url, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
  const data = await res.json();
  if (!res.ok && !('ok' in data)) throw new Error(data.error || 'Something went wrong.');
  return data;
}

let shows = [], concessions = [], current = null, seatRows = [], picked = new Set();
let conc = {}, holdExpiresAt = null, timerId = null, pollId = null, stage = 'select', holdTimer = null;
let authUser = null, authMode = 'login', pendingBook = false, pendingMyBookings = false;

function renderAccountBar() {
  const bar = $('#accountBar');
  bar.innerHTML = authUser
    ? `<span>${esc(authUser)}</span><button type="button" id="logoutBtn">Log out</button>`
    : `<button type="button" id="signinBtn">Log in</button>`;
  $('#logoutBtn') && ($('#logoutBtn').onclick = async () => {
    await api('/api/logout', {}); authUser = null; renderAccountBar();
    if ($('#find').classList.contains('on')) loadMyBookings();
  });
  $('#signinBtn') && ($('#signinBtn').onclick = () => openAuth('login'));
}

function openAuth(mode) {
  authMode = mode;
  $('#authTitle').textContent = pendingBook ? 'Sign in to book' : 'Your account';
  $('#authSubtitle').textContent = 'Create an account with a username and password, then log in to continue.';
  $('#authErr').textContent = '';
  $('#authPassword').value = '';
  updateAuthMode();
  $('#authDlg').showModal();
}

function updateAuthMode() {
  const registering = authMode === 'register';
  $('#loginTab').setAttribute('aria-selected', !registering);
  $('#registerTab').setAttribute('aria-selected', registering);
  $('#authSubmit').textContent = registering ? 'Create account' : 'Log in';
  $('#authPassword').autocomplete = registering ? 'new-password' : 'current-password';
}

$('#loginTab').onclick = () => { authMode = 'login'; updateAuthMode(); $('#authErr').textContent = ''; };
$('#registerTab').onclick = () => { authMode = 'register'; updateAuthMode(); $('#authErr').textContent = ''; };
$('#authForm').onsubmit = async e => {
  e.preventDefault();
  const username = $('#authUsername').value.trim(), password = $('#authPassword').value;
  $('#authErr').textContent = ''; $('#authErr').style.color = '';
  try {
    if (authMode === 'register') {
      await api('/api/register', {username, password});
      authMode = 'login'; updateAuthMode(); $('#authPassword').value = '';
      $('#authErr').textContent = 'Account created. Log in with your new password.';
      $('#authErr').style.color = '#1F806E';
      return;
    }
    const result = await api('/api/login', {username, password});
    const continueBooking = pendingBook, continueMyBookings = pendingMyBookings;
    authUser = result.username; pendingBook = false; renderAccountBar(); $('#authDlg').close();
    if (continueBooking && current) { stage = 'details'; renderStage(); }
    if (continueMyBookings) { pendingMyBookings = false; loadMyBookings(); }
  } catch (err) { $('#authErr').textContent = err.message; }
};

document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.setAttribute('aria-selected', x === b));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('on', v.id === b.dataset.view));
  if (b.dataset.view === 'find') loadMyBookings();
});
$('#policyLinkFind').onclick = () => $('#policyDlg').showModal();
$('#closePolicy').onclick = () => $('#policyDlg').close();

/* ---- filters ---- */
const filt = {q:'', city:'', date:'', theatre:'', lang:'', fmt:'', screen:'', time:'', when:'', sort:'date'};
const CITY_COORDS = {
  Hyderabad: [17.3850, 78.4867],
  Mumbai: [19.0760, 72.8777],
  Chennai: [13.0827, 80.2707],
  Bengaluru: [12.9716, 77.5946],
};

function findNearestCity(lat, lng) {
  let bestCity = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const [city, [cityLat, cityLng]] of Object.entries(CITY_COORDS)) {
    const distance = Math.hypot(lat - cityLat, lng - cityLng);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestCity = city;
    }
  }
  return bestCity;
}

function applyRecommendedCity(city) {
  if (!city || !shows.length) return;
  const citySelect = $('#fCity');
  if (!citySelect.value) {
    citySelect.value = city;
    filt.city = city;
    updateOptions();
    renderShows();
  }
}

function detectNearbyShows() {
  if (!('geolocation' in navigator)) return;
  navigator.geolocation.getCurrentPosition(
    ({ coords }) => {
      const nearestCity = findNearestCity(coords.latitude, coords.longitude);
      if (nearestCity) {
        $('#shows').dataset.nearbyCity = nearestCity;
        applyRecommendedCity(nearestCity);
      }
    },
    () => { /* Ignore geolocation failures and keep the manual city selector. */ },
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 600000 }
  );
}

function filteredShows() {
  let l = shows.filter(s => (!filt.q || (s.title + ' ' + s.genre).toLowerCase().includes(filt.q))
    && (!filt.city || s.city === filt.city)
    && (!filt.date || s.show_date === filt.date)
    && (!filt.theatre || s.theatre === filt.theatre)
    && (!filt.lang || s.language === filt.lang)
    && (!filt.fmt || s.format === filt.fmt)
    && (!filt.screen || (s.screen_name || s.screen) === filt.screen)
    && (!filt.time || s.show_time === filt.time)
    && (!filt.when || s.status === filt.when));
  if (filt.sort === 'rating') l = [...l].sort((a, b) => (b.rating || 0) - (a.rating || 0));
  else if (filt.sort === 'price') l = [...l].sort((a, b) => a.ticket_price - b.ticket_price);
  const grouped = new Map();
  for (const show of l) {
    const key = show.title.toLowerCase();
    const match = grouped.get(key);
    if (match) match.showing_count += 1;
    else grouped.set(key, {...show, showing_count: 1});
  }
  return [...grouped.values()];
}

function updateOptions() {
  const cityShows = filt.city ? shows.filter(s => s.city === filt.city && (!filt.date || s.show_date === filt.date)) : [];
  const dateSelect = $('#fDate');
  dateSelect.disabled = !filt.city;
  dateSelect.options[0].textContent = filt.city ? 'Any date (including upcoming)' : 'Select a city first';
  dateSelect.options[0].disabled = !filt.city;

  const theatres = [...new Set(cityShows.map(s => s.theatre))].sort();
  const theatreSelect = $('#fTheatre');
  theatreSelect.disabled = !filt.city;
  theatreSelect.innerHTML = `<option value="">${filt.city ? 'All theatres' : 'Select city first'}</option>` + theatres.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  if (theatres.includes(filt.theatre)) theatreSelect.value = filt.theatre;
  else filt.theatre = '';

  const theatreShows = cityShows.filter(s => !filt.theatre || s.theatre === filt.theatre);
  const screens = [...new Set(theatreShows.map(s => s.screen_name || s.screen))].sort();
  const screenSelect = $('#fScreen');
  screenSelect.disabled = !filt.theatre;
  screenSelect.innerHTML = `<option value="">${filt.theatre ? 'All screens' : 'Select theatre first'}</option>` + screens.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  if (screens.includes(filt.screen)) screenSelect.value = filt.screen;
  else filt.screen = '';

  const timeMinutes = value => {
    const [clock, period] = value.split(' ');
    const [hour, minute] = clock.split(':').map(Number);
    return ((hour % 12) + (period === 'PM' ? 12 : 0)) * 60 + minute;
  };
  const timeShows = theatreShows.filter(s => !filt.screen || (s.screen_name || s.screen) === filt.screen);
  const times = [...new Set(timeShows.map(s => s.show_time))].sort((a, b) => timeMinutes(a) - timeMinutes(b));
  const timeSelect = $('#fTime');
  timeSelect.disabled = !filt.theatre;
  timeSelect.innerHTML = `<option value="">${filt.theatre ? 'All times' : 'Select theatre first'}</option>` + times.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  if (times.includes(filt.time)) timeSelect.value = filt.time;
  else filt.time = '';
}

function initFilters() {
  const cities = [...new Set(shows.map(s => s.city))].sort();
  $('#fCity').insertAdjacentHTML('beforeend', cities.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join(''));
  const dateKey = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const today = new Date(); today.setHours(0,0,0,0);
  const dateOptions = Array.from({length:3}, (_, offset) => {
    const date = new Date(today); date.setDate(date.getDate() + offset);
    const labels = ['Today', 'Tomorrow', 'In 2 days'];
    const displayDate = date.toLocaleDateString('en-IN', {day:'numeric', month:'short'});
    return {value:dateKey(date), label:`${labels[offset]} (${displayDate})`};
  });
  $('#fDate').options[0].textContent = 'Any date (including upcoming)';
  $('#fDate').insertAdjacentHTML('beforeend', dateOptions.map(d => `<option value="${d.value}">${d.label}</option>`).join(''));
  filt.date = '';
  $('#fDate').value = '';
  const langs = [...new Set(shows.map(s => s.language))].sort();
  $('#fLang').insertAdjacentHTML('beforeend', langs.map(l => `<option>${esc(l)}</option>`).join(''));
  const map = {fSearch:'q', fCity:'city', fDate:'date', fTheatre:'theatre', fLang:'lang', fFormat:'fmt', fScreen:'screen', fTime:'time', fWhen:'when', fSort:'sort'};
  updateOptions();
  Object.entries(map).forEach(([id, k]) => {
    const el = $('#' + id);
    el.onchange = e => {
      filt[k] = k === 'q' ? e.target.value.trim().toLowerCase() : e.target.value;
      if (k === 'city') filt.theatre = filt.screen = filt.time = '';
      if (k === 'theatre') filt.screen = filt.time = '';
      if (k === 'screen') filt.time = '';
      if (k === 'when' && filt.when === 'coming_soon') {
        filt.date = '';
        $('#fDate').value = '';
      } else if (k === 'when' && !filt.date && filt.when) {
        filt.date = dateOptions[0].value;
        $('#fDate').value = filt.date;
      }
      if (['city', 'date', 'theatre', 'screen'].includes(k)) updateOptions();
      if (k === 'when') updateOptions();
      renderShows();
    };
    if (k === 'q') el.oninput = el.onchange;
  });
}

/* ---- shows ---- */
function renderShows() {
  if (!filt.city) {
    const featuredShows = filteredShows().slice(0, 8);
    const locationNote = $('#shows').dataset.nearbyCity
      ? `<div class="location-note"><strong>Nearby picks:</strong> We found shows around ${esc($('#shows').dataset.nearbyCity)} based on your location.</div>`
      : `<div class="location-note"><strong>Featured picks:</strong> Showing a few top movie picks for you to browse.</div>`;
    $('#shows').innerHTML = featuredShows.length ? locationNote + featuredShows.map(s => `
      <button class="show" data-id="${s.show_id}" aria-pressed="${current && current.show_id === s.show_id}">
        <div class="poster ${s.poster ? 'photo' : ''}" style="background:${posterBg(s)}"><span>${esc(s.title.split(':')[0])}</span></div>
        <div class="info">
          <h3>${esc(s.title)}</h3>
          <div class="tags">
            <span class="tag cert">${esc(s.certificate)}</span><span class="tag">${esc(s.language)}</span>
            <span class="tag">${esc(s.genre)}</span><span class="tag">${Math.floor(s.duration_min/60)}h ${s.duration_min%60}m</span>
            <span class="tag">${esc(s.format)}</span>
            <span class="tag">${s.showing_count} showings</span>
            ${s.rating ? `<span class="tag rating">\u2605 ${s.rating.toFixed(1)} (demo)</span>` : ''}
            ${s.status === 'coming_soon' ? '<span class="tag soon">Coming soon</span>' : ''}
            ${s.fast_filling ? '<span class="tag fast">Filling fast</span>' : ''}
            ${s.status === 'now_showing'
              ? `<span class="tag availability ${s.available_seats === 0 ? 'sold' : s.available_seats <= 5 ? 'low' : ''}">${s.available_seats} / ${s.total_seats} seats available</span>`
              : '<span class="tag soon">Seats not on sale</span>'}
          </div>
          <div class="when"><b>${fmtDate(s.show_date)}</b> • <b>${esc(s.show_time)}</b> • ${esc(s.city)} • ${esc(s.theatre)} • ${esc(s.screen)}</div>
          ${s.synopsis ? `<div class="synopsis">${esc(s.synopsis)} <b>${esc(s.cast || '')}</b></div>` : ''}
        </div>
        <div class="price"><strong>${inr(s.ticket_price)}</strong><span>Standard, per seat</span></div>
      </button>`).join('') : '<div class="empty" style="color:#9AA3D1"><b style="color:#FBEFD3">No featured shows</b>Try selecting a city or filter.</div>';
    document.querySelectorAll('.show').forEach(el => el.onclick = () => selectShow(+el.dataset.id));
    return;
  }
  const list = filteredShows();
  const locationNote = $('#shows').dataset.nearbyCity
    ? `<div class="location-note"><strong>Nearby picks:</strong> We found shows around ${esc($('#shows').dataset.nearbyCity)} based on your location.</div>`
    : '';
  $('#shows').innerHTML = list.length ? locationNote + list.map(s => `
    <button class="show" data-id="${s.show_id}" aria-pressed="${current && current.show_id === s.show_id}">
      <div class="poster ${s.poster ? 'photo' : ''}" style="background:${posterBg(s)}"><span>${esc(s.title.split(':')[0])}</span></div>
      <div class="info">
        <h3>${esc(s.title)}</h3>
        <div class="tags">
          <span class="tag cert">${esc(s.certificate)}</span><span class="tag">${esc(s.language)}</span>
          <span class="tag">${esc(s.genre)}</span><span class="tag">${Math.floor(s.duration_min/60)}h ${s.duration_min%60}m</span>
          <span class="tag">${esc(s.format)}</span>
          <span class="tag">${s.showing_count} showings</span>
          ${s.rating ? `<span class="tag rating">\u2605 ${s.rating.toFixed(1)} (demo)</span>` : ''}
          ${s.status === 'coming_soon' ? '<span class="tag soon">Coming soon</span>' : ''}
          ${s.fast_filling ? '<span class="tag fast">Filling fast</span>' : ''}
          ${s.status === 'now_showing'
            ? `<span class="tag availability ${s.available_seats === 0 ? 'sold' : s.available_seats <= 5 ? 'low' : ''}">${s.available_seats} / ${s.total_seats} seats available</span>`
            : '<span class="tag soon">Seats not on sale</span>'}
        </div>
        <div class="when"><b>${fmtDate(s.show_date)}</b> • <b>${esc(s.show_time)}</b> • ${esc(s.city)} • ${esc(s.theatre)} • ${esc(s.screen)}</div>
        ${s.synopsis ? `<div class="synopsis">${esc(s.synopsis)} <b>${esc(s.cast || '')}</b></div>` : ''}
      </div>
      <div class="price"><strong>${inr(s.ticket_price)}</strong><span>Standard, per seat</span></div>
    </button>`).join('') : '<div class="empty" style="color:#9AA3D1"><b style="color:#FBEFD3">No matching shows</b>Try clearing the filters.</div>';
  document.querySelectorAll('.show').forEach(el => el.onclick = () => selectShow(+el.dataset.id));
}

async function selectShow(id) {
  stopTimers();
  current = shows.find(s => s.show_id === id);
  picked = new Set(); conc = {}; stage = 'select';
  renderShows();
  if (current.status === 'now_showing') await loadSeats();
  renderPanel();
  if (current.status === 'now_showing') pollId = setInterval(loadSeats, 15000);
}

async function loadSeats() {
  if (!current) return;
  const r = await api('/api/seats?show_id=' + current.show_id);
  seatRows = r.seats;
  const byN = Object.fromEntries(seatRows.map(s => [s.n, s]));
  picked = new Set([...picked].filter(n => byN[n] && byN[n].status !== 'booked' && byN[n].status !== 'held_other'));
  holdExpiresAt = r.hold_expires_at;
  seatRows.forEach(s => { if (s.status === 'held_you') picked.add(s.n); });
  startTimer();
  if (document.getElementById('bookForm')) renderSeatArea();
}

function stopTimers() {
  clearInterval(pollId); pollId = null;
  clearInterval(timerId); timerId = null;
  clearTimeout(holdTimer); holdTimer = null;
}

function startTimer() {
  clearInterval(timerId);
  if (!holdExpiresAt) { const t = $('#timerbar'); if (t) t.remove(); return; }
  timerId = setInterval(() => {
    const left = new Date(holdExpiresAt) - new Date();
    const box = $('#timerText'), fill = $('#timerFill');
    if (left <= 0) {
      clearInterval(timerId); timerId = null; holdExpiresAt = null; picked = new Set();
      $('#err') && ($('#err').textContent = 'Your held seats were released. Please choose again.');
      loadSeats(); renderPanel();
      return;
    }
    const m = Math.floor(left / 60000), sec = Math.floor((left % 60000) / 1000);
    if (box) box.textContent = `Seats held for ${m}:${String(sec).padStart(2,'0')}`;
    if (fill) fill.style.transform = `scaleX(${Math.max(0, left / (HOLD_MIN*60000))})`;
  }, 1000);
}
const HOLD_MIN = 5;

function scheduleHold() {
  clearTimeout(holdTimer);
  holdTimer = setTimeout(async () => {
    if (picked.size === 0) { await api('/api/release', {show_id: current.show_id}); holdExpiresAt = null; startTimer(); return; }
    try {
      const r = await api('/api/hold', {show_id: current.show_id, seats: [...picked]});
      if (!r.ok) {
        r.conflicts.forEach(n => picked.delete(n));
        $('#err') && ($('#err').textContent = 'Someone else just took a seat you picked. Choose another.');
        await loadSeats();
      } else {
        holdExpiresAt = r.expires_at; startTimer();
      }
    } catch (e) { $('#err') && ($('#err').textContent = e.message); }
  }, 350);
}

/* ---- booking panel ---- */
function renderPanel() {
  const p = $('#panel');
  if (!current) { p.innerHTML = `<div class="empty"><b>No show picked yet</b>Choose a show to see availability.</div>`; return; }

  const banner = current.poster
    ? `<div class="banner" style="background-image:url('${current.poster}')"><h3>${esc(current.title)}</h3></div>`
    : `<h3>${esc(current.title)}</h3>`;

  if (current.status === 'coming_soon') {
    p.innerHTML = `${banner}
      <div class="meta">${fmtDate(current.show_date)}, ${esc(current.show_time)} \u00b7 ${esc(current.screen)}</div>
      <p style="font-size:14px;color:#5B5670;margin-bottom:14px">${esc(current.synopsis)}</p>
      <div class="notice">Booking opens closer to release. This sample date is illustrative, not a confirmed cinema schedule.</div>
      <button class="btn ${current.interested ? 'dark' : 'ghost'}" type="button" id="notifyBtn">${current.interested ? '\u2713 Interested' : "I'm interested"}</button>
      <div class="meta" style="margin:8px 0 0">${current.interest_count} interested (this demo only records interest; it sends no alerts)</div>`;
    $('#notifyBtn').onclick = async () => {
      const r = await api('/api/notify', {movie_id: current.movie_id});
      current.interested = r.interested; current.interest_count = r.count; renderPanel();
    };
    return;
  }

  p.innerHTML = `${banner}
    <div class="meta">${fmtDate(current.show_date)}, ${esc(current.show_time)} \u00b7 ${esc(current.screen)} \u00b7 ${esc(current.format)}</div>
    <div id="seatArea"></div>
    <form id="bookForm" novalidate></form>`;
  renderSeatArea();
  renderStage();
}

function renderSeatArea() {
  const area = $('#seatArea'); if (!area) return;
  const rows = [0,1,2,3].map(r => `<div class="row"><i>${'ABCD'[r]}</i>${
    Array.from({length:10}, (_, c) => { const n = r*10 + c + 1; const s = seatRows.find(x => x.n === n) || {};
      const taken = s.status === 'booked' || s.status === 'held_other';
      return `<button type="button" class="seat type-${s.type||''} ${s.status==='held_other'?'other':''}" data-n="${n}"
        aria-label="${s.label} ${s.type}, ${inr(s.price)}" aria-pressed="${picked.has(n)}" ${taken?'disabled':''}
        title="${s.label} \u00b7 ${s.type} \u00b7 ${inr(s.price)}">${c+1}</button>`; }).join('')
  }</div>`).join('');
  area.innerHTML = `
    <div class="screen">SCREEN</div>
    <div class="rows">${rows}</div>
    <div class="legend">
      <span style="--c:var(--jade)">Standard</span><span style="--c:var(--teal)">Premium</span>
      <span style="--c:var(--violet)">Recliner</span><span style="--c:var(--kumkum)">Yours</span>
      <span style="--c:#E7A8AE">Held by others</span><span style="--c:#CFC7B5">Taken</span>
    </div>
    ${holdExpiresAt ? `<div class="timerbar" id="timerbar"><span id="timerText">Seats held</span><div class="bar"><i id="timerFill"></i></div></div>` : ''}`;
  document.querySelectorAll('.seat:not(:disabled)').forEach(b => b.onclick = () => {
    const n = +b.dataset.n;
      if (picked.has(n)) picked.delete(n);
      else if (picked.size >= MAX_SEATS) { $('#err') && ($('#err').textContent = `You can book up to ${MAX_SEATS} seats at a time.`); return; }
      else picked.add(n);
    b.setAttribute('aria-pressed', picked.has(n)); scheduleHold(); renderStage();
  });
}

function seatTotal() { return [...picked].reduce((sum, n) => sum + (seatRows.find(s => s.n === n)?.price || 0), 0); }
function concTotal() { return Object.entries(conc).reduce((sum, [id, qty]) => sum + qty * concessions.find(c => +c.item_id === +id).price, 0); }
const FEE = 20, MAX_SEATS = 8;
let promo = {code:'', rule:null, msg:''};
function calcDiscount() {
  const r = promo.rule; if (!r) return 0;
  const s = seatTotal(), c = concTotal();
  const base = r.on === 'seats' ? s : r.on === 'conc' ? c : s + c;
  if (!base || base < (r.min || 0)) return 0;
  let d = r.type === 'flat' ? r.value : base * r.value / 100;
  if (r.cap) d = Math.min(d, r.cap);
  return Math.round(Math.min(d, s + c));
}
function grandTotal() { return seatTotal() + concTotal() + FEE - calcDiscount(); }
const discLine = () => calcDiscount() ? `<div class="line" style="color:#1F806E"><span>Promo ${esc(promo.code)}</span><span>\u2212${inr(calcDiscount())}</span></div>` : '';

function renderStage() {
  const f = $('#bookForm'); if (!f) return;
  if (stage === 'select') {
    f.innerHTML = `
      <div class="section-label">Snacks &amp; drinks</div>
      <div class="concessions">${concessions.map(c => `
        <div class="conc-row"><span class="name">${esc(c.name)}<span class="cat">${esc(c.category)} \u00b7 ${inr(c.price)}</span></span>
          <div class="stepper"><button type="button" data-d="-1" data-id="${c.item_id}">\u2212</button>
            <span>${conc[c.item_id]||0}</span><button type="button" data-d="1" data-id="${c.item_id}">+</button></div></div>`).join('')}
      </div>
      <div class="section-label">Offers</div>
      <div class="promo"><input id="promoIn" placeholder="Promo code (try FIRST50)" value="${esc(promo.code)}" autocomplete="off">
        <button type="button" class="btn dark" id="promoBtn">Apply</button></div>
      <div class="promo-msg" id="promoMsg" ${promo.rule ? '' : 'style="color:#B3121F"'}>${esc(promo.msg)}${promo.rule && !calcDiscount() ? ' (not applicable to this order yet)' : ''}</div>
      <div class="breakdown">
        <div class="line"><span>Seats (${picked.size})</span><span>${inr(seatTotal())}</span></div>
        <div class="line"><span>Snacks &amp; drinks</span><span>${inr(concTotal())}</span></div>
        <div class="line"><span>Convenience fee</span><span>${inr(FEE)}</span></div>
        ${discLine()}
      </div>
      <div class="total"><span>Total</span><b>${inr(grandTotal())}</b></div>
      <div class="err" id="err"></div>
      <button class="btn" id="next" type="button" ${picked.size?'':'disabled'}>Review &amp; pay</button>
      <button class="policylink" type="button" id="policyLinkBook">Cancellation &amp; refund policy</button>`;
    document.querySelectorAll('.stepper button').forEach(b => b.onclick = () => {
      const id = +b.dataset.id, d = +b.dataset.d;
      conc[id] = Math.max(0, (conc[id]||0) + d); renderStage();
    });
    $('#next').onclick = async () => {
      if (!authUser) {
        try {
          const account = await api('/api/auth'); authUser = account.username;
          renderAccountBar();
        } catch (e) { $('#err').textContent = e.message; return; }
      }
      if (!authUser) { pendingBook = true; openAuth('login'); return; }
      stage = 'details'; renderStage();
    };
    $('#policyLinkBook').onclick = () => $('#policyDlg').showModal();
    $('#promoBtn').onclick = async () => {
      const code = $('#promoIn').value.trim().toUpperCase();
      if (!code) { promo = {code:'', rule:null, msg:''}; renderStage(); return; }
      try {
        const r = await api('/api/promo?code=' + encodeURIComponent(code));
        promo = r.valid ? {code, rule:r.rule, msg:r.message} : {code:'', rule:null, msg:r.message};
      } catch (e) { promo = {code:'', rule:null, msg:e.message}; }
      renderStage();
    };
  } else if (stage === 'details') {
    f.innerHTML = `
      <label>Name<input id="cName" autocomplete="name" placeholder="Full name"></label>
      <label>Phone<input id="cPhone" inputmode="numeric" maxlength="10" autocomplete="tel" placeholder="10-digit mobile number"></label>
      <div class="total"><span>Total</span><b>${inr(grandTotal())}</b></div>
      <div class="err" id="err"></div>
      <div class="actions"><button class="btn ghost" id="back" type="button">Back</button>
        <button class="btn" id="toPay" type="button">Choose payment</button></div>`;
    $('#back').onclick = () => { stage = 'select'; renderStage(); };
    $('#toPay').onclick = () => {
      const name = $('#cName').value.trim(), phone = $('#cPhone').value.trim();
      if (!name) return $('#err').textContent = 'Enter the customer name.';
      if (!/^\d{10}$/.test(phone)) return $('#err').textContent = 'Enter a 10-digit phone number.';
      window.__cust = {name, phone}; stage = 'pay'; renderStage();
    };
  } else if (stage === 'pay') {
    f.innerHTML = `
      <div class="section-label">Payment method (demo &mdash; nothing is charged)</div>
      <div class="paymethods">
        <label><input type="radio" name="pm" value="UPI" checked> UPI</label>
        <label><input type="radio" name="pm" value="Card"> Card</label>
        <label><input type="radio" name="pm" value="Pay at Counter"> Pay at counter</label>
      </div>
      <div class="breakdown">
        <div class="line"><span>Seats (${[...picked].map(n=>seatRows.find(s=>s.n===n)?.label).join(', ')})</span><span>${inr(seatTotal())}</span></div>
        <div class="line"><span>Snacks &amp; drinks</span><span>${inr(concTotal())}</span></div>
        <div class="line"><span>Convenience fee</span><span>${inr(FEE)}</span></div>
        ${discLine()}
      </div>
      <div class="total"><span>Total</span><b>${inr(grandTotal())}</b></div>
      <div class="err" id="err"></div>
      <div class="actions"><button class="btn ghost" id="back2" type="button">Back</button>
        <button class="btn" id="pay" type="button">Confirm &amp; pay ${inr(grandTotal())}</button></div>`;
    $('#back2').onclick = () => { stage = 'details'; renderStage(); };
    $('#pay').onclick = doBook;
  }
}

async function doBook() {
  $('#err').textContent = '';
  const pm = document.querySelector('input[name=pm]:checked').value;
  try {
    const b = await api('/api/book', {
      show_id: current.show_id, customer: window.__cust.name, phone: window.__cust.phone,
      seats: [...picked], payment_method: pm, promo: promo.code,
      concessions: Object.entries(conc).filter(([,q]) => q>0).map(([item_id, qty]) => ({item_id:+item_id, qty})),
    });
    const full = await api('/api/booking?id=' + b.booking_id);
    showTicket(full, true);
    stopTimers(); picked = new Set(); conc = {}; stage = 'select'; holdExpiresAt = null; promo = {code:'', rule:null, msg:''};
    await Promise.all([selectShow(current.show_id), refreshShows()]);
  } catch (e) { $('#err').textContent = e.message; }
}

async function refreshShows() { shows = await api('/api/shows'); renderShows(); }

/* ---- ticket ---- */
function ticketHTML(b) {
  const cancelled = b.status === 'CANCELLED';
  const ex = b.extras || {};
  const concLines = (ex.concessions || []).map(c => `<div class="line">${c.qty}\u00d7 ${esc(c.name)}<span>${inr(c.subtotal)}</span></div>`).join('');
  return `<div class="ticket">
    <div class="t-main">
      <div class="lab">Admit for</div><h4>${esc(b.title)}</h4>
      <div class="grid2">
        <div><span class="lab">Date</span><b>${fmtDate(b.show_date)}</b></div>
        <div><span class="lab">Time</span><b>${esc(b.show_time)}</b></div>
        <div><span class="lab">Screen</span><b>${esc(b.screen)}</b></div>
        <div><span class="lab">Seats</span><b>${b.seat_labels.join(', ')}</b></div>
        <div><span class="lab">Name</span><b>${esc(b.customer_name)}</b></div>
        <div><span class="lab">Amount</span><b>${inr(b.amount)}</b></div>
      </div>
      ${concLines || ex.payment_method ? `<div class="t-extras">${concLines}${ex.discount ? `<div class="line">Promo ${esc(ex.promo)}<span>\u2212${inr(ex.discount)}</span></div>` : ''}${ex.payment_method ? `<div class="line">Paid via<span>${esc(ex.payment_method)}</span></div>` : ''}</div>` : ''}
    </div>
    <div class="t-stub">
      <div class="status ${cancelled ? 'x' : ''}">${cancelled ? 'CANCELLED' : 'CONFIRMED'}</div>
      <div class="bars" aria-hidden="true"></div>
      <div class="stub-cap">SHOW AT COUNTER</div>
      <div class="stub-id">${esc(b.booking_id)}</div>
    </div></div>`;
}

function showTicket(b, isNew) {
  $('#dlgBody').innerHTML = (isNew ? `<h2 style="text-align:center;margin-bottom:16px">You're in. Enjoy the show!</h2>` : '') + ticketHTML(b) +
    `<div class="actions"><button class="btn light" id="printTicket">Print ticket</button><button class="btn dark" id="closeDlg">Done</button></div>`;
  $('#printTicket').onclick = () => window.print();
  $('#closeDlg').onclick = () => $('#dlg').close();
  $('#dlg').showModal();
}

/* ---- lookup / cancel ---- */
function renderMyBookings(bookings) {
  const out = $('#myBookings');
  if (!bookings.length) {
    out.innerHTML = '<p class="empty">No tickets have been booked with this account yet.</p>';
    return;
  }
  out.innerHTML = bookings.map(b => `<article class="booking-entry">${ticketHTML(b)}${b.status === 'CONFIRMED'
    ? `<div class="actions"><button class="btn ghost cancel-my-booking" data-id="${esc(b.booking_id)}">Cancel this booking</button></div>` : ''}</article>`).join('');
  document.querySelectorAll('.cancel-my-booking').forEach(button => button.onclick = async () => {
    const id = button.dataset.id;
    if (!confirm('Cancel booking ' + id + '? The seats will be released.')) return;
    try {
      await api('/api/cancel', {booking_id: id});
      await loadMyBookings();
      if (current) await loadSeats();
      await refreshShows();
    } catch (e) {
      button.closest('.booking-entry').insertAdjacentHTML('beforeend', `<p class="err">${esc(e.message)}</p>`);
    }
  });
}

async function loadMyBookings() {
  const out = $('#myBookings');
  if (!authUser) {
    pendingMyBookings = true;
    out.innerHTML = '<p class="sub">Log in to see the tickets booked with your account.</p><button class="btn" id="loginForBookings" type="button">Log in</button>';
    $('#loginForBookings').onclick = () => openAuth('login');
    return;
  }
  try {
    renderMyBookings(await api('/api/my-bookings'));
  } catch (e) { out.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

/* ---- boot ---- */
(async () => {
  [shows, concessions] = await Promise.all([api('/api/shows'), api('/api/concessions')]);
  const account = await api('/api/auth'); authUser = account.username;
  renderAccountBar(); initFilters(); renderShows(); renderPanel();
  detectNearbyShows();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()

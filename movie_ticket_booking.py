#!/usr/bin/env python3
"""Movie Ticket Booking System - Course End Project (A9513).

A console application that demonstrates movie discovery, showtime selection,
seat availability, ticket booking, cancellation, and booking lookup using SQLite.

Run:
python3 movie_ticket_booking.py --demo
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from uuid import uuid4

DB_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "movie_booking.db"
)

CSV_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "indian_theatres_hyderabad.csv"),
    r"C:\Users\manis\Downloads\indian_theatres_hyderabad.csv",
]

DEFAULT_THEATRE_DATA = [
    {"city": "Hyderabad", "theatre_name": "Prasads Multiplex (PCX)", "screens": [f"Screen {i}" for i in range(1, 7)]},
    {"city": "Hyderabad", "theatre_name": "AMB Cinemas", "screens": [f"Screen {i}" for i in range(1, 8)]},
    {"city": "Hyderabad", "theatre_name": "Allu Cinemas", "screens": [f"Screen {i}" for i in range(1, 5)]},
    {"city": "Hyderabad", "theatre_name": "AAA Cinemas", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Hyderabad", "theatre_name": "ART Cinemas", "screens": [f"Screen {i}" for i in range(1, 7)]},
    {"city": "Hyderabad", "theatre_name": "Aparna Cinemas (Nallagandla)", "screens": [f"Screen {i}" for i in range(1, 8)]},
    {"city": "Hyderabad", "theatre_name": "Aparna Cinemas (Shamshabad)", "screens": [f"Screen {i}" for i in range(1, 8)]},
    {"city": "Hyderabad", "theatre_name": "Cinepolis Lulu Mall", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Hyderabad", "theatre_name": "PVR Superplex Inorbit", "screens": [f"Screen {i}" for i in range(1, 12)]},
    {"city": "Hyderabad", "theatre_name": "Aradhana 70MM Theatre", "screens": ["Screen 1"]},
    {"city": "Hyderabad", "theatre_name": "Sandhya 70MM", "screens": ["Screen 1"]},
    {"city": "Hyderabad", "theatre_name": "Mythri Vimal 70MM", "screens": ["Screen 1"]},
    {"city": "Hyderabad", "theatre_name": "Sudarshan 35MM", "screens": ["Screen 1"]},
    {"city": "Hyderabad", "theatre_name": "Devi 70MM", "screens": ["Screen 1"]},
    {"city": "Hyderabad", "theatre_name": "Asian Lakshmikala Cinepride", "screens": [f"Screen {i}" for i in range(1, 5)]},
    {"city": "Hyderabad", "theatre_name": "Cineverse Multiplex", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Hyderabad", "theatre_name": "PVR Atrium Mall", "screens": [f"Screen {i}" for i in range(1, 5)]},
    {"city": "Hyderabad", "theatre_name": "INOX Prism Mall", "screens": [f"Screen {i}" for i in range(1, 5)]},
    {"city": "Hyderabad", "theatre_name": "Cinepolis TNR North City", "screens": [f"Screen {i}" for i in range(1, 11)]},
    {"city": "Hyderabad", "theatre_name": "AMB Classic Victory", "screens": [f"Screen {i}" for i in range(1, 8)]},
    {"city": "Mumbai", "theatre_name": "PVR ICON, Phoenix Palladium", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Mumbai", "theatre_name": "PVR ICON, Phoenix Marketcity", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Mumbai", "theatre_name": "INOX Megaplex, R City Mall", "screens": [f"Screen {i}" for i in range(1, 8)]},
    {"city": "Mumbai", "theatre_name": "Metro INOX, Marine Lines", "screens": [f"Screen {i}" for i in range(1, 5)]},
    {"city": "Mumbai", "theatre_name": "Cinepolis, Fun Republic Mall", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Chennai", "theatre_name": "PVR Sathyam, Royapettah", "screens": [f"Screen {i}" for i in range(1, 7)]},
    {"city": "Chennai", "theatre_name": "PVR Palazzo, Forum Vijaya Mall", "screens": [f"Screen {i}" for i in range(1, 7)]},
    {"city": "Chennai", "theatre_name": "Luxe Cinemas, Phoenix Marketcity", "screens": [f"Screen {i}" for i in range(1, 12)]},
    {"city": "Chennai", "theatre_name": "AGS Cinemas, T. Nagar", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Chennai", "theatre_name": "Rohini Silver Screens, Koyambedu", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Chennai", "theatre_name": "Kamala Cinemas, Vadapalani", "screens": [f"Screen {i}" for i in range(1, 6)]},
    {"city": "Bengaluru", "theatre_name": "PVR Orion Mall", "screens": [f"Screen {i}" for i in range(1, 12)]},
    {"city": "Bengaluru", "theatre_name": "PVR Nexus Koramangala", "screens": [f"Screen {i}" for i in range(1, 9)]},
    {"city": "Bengaluru", "theatre_name": "PVR Vega City", "screens": [f"Screen {i}" for i in range(1, 10)]},
    {"city": "Bengaluru", "theatre_name": "INOX Garuda Mall", "screens": [f"Screen {i}" for i in range(1, 7)]},
    {"city": "Bengaluru", "theatre_name": "Cinepolis Royal Meenakshi Mall", "screens": [f"Screen {i}" for i in range(1, 7)]},
    {"city": "Bengaluru", "theatre_name": "INOX Mantri Square Mall", "screens": [f"Screen {i}" for i in range(1, 7)]},
]


def load_theatre_catalog() -> list[dict]:
    for csv_path in CSV_CANDIDATES:
        if not os.path.isfile(csv_path):
            continue
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            out = []
            for row in rows:
                city = (row.get("city") or "").strip()
                theatre_name = (row.get("theatre_name") or "").strip()
                area = (row.get("area") or "").strip()
                if not city or not theatre_name:
                    continue
                try:
                    count = int((row.get("screens") or "0").strip())
                except ValueError:
                    count = 1
                screens = [f"Screen {i}" for i in range(1, max(count, 1) + 1)]
                out.append({"city": city, "theatre_name": theatre_name, "area": area, "screens": screens})
            if out:
                catalogue_keys = {(item["city"].casefold(), item["theatre_name"].casefold()) for item in out}
                for theatre in DEFAULT_THEATRE_DATA:
                    if theatre["city"] == "Hyderabad":
                        continue
                    key = (theatre["city"].casefold(), theatre["theatre_name"].casefold())
                    if key not in catalogue_keys:
                        out.append(theatre.copy())
                        catalogue_keys.add(key)
                name_counts = {}
                for theatre in out:
                    key = (theatre["city"].casefold(), theatre["theatre_name"].casefold())
                    name_counts[key] = name_counts.get(key, 0) + 1
                for theatre in out:
                    key = (theatre["city"].casefold(), theatre["theatre_name"].casefold())
                    area = theatre.get("area", "")
                    if name_counts[key] > 1 and area:
                        theatre["theatre_name"] += f" ({area})"
                return out
        except Exception:
            continue
    return DEFAULT_THEATRE_DATA


@dataclass(frozen=True)
class Show:
    show_id: int
    movie: str
    language: str
    certificate: str
    show_date: str
    show_time: str
    city: str
    theatre: str
    screen: str
    price: float


class MovieTicketBookingSystem:

    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.conn = self._open_database()
        self._initialize_database()

    def _open_database(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.DatabaseError:
            conn.close()
            self._recover_corrupted_database()
            restored = sqlite3.connect(self.db_path)
            restored.row_factory = sqlite3.Row
            return restored

    def _recover_corrupted_database(self) -> None:
        if os.path.exists(self.db_path):
            backup = f"{self.db_path}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(self.db_path, backup)
            except OSError:
                os.remove(self.db_path)
            print(f"Database was corrupted; backed up to {backup} and created a fresh DB.")

    def _initialize_database(self) -> None:
        try:
            self._create_schema()
            self._seed_data()
        except sqlite3.DatabaseError:
            self._recover_corrupted_database()
            self.conn.close()
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            self._create_schema()
            self._seed_data()

    def close(self) -> None:
        self.conn.close()

    # DATABASE CREATION

    def _create_schema(self) -> None:
        self.conn.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS movies (
            movie_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            language TEXT NOT NULL,
            genre TEXT NOT NULL,
            certificate TEXT NOT NULL,
            duration_min INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shows (
            show_id INTEGER PRIMARY KEY,
            movie_id INTEGER NOT NULL REFERENCES movies(movie_id),
            show_date TEXT NOT NULL,
            show_time TEXT NOT NULL,
            city TEXT NOT NULL DEFAULT 'Hyderabad',
            theatre TEXT NOT NULL DEFAULT 'Prabhas Cinemas',
            screen TEXT NOT NULL,
            ticket_price REAL NOT NULL,
            total_seats INTEGER NOT NULL DEFAULT 40
        );

        CREATE TABLE IF NOT EXISTS bookings (
            booking_id TEXT PRIMARY KEY,
            show_id INTEGER NOT NULL REFERENCES shows(show_id),
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            seats TEXT NOT NULL,
            amount REAL NOT NULL,
            booked_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'CONFIRMED'
        );
        """)

        for column_sql in (
            "ALTER TABLE shows ADD COLUMN city TEXT NOT NULL DEFAULT 'Hyderabad'",
            "ALTER TABLE shows ADD COLUMN theatre TEXT NOT NULL DEFAULT 'Prabhas Cinemas'",
        ):
            try:
                self.conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass

        self.conn.execute("""
            UPDATE shows
            SET city=screen, theatre=CAST(ticket_price AS TEXT), screen=total_seats,
                ticket_price=CAST(theatre AS REAL), total_seats=40
            WHERE city='40' AND total_seats LIKE 'Screen %' AND typeof(ticket_price)='text'
        """)
        self.conn.execute("""
            UPDATE shows
            SET theatre=screen, screen=ticket_price, ticket_price=CAST(total_seats AS REAL), total_seats=40
            WHERE theatre='40' AND ticket_price LIKE 'Screen %'
              AND typeof(total_seats) IN ('integer', 'real')
        """)

        self.conn.commit()

    # INSERT MOVIE AND SHOW DATA

    def _seed_data(self) -> None:

        movies = [
            (1, "Pushpa 2: The Rule",
             "Telugu", "Action Drama", "U/A", 182),

            (2, "Kalki 2898 AD",
             "Telugu", "Sci-Fi", "U/A", 181),

            (3, "Devara: Part 1",
             "Telugu", "Action Thriller", "U/A", 180),

            (4, "Salaar: Part 1 - Ceasefire",
             "Telugu", "Action", "A", 175),

            (5, "Stree 2",
             "Hindi", "Comedy Horror", "U/A", 155),

            (6, "Bhool Bhulaiyaa 3",
             "Hindi", "Comedy Horror", "U/A", 156),

            (7, "Maharaja",
             "Tamil", "Action Thriller", "U/A", 150),

            (8, "Vettaiyan",
             "Tamil", "Action Drama", "U/A", 170),

            (9, "Pushpa 2: The Rule",
             "Hindi", "Action Drama", "U/A", 182),

            (10, "Pushpa 2: The Rule",
             "Tamil", "Action Drama", "U/A", 182),

            (11, "Kalki 2898 AD",
             "Hindi", "Sci-Fi", "U/A", 181),

            (12, "Kalki 2898 AD",
             "Tamil", "Sci-Fi", "U/A", 181),

            (13, "Devara: Part 1",
             "Hindi", "Action Thriller", "U/A", 180),

            (14, "Devara: Part 1",
             "Tamil", "Action Thriller", "U/A", 180),

            (15, "Salaar: Part 1 - Ceasefire",
             "Hindi", "Action", "A", 175),
        ]

        self.conn.executemany(
            """INSERT OR REPLACE INTO movies VALUES (?,?,?,?,?,?)""",
            movies
        )

        theatre_catalog = load_theatre_catalog()
        movie_ids = list(range(1, 16))
        dates = ("2026-10-05", "2026-10-06", "2026-10-07")
        movie_prices = {
            1: 220.0, 2: 250.0, 3: 180.0, 4: 220.0, 5: 280.0, 6: 250.0, 7: 350.0, 8: 300.0,
            9: 220.0, 10: 220.0, 11: 220.0, 12: 220.0, 13: 250.0, 14: 250.0, 15: 250.0,
        }
        self._rebuild_show_schedule(theatre_catalog, movie_ids, dates, movie_prices)

    def _rebuild_show_schedule(
        self,
        theatre_catalog: list[dict],
        movie_ids: list[int],
        dates: tuple[str, ...],
        movie_prices: dict[int, float],
    ) -> None:
        show_times = ["10:30 AM", "01:15 PM", "06:15 PM", "09:30 PM"]
        theatre_by_key = {
            (theatre["city"], theatre["theatre_name"]): {
                **theatre,
                "screens": set(theatre.get("screens", [])),
            }
            for theatre in theatre_catalog
        }
        existing_by_theatre: dict[tuple[str, str], list[dict]] = {}
        for row in self.conn.execute(
            "SELECT show_id, movie_id, city, theatre, screen, ticket_price, total_seats "
            "FROM shows ORDER BY show_id"
        ):
            key = (row["city"] or "Hyderabad", row["theatre"] or "Prabhas Cinemas")
            theatre = theatre_by_key.setdefault(key, {
                "city": key[0], "theatre_name": key[1], "screens": set()
            })
            if row["screen"]:
                theatre["screens"].add(row["screen"])
            existing_by_theatre.setdefault(key, []).append(dict(row))

        referenced_show_ids = {
            int(row[0]) for row in self.conn.execute("SELECT DISTINCT show_id FROM bookings")
        }
        has_holds = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='seat_holds'"
        ).fetchone()
        now_text = datetime.now().isoformat()
        if has_holds:
            self.conn.execute("DELETE FROM seat_holds WHERE expires_at<=?", (now_text,))
            referenced_show_ids.update(
                int(row[0]) for row in self.conn.execute("SELECT DISTINCT show_id FROM seat_holds")
            )

        theatre_catalog = list(theatre_by_key.values())
        max_show_id = self.conn.execute("SELECT COALESCE(MAX(show_id), 100) FROM shows").fetchone()[0]
        next_show_id = max_show_id + 1
        used_show_ids: set[int] = set()

        for theatre_index, theatre in enumerate(theatre_catalog):
            key = (theatre["city"], theatre["theatre_name"])
            existing_rows = existing_by_theatre.get(key, [])
            retained_rows = [row for row in existing_rows if row["show_id"] in referenced_show_ids]
            reusable_rows = [row for row in existing_rows if row["show_id"] not in referenced_show_ids]

            screen_count = max(4, len(theatre["screens"]))
            needed_screen_count = (len(retained_rows) + len(dates) * len(show_times) - 1) // (len(dates) * len(show_times))
            screen_count = max(screen_count, needed_screen_count)
            screens = [f"Screen {number}" for number in range(1, screen_count + 1)]
            slots = [
                (show_date, show_time, screen)
                for show_date in dates
                for show_time in show_times
                for screen in screens
            ]
            scheduled_movies = [movie_ids[(slot_index + theatre_index) % len(movie_ids)] for slot_index in range(len(slots))]
            movie_counts = {movie_id: scheduled_movies.count(movie_id) for movie_id in movie_ids}
            retained_by_slot: dict[int, dict] = {}

            for retained in retained_rows:
                movie_id = int(retained["movie_id"])
                slot_index = next(
                    (index for index, assigned_movie in enumerate(scheduled_movies)
                     if index not in retained_by_slot and assigned_movie == movie_id),
                    None,
                )
                if slot_index is None:
                    slot_index = next(
                        (index for index, assigned_movie in enumerate(scheduled_movies)
                         if index not in retained_by_slot and movie_counts[assigned_movie] > 1),
                        None,
                    )
                    if slot_index is None:
                        raise ValueError(f"Could not preserve a booked show at {key[1]}.")
                    previous_movie = scheduled_movies[slot_index]
                    movie_counts[previous_movie] -= 1
                    scheduled_movies[slot_index] = movie_id
                    movie_counts[movie_id] += 1
                retained_by_slot[slot_index] = retained

            reusable_index = 0
            for slot_index, (show_date, show_time, screen) in enumerate(slots):
                movie_id = scheduled_movies[slot_index]
                retained = retained_by_slot.get(slot_index)
                if retained:
                    show_id = int(retained["show_id"])
                    ticket_price = float(retained["ticket_price"])
                    total_seats = int(retained["total_seats"])
                elif reusable_index < len(reusable_rows):
                    reusable = reusable_rows[reusable_index]
                    reusable_index += 1
                    show_id = int(reusable["show_id"])
                    ticket_price = movie_prices[movie_id]
                    total_seats = 40
                else:
                    show_id = next_show_id
                    next_show_id += 1
                    ticket_price = movie_prices[movie_id]
                    total_seats = 40

                updated = self.conn.execute(
                    "UPDATE shows SET movie_id=?, show_date=?, show_time=?, city=?, theatre=?, screen=?, "
                    "ticket_price=?, total_seats=? WHERE show_id=?",
                    (movie_id, show_date, show_time, key[0], key[1], screen, ticket_price, total_seats, show_id),
                )
                if updated.rowcount == 0:
                    self.conn.execute(
                        "INSERT INTO shows (show_id, movie_id, show_date, show_time, city, theatre, screen, ticket_price, total_seats) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (show_id, movie_id, show_date, show_time, key[0], key[1], screen, ticket_price, total_seats),
                    )
                used_show_ids.add(show_id)

        stale_show_ids = [
            int(row[0]) for row in self.conn.execute("SELECT show_id FROM shows")
            if int(row[0]) not in used_show_ids
        ]
        self.conn.executemany("DELETE FROM shows WHERE show_id=?", [(show_id,) for show_id in stale_show_ids])
        self.conn.commit()

    # DISPLAY AVAILABLE SHOWS

    def list_shows(self) -> list[Show]:

        rows = self.conn.execute("""
            SELECT
                s.show_id,
                m.title,
                m.language,
                m.certificate,
                s.show_date,
                s.show_time,
                COALESCE(s.city, 'Hyderabad') AS city,
                COALESCE(s.theatre, 'Prabhas Cinemas') AS theatre,
                s.screen,
                s.ticket_price
            FROM shows s
            JOIN movies m
            ON s.movie_id = m.movie_id
            ORDER BY s.show_date, s.show_id
        """).fetchall()

        return [
            Show(
                r["show_id"],
                r["title"],
                r["language"],
                r["certificate"],
                r["show_date"],
                r["show_time"],
                r["city"],
                r["theatre"],
                r["screen"],
                r["ticket_price"]
            )
            for r in rows
        ]

    # CHECK AVAILABLE SEATS

    def available_seats(self, show_id: int) -> list[int]:

        row = self.conn.execute(
            "SELECT total_seats FROM shows WHERE show_id=?",
            (show_id,)
        ).fetchone()

        if not row:
            raise ValueError("Show ID not found")

        taken = set()

        for b in self.conn.execute(
            """
            SELECT seats FROM bookings
            WHERE show_id=? AND status='CONFIRMED'
            """,
            (show_id,)
        ):

            taken.update(
                int(x)
                for x in b["seats"].split(",")
                if x
            )

        return [
            s for s in range(1, row["total_seats"] + 1)
            if s not in taken
        ]

    # BOOK TICKETS

    def book_tickets(
        self,
        show_id: int,
        customer: str,
        phone: str,
        seats: Iterable[int]
    ) -> dict:

        if not customer.strip() or not phone.strip():
            raise ValueError("Customer name and phone are required")

        seats = sorted(set(int(s) for s in seats))

        if not seats or any(s < 1 or s > 40 for s in seats):
            raise ValueError(
                "Seat numbers must be between 1 and 40"
            )

        show = self.conn.execute(
            "SELECT ticket_price FROM shows WHERE show_id=?",
            (show_id,)
        ).fetchone()

        if not show:
            raise ValueError("Show ID not found")

        available = set(self.available_seats(show_id))

        if not set(seats).issubset(available):

            unavailable = sorted(set(seats) - available)

            raise ValueError(
                f"Seat(s) already booked: {unavailable}"
            )

        booking_id = "MTB" + uuid4().hex.upper()

        amount = len(seats) * float(show["ticket_price"])

        self.conn.execute(
            """
            INSERT INTO bookings
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                booking_id,
                show_id,
                customer.strip(),
                phone.strip(),
                ",".join(map(str, seats)),
                amount,
                datetime.now().isoformat(timespec="seconds"),
                "CONFIRMED"
            )
        )

        self.conn.commit()

        return {
            "booking_id": booking_id,
            "show_id": show_id,
            "customer": customer,
            "seats": seats,
            "amount": amount,
            "status": "CONFIRMED"
        }

    # CANCEL BOOKING

    def cancel_booking(self, booking_id: str) -> bool:

        cur = self.conn.execute(
            """
            UPDATE bookings
            SET status='CANCELLED'
            WHERE booking_id=?
            AND status='CONFIRMED'
            """,
            (booking_id,)
        )

        self.conn.commit()

        return cur.rowcount == 1

    # FIND BOOKING

    def find_booking(self, booking_id: str):

        return self.conn.execute("""
            SELECT
                b.*,
                m.title,
                s.show_date,
                s.show_time,
                s.screen
            FROM bookings b
            JOIN shows s ON b.show_id=s.show_id
            JOIN movies m ON s.movie_id=m.movie_id
            WHERE b.booking_id=?
        """, (booking_id,)).fetchone()

    def find_bookings_by_phone(self, phone: str):

        return self.conn.execute("""
            SELECT
                b.*,
                m.title,
                s.show_date,
                s.show_time,
                s.screen
            FROM bookings b
            JOIN shows s ON b.show_id=s.show_id
            JOIN movies m ON s.movie_id=m.movie_id
            WHERE b.phone=?
            ORDER BY b.booked_at DESC, b.booking_id DESC
        """, (phone.strip(),)).fetchall()

    # BOOKING SUMMARY

    def booking_summary(self) -> dict:

        return dict(
            self.conn.execute("""
                SELECT
                    COUNT(*) AS total,

                    COALESCE(
                        SUM(
                            CASE WHEN status='CONFIRMED'
                            THEN 1 ELSE 0 END
                        ), 0
                    ) AS confirmed,

                    COALESCE(
                        SUM(
                            CASE WHEN status='CANCELLED'
                            THEN 1 ELSE 0 END
                        ), 0
                    ) AS cancelled,

                    COALESCE(
                        SUM(
                            CASE WHEN status='CONFIRMED'
                            THEN amount ELSE 0 END
                        ), 0
                    ) AS revenue

                FROM bookings
            """).fetchone()
        )


# DISPLAY SHOWS

def print_shows(system: MovieTicketBookingSystem) -> None:

    print("\nAVAILABLE PRABHAS MOVIES AND SHOWTIMES")

    print(
        "ID   MOVIE                 LANG     DATE         "
        "TIME      SCREEN    PRICE"
    )

    print("-" * 90)

    for s in system.list_shows():

        print(
            f"{s.show_id:<4} "
            f"{s.movie:<21} "
            f"{s.language:<8} "
            f"{s.show_date}  "
            f"{s.show_time:<9} "
            f"{s.screen:<9} "
            f"₹{s.price:.2f}"
        )


# DEMO MODE

def demo(system: MovieTicketBookingSystem) -> None:

    print("=" * 78)

    print(
        "PRABHAS MOVIE TICKET BOOKING SYSTEM | "
        "COURSE END PROJECT | A9513"
    )

    print("=" * 78)

    print_shows(system)

    show_id = 104

    print(
        f"\nCHECKING SEAT AVAILABILITY FOR SHOW {show_id} ..."
    )

    print(
        "Available seats (first 12):",
        system.available_seats(show_id)[:12]
    )

    booking = system.book_tickets(
        show_id,
        "A. Rakesh Yadav",
        "9876543210",
        [7, 8]
    )

    print("\nBOOKING SUCCESSFUL")

    print(f"Booking ID : {booking['booking_id']}")
    print(f"Customer   : {booking['customer']}")
    print(f"Seats      : {booking['seats']}")
    print(f"Amount     : ₹{booking['amount']:.2f}")
    print(f"Status     : {booking['status']}")

    print("\nVALIDATING DOUBLE-BOOKING PROTECTION ...")

    try:

        system.book_tickets(
            show_id,
            "Test User",
            "9000000000",
            [8]
        )

    except ValueError as exc:

        print("Rejected as expected:", exc)

    print("\nBOOKING LOOKUP")

    found = system.find_booking(
        booking["booking_id"]
    )

    print(
        f"{found['booking_id']} | "
        f"{found['title']} | "
        f"{found['show_date']} "
        f"{found['show_time']} | "
        f"{found['status']}"
    )

    print("\nCANCELLATION TEST")

    print(
        "Cancellation result:",
        "SUCCESS"
        if system.cancel_booking(booking["booking_id"])
        else "FAILED"
    )

    print("\nSummary:", system.booking_summary())

    print("\nDEMO COMPLETED SUCCESSFULLY")


# INTERACTIVE MODE

def interactive(system: MovieTicketBookingSystem) -> None:

    while True:

        print("""
1. View shows
2. View seats
3. Book tickets
4. Lookup booking
5. Cancel booking
6. Exit
""")

        choice = input("Select an option: ").strip()

        try:

            if choice == "1":

                print_shows(system)

            elif choice == "2":

                sid = int(input("Show ID: "))

                print(
                    "Available seats:",
                    system.available_seats(sid)
                )

            elif choice == "3":

                sid = int(input("Show ID: "))

                name = input("Customer name: ")

                phone = input("Phone: ")

                available = system.available_seats(sid)
                if not available:
                    raise ValueError("No seats available for this show")
                seats = [available[0]]

                print(
                    "Booking confirmed:",
                    system.book_tickets(
                        sid,
                        name,
                        phone,
                        seats
                    )
                )

            elif choice == "4":

                b = system.find_booking(
                    input("Booking ID: ").strip()
                )

                print(
                    dict(b)
                    if b
                    else "Booking not found"
                )

            elif choice == "5":

                print(
                    "Cancelled"
                    if system.cancel_booking(
                        input("Booking ID: ").strip()
                    )
                    else "Booking not found or already cancelled"
                )

            elif choice == "6":

                print("Thank you for using the system!")

                break

            else:

                print("Please select a valid option.")

        except (ValueError, sqlite3.Error) as exc:

            print("Error:", exc)


# MAIN FUNCTION

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Prabhas Movie Ticket Booking System"
    )

    parser.add_argument(
        "--demo",
        action="store_true",
        help="run a reproducible end-to-end demonstration"
    )

    parser.add_argument(
        "--db",
        default=DB_FILE,
        help="SQLite database path"
    )

    args = parser.parse_args()

    system = MovieTicketBookingSystem(args.db)

    try:

        demo(system) if args.demo else interactive(system)

    finally:

        system.close()


if __name__ == "__main__":
    main()

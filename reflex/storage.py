from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .types import State, json_text, strict_json

SEED_VERSION = "local-support-v1"
SEED_ORDERS = [
    ("O-100", "C-100", "Keyboard", 8900, "delivered", 7, 1),
    ("O-101", "C-100", "Monitor", 25900, "delivered", 45, 1),
    ("O-102", "C-100", "Digital course", 3900, "delivered", 3, 0),
    ("O-200", "C-200", "Headphones", 12900, "delivered", 5, 1),
]
SEED_KNOWLEDGE = [
    ("refund-policy", "Refund policy / 退款政策", "refund return 退款 退货 退款政策",
     "Full refunds are available for refundable paid/delivered orders within 30 days. "
     "The customer must request a refund, own the order, and the application identity "
     "must have refund scope. Digital non-refundable products are excluded. "
     "Otherwise explain the restriction; create a support ticket only when requested. "
     "可退款商品在30天内可全额退款；数字商品不可退款。"),
    ("delivery-policy", "Delivery help / 配送帮助", "delivery shipping tracking 配送 物流 发货",
     "Use get_order to inspect the actual order. Delivery support can be recorded "
     "in a ticket when the customer requests help. Never invent tracking events."),
    ("account-help", "Account help / 账户帮助", "account login password 账户 登录 密码",
     "Password reset is available on the account settings page. This local agent "
     "cannot reset a password; it can create a support ticket when requested."),
]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
                product TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL, age_days INTEGER NOT NULL, refundable INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
                order_id TEXT REFERENCES orders(id), reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open');
            CREATE TABLE IF NOT EXISTS ticket_notes (
                id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS refunds (
                id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
                amount_cents INTEGER NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge USING fts5(id UNINDEXED, title, keywords, body);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, step INTEGER NOT NULL,
                role TEXT NOT NULL, logical_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (
                operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def seed(self) -> None:
        if self.db.execute("SELECT 1 FROM metadata WHERE key='seed_version'").fetchone():
            return
        with self.transaction():
            self.db.executemany("INSERT INTO customers VALUES (?, ?, ?)", [
                ("C-100", "Lin", "lin@example.test"), ("C-200", "Alex", "alex@example.test")])
            self.db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", SEED_ORDERS)
            self.db.executemany("INSERT INTO knowledge VALUES (?, ?, ?, ?)", SEED_KNOWLEDGE)
            self.db.execute("INSERT INTO metadata VALUES ('seed_version', ?)", (SEED_VERSION,))

    def save(self, state: State) -> None:
        self.db.execute("INSERT INTO sessions VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                        (state.session_id, json_text(state.to_dict())))

    def load(self, session_id: str) -> State:
        row = self.db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown session: {session_id}")
        return State(**strict_json(row["state"]))

    def event(self, session_id: str, kind: str, payload: dict) -> None:
        self.db.execute("INSERT INTO events(session_id, at, kind, payload) VALUES (?, ?, ?, ?)",
                        (session_id, datetime.now(timezone.utc).isoformat(), kind, json_text(payload)))

    def record_call(self, session_id: str, step: int, role: str, logical_id: str,
                    attempt: int, payload: dict) -> None:
        self.db.execute("INSERT INTO calls(session_id, step, role, logical_id, attempt, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, step, role, logical_id, attempt, json_text(payload)))

    def calls(self, session_id: str) -> list[dict]:
        return [{**dict(row), "payload": strict_json(row["payload"])} for row in self.db.execute(
            "SELECT * FROM calls WHERE session_id=? ORDER BY id", (session_id,))]

    def metrics(self, session_id: str) -> dict:
        calls = self.calls(session_id)
        roles = {}
        for role in ("jev", "small", "strong"):
            selected = [r for r in calls if r["role"] == role]
            payloads = [r["payload"] for r in selected]
            roles[role] = {
                "calls": len({r["logical_id"] for r in selected}), "attempts": len(selected),
                "input_tokens": self._known_sum(payloads, "input_tokens"),
                "output_tokens": self._known_sum(payloads, "output_tokens"),
                "cost_usd": self._known_sum(payloads, "cost_usd"),
                "cost_cny_lower": self._known_sum(payloads, "cost_cny_lower"),
                "cost_cny_upper": self._known_sum(payloads, "cost_cny_upper"),
                "prompt_cache_hit_tokens": self._known_sum(payloads, "prompt_cache_hit_tokens"),
                "prompt_cache_miss_tokens": self._known_sum(payloads, "prompt_cache_miss_tokens"),
                "http_latency_ms": sum(p["latency_ms"] for p in payloads),
            }
        payloads = [r["payload"] for r in calls]
        operations = {}
        for name in sorted({p.get("operation", "unknown") for p in payloads}):
            selected = [r for r in calls if r["payload"].get("operation", "unknown") == name]
            values = [r["payload"] for r in selected]
            operations[name] = {"calls": len({r["logical_id"] for r in selected}), "attempts": len(selected),
                                **{key: self._known_sum(values, key) for key in
                                   ("input_tokens", "output_tokens", "cost_usd", "cost_cny_lower", "cost_cny_upper")}}
        return {"providers": roles, "cost_usd": self._known_sum(payloads, "cost_usd"),
                "operations": operations,
                "cost_cny_lower": self._known_sum(payloads, "cost_cny_lower"),
                "cost_cny_upper": self._known_sum(payloads, "cost_cny_upper"),
                "prompt_cache_hit_tokens": self._known_sum(payloads, "prompt_cache_hit_tokens"),
                "prompt_cache_miss_tokens": self._known_sum(payloads, "prompt_cache_miss_tokens")}

    @staticmethod
    def _known_sum(rows: list[dict], field: str):
        values = [row.get(field) for row in rows]
        return None if any(v is None for v in values) else sum(values)

    def export(self, session_id: str) -> dict:
        state = self.load(session_id)
        events = [{**dict(row), "payload": strict_json(row["payload"])} for row in self.db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY id", (session_id,))]
        return {"state": state.to_dict(), "events": events, "calls": self.calls(session_id),
                "metrics": self.metrics(session_id)}

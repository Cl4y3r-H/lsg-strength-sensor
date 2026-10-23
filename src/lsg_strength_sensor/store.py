"""Estado local persistente (SQLite).

Garantías que entrega:
* Cada serie de wger se evalúa una sola vez (tabla processed_sets + marca de agua).
* El tope diario se calcula sobre lo ya acreditado ese día local.
* Cada crédito tiene un client_ref estable y queda en una cola hasta que LSG
  lo confirma, así un corte de red nunca pierde ni duplica puntos.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import CreditRecord, CreditStatus, DayEvaluation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_sets (
    log_id INTEGER PRIMARY KEY,
    local_day TEXT NOT NULL,
    performed_at TEXT NOT NULL,
    exercise_id INTEGER NOT NULL,
    reps INTEGER NOT NULL,
    weight_kg REAL NOT NULL,
    valid INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    muscles TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sets_day ON processed_sets(local_day);
CREATE TABLE IF NOT EXISTS muscle_last_trained (muscle TEXT PRIMARY KEY, last_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exercise_best (exercise_id INTEGER PRIMARY KEY, best_weight_kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS training_days (local_day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS credits (
    client_ref TEXT PRIMARY KEY,
    local_day TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    remote_ref TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_outbox (
    client_ref TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS exercise_cache (exercise_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
"""

_COUNTED_STATUSES = (CreditStatus.PENDING.value, CreditStatus.SYNCED.value, CreditStatus.DUPLICATE.value)


class StateStore:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --------------------------------------------------------- Marca de agua
    def get_watermark(self) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key='wger_watermark'").fetchone()
        return int(row["value"]) if row else 0

    def _set_watermark(self, log_id: int) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('wger_watermark', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE CAST(excluded.value AS INTEGER) > CAST(meta.value AS INTEGER)",
            (str(log_id),),
        )

    # -------------------------------------------------- Lecturas de reglas
    def credited_points_on(self, day: date) -> int:
        q = f"SELECT COALESCE(SUM(amount),0) AS s FROM credits WHERE local_day=? AND status IN ({','.join('?' * len(_COUNTED_STATUSES))})"
        return int(self.conn.execute(q, (day.isoformat(), *_COUNTED_STATUSES)).fetchone()["s"])

    def muscle_last_trained(self) -> dict[str, datetime]:
        rows = self.conn.execute("SELECT muscle, last_at FROM muscle_last_trained").fetchall()
        return {r["muscle"]: datetime.fromisoformat(r["last_at"]) for r in rows}

    def exercise_bests(self) -> dict[int, float]:
        rows = self.conn.execute("SELECT exercise_id, best_weight_kg FROM exercise_best").fetchall()
        return {int(r["exercise_id"]): float(r["best_weight_kg"]) for r in rows}

    def training_days_in_week(self, day: date) -> set[date]:
        """Días con series válidas en la semana ISO (lunes-domingo) de `day`."""
        monday = day - timedelta(days=day.weekday())
        sunday = monday + timedelta(days=6)
        rows = self.conn.execute(
            "SELECT local_day FROM training_days WHERE local_day BETWEEN ? AND ?",
            (monday.isoformat(), sunday.isoformat()),
        ).fetchall()
        return {date.fromisoformat(r["local_day"]) for r in rows}

    def valid_sets_on(self, day: date) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM processed_sets WHERE local_day=? AND valid=1", (day.isoformat(),)
        ).fetchone()
        return int(row["c"])

    def processed_log_ids(self, log_ids: list[int]) -> set[int]:
        if not log_ids:
            return set()
        q = f"SELECT log_id FROM processed_sets WHERE log_id IN ({','.join('?' * len(log_ids))})"
        return {int(r["log_id"]) for r in self.conn.execute(q, log_ids).fetchall()}

    # ------------------------------------------- Escritura atómica de un día
    def record_evaluation(
        self,
        ev: DayEvaluation,
        credit: CreditRecord | None,
        webhook_payload: dict | None,
    ) -> None:
        """Persiste todo lo derivado de una evaluación en una sola transacción."""
        with self.transaction() as c:
            for v in ev.verdicts:
                s = v.workout_set
                c.execute(
                    "INSERT OR IGNORE INTO processed_sets VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        s.log_id, ev.local_day.isoformat(), s.performed_at.isoformat(), s.exercise_id,
                        s.reps, s.weight_kg, int(v.valid), json.dumps([r.value for r in v.reasons]),
                        json.dumps(sorted(s.primary_muscles)),
                    ),
                )
                if v.valid:
                    for m in s.primary_muscles:
                        c.execute(
                            "INSERT INTO muscle_last_trained VALUES (?,?) ON CONFLICT(muscle) "
                            "DO UPDATE SET last_at=excluded.last_at WHERE excluded.last_at > muscle_last_trained.last_at",
                            (m, s.performed_at.isoformat()),
                        )
                    c.execute(
                        "INSERT INTO exercise_best VALUES (?,?) ON CONFLICT(exercise_id) "
                        "DO UPDATE SET best_weight_kg=MAX(best_weight_kg, excluded.best_weight_kg)",
                        (s.exercise_id, s.weight_kg),
                    )
            if ev.valid_sets > 0:
                c.execute("INSERT OR IGNORE INTO training_days VALUES (?)", (ev.local_day.isoformat(),))
            if credit is not None:
                c.execute(
                    "INSERT OR IGNORE INTO credits VALUES (?,?,?,?,?,?,?,?)",
                    (
                        credit.client_ref, credit.local_day.isoformat(), credit.amount, credit.status.value,
                        credit.attempts, credit.remote_ref, credit.last_error, credit.created_at.isoformat(),
                    ),
                )
            if webhook_payload is not None:
                c.execute(
                    "INSERT OR IGNORE INTO webhook_outbox(client_ref, payload) VALUES (?,?)",
                    (webhook_payload["raw_payload"]["client_ref"], json.dumps(webhook_payload, default=str)),
                )

    def advance_watermark(self, log_id: int) -> None:
        """Se llama solo al final de una pasada completa (ver pipeline)."""
        with self.transaction():
            self._set_watermark(log_id)

    # -------------------------------------------------------- Cola de créditos
    def pending_credits(self) -> list[CreditRecord]:
        rows = self.conn.execute(
            "SELECT * FROM credits WHERE status=? ORDER BY created_at", (CreditStatus.PENDING.value,)
        ).fetchall()
        return [
            CreditRecord(
                client_ref=r["client_ref"], local_day=date.fromisoformat(r["local_day"]), amount=r["amount"],
                created_at=datetime.fromisoformat(r["created_at"]), status=CreditStatus(r["status"]),
                attempts=r["attempts"], remote_ref=r["remote_ref"], last_error=r["last_error"],
            )
            for r in rows
        ]

    def update_credit(self, client_ref: str, status: CreditStatus, remote_ref: str | None, error: str | None) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE credits SET status=?, remote_ref=COALESCE(?, remote_ref), last_error=?, attempts=attempts+1 WHERE client_ref=?",
                (status.value, remote_ref, error, client_ref),
            )

    def bump_credit_attempt(self, client_ref: str, error: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE credits SET attempts=attempts+1, last_error=? WHERE client_ref=?", (error, client_ref))

    # --------------------------------------------------------- Cola webhook
    def pending_webhooks(self) -> list[tuple[str, dict]]:
        rows = self.conn.execute("SELECT client_ref, payload FROM webhook_outbox WHERE sent=0").fetchall()
        return [(r["client_ref"], json.loads(r["payload"])) for r in rows]

    def mark_webhook(self, client_ref: str, sent: bool, error: str | None = None) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE webhook_outbox SET sent=?, attempts=attempts+1, last_error=? WHERE client_ref=?",
                (int(sent), error, client_ref),
            )

    # ------------------------------------------------------ Caché ejercicios
    def load_exercise_cache(self) -> dict[int, dict]:
        out = {}
        for r in self.conn.execute("SELECT exercise_id, data FROM exercise_cache").fetchall():
            d = json.loads(r["data"])
            out[int(r["exercise_id"])] = {**d, "primary": frozenset(d["primary"]), "secondary": frozenset(d["secondary"])}
        return out

    def save_exercise_cache(self, cache: dict[int, dict]) -> None:
        with self.transaction() as c:
            for ex_id, d in cache.items():
                c.execute(
                    "INSERT OR REPLACE INTO exercise_cache VALUES (?,?)",
                    (ex_id, json.dumps({**d, "primary": sorted(d["primary"]), "secondary": sorted(d["secondary"])})),
                )

"""Orquestación de una pasada del sensor.

    wger ──► validar ──► calcular puntos ──► persistir (SQLite, atómico)
                                                   │
                         ┌─────────────────────────┴──────────────┐
                         ▼                                        ▼
               webhook (dato crudo)                   abono de puntos (Creditor)

Persistir ANTES de enviar es lo que permite reintentar sin perder ni
duplicar: si la red falla, lo pendiente se reenvía en la siguiente pasada.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import __version__
from .config import SensorConfig
from .crediting import Creditor, safe_credit
from .http import HttpError, NetworkError
from .lsg_client import LsgCoreClient
from .models import CreditRecord, CreditStatus, DayEvaluation, WorkoutSet
from .rules import DayContext, evaluate_day, local_day, make_client_ref
from .store import StateStore
from .wger_client import WgerClient

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    new_sets: int = 0
    days_evaluated: int = 0
    points_enqueued: int = 0
    credits_synced: int = 0
    credits_pending: int = 0
    credits_rejected: int = 0
    webhooks_sent: int = 0
    webhooks_pending: int = 0


class SensorPipeline:
    def __init__(
        self,
        cfg: SensorConfig,
        store: StateStore,
        wger: WgerClient,
        creditor: Creditor,
        core: LsgCoreClient | None,
        player_id: int | None,
    ):
        self.cfg = cfg
        self.store = store
        self.wger = wger
        self.creditor = creditor
        self.core = core
        self.player_id = player_id

    # ----------------------------------------------------------------- API
    def run_once(self) -> RunSummary:
        summary = RunSummary()
        self._ingest_new_sets(summary)
        self._drain_webhooks(summary)
        self._drain_credits(summary)
        return summary

    # ------------------------------------------------------------ Ingesta
    def _ingest_new_sets(self, summary: RunSummary) -> None:
        watermark = self.store.get_watermark()
        sets = self.wger.fetch_new_sets(watermark)
        if not sets:
            return
        max_id = max(s.log_id for s in sets)

        already = self.store.processed_log_ids([s.log_id for s in sets])
        start = date.fromisoformat(self.cfg.start_date) if self.cfg.start_date else None
        fresh = [
            s for s in sets
            if s.log_id not in already and (start is None or local_day(s.performed_at, self.cfg.timezone) >= start)
        ]
        summary.new_sets = len(fresh)

        by_day: dict[date, list[WorkoutSet]] = defaultdict(list)
        for s in fresh:
            by_day[local_day(s.performed_at, self.cfg.timezone)].append(s)

        for day in sorted(by_day):
            ev = self._evaluate(day, by_day[day])
            self._persist(ev, by_day[day], summary)
            summary.days_evaluated += 1

        # La marca de agua avanza solo cuando todos los días quedaron persistidos.
        self.store.advance_watermark(max_id)
        self.store.save_exercise_cache(self.wger._exercise_cache)

    def _evaluate(self, day: date, sets: list[WorkoutSet]) -> DayEvaluation:
        ctx = DayContext(
            muscle_last_trained=self.store.muscle_last_trained(),
            exercise_bests=self.store.exercise_bests(),
            training_days_this_week=self.store.training_days_in_week(day),
            valid_sets_already=self.store.valid_sets_on(day),
            credited_already=self.store.credited_points_on(day),
        )
        ev = evaluate_day(day, sets, ctx, self.cfg.rules, self.cfg.timezone)
        log.info(
            "Día %s: %d series (%d válidas), %d pts a acreditar, rechazos=%s",
            day, ev.total_sets, ev.valid_sets, ev.credited_points, ev.reason_counts(),
        )
        return ev

    def _persist(self, ev: DayEvaluation, sets: list[WorkoutSet], summary: RunSummary) -> None:
        client_ref = make_client_ref(self.player_id, ev.local_day, [s.log_id for s in sets])
        credit = None
        if ev.credited_points > 0:
            credit = CreditRecord(
                client_ref=client_ref,
                local_day=ev.local_day,
                amount=ev.credited_points,
                created_at=datetime.now(timezone.utc),
            )
            summary.points_enqueued += ev.credited_points
        payload = self._webhook_payload(ev, client_ref) if self.cfg.lsg.send_webhook else None
        self.store.record_evaluation(ev, credit, payload)

    def _webhook_payload(self, ev: DayEvaluation, client_ref: str) -> dict:
        last = max(v.workout_set.performed_at for v in ev.verdicts)
        return {
            "player_id": self.player_id,
            "sensor_endpoint_id": self.cfg.lsg.sensor_endpoint_id,
            "players_sensor_endpoint_id": self.cfg.lsg.players_sensor_endpoint_id,
            "parsed_value": float(ev.valid_sets),
            "status": "OK" if ev.valid_sets > 0 else "IGNORED",
            "occurred_at": last.astimezone(timezone.utc).isoformat(),
            "raw_payload": {
                "client_ref": client_ref,
                "sensor": "lsg-strength",
                "sensor_version": __version__,
                "local_day": ev.local_day.isoformat(),
                "total_sets": ev.total_sets,
                "valid_sets": ev.valid_sets,
                "points_to_credit": ev.credited_points,
                "reject_reasons": ev.reason_counts(),
                "trained_muscles": sorted(ev.trained_muscles),
                "sets": [
                    {
                        "log_id": v.workout_set.log_id,
                        "exercise": v.workout_set.exercise_name,
                        "reps": v.workout_set.reps,
                        "weight_kg": v.workout_set.weight_kg,
                        "rir": v.workout_set.rir,
                        "valid": v.valid,
                        "reasons": [r.value for r in v.reasons],
                    }
                    for v in ev.verdicts
                ],
            },
        }

    # ------------------------------------------------------------ Envíos
    def _drain_webhooks(self, summary: RunSummary) -> None:
        if self.core is None or not self.cfg.lsg.send_webhook:
            return
        for ref, payload in self.store.pending_webhooks():
            try:
                self.core.ingest_webhook(payload)
                self.store.mark_webhook(ref, sent=True)
                summary.webhooks_sent += 1
            except (NetworkError, HttpError) as exc:
                self.store.mark_webhook(ref, sent=False, error=str(exc))
                summary.webhooks_pending += 1
                log.warning("Webhook %s pendiente: %s", ref, exc)

    def _drain_credits(self, summary: RunSummary) -> None:
        for record in self.store.pending_credits():
            outcome = safe_credit(self.creditor, record)
            if outcome.status == CreditStatus.PENDING:
                self.store.bump_credit_attempt(record.client_ref, outcome.error or "pendiente")
                summary.credits_pending += 1
                continue
            self.store.update_credit(record.client_ref, outcome.status, outcome.remote_ref, outcome.error)
            if outcome.status in (CreditStatus.SYNCED, CreditStatus.DUPLICATE):
                summary.credits_synced += 1
            else:
                summary.credits_rejected += 1
                log.error("Crédito %s RECHAZADO: %s", record.client_ref, outcome.error)

"""Estrategias intercambiables para abonar puntos en el ledger de LSG.

La documentación de LSG no define todavía cómo un sensor con rol `player`
convierte eventos del webhook en créditos SENSOR. Mientras se confirma con el
laboratorio, el abono queda detrás de esta interfaz y se elige por
configuración (`lsg.credit_strategy`):

* dry_run       → no envía nada; los créditos quedan PENDIENTES en la cola local.
* offline_sync  → POST /offline/sync. Idempotente por client_ref. Requiere game_id
                  y quedan etiquetados como OFFLINE_GAME (semántica imperfecta).
* adjust        → POST /points/adjust. Requiere rol developer/researcher/admin y
                  NO es idempotente: antes de reintentar se busca el client_ref
                  en el ledger para no acreditar dos veces.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from .http import HttpError, NetworkError
from .lsg_client import LsgCoreClient
from .models import CreditRecord, CreditStatus

log = logging.getLogger(__name__)


@dataclass
class CreditOutcome:
    status: CreditStatus
    remote_ref: str | None = None
    error: str | None = None


class Creditor(Protocol):
    name: str

    def credit(self, record: CreditRecord) -> CreditOutcome: ...


class DryRunCreditor:
    name = "dry_run"

    def credit(self, record: CreditRecord) -> CreditOutcome:
        log.info("[dry_run] Se acreditarían %d pts Físicos (día %s, ref %s)",
                 record.amount, record.local_day, record.client_ref)
        return CreditOutcome(CreditStatus.PENDING, error="dry_run")


class OfflineSyncCreditor:
    name = "offline_sync"

    def __init__(self, core: LsgCoreClient, point_dimension_id: int):
        self.core = core
        self.point_dimension_id = point_dimension_id

    def credit(self, record: CreditRecord) -> CreditOutcome:
        event = {
            "client_ref": record.client_ref,
            "client_generated_at": record.created_at.isoformat(),
            "point_dimension_id": self.point_dimension_id,
            "direction": "CREDIT",
            "amount": record.amount,
            "source_type": "OFFLINE_GAME",
            "payload": {"sensor": "lsg-strength", "local_day": record.local_day.isoformat()},
        }
        resp = self.core.offline_sync([event])
        result = next((r for r in (resp or {}).get("results", []) if r.get("client_ref") == record.client_ref), None)
        if result is None:
            return CreditOutcome(CreditStatus.PENDING, error=f"Respuesta sin resultado: {resp}")
        status = str(result.get("status", "")).upper()
        if status == "SYNCED":
            return CreditOutcome(CreditStatus.SYNCED, remote_ref=str(result.get("id_points_ledger")))
        if status == "DUPLICATE":
            return CreditOutcome(CreditStatus.DUPLICATE)
        return CreditOutcome(CreditStatus.REJECTED, error=result.get("rejection_reason") or status)


class AdjustCreditor:
    name = "adjust"

    def __init__(self, core: LsgCoreClient):
        self.core = core

    def _reason(self, record: CreditRecord) -> str:
        return f"lsg-strength|{record.local_day.isoformat()}|{record.client_ref}"

    def _already_in_ledger(self, record: CreditRecord) -> str | None:
        rows = self.core._call("GET", "/points/ledger", params={
            "player_id": self.core.auth.player_id, "source_type": "ADJUST",
        }) or []
        for row in rows:
            payload = row.get("payload")
            text = payload if isinstance(payload, str) else json.dumps(payload or {})
            if record.client_ref in text:
                return row.get("source_ref")
        return None

    def credit(self, record: CreditRecord) -> CreditOutcome:
        if record.attempts > 0:
            # Un intento previo pudo llegar al servidor aunque la respuesta se perdiera.
            found = self._already_in_ledger(record)
            if found:
                return CreditOutcome(CreditStatus.DUPLICATE, remote_ref=found)
        resp = self.core.adjust(record.amount, self._reason(record))
        return CreditOutcome(CreditStatus.SYNCED, remote_ref=(resp or {}).get("source_ref"))


def build_creditor(strategy: str, core: LsgCoreClient | None, point_dimension_id: int) -> Creditor:
    if strategy == "dry_run" or core is None:
        return DryRunCreditor()
    if strategy == "offline_sync":
        return OfflineSyncCreditor(core, point_dimension_id)
    if strategy == "adjust":
        return AdjustCreditor(core)
    raise ValueError(strategy)


def safe_credit(creditor: Creditor, record: CreditRecord) -> CreditOutcome:
    """Envuelve errores de red/HTTP para que un fallo deje el crédito PENDIENTE."""
    try:
        return creditor.credit(record)
    except NetworkError as exc:
        return CreditOutcome(CreditStatus.PENDING, error=f"red: {exc}")
    except HttpError as exc:
        if exc.status in (400, 404, 409, 422):
            return CreditOutcome(CreditStatus.REJECTED, error=f"HTTP {exc.status}: {exc.body}")
        return CreditOutcome(CreditStatus.PENDING, error=f"HTTP {exc.status}: {exc.body}")

"""Modelos de dominio del sensor de fuerza.

Se usan dataclasses de la biblioteca estándar para que el sensor no tenga
dependencias externas y sea fácil de empaquetar para los participantes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


@dataclass(frozen=True)
class WorkoutSet:
    """Una serie registrada por el usuario en wger (un 'workoutlog')."""

    log_id: int
    performed_at: datetime          # con zona horaria
    exercise_id: int
    exercise_name: str
    reps: int
    weight_kg: float
    rir: float | None = None
    session_id: int | None = None
    # Grupos musculares (nombres normalizados, p. ej. "chest", "quads").
    primary_muscles: frozenset[str] = field(default_factory=frozenset)
    secondary_muscles: frozenset[str] = field(default_factory=frozenset)


class RejectReason(str, Enum):
    """Motivos por los que una serie no cuenta como 'efectiva'."""

    IMPLAUSIBLE_REPS = "IMPLAUSIBLE_REPS"
    IMPLAUSIBLE_WEIGHT = "IMPLAUSIBLE_WEIGHT"
    UNREALISTIC_PROGRESSION = "UNREALISTIC_PROGRESSION"
    MUSCLE_COOLDOWN = "MUSCLE_COOLDOWN"
    WEEKLY_FREQUENCY_EXCEEDED = "WEEKLY_FREQUENCY_EXCEEDED"
    SESSION_VOLUME_EXCEEDED = "SESSION_VOLUME_EXCEEDED"
    NO_MUSCLE_DATA = "NO_MUSCLE_DATA"


@dataclass
class SetVerdict:
    workout_set: WorkoutSet
    valid: bool
    reasons: list[RejectReason] = field(default_factory=list)


@dataclass
class DayEvaluation:
    """Resultado de validar y convertir los registros de un día local."""

    local_day: date
    verdicts: list[SetVerdict]
    valid_sets: int
    raw_points: int            # puntos antes de aplicar el tope diario
    credited_points: int       # puntos efectivamente a acreditar hoy
    cap_remaining_before: int
    trained_muscles: frozenset[str]

    @property
    def total_sets(self) -> int:
        return len(self.verdicts)

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.verdicts:
            for r in v.reasons:
                counts[r.value] = counts.get(r.value, 0) + 1
        return counts


class CreditStatus(str, Enum):
    PENDING = "PENDING"        # registrado localmente, aún no confirmado por LSG
    SYNCED = "SYNCED"          # LSG confirmó el crédito
    DUPLICATE = "DUPLICATE"    # LSG indicó que ya lo tenía (idempotencia)
    REJECTED = "REJECTED"      # LSG lo rechazó; requiere revisión manual


@dataclass
class CreditRecord:
    client_ref: str            # UUID determinista: clave de idempotencia
    local_day: date
    amount: int
    created_at: datetime
    status: CreditStatus = CreditStatus.PENDING
    attempts: int = 0
    remote_ref: str | None = None
    last_error: str | None = None

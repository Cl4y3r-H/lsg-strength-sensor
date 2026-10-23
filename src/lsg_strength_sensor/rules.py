"""Reglas de validación conductual y conversión a puntos.

Funciones puras (sin red ni base de datos) para poder probarlas de forma
exhaustiva: son el núcleo verificable de la Propuesta §4.4 y del GDD §2.

Qué valida (coherencia estructural, NO veracidad biométrica):
  1. Plausibilidad de cada serie (repeticiones y carga dentro de rangos humanos).
  2. Frecuencia semanal máxima de días de entrenamiento.
  3. Enfriamiento por grupo muscular entre días distintos.
  4. Progresión de carga realista frente al mejor registro previo del ejercicio.
  5. Volumen diario máximo de series.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import RulesConfig
from .models import DayEvaluation, RejectReason, SetVerdict, WorkoutSet

# Espacio de nombres fijo para generar client_ref deterministas (UUIDv5).
_NAMESPACE = uuid.UUID("6c1f7a52-1d1e-4f0b-9a57-6e5a2b9c4d10")


@dataclass
class DayContext:
    """Estado histórico necesario para evaluar un día (lo entrega StateStore)."""

    muscle_last_trained: dict[str, datetime]
    exercise_bests: dict[int, float]
    training_days_this_week: set[date]
    valid_sets_already: int        # series válidas de este día ya procesadas antes
    credited_already: int          # puntos de este día ya acreditados/encolados


def local_day(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def points_for_sets(valid_sets: int, rules: RulesConfig) -> int:
    """P_fisico = floor(S / sets_per_block) * points_per_block  (GDD §2)."""
    return (valid_sets // rules.sets_per_block) * rules.points_per_block


def evaluate_day(
    day: date,
    sets: list[WorkoutSet],
    ctx: DayContext,
    rules: RulesConfig,
    tz: ZoneInfo,
) -> DayEvaluation:
    """Valida las series nuevas de `day` y calcula cuántos puntos acreditar.

    El cálculo es acumulativo dentro del día: si el sensor procesa el
    entrenamiento en varias pasadas (p. ej. durante la sesión), los bloques
    de 3 series se completan entre pasadas y el tope diario se respeta.
    """
    verdicts: list[SetVerdict] = []
    bests = dict(ctx.exercise_bests)
    valid_so_far = ctx.valid_sets_already
    cooldown = timedelta(hours=rules.muscle_cooldown_hours)

    week_full = (
        day not in ctx.training_days_this_week
        and len(ctx.training_days_this_week) >= rules.max_training_days_per_week
    )

    for s in sorted(sets, key=lambda x: (x.performed_at, x.log_id)):
        reasons: list[RejectReason] = []

        if not (1 <= s.reps <= rules.max_reps):
            reasons.append(RejectReason.IMPLAUSIBLE_REPS)
        if not (0 <= s.weight_kg <= rules.max_weight_kg):
            reasons.append(RejectReason.IMPLAUSIBLE_WEIGHT)

        muscles = set(s.primary_muscles)
        if rules.count_secondary_muscles:
            muscles |= s.secondary_muscles
        if not s.primary_muscles:
            reasons.append(RejectReason.NO_MUSCLE_DATA)

        if week_full:
            reasons.append(RejectReason.WEEKLY_FREQUENCY_EXCEEDED)

        for m in muscles:
            last = ctx.muscle_last_trained.get(m)
            # Solo cuenta un entrenamiento de un DÍA ANTERIOR: varias series del
            # mismo músculo en la misma sesión/día son legítimas.
            if last and local_day(last, tz) < day and s.performed_at - last < cooldown:
                reasons.append(RejectReason.MUSCLE_COOLDOWN)
                break

        best = bests.get(s.exercise_id)
        if best and best > 0 and s.weight_kg > best * (1 + rules.max_progression_pct / 100):
            reasons.append(RejectReason.UNREALISTIC_PROGRESSION)

        if not reasons and valid_so_far >= rules.max_sets_per_day:
            reasons.append(RejectReason.SESSION_VOLUME_EXCEEDED)

        valid = not reasons
        if valid:
            valid_so_far += 1
            bests[s.exercise_id] = max(bests.get(s.exercise_id, 0.0), s.weight_kg)
        verdicts.append(SetVerdict(workout_set=s, valid=valid, reasons=reasons))

    new_valid = valid_so_far - ctx.valid_sets_already
    raw_points = points_for_sets(valid_so_far, rules)
    capped_total = min(raw_points, rules.daily_cap)
    to_credit = max(0, capped_total - ctx.credited_already)
    trained = frozenset(m for v in verdicts if v.valid for m in v.workout_set.primary_muscles)

    return DayEvaluation(
        local_day=day,
        verdicts=verdicts,
        valid_sets=new_valid,
        raw_points=raw_points,
        credited_points=to_credit,
        cap_remaining_before=max(0, rules.daily_cap - ctx.credited_already),
        trained_muscles=trained,
    )


def make_client_ref(player_id: int | None, day: date, log_ids: list[int]) -> str:
    """Clave de idempotencia estable: mismo jugador + día + series → mismo UUID."""
    digest = hashlib.sha256(",".join(map(str, sorted(log_ids))).encode()).hexdigest()[:16]
    return str(uuid.uuid5(_NAMESPACE, f"lsg-strength:{player_id}:{day.isoformat()}:{digest}"))

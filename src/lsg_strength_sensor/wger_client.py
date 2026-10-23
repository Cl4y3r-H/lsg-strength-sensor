"""Cliente de la API REST de wger (fuente de los registros de entrenamiento).

IMPORTANTE: los nombres de campo de wger cambiaron con la reestructuración de
rutinas (workout → routine). El mapeo se concentra en `_parse_log` y
`_parse_exercise` para que, si la API difiere, solo haya que tocar esas dos
funciones. Verificar contra el esquema real: GET {base_url}/api/v2/schema
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .config import WgerConfig
from .http import request_json
from .models import WorkoutSet

log = logging.getLogger(__name__)

_LB_TO_KG = 0.45359237
_WGER_UNIT_LB = 2  # en wger: 1 = kg, 2 = lb (VERIFICAR en /api/v2/setting-weightunit/)


class WgerClient:
    def __init__(self, cfg: WgerConfig, tz: ZoneInfo, exercise_cache: dict[int, dict] | None = None):
        self.cfg = cfg
        self.tz = tz
        self.base = cfg.base_url.rstrip("/") + "/api/v2"
        self._exercise_cache: dict[int, dict] = exercise_cache if exercise_cache is not None else {}

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"{self.cfg.auth_scheme} {self.cfg.token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return request_json("GET", f"{self.base}/{path.lstrip('/')}", headers=self._headers(), params=params)

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict]:
        offset = 0
        while True:
            page = self._get(path, {**params, "limit": self.cfg.page_size, "offset": offset})
            results = page.get("results", []) if isinstance(page, dict) else page
            yield from results
            if not isinstance(page, dict) or not page.get("next"):
                return
            offset += self.cfg.page_size

    # ------------------------------------------------------------- Públicos
    def fetch_new_sets(self, after_log_id: int) -> list[WorkoutSet]:
        """Devuelve las series con id > after_log_id (marca de agua).

        Se pide orden descendente por id y se corta al llegar a la marca de
        agua, así cada ejecución solo descarga lo nuevo. Si el servidor
        ignora `ordering`, el filtro local igual garantiza la corrección.
        """
        new_raw: list[dict] = []
        for raw in self._paginate("workoutlog/", {"ordering": "-id"}):
            if int(raw["id"]) <= after_log_id:
                break
            new_raw.append(raw)

        sets = [s for s in (self._parse_log(r) for r in new_raw) if s is not None]
        sets.sort(key=lambda s: (s.performed_at, s.log_id))
        log.info("wger: %d series nuevas (marca de agua %d)", len(sets), after_log_id)
        return sets

    def list_muscles(self) -> list[dict]:
        """Útil para construir `muscle_map` en la configuración."""
        return list(self._paginate("muscle/", {}))

    # ------------------------------------------------------------ Parsing
    def _parse_log(self, raw: dict) -> WorkoutSet | None:
        try:
            exercise_id = int(raw.get("exercise") or raw.get("exercise_base"))
            reps = int(float(raw.get("repetitions") if raw.get("repetitions") is not None else raw.get("reps", 0)))
            weight = float(raw.get("weight") or 0.0)
            if raw.get("weight_unit") == _WGER_UNIT_LB:
                weight *= _LB_TO_KG
            performed_at = self._parse_datetime(raw["date"])
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Registro wger %s ignorado por formato inesperado: %s", raw.get("id"), exc)
            return None

        info = self._exercise_info(exercise_id)
        rir = raw.get("rir")
        return WorkoutSet(
            log_id=int(raw["id"]),
            performed_at=performed_at,
            exercise_id=exercise_id,
            exercise_name=info["name"],
            reps=reps,
            weight_kg=round(weight, 2),
            rir=float(rir) if rir not in (None, "") else None,
            session_id=raw.get("session"),
            primary_muscles=info["primary"],
            secondary_muscles=info["secondary"],
        )

    def _parse_datetime(self, value: str) -> datetime:
        # wger puede devolver solo fecha ("2026-10-13") o fecha-hora ISO.
        if len(value) == 10:
            dt = datetime.combine(datetime.fromisoformat(value).date(), time(12, 0))
            return dt.replace(tzinfo=self.tz)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=self.tz)

    def _exercise_info(self, exercise_id: int) -> dict:
        if exercise_id not in self._exercise_cache:
            raw = self._get(f"exerciseinfo/{exercise_id}/")
            self._exercise_cache[exercise_id] = self._parse_exercise(raw)
        return self._exercise_cache[exercise_id]

    def _parse_exercise(self, raw: dict) -> dict:
        name = raw.get("name") or ""
        for tr in raw.get("translations", []) or []:
            if tr.get("name"):
                name = tr["name"]
                if tr.get("language") == 4:  # 4 = español en wger (VERIFICAR)
                    break
        return {
            "name": name or f"exercise-{raw.get('id')}",
            "primary": self._normalize_muscles(raw.get("muscles", [])),
            "secondary": self._normalize_muscles(raw.get("muscles_secondary", [])),
        }

    def _normalize_muscles(self, muscles: list[dict]) -> frozenset[str]:
        groups = set()
        for m in muscles or []:
            mapped = self.cfg.muscle_map.get(str(m.get("id")))
            groups.add(mapped or (m.get("name_en") or m.get("name") or "unknown").strip().lower())
        return frozenset(groups)

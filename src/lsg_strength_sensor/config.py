"""Carga de configuración desde un archivo TOML + variables de entorno.

Los secretos (contraseña LSG y token de wger) NUNCA se leen del TOML:
solo de variables de entorno, para que el archivo de configuración pueda
compartirse o versionarse sin filtrar credenciales.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RulesConfig:
    """Parámetros de la economía y de la validación (GDD §2 / Propuesta §4.4)."""

    sets_per_block: int = 3
    points_per_block: int = 10
    daily_cap: int = 30
    # PENDIENTE DE DECISIÓN: la propuesta dice 24 h y el GDD 48 h.
    muscle_cooldown_hours: int = 48
    count_secondary_muscles: bool = False
    max_reps: int = 50
    max_weight_kg: float = 500.0
    # Aumento máximo de carga aceptado frente al mejor registro previo del
    # mismo ejercicio (en %). Ej.: 20 → de 100 kg se acepta hasta 120 kg.
    max_progression_pct: float = 20.0
    max_sets_per_day: int = 40
    max_training_days_per_week: int = 6


@dataclass(frozen=True)
class WgerConfig:
    base_url: str = "https://wger.de"
    auth_scheme: str = "Token"          # "Token" (clave permanente) o "Bearer" (JWT)
    token_env: str = "WGER_TOKEN"
    page_size: int = 100
    # Mapa id de músculo wger -> grupo normalizado. Se completa en el TOML.
    muscle_map: dict[str, str] = field(default_factory=dict)

    @property
    def token(self) -> str:
        value = os.environ.get(self.token_env, "")
        if not value:
            raise ConfigError(f"Falta la variable de entorno {self.token_env}")
        return value


@dataclass(frozen=True)
class LsgConfig:
    auth_url: str = "https://lsg.diinf.usach.cl/lsg-auth"
    core_url: str = "https://lsg.diinf.usach.cl/lsg-core-api"
    email: str = ""
    password_env: str = "LSG_PASSWORD"
    # IDs obtenidos tras el alta del sensor (GET /sensors/players/{id}).
    sensor_endpoint_id: int | None = None
    players_sensor_endpoint_id: int | None = None
    # Dimensión Físico. attribute_id=2 → FISICO_BASE según el Swagger.
    attribute_id: int = 2
    point_dimension_id: int = 2
    # Requerido solo por la estrategia offline_sync (game_id obligatorio).
    game_id: int | None = None
    # dry_run | offline_sync | adjust  (ver crediting.py)
    credit_strategy: str = "dry_run"
    send_webhook: bool = True
    refresh_margin_seconds: int = 600

    @property
    def password(self) -> str:
        value = os.environ.get(self.password_env, "")
        if not value:
            raise ConfigError(f"Falta la variable de entorno {self.password_env}")
        return value


@dataclass(frozen=True)
class SensorConfig:
    timezone: ZoneInfo
    state_db: Path
    poll_interval_minutes: int
    # Fecha desde la que se procesan registros la primera vez (YYYY-MM-DD).
    start_date: str | None
    rules: RulesConfig
    wger: WgerConfig
    lsg: LsgConfig


_VALID_STRATEGIES = {"dry_run", "offline_sync", "adjust"}


def load_config(path: str | Path) -> SensorConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"No existe el archivo de configuración: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    sensor = raw.get("sensor", {})
    rules = RulesConfig(**raw.get("rules", {}))
    wger_raw = dict(raw.get("wger", {}))
    wger_raw["muscle_map"] = {str(k): str(v) for k, v in wger_raw.get("muscle_map", {}).items()}
    wger = WgerConfig(**wger_raw)
    lsg = LsgConfig(**raw.get("lsg", {}))

    if lsg.credit_strategy not in _VALID_STRATEGIES:
        raise ConfigError(f"credit_strategy inválida: {lsg.credit_strategy}")
    if lsg.credit_strategy == "offline_sync" and lsg.game_id is None:
        raise ConfigError("offline_sync requiere lsg.game_id")
    if lsg.send_webhook and lsg.sensor_endpoint_id is None:
        raise ConfigError("send_webhook requiere lsg.sensor_endpoint_id")
    if rules.sets_per_block <= 0 or rules.daily_cap < 0:
        raise ConfigError("Reglas de conversión inválidas")

    return SensorConfig(
        timezone=ZoneInfo(sensor.get("timezone", "America/Santiago")),
        state_db=Path(sensor.get("state_db", "sensor_state.sqlite3")).expanduser(),
        poll_interval_minutes=int(sensor.get("poll_interval_minutes", 30)),
        start_date=sensor.get("start_date"),
        rules=rules,
        wger=wger,
        lsg=lsg,
    )

"""Interfaz de línea de comandos.

    python -m lsg_strength_sensor -c config.toml check-lsg   # prueba login, roles y sensores
    python -m lsg_strength_sensor -c config.toml muscles     # lista músculos wger (para muscle_map)
    python -m lsg_strength_sensor -c config.toml run         # una pasada
    python -m lsg_strength_sensor -c config.toml loop        # pasadas periódicas
    python -m lsg_strength_sensor -c config.toml status      # estado local y cola
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict

from .config import ConfigError, SensorConfig, load_config
from .crediting import build_creditor
from .http import HttpError, NetworkError
from .lsg_client import LsgAuth, LsgCoreClient
from .pipeline import SensorPipeline
from .store import StateStore
from .wger_client import WgerClient

log = logging.getLogger("lsg_strength_sensor")


def _build(cfg: SensorConfig) -> tuple[SensorPipeline, StateStore]:
    store = StateStore(cfg.state_db)
    wger = WgerClient(cfg.wger, cfg.timezone, exercise_cache=store.load_exercise_cache())

    core = player_id = None
    if cfg.lsg.email:
        auth = LsgAuth(cfg.lsg)
        core = LsgCoreClient(cfg.lsg, auth)
        player_id = auth.player_id  # hace login
    elif cfg.lsg.credit_strategy != "dry_run" or cfg.lsg.send_webhook:
        raise ConfigError("lsg.email es obligatorio salvo en dry_run sin webhook")

    creditor = build_creditor(cfg.lsg.credit_strategy, core, cfg.lsg.point_dimension_id)
    return SensorPipeline(cfg, store, wger, creditor, core, player_id), store


def cmd_run(cfg: SensorConfig) -> int:
    pipeline, store = _build(cfg)
    try:
        summary = pipeline.run_once()
        print(json.dumps(asdict(summary), indent=2))
    finally:
        store.close()
    return 0


def cmd_loop(cfg: SensorConfig) -> int:
    pipeline, store = _build(cfg)
    interval = max(1, cfg.poll_interval_minutes) * 60
    try:
        while True:
            try:
                log.info("Pasada: %s", asdict(pipeline.run_once()))
            except (NetworkError, HttpError) as exc:
                log.warning("Pasada fallida, se reintenta en el siguiente ciclo: %s", exc)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


def cmd_status(cfg: SensorConfig) -> int:
    store = StateStore(cfg.state_db)
    try:
        c = store.conn
        out = {
            "watermark_log_id": store.get_watermark(),
            "processed_sets": c.execute("SELECT COUNT(*) FROM processed_sets").fetchone()[0],
            "credits_by_status": {
                r[0]: {"count": r[1], "points": r[2]}
                for r in c.execute("SELECT status, COUNT(*), SUM(amount) FROM credits GROUP BY status")
            },
            "webhooks_pending": c.execute("SELECT COUNT(*) FROM webhook_outbox WHERE sent=0").fetchone()[0],
            "last_days": [
                dict(r) for r in c.execute(
                    "SELECT local_day, SUM(valid) AS valid_sets, COUNT(*) AS total_sets "
                    "FROM processed_sets GROUP BY local_day ORDER BY local_day DESC LIMIT 7"
                )
            ],
        }
        print(json.dumps(out, indent=2, default=str))
    finally:
        store.close()
    return 0


def cmd_muscles(cfg: SensorConfig) -> int:
    wger = WgerClient(cfg.wger, cfg.timezone)
    for m in wger.list_muscles():
        print(f'"{m.get("id")}" = "{(m.get("name_en") or m.get("name") or "").lower()}"   # {m.get("name")}')
    return 0


def cmd_check_lsg(cfg: SensorConfig) -> int:
    auth = LsgAuth(cfg.lsg)
    core = LsgCoreClient(cfg.lsg, auth)
    print("health:", core.health())
    print("player_id:", auth.player_id, "roles:", auth.roles)
    print("sensores vinculados:", json.dumps(core.linked_sensors(), indent=2, ensure_ascii=False))
    print("saldo:", json.dumps(core.balance(), indent=2, ensure_ascii=False))
    if cfg.lsg.credit_strategy == "adjust" and not {"developer", "researcher", "admin"} & set(auth.roles):
        print("AVISO: la estrategia 'adjust' requiere rol developer/researcher/admin.")
    return 0


COMMANDS = {
    "run": cmd_run, "loop": cmd_loop, "status": cmd_status,
    "muscles": cmd_muscles, "check-lsg": cmd_check_lsg,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lsg_strength_sensor")
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return COMMANDS[args.command](load_config(args.config))
    except ConfigError as exc:
        log.error("Configuración: %s", exc)
        return 2

"""Cliente de LifeSync-Games: LSG-Auth (JWT) y LSG-Core-API.

Basado en el Swagger de producción (LSG-Auth 1.2.0.1, LSG-Core-API 1.2.x) y
en MANUAL.md del repositorio lsg-core-api-prod.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import LsgConfig
from .http import HttpError, request_json

log = logging.getLogger(__name__)


def _jwt_exp(token: str) -> float:
    """Lee el claim `exp` del JWT sin verificar firma (solo para planificar el refresh)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return time.time() + 60  # desconocido: forzar refresh pronto


class LsgAuth:
    """Mantiene un token válido: login → refresh antes de expirar → re-login si falla."""

    def __init__(self, cfg: LsgConfig):
        self.cfg = cfg
        self._token: str | None = None
        self._exp: float = 0.0
        self.player: dict[str, Any] | None = None

    @property
    def player_id(self) -> int:
        if self.player is None:
            self.login()
        return int(self.player["id_players"])

    @property
    def roles(self) -> list[str]:
        if self.player is None:
            self.login()
        return list(self.player.get("roles", []))

    def login(self) -> None:
        data = request_json(
            "POST",
            f"{self.cfg.auth_url}/login",
            form_body={"username": self.cfg.email, "password": self.cfg.password},
        )
        self._token = data["access_token"]
        self._exp = _jwt_exp(self._token)
        self.player = data.get("player") or self.player
        log.info("LSG login OK (player %s, roles %s)", self.player and self.player.get("id_players"), self.roles)

    def _refresh(self) -> None:
        try:
            data = request_json(
                "POST", f"{self.cfg.auth_url}/token/refresh",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            self._token = data["access_token"]
            self._exp = _jwt_exp(self._token)
            log.debug("Token LSG renovado")
        except HttpError as exc:
            if exc.status in (401, 403):
                self.login()   # el token ya expiró: el refresh exige uno vigente
            else:
                raise

    def token(self) -> str:
        if self._token is None:
            self.login()
        elif self._exp - time.time() < self.cfg.refresh_margin_seconds:
            self._refresh()
        return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        self._token = None


class LsgCoreClient:
    def __init__(self, cfg: LsgConfig, auth: LsgAuth):
        self.cfg = cfg
        self.auth = auth
        self.base = cfg.core_url.rstrip("/")

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        """Llama a la Core API; ante un 401 hace re-login una vez y reintenta."""
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self.auth.token()}"}
            try:
                return request_json(method, f"{self.base}/{path.lstrip('/')}", headers=headers, **kw)
            except HttpError as exc:
                if exc.status == 401 and attempt == 1:
                    self.auth.invalidate()
                    continue
                raise

    # ------------------------------------------------------------- Lectura
    def health(self) -> Any:
        return request_json("GET", f"{self.base}/health")

    def balance(self) -> Any:
        return self._call("GET", f"/players/{self.auth.player_id}/points/balance")

    def linked_sensors(self) -> Any:
        return self._call("GET", f"/sensors/players/{self.auth.player_id}")

    # ------------------------------------------------- Ingesta (trazabilidad)
    def ingest_webhook(self, payload: dict) -> Any:
        """POST /sensors/ingest/webhook — registra el dato crudo (no acredita puntos)."""
        return self._call("POST", "/sensors/ingest/webhook", json_body=payload)

    # --------------------------------------------- Abono de puntos (opciones)
    def offline_sync(self, events: list[dict]) -> Any:
        """POST /offline/sync — idempotente por client_ref. Responde 207 por evento."""
        return self._call(
            "POST", "/offline/sync",
            json_body={"player_id": self.auth.player_id, "game_id": self.cfg.game_id, "events": events},
        )

    def adjust(self, amount: int, reason: str) -> Any:
        """POST /players/{id}/points/adjust — NO idempotente; requiere rol developer/researcher/admin."""
        return self._call(
            "POST", f"/players/{self.auth.player_id}/points/adjust",
            json_body={
                "attribute_id": self.cfg.attribute_id,
                "direction": "CREDIT",
                "amount": amount,
                "reason": reason,
            },
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

"""Cliente HTTP mínimo (stdlib) con reintentos para errores transitorios."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, body: Any, url: str):
        super().__init__(f"HTTP {status} en {url}: {body}")
        self.status = status
        self.body = body
        self.url = url


class NetworkError(Exception):
    """Fallo de red sin respuesta HTTP (timeout, DNS, conexión rechazada)."""


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form_body: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 15.0,
    retries: int = 3,
    backoff: float = 1.5,
) -> Any:
    """Ejecuta una petición y devuelve el JSON decodificado (o None si no hay cuerpo).

    Reintenta solo errores transitorios. Los 4xx (salvo 408/429) se propagan
    de inmediato, porque reintentarlos no cambia el resultado.
    """
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"

    hdrs = {"Accept": "application/json", **(headers or {})}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body, default=str).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            body = _read_body(exc)
            if exc.code in _RETRYABLE_STATUS and attempt <= retries:
                _sleep(attempt, backoff, f"HTTP {exc.code} {url}")
                continue
            raise HttpError(exc.code, body, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt <= retries:
                _sleep(attempt, backoff, f"red: {exc}")
                continue
            raise NetworkError(f"{method} {url}: {exc}") from exc


def _read_body(exc: urllib.error.HTTPError) -> Any:
    try:
        raw = exc.read()
        return json.loads(raw) if raw else None
    except Exception:  # cuerpo no JSON
        return None


def _sleep(attempt: int, backoff: float, why: str) -> None:
    delay = backoff ** attempt
    log.warning("Reintento %d en %.1fs (%s)", attempt, delay, why)
    time.sleep(delay)

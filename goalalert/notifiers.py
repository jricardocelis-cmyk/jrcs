"""Canales de alerta: consola, Telegram y ntfy (push al celular sin registro)."""
from __future__ import annotations

import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


class ConsoleNotifier:
    def send(self, text: str, title: str = "") -> None:
        print(f"\n{'=' * 64}\n{text}\n{'=' * 64}", flush=True)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout_s: float = 10.0):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id, self.timeout_s = chat_id, timeout_s

    def send(self, text: str, title: str = "") -> None:
        body = urlencode({"chat_id": self.chat_id, "text": text}).encode()
        _post(Request(self.url, data=body), self.timeout_s)


class NtfyNotifier:
    def __init__(self, topic: str, server: str = "https://ntfy.sh", timeout_s: float = 10.0):
        self.url = f"{server.rstrip('/')}/{topic}"
        self.timeout_s = timeout_s

    def send(self, text: str, title: str = "") -> None:
        # Los headers HTTP deben ser latin-1: título ASCII; el cuerpo va en UTF-8.
        headers = {"Title": title.encode("ascii", "replace").decode() or "goalalert",
                   "Priority": "high", "Tags": "soccer"}
        _post(Request(self.url, data=text.encode("utf-8"), headers=headers), self.timeout_s)


def _post(req: Request, timeout_s: float) -> None:
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            resp.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"notificación falló: {exc}") from exc


def send_all(notifiers: list, text: str, title: str) -> int:
    """Envía por todos los canales; un canal caído no tumba a los demás. Devuelve envíos OK."""
    ok = 0
    for n in notifiers:
        try:
            n.send(text, title)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - el bucle en vivo nunca debe morir por una alerta
            log.error("%s: %s", type(n).__name__, exc)
    return ok

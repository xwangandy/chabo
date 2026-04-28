from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class TelegramError(RuntimeError):
    pass


class MessageGateway(Protocol):
    def send_ad(
        self,
        *,
        chat_id: str,
        text: str,
        button_text: str,
        button_url: str,
    ) -> str:
        ...

    def send_private_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> str | None:
        ...

    def edit_private_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        ...

    def edit_channel_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        ...

    def send_invoice(
        self,
        *,
        chat_id: str | int,
        title: str,
        description: str,
        payload: str,
        prices: list[dict[str, int | str]],
        currency: str = "XTR",
    ) -> str | None:
        ...

    def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        ...

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        ...

    def get_me(self) -> dict[str, Any]:
        ...

    def get_chat(self, *, chat_id: str | int) -> dict[str, Any]:
        ...

    def get_chat_member(self, *, chat_id: str | int, user_id: str | int) -> dict[str, Any]:
        ...

    def get_chat_administrators(self, *, chat_id: str | int) -> list[dict[str, Any]]:
        ...

    def edit_message_reply_markup(
        self,
        *,
        chat_id: str,
        message_id: str | int,
        inline_keyboard: list[list[dict[str, str]]],
    ) -> None:
        ...

    def pin_message(self, *, chat_id: str, message_id: str | int) -> None:
        ...


@dataclass
class BotApiClient:
    token: str
    http_backend: str = "auto"

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    def _post(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if self.http_backend == "curl":
            return self._post_with_curl(method, payload)
        timeout_seconds = self._request_timeout(payload)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TelegramError(exc.read().decode("utf-8", errors="replace")) from exc
        except urllib.error.URLError as exc:
            if self.http_backend == "auto":
                return self._post_with_curl(method, payload)
            raise TelegramError(str(exc)) from exc
        if not body.get("ok"):
            raise TelegramError(str(body))
        return body["result"]

    def _request_timeout(self, payload: dict[str, Any]) -> int:
        api_timeout = payload.get("timeout")
        if isinstance(api_timeout, int | float) and api_timeout > 0:
            return int(api_timeout) + 10
        return 20

    def _post_with_curl(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = self._request_timeout(payload)
        cmd = [
            "/usr/bin/curl",
            "-sS",
            "--max-time",
            str(timeout_seconds),
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload, ensure_ascii=False),
            f"{self.base_url}/{method}",
        ]
        try:
            raw = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout_seconds + 5)
            body = json.loads(raw.decode("utf-8"))
        except subprocess.CalledProcessError as exc:
            raise TelegramError(exc.output.decode("utf-8", errors="replace")) from exc
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            raise TelegramError(str(exc)) from exc
        if not body.get("ok"):
            raise TelegramError(str(body))
        return body["result"]

    def get_updates(self, *, offset: int | None = None, timeout: int = 30, limit: int = 100) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": ["message", "channel_post", "callback_query", "pre_checkout_query", "my_chat_member", "chat_member"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._post("getUpdates", payload)

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        return bool(self._post("deleteWebhook", {"drop_pending_updates": drop_pending_updates}))

    def set_webhook(
        self,
        *,
        url: str,
        secret_token: str | None = None,
        drop_pending_updates: bool = False,
    ) -> bool:
        payload: dict[str, Any] = {
            "url": url,
            "drop_pending_updates": drop_pending_updates,
            "allowed_updates": ["message", "channel_post", "callback_query", "pre_checkout_query", "my_chat_member", "chat_member"],
        }
        if secret_token:
            payload["secret_token"] = secret_token
        return bool(self._post("setWebhook", payload))

    def get_me(self) -> dict[str, Any]:
        return self._post("getMe", {})

    def get_chat(self, *, chat_id: str | int) -> dict[str, Any]:
        return self._post("getChat", {"chat_id": chat_id})

    def get_chat_member(self, *, chat_id: str | int, user_id: str | int) -> dict[str, Any]:
        return self._post("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def get_chat_administrators(self, *, chat_id: str | int) -> list[dict[str, Any]]:
        return list(self._post("getChatAdministrators", {"chat_id": chat_id}))

    def send_ad(self, *, chat_id: str, text: str, button_text: str, button_url: str) -> str:
        result = self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": [[{"text": button_text, "url": button_url}]]},
                "disable_web_page_preview": False,
            },
        )
        return str(result["message_id"])

    def send_private_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        result = self._post("sendMessage", payload)
        return str(result["message_id"])

    def edit_private_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        self._post("editMessageText", payload)

    def edit_channel_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        self._post("editMessageText", payload)

    def send_invoice(
        self,
        *,
        chat_id: str | int,
        title: str,
        description: str,
        payload: str,
        prices: list[dict[str, int | str]],
        currency: str = "XTR",
    ) -> str | None:
        result = self._post(
            "sendInvoice",
            {
                "chat_id": chat_id,
                "title": title,
                "description": description,
                "payload": payload,
                "provider_token": "",
                "currency": currency,
                "prices": prices,
            },
        )
        return str(result["message_id"])

    def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "pre_checkout_query_id": pre_checkout_query_id,
            "ok": ok,
        }
        if error_message:
            payload["error_message"] = error_message
        self._post("answerPreCheckoutQuery", payload)

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)

    def edit_message_reply_markup(
        self,
        *,
        chat_id: str,
        message_id: str | int,
        inline_keyboard: list[list[dict[str, str]]],
    ) -> None:
        self._post(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": inline_keyboard},
            },
        )

    def pin_message(self, *, chat_id: str, message_id: str | int) -> None:
        self._post(
            "pinChatMessage",
            {"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
        )


class NullGateway:
    """Useful for local CLI runs where no Telegram token is configured."""

    def send_ad(self, *, chat_id: str, text: str, button_text: str, button_url: str) -> str:
        raise TelegramError("CHABO_BOT_TOKEN is not configured")

    def send_private_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> str | None:
        return None

    def edit_private_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        return None

    def edit_channel_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        return None

    def send_invoice(
        self,
        *,
        chat_id: str | int,
        title: str,
        description: str,
        payload: str,
        prices: list[dict[str, int | str]],
        currency: str = "XTR",
    ) -> str | None:
        return None

    def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        return None

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        return None

    def get_me(self) -> dict[str, Any]:
        raise TelegramError("CHABO_BOT_TOKEN is not configured")

    def get_chat(self, *, chat_id: str | int) -> dict[str, Any]:
        raise TelegramError("CHABO_BOT_TOKEN is not configured")

    def get_chat_member(self, *, chat_id: str | int, user_id: str | int) -> dict[str, Any]:
        raise TelegramError("CHABO_BOT_TOKEN is not configured")

    def get_chat_administrators(self, *, chat_id: str | int) -> list[dict[str, Any]]:
        raise TelegramError("CHABO_BOT_TOKEN is not configured")

    def edit_message_reply_markup(
        self,
        *,
        chat_id: str,
        message_id: str | int,
        inline_keyboard: list[list[dict[str, str]]],
    ) -> None:
        return None

    def pin_message(self, *, chat_id: str, message_id: str | int) -> None:
        raise TelegramError("CHABO_BOT_TOKEN is not configured")

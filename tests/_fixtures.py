"""Shared test fixtures.

Lives outside the ``test_*`` glob so unittest discover skips it. Test files
add ``tests/`` to ``sys.path`` and ``from _fixtures import FakeGateway``.
"""

from __future__ import annotations

from chabo.telegram import TelegramError


class FakeGateway:
    def __init__(self) -> None:
        self.next_message_id = 100
        self.sent_ads = []
        self.sent_media_ads = []
        self.private_messages = []
        self.invoices = []
        self.pre_checkout_answers = []
        self.callback_answers = []
        self.edits = []
        self.text_edits = []
        self.channel_text_edits = []
        self.pins = []
        self.fail_send = False
        self.fail_pin = False
        self.bot_user = {"id": 999001, "username": "ChaBoTestBot"}
        self.chats = {}
        self.chat_members = {}
        self.chat_administrators = {}

    def send_ad(
        self,
        *,
        chat_id: str,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> str:
        if self.fail_send:
            raise TelegramError("send failed")
        self.next_message_id += 1
        message_id = str(self.next_message_id)
        if inline_keyboard is None and button_text and button_url:
            inline_keyboard = [[{"text": button_text, "url": button_url}]]
        self.sent_ads.append(
            {
                "chat_id": chat_id,
                "text": text,
                "inline_keyboard": inline_keyboard,
                "button_text": button_text,
                "button_url": button_url,
                "message_id": message_id,
            }
        )
        return message_id

    def send_media_ad(
        self,
        *,
        chat_id: str,
        media_file_id: str,
        media_type: str,
        caption: str,
        button_text: str,
        button_url: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> str:
        if self.fail_send:
            raise TelegramError("send failed")
        self.next_message_id += 1
        message_id = str(self.next_message_id)
        self.sent_media_ads.append(
            {
                "chat_id": chat_id,
                "media_file_id": media_file_id,
                "media_type": media_type,
                "caption": caption,
                "button_text": button_text,
                "button_url": button_url,
                "inline_keyboard": inline_keyboard,
                "message_id": message_id,
            }
        )
        return message_id

    def send_private_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = None,
    ) -> str | None:
        self.private_messages.append({"chat_id": str(chat_id), "text": text, "inline_keyboard": inline_keyboard, "parse_mode": parse_mode})
        return "pm_1"

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
        self.invoices.append(
            {
                "chat_id": str(chat_id),
                "title": title,
                "description": description,
                "payload": payload,
                "prices": prices,
                "currency": currency,
            }
        )
        return "inv_1"

    def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        self.pre_checkout_answers.append(
            {
                "pre_checkout_query_id": pre_checkout_query_id,
                "ok": ok,
                "error_message": error_message,
            }
        )

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self.callback_answers.append(
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            }
        )

    def get_me(self) -> dict:
        return self.bot_user

    def get_chat(self, *, chat_id: str | int) -> dict:
        key = str(chat_id)
        if key not in self.chats:
            raise TelegramError("chat not found")
        return self.chats[key]

    def get_chat_member(self, *, chat_id: str | int, user_id: str | int) -> dict:
        key = (str(chat_id), str(user_id))
        if key not in self.chat_members:
            raise TelegramError("member not found")
        return self.chat_members[key]

    def get_chat_administrators(self, *, chat_id: str | int) -> list[dict]:
        key = str(chat_id)
        if key not in self.chat_administrators:
            raise TelegramError("administrators not found")
        return self.chat_administrators[key]

    def edit_message_reply_markup(self, *, chat_id: str, message_id: str | int, inline_keyboard: list[list[dict[str, str]]]) -> None:
        self.edits.append({"chat_id": str(chat_id), "message_id": str(message_id), "inline_keyboard": inline_keyboard})

    def edit_private_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.text_edits.append({"chat_id": str(chat_id), "message_id": str(message_id), "text": text, "inline_keyboard": inline_keyboard, "parse_mode": parse_mode})

    def edit_channel_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.channel_text_edits.append({"chat_id": str(chat_id), "message_id": str(message_id), "text": text, "inline_keyboard": inline_keyboard})

    def pin_message(self, *, chat_id: str, message_id: str | int) -> None:
        if self.fail_pin:
            raise TelegramError("pin failed")
        self.pins.append({"chat_id": str(chat_id), "message_id": str(message_id)})

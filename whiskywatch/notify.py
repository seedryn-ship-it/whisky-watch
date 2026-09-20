"""알림 채널: 텔레그램(기본) / 콘솔(드라이런)."""

from __future__ import annotations

import html
import os

import requests


class ConsoleNotifier:
    def send(self, text: str) -> bool:
        print("---- [알림 미리보기] ----")
        print(html.unescape(text.replace("<b>", "").replace("</b>", "")))
        print("-------------------------")
        return True


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, http_post=None):
        self.token = token
        self.chat_id = chat_id
        self._post = http_post or (lambda url, payload: requests.post(url, json=payload, timeout=20))
        self.last_error = ""  # 실패 사유 (토큰은 포함되지 않음)

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            raise RuntimeError("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 필요합니다")
        return cls(token, chat)

    def send(self, text: str) -> bool:
        chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or [""]
        ok = True
        for chunk in chunks:
            try:
                r = self._post(
                    self.API.format(token=self.token),
                    {
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if r.status_code != 200:
                    ok = False
                    try:
                        desc = r.json().get("description", "")
                    except Exception:
                        desc = ""
                    self.last_error = f"HTTP {r.status_code} {desc}".strip()
            except requests.RequestException as e:
                ok = False
                self.last_error = f"연결 오류: {type(e).__name__}"
        return ok


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def won(n: int | float) -> str:
    return f"₩{int(round(n)):,}"

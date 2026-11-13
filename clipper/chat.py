"""Señal del chat de Twitch: picos de mensajes y emotes de risa/asombro son la mejor pista de que algo fue gracioso.
Conexión anónima de solo lectura al IRC público de Twitch (no pide cuenta ni token)."""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections import deque

from .detect.triggers import Trigger

log = logging.getLogger(__name__)
HYPE = re.compile(r"\b(kekw|lul|lmao|lmfao|omegalul|pog|poggers|pogchamp|omg|wtf|xd+|jaja+|jeje+|clip(it)?|rip|f)\b", re.I)


def twitch_channel(url: str) -> str | None:
    m = re.match(r"https?://(?:www\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?$", (url or "").strip())
    return m.group(1).lower() if m and m.group(1).lower() not in ("videos", "directory") else None


class ChatSpikes:
    """Cuenta mensajes por ventana de 6 s y avisa cuando superan 2,5 veces el ritmo habitual (mínimo 8 mensajes)."""

    def __init__(self, on_trigger, now_stream):
        self.on_trigger, self.now_stream = on_trigger, now_stream
        self.msgs: deque[tuple[float, bool]] = deque()
        self.last = 0.0

    def feed(self, text: str, now: float | None = None) -> None:
        now = now or time.time()
        self.msgs.append((now, bool(HYPE.search(text))))
        while self.msgs and now - self.msgs[0][0] > 90:
            self.msgs.popleft()
        recent = [h for t, h in self.msgs if now - t <= 6]
        base = max(1.0, (len(self.msgs) - len(recent)) / 84 * 6)               # mensajes esperados en 6 s
        hype = sum(recent)
        if len(recent) >= 8 and len(recent) >= 2.5 * base and now - self.last > 20:
            self.last = now
            t = self.now_stream()
            if t is not None:
                self.on_trigger(Trigger("chat", t - 4.0, f"chat disparado: {len(recent)} mensajes en 6 s ({hype} de risa/asombro)",
                                        min(1.5, 0.7 + hype / max(1, len(recent)))))


class TwitchChat(threading.Thread):
    def __init__(self, channel: str, spikes: ChatSpikes, stop: threading.Event):
        super().__init__(name="twitch-chat", daemon=True)
        self.channel, self.spikes, self.stop_ev = channel, spikes, stop

    def run(self) -> None:
        delay = 3
        while not self.stop_ev.is_set():
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=20)
                s.settimeout(60)
                s.sendall(f"NICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{self.channel}\r\n".encode())
                buf, delay = "", 3
                while not self.stop_ev.is_set():
                    data = s.recv(4096).decode("utf-8", "ignore")
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split("\r\n")
                    for ln in lines:
                        if ln.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif " PRIVMSG " in ln:
                            self.spikes.feed(ln.split(" :", 1)[-1])
            except OSError as e:
                log.info("chat de Twitch: %s (reintento en %ss)", e, delay)
            self.stop_ev.wait(delay)
            delay = min(delay * 2, 60)

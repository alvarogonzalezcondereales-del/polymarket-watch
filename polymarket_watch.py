#!/usr/bin/env python3
"""
polymarket_watch.py — Escáner 24/7 de Polymarket vía GitHub Actions.
Corre en servidores de GitHub (no necesita tu Mac encendida).

GitHub Secrets necesarios:
  TELEGRAM_BOT_TOKEN  (obligatorio, de @BotFather)
  TELEGRAM_CHAT_ID    (opcional, default 8934957659)
"""

import html
from html.parser import HTMLParser
import json
import os
import sys
import urllib.request
import urllib.parse
import time

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "8934957659")
LIMIT = int(os.environ.get("TRENDING_LIMIT", "5"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def api_get(url, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}


def parse_clob_token_ids(value):
    """Decode and validate Gamma's clobTokenIds value."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []

    if not isinstance(value, list):
        return []

    tokens = []
    for token in value:
        if not isinstance(token, str) or not token.strip():
            return []
        tokens.append(token.strip())
    return tokens


def escape_html_text(value):
    """Escape untrusted text inserted into Telegram HTML text nodes."""
    return html.escape(str(value), quote=False)


def _telegram_text_units(value):
    return len(value.encode("utf-16-le", errors="replace")) // 2


class _TelegramHTMLLength(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.length = 0

    def handle_data(self, data):
        self.length += _telegram_text_units(data)

    def handle_entityref(self, name):
        self.length += _telegram_text_units(html.unescape(f"&{name};"))

    def handle_charref(self, name):
        self.length += _telegram_text_units(html.unescape(f"&#{name};"))


class _TelegramHTMLTruncator(HTMLParser):
    def __init__(self, text_limit, suffix):
        super().__init__(convert_charrefs=False)
        self.text_limit = text_limit
        self.suffix = suffix
        self.length = 0
        self.output = []
        self.open_tags = []
        self.truncated = False

    def _finish_truncated(self):
        self.output.append(self.suffix)
        self.output.extend(f"</{tag}>" for tag in reversed(self.open_tags))
        self.truncated = True

    def handle_data(self, data):
        if self.truncated:
            return
        for character in data:
            units = _telegram_text_units(character)
            if self.length + units > self.text_limit:
                self._finish_truncated()
                return
            self.output.append(character)
            self.length += units

    def _handle_entity(self, source, decoded):
        if self.truncated:
            return
        units = _telegram_text_units(decoded)
        if self.length + units > self.text_limit:
            self._finish_truncated()
            return
        self.output.append(source)
        self.length += units

    def handle_entityref(self, name):
        source = f"&{name};"
        self._handle_entity(source, html.unescape(source))

    def handle_charref(self, name):
        source = f"&#{name};"
        self._handle_entity(source, html.unescape(source))

    def handle_starttag(self, tag, attrs):
        if not self.truncated:
            self.output.append(self.get_starttag_text())
            self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if self.truncated:
            return
        self.output.append(f"</{tag}>")
        if self.open_tags and self.open_tags[-1] == tag:
            self.open_tags.pop()


def truncate_telegram_html(message, max_chars=4000):
    """Truncate generated HTML by parsed text length without splitting markup."""
    counter = _TelegramHTMLLength()
    counter.feed(message)
    counter.close()
    if counter.length <= max_chars:
        return message

    suffix = "..." if max_chars >= 3 else "." * max_chars
    truncator = _TelegramHTMLTruncator(max_chars - len(suffix), suffix)
    truncator.feed(message)
    truncator.close()
    return "".join(truncator.output)


def get_trending(limit=5):
    """Trae eventos trending de Gamma API (sin auth)."""
    return api_get(
        f"https://gamma-api.polymarket.com/events?"
        f"closed=false&limit={limit}&tag=trending"
    )


def get_prices_batch(tokens):
    """
    Obtiene precios de varios tokens de a uno (Gamma/CLOB no tienen batch).
    Timeout corto por token para no colgarse.
    """
    prices = {}
    for tid in tokens:
        query = urllib.parse.urlencode({"token_id": tid})
        data = api_get(
            f"https://clob.polymarket.com/last-trade-price?{query}",
            timeout=6,
        )
        if (isinstance(data, dict)
                and isinstance(data.get("price"), str)
                and data.get("side") in ("BUY", "SELL")):
            prices[tid] = data["price"]
        else:
            prices[tid] = None
    return prices


def telegram_send(text):
    if not TELEGRAM_TOKEN:
        print(f"[SKIP] TELEGRAM_BOT_TOKEN no configurado")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()

    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            if result.get("ok"):
                return True
        except Exception as e:
            print(f"[TG] Attempt {attempt+1}: {e}")
            time.sleep(2)
    return False


def main():
    trending = get_trending(LIMIT)

    if isinstance(trending, dict) and "error" in trending:
        msg = (f"⚠️ <b>Polymarket — API Error</b>\n"
               f"{escape_html_text(trending['error'])}")
        telegram_send(msg)
        print(msg)
        sys.exit(1)

    if not isinstance(trending, list) or len(trending) == 0:
        msg = "📊 <b>Polymarket</b>: Sin eventos trending ahora."
        sent = telegram_send(msg)
        print(msg)
        if not sent:
            print("[ERROR] No se pudo entregar el mensaje a Telegram.")
            sys.exit(1)
        return

    lines = [
        f"📊 <b>Polymarket Trends</b> — {len(trending)} eventos activos\n",
    ]

    for i, ev in enumerate(trending[:LIMIT], 1):
        title = str(ev.get("title") or "Evento")[:80]
        slug = ev.get("slug", "")
        volume = ev.get("volume", 0)
        if volume is None:
            vol_fmt = "—"
        else:
            try:
                vol_fmt = f"${float(volume):,.0f}"
            except (ValueError, TypeError):
                vol_fmt = str(volume)

        lines.append(f"{i}. <b>{escape_html_text(title)}</b>")
        if slug:
            lines.append(f"   polymarket.com/event/{escape_html_text(slug)}")
        lines.append(f"   Vol: {escape_html_text(vol_fmt)}")

        # Solo el primer mercado para no hacer mil requests por evento
        markets = ev.get("markets", [])
        if markets:
            m = markets[0]
            q = str(m.get("question") or "")[:60]
            tokens = parse_clob_token_ids(m.get("clobTokenIds", []))
            prices = get_prices_batch(tokens[:2])
            price_parts = []
            labels = ["Sí", "No"]
            for j, tid in enumerate(tokens[:2]):
                p = prices.get(tid)
                label = labels[j] if j < len(labels) else f"#{j}"
                price_parts.append(
                    f"{label}: ${escape_html_text(p)}" if p else f"{label}: —"
                )
            if q:
                lines.append(f"   📌 {escape_html_text(q)}")
            if price_parts:
                lines.append(f"      {' | '.join(price_parts)}")

        lines.append("")

    msg = truncate_telegram_html("\n".join(lines))

    sent = telegram_send(msg)
    if sent:
        print(f"[OK] Enviado a Telegram — {len(trending)} eventos, "
              f"{len(msg)} chars")
    else:
        print("[FALLBACK] No se pudo enviar a Telegram. Mensaje:")
        print(msg[:1000])
        sys.exit(1)


if __name__ == "__main__":
    main()

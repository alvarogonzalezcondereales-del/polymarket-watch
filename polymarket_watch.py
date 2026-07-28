#!/usr/bin/env python3
"""
polymarket_watch.py — Escáner 24/7 de Polymarket vía GitHub Actions.
Corre en servidores de GitHub (no necesita tu Mac encendida).

GitHub Secrets necesarios:
  TELEGRAM_BOT_TOKEN  (obligatorio, de @BotFather)
  TELEGRAM_CHAT_ID    (opcional, default 8934957659)
"""

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
        data = api_get(
            f"https://clob.polymarket.com/last-trade-price?token_id={tid}",
            timeout=6,
        )
        if isinstance(data, dict) and "price" in data:
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
        msg = f"⚠️ <b>Polymarket — API Error</b>\n{trending['error']}"
        telegram_send(msg)
        print(msg)
        sys.exit(1)

    if not isinstance(trending, list) or len(trending) == 0:
        msg = "📊 <b>Polymarket</b>: Sin eventos trending ahora."
        telegram_send(msg)
        print(msg)
        return

    lines = [
        f"📊 <b>Polymarket Trends</b> — {len(trending)} eventos activos\n",
    ]

    for i, ev in enumerate(trending[:LIMIT], 1):
        title = ev.get("title", "Evento")[:80]
        slug = ev.get("slug", "")
        volume = ev.get("volume", 0)
        try:
            vol_fmt = f"${float(volume):,.0f}"
        except (ValueError, TypeError):
            vol_fmt = str(volume)

        lines.append(f"{i}. <b>{title}</b>")
        if slug:
            lines.append(f"   polymarket.com/event/{slug}")
        lines.append(f"   Vol: {vol_fmt}")

        # Solo el primer mercado para no hacer mil requests por evento
        markets = ev.get("markets", [])
        if markets:
            m = markets[0]
            q = (m.get("question") or "")[:60]
            tokens = m.get("clobTokenIds", [])
            prices = get_prices_batch(tokens[:2])
            price_parts = []
            labels = ["Sí", "No"]
            for j, tid in enumerate(tokens[:2]):
                p = prices.get(tid)
                label = labels[j] if j < len(labels) else f"#{j}"
                price_parts.append(f"{label}: ${p}" if p else f"{label}: —")
            if q:
                lines.append(f"   📌 {q}")
            if price_parts:
                lines.append(f"      {' | '.join(price_parts)}")

        lines.append("")

    msg = "\n".join(lines)

    if len(msg) > 4000:
        msg = msg[:3997] + "..."

    sent = telegram_send(msg)
    if sent:
        print(f"[OK] Enviado a Telegram — {len(trending)} eventos, "
              f"{len(msg)} chars")
    else:
        print("[FALLBACK] No se pudo enviar a Telegram. Mensaje:")
        print(msg[:1000])


if __name__ == "__main__":
    main()

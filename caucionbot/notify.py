"""Avisos por Telegram (si hay token) y siempre por consola."""
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("caucionbot")


def avisar(cfg, texto):
    log.info(texto)
    if not (cfg.telegram_token and cfg.telegram_chat):
        return
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": cfg.telegram_chat, "text": texto}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            json.load(r)
    except Exception as e:  # un aviso fallido no debe frenar el bot
        log.warning("No pude enviar Telegram: %s", e)

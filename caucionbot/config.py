"""Configuración leída de variables de entorno (o de un archivo .env)."""
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path


def load_dotenv(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _f(name, default):
    return float(os.environ.get(name, default))


def _b(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "si", "sí", "yes")


def _t(name, default):
    h, m = os.environ.get(name, default).split(":")
    return time(int(h), int(m))


@dataclass
class Config:
    iol_user: str
    iol_pass: str
    # Ejecución
    modo: str              # "alerta" (avisa por Telegram) | "iol" (envía la orden)
    dry_run: bool
    # Horarios (hora de Buenos Aires)
    inicio: time           # empieza a muestrear para armar la tasa base del día
    ventana: time          # desde acá busca la disparada
    limite: time           # última hora para colocar
    cada_seg: int          # muestreo antes de la ventana
    cada_seg_ventana: int  # muestreo dentro de la ventana
    # Criterio de disparada
    salto_pp: float        # puntos porcentuales de TNA sobre la base
    salto_pct: float       # o porcentaje relativo sobre la base (se usa el mayor)
    confirmaciones: int    # muestras seguidas que deben confirmar la disparada
    dias_historia: int     # días previos para la base si hoy no hay muestras
    min_tna_neta: float    # piso: no colocar por debajo de esta TNA neta de costos
    costos_pp: float       # costo total anualizado (arancel + derechos), en pp de TNA
    slippage_pp: float     # cuánto por debajo de la tasa actual se pone la tasa límite
    min_monto_operado: float
    solo_disparada: bool   # False: al límite coloca igual si supera min_tna_neta
    # Montos
    reserva: float         # pesos que nunca se tocan
    monto_max: float
    monto_min: float
    # Otros
    panel: str
    db_path: str
    telegram_token: str
    telegram_chat: str

    @classmethod
    def from_env(cls):
        load_dotenv()
        e = os.environ.get
        return cls(
            iol_user=e("IOL_USER", ""),
            iol_pass=e("IOL_PASS", ""),
            modo=e("MODO", "alerta"),
            dry_run=_b("DRY_RUN", True),
            inicio=_t("HORA_INICIO", "11:05"),
            ventana=_t("HORA_VENTANA", "16:15"),
            limite=_t("HORA_LIMITE", "16:52"),
            cada_seg=int(_f("CADA_SEG", 300)),
            cada_seg_ventana=int(_f("CADA_SEG_VENTANA", 30)),
            salto_pp=_f("SALTO_PP", 4),
            salto_pct=_f("SALTO_PCT", 15),
            confirmaciones=int(_f("CONFIRMACIONES", 2)),
            dias_historia=int(_f("DIAS_HISTORIA", 10)),
            min_tna_neta=_f("MIN_TNA_NETA", 0),
            costos_pp=_f("COSTOS_PP", 2),
            slippage_pp=_f("SLIPPAGE_PP", 0.5),
            min_monto_operado=_f("MIN_MONTO_OPERADO", 50_000_000),
            solo_disparada=_b("SOLO_DISPARADA", True),
            reserva=_f("RESERVA", 0),
            monto_max=_f("MONTO_MAX", 1e12),
            monto_min=_f("MONTO_MIN", 10_000),
            panel=e("IOL_PANEL", "todos"),
            db_path=e("DB_PATH", "caucionbot.sqlite"),
            telegram_token=e("TELEGRAM_TOKEN", ""),
            telegram_chat=e("TELEGRAM_CHAT_ID", ""),
        )

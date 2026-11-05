"""Diagnóstico de la conexión con PPI. Genera un reporte sin secretos para compartir.

Uso:  python3 -m caucionbot.main doctor            (solo lecturas)
      python3 -m caucionbot.main doctor --budget   (además pide un PRESUPUESTO de caución;
                                                    no confirma, no crea ninguna orden)
"""
import json
import sys
import traceback

from .config import Config
from .ppi import LIQUIDACION, TIPO, PPIBroker, PPIClient
from .strategy import elegir

CAMPOS = ("PPI_AUTHORIZED_CLIENT", "PPI_CLIENT_KEY", "PPI_KEY", "PPI_SECRET")


class Reporte:
    def __init__(self, secretos):
        self.lineas, self.secretos = [], [s for s in secretos if s]

    def __call__(self, txt=""):
        txt = str(txt)
        for s in self.secretos:
            txt = txt.replace(s, "<SECRETO>")
        self.lineas.append(txt)
        print(txt)

    def json(self, titulo, obj, limite=3000):
        self(f"{titulo}:")
        self(json.dumps(obj, indent=2, ensure_ascii=False, default=str)[:limite])


def enmascarar_cuenta(c):
    c = str(c)
    return "*" * max(len(c) - 3, 0) + c[-3:]


def paso(r, nombre, fn):
    try:
        res = fn()
        r(f"[OK]    {nombre}")
        return res
    except Exception as e:
        r(f"[FALLA] {nombre}: {type(e).__name__}: {e}")
        r("        " + traceback.format_exc().strip().splitlines()[-1])
        return None


def doctor(con_budget=False, salida="doctor.txt"):
    cfg = Config.from_env()
    r = Reporte([cfg.ppi_authorized_client, cfg.ppi_client_key, cfg.ppi_key, cfg.ppi_secret])
    r("== CaucionBot doctor ==")
    r(f"Python {sys.version.split()[0]} | ambiente: {'SANDBOX' if cfg.ppi_sandbox else 'PRODUCCIÓN'}")
    faltan = [c for c, v in zip(CAMPOS, (cfg.ppi_authorized_client, cfg.ppi_client_key,
                                         cfg.ppi_key, cfg.ppi_secret)) if not v]
    if faltan:
        r(f"[FALLA] Faltan en .env: {', '.join(faltan)}")
        return _guardar(r, salida)

    api = PPIClient(cfg.ppi_authorized_client, cfg.ppi_client_key, cfg.ppi_key, cfg.ppi_secret, cfg.ppi_sandbox)
    paso(r, "Login", api.login)
    if not api._token:
        r("Revisá las 4 claves y que correspondan al ambiente (sandbox y producción usan claves distintas).")
        return _guardar(r, salida)

    cuentas = paso(r, "Cuentas", lambda: api.get("Account/Accounts")) or []
    r(f"        cuentas: {[enmascarar_cuenta(c.get('accountNumber')) for c in cuentas]}")
    if not cuentas and not cfg.ppi_cuenta:
        return _guardar(r, salida)
    broker = PPIBroker(cfg, client=api)
    r(f"        usando cuenta {enmascarar_cuenta(broker.cuenta)}")

    r(f"        feriado hoy: {paso(r, 'Feriado', lambda: api.get('Configuration/IsLocalHoliday'))}")

    ins = paso(r, "Buscar instrumentos CAUCIONES", broker.buscar_instrumentos) or []
    r(f"        {len(ins)} instrumentos")
    r.json("        primeros 30", ins[:30])
    mapa = broker.instrumentos()
    r(f"        interpretados (ticker -> días): {mapa}")
    if not mapa:
        r("[FALLA] No pude deducir el plazo de ningún ticker. Pegame este reporte y lo ajusto.")

    for ticker, dias in sorted(mapa.items(), key=lambda x: x[1])[:6]:
        md = paso(r, f"Cotización {ticker} ({dias}d)",
                  lambda t=ticker: api.get("MarketData/Current", ticker=t, type=TIPO, settlement=LIQUIDACION))
        r.json("        current", md, 800)
        book = paso(r, f"Puntas {ticker}",
                    lambda t=ticker: api.get("MarketData/Book", ticker=t, type=TIPO, settlement=LIQUIDACION))
        if isinstance(book, dict):
            r.json("        book (top 3)", {"date": book.get("date"), "bids": (book.get("bids") or [])[:3],
                                            "offers": (book.get("offers") or [])[:3]}, 800)

    saldos = paso(r, "Saldo disponible", lambda: api.get("Account/AvailableBalance", accountNumber=broker.cuenta))
    r.json("        saldos", saldos, 1500)
    r(f"        pesos inmediatos interpretados: {broker.pesos_disponibles():,.2f}")

    quotes = paso(r, "Cotizaciones interpretadas", broker.cotizaciones) or []
    for q in quotes:
        r(f"        {q.simbolo:>16} {q.plazo_dias:>3}d TNA {q.tna:6.2f}% vol {q.monto_operado:,.0f} fecha {q.fecha}")
    q = elegir(quotes, cfg.min_monto_operado)
    r(f"        elegida: {q.simbolo if q else None}")

    if con_budget and q:
        orden = broker._orden(cfg.monto_min, round(q.tna - cfg.slippage_pp, 2), q.simbolo)
        r.json("Cuerpo del presupuesto", {**orden, "accountNumber": enmascarar_cuenta(orden["accountNumber"])})
        budget = paso(r, "Presupuesto de caución (NO confirma)", lambda: api.post("Order/Budget", orden))
        r.json("        respuesta", budget, 3000)
    elif con_budget:
        r("[SALTEADO] Presupuesto: no hay cotización elegida.")

    activas = paso(r, "Órdenes activas", lambda: api.get("Order/ActiveOrders", accountNumber=broker.cuenta))
    r.json("        activas", activas, 1500)
    return _guardar(r, salida)


def _guardar(r, salida):
    with open(salida, "w", encoding="utf-8") as f:
        f.write("\n".join(r.lineas) + "\n")
    print(f"\nReporte guardado en {salida} (sin claves; revisalo antes de compartirlo).")
    return r.lineas

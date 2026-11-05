"""Diagnóstico de la conexión con PPI. Genera un reporte sin secretos para compartir.

Uso:  python -m caucionbot.main doctor            (solo lecturas)
      python -m caucionbot.main doctor --budget   (además pide un PRESUPUESTO de caución;
                                                   no confirma, no crea ninguna orden)
"""
import json
import sys
from datetime import date, datetime, timedelta

from .config import Config
from .ppi import LIQUIDACION, TIPO, PPIBroker, PPIClient

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
        self(f"{titulo}: " + json.dumps(obj, ensure_ascii=False, default=str)[:limite])


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
        return None


def doctor(con_budget=False, salida="doctor.txt"):
    cfg = Config.from_env()
    r = Reporte([cfg.ppi_authorized_client, cfg.ppi_client_key, cfg.ppi_key, cfg.ppi_secret])
    r("== CaucionBot doctor ==")
    r(f"Python {sys.version.split()[0]} | ambiente: {'SANDBOX' if cfg.ppi_sandbox else 'PRODUCCIÓN'} "
      f"| hora local {datetime.now():%Y-%m-%d %H:%M}")
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

    # Calendario y plazo
    r(f"        feriado hoy: {paso(r, 'Feriado hoy', broker.es_feriado)}")
    hoy = date.today()
    fer = paso(r, "Feriados próximos 30 días", lambda: broker.feriados(hoy, hoy + timedelta(days=30)))
    r(f"        {sorted(str(f) for f in fer or [])}")
    objetivo = broker.plazo_objetivo(hoy)
    t_obj = f"PESOS{objetivo}"
    r(f"        plazo al próximo hábil: {objetivo} días -> ticker {t_obj}")

    # Valores exactos que usa PPI para la orden
    for recurso in ("Operations", "OperationTerms", "OperationTypes", "QuantityTypes"):
        lista = paso(r, f"Configuration/{recurso}", lambda x=recurso: api.get(f"Configuration/{x}"))
        r(f"        {lista}")
    o = broker._orden(0, 0, t_obj)
    r(f"        valores elegidos para la orden: operation={o['operation']!r} operationTerm={o['operationTerm']!r} "
      f"operationType={o['operationType']!r} quantityType={o['quantityType']!r}")

    # Mercado: ¿se actualiza en vivo?
    r("== Cauciones en pesos (corré esto también DURANTE la rueda, idealmente 16:30-16:55) ==")
    elegida = None
    for ticker, dias in sorted(broker.instrumentos().items(), key=lambda x: x[1]):
        md = paso(r, f"Cotización {ticker}",
                  lambda t=ticker: api.get("MarketData/Current", ticker=t, type=TIPO, settlement=LIQUIDACION))
        r.json("        current", md, 400)
        if dias == objetivo and md and md.get("price"):
            elegida = (ticker, float(md["price"]), str(md.get("date", ""))[:10])
    book = paso(r, f"Puntas {t_obj}",
                lambda: api.get("MarketData/Book", ticker=t_obj, type=TIPO, settlement=LIQUIDACION))
    if isinstance(book, dict):
        r.json("        book (top 3)", {"date": book.get("date"), "bids": (book.get("bids") or [])[:3],
                                        "offers": (book.get("offers") or [])[:3]}, 800)
    intra = paso(r, f"Intradiario {t_obj}",
                 lambda: api.get("MarketData/Intraday", ticker=t_obj, type=TIPO, settlement=LIQUIDACION))
    if isinstance(intra, list):
        r.json(f"        {len(intra)} puntos; últimos 10", intra[-10:], 1500)
    if elegida:
        fresca = "SÍ" if elegida[2] == hoy.isoformat() else f"NO (es del {elegida[2]})"
        r(f"        ¿cotización {t_obj} de hoy?: {fresca}")

    # Cuenta
    saldos = paso(r, "Saldo disponible", lambda: api.get("Account/AvailableBalance", accountNumber=broker.cuenta))
    r.json("        saldos en pesos", [s for s in saldos or [] if "peso" in str(s.get("name", "")).lower()], 800)
    r(f"        pesos inmediatos interpretados: {paso(r, 'Interpretar saldo', broker.pesos_disponibles)}")

    if con_budget:
        if elegida:
            orden = broker._orden(cfg.monto_min, round(elegida[1] - cfg.slippage_pp, 2), elegida[0])
            r.json("Cuerpo del presupuesto", {**orden, "accountNumber": enmascarar_cuenta(orden["accountNumber"])})
            budget = paso(r, "Presupuesto de caución (NO confirma)", lambda: api.post("Order/Budget", orden))
            r.json("        respuesta", budget, 3000)
        else:
            r(f"[SALTEADO] Presupuesto: no hay precio para {t_obj}.")

    activas = paso(r, "Órdenes activas", lambda: api.get("Order/ActiveOrders", accountNumber=broker.cuenta))
    r.json("        activas", activas, 1500)
    desde = hoy - timedelta(days=365)
    ords = paso(r, "Historial de órdenes (1 año)", lambda: api.get(
        "Order/Orders", accountNumber=broker.cuenta, dateFrom=desde.isoformat(), dateTo=hoy.isoformat()))
    cauc = [x for x in ords or [] if "CAUC" in str(x.get("instrumentType", "")).upper()]
    r(f"        {len(ords or [])} órdenes, {len(cauc)} de caución")
    if cauc:
        r.json("        últimas cauciones", cauc[-5:], 2000)
    return _guardar(r, salida)


def _guardar(r, salida):
    with open(salida, "w", encoding="utf-8") as f:
        f.write("\n".join(r.lineas) + "\n")
    print(f"\nReporte guardado en {salida} (sin claves; revisalo antes de compartirlo).")
    return r.lineas

"""Diagnóstico de la conexión con PPI. Genera un reporte sin secretos para compartir.

Uso:  python3 -m caucionbot.main doctor            (solo lecturas)
      python3 -m caucionbot.main doctor --budget   (además pide un PRESUPUESTO de caución;
                                                    no confirma, no crea ninguna orden)
"""
import json
import sys
import traceback

from .config import Config
from datetime import date, timedelta

from .ppi import BUSQUEDAS, LIQUIDACION, TIPO, PPIBroker, PPIClient, PPIError
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

    descubrir(r, api, broker)

    ins = paso(r, "Buscar instrumentos CAUCIONES", broker.buscar_instrumentos) or []
    r(f"        {len(ins)} instrumentos")
    r.json("        primeros 30", ins[:30])
    mapa = paso(r, "Interpretar plazos", broker.instrumentos) or {}
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
    r(f"        pesos inmediatos interpretados: {paso(r, 'Interpretar saldo', broker.pesos_disponibles)}")

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


TICKERS_A_PROBAR = ["PESOS", "PESOS1", "PESOS1D", "PESOS 1D", "PESOS-1D", "PESOS1DIA", "PESOS001",
                    "CAUCION", "CAUCION1", "CAUCION1D", "CAU1", "CAU1D", "ARS1D", "1D"]


def _intento(fn):
    try:
        return "OK", fn()
    except PPIError as e:
        return "ERR", str(e).split("->", 1)[-1].strip()[:200]


def _tickers(obj):
    """Junta (ticker, descripción) de cualquier estructura anidada."""
    out = set()
    if isinstance(obj, dict):
        if "ticker" in obj:
            out.add((str(obj.get("ticker")), str(obj.get("description", ""))))
        for v in obj.values():
            out |= _tickers(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _tickers(v)
    return out


def descubrir(r, api, broker):
    """PPI no deja listar cauciones sin ticker. Probamos de todo para encontrar cómo las nombra."""
    r("== Descubrimiento de tickers de caución ==")
    for path in ("Configuration/InstrumentTypes", "Configuration/Markets",
                 "Configuration/Settlements", "Configuration/Operations"):
        estado, res = _intento(lambda p=path: api.get(p))
        r(f"  {path}: {estado} {json.dumps(res, ensure_ascii=False)[:300]}")

    r("  SearchInstrument (ticker, name, market, type):")
    for t, n in BUSQUEDAS:
        for market, tipo in (("BYMA", TIPO), ("", TIPO), ("BYMA", "")):
            estado, res = _intento(lambda: broker.buscar(t, n, market, tipo))
            if estado == "OK":
                cauc = [x for x in res if "CAUC" in str(x.get("type", "")).upper()]
                r(f"    ({t!r}, {n!r}, {market!r}, {tipo!r}) -> {len(res)} resultados, {len(cauc)} cauciones "
                  f"{json.dumps(cauc[:5] or res[:3], ensure_ascii=False)[:600]}")
            else:
                r(f"    ({t!r}, {n!r}, {market!r}, {tipo!r}) -> {res}")

    r("  MarketData/Current con tickers adivinados (type CAUCIONES):")
    for t in TICKERS_A_PROBAR:
        for liq in (LIQUIDACION, "A-24HS"):
            estado, res = _intento(lambda: api.get("MarketData/Current", ticker=t, type=TIPO, settlement=liq))
            if estado == "OK" and res:
                r(f"    {t!r} {liq}: {json.dumps(res, ensure_ascii=False)[:300]}")
            elif liq == LIQUIDACION:
                r(f"    {t!r} {liq}: {estado} {res}")

    hasta, desde = date.today(), date.today() - timedelta(days=365)
    estado, ords = _intento(lambda: api.get("Order/Orders", accountNumber=broker.cuenta,
                                            dateFrom=desde.isoformat(), dateTo=hasta.isoformat()))
    if estado == "OK":
        tipos = sorted({(str(o.get("instrumentType")), str(o.get("ticker")), str(o.get("operation")))
                        for o in ords or []})
        r(f"  Historial de órdenes del último año: {len(ords or [])} órdenes. (tipo, ticker, operación):")
        for x in tipos[:40]:
            r(f"    {x}")
    else:
        r(f"  Historial de órdenes: {ords}")
    estado, pos = _intento(lambda: api.get("Account/BalancesAndPositions", accountNumber=broker.cuenta))
    if estado == "OK":
        r(f"  Posiciones (solo tickers, sin montos): {sorted(_tickers(pos))[:60]}")
    else:
        r(f"  Posiciones: {pos}")
    r("== Fin descubrimiento ==")


def _guardar(r, salida):
    with open(salida, "w", encoding="utf-8") as f:
        f.write("\n".join(r.lineas) + "\n")
    print(f"\nReporte guardado en {salida} (sin claves; revisalo antes de compartirlo).")
    return r.lineas

"""Broker PPI (Portfolio Personal Inversiones) vía su API REST oficial.

La API pública de PPI soporta la operación "COLOCAR-CAUCION" con el tipo de
instrumento "CAUCIONES", así que el bot puede operar punta a punta:
feriados, cotización, saldo, orden y seguimiento.
Docs: https://itatppi.github.io/ppi-official-api-docs/

Verificado contra producción (doctor, nov-2026):
- Las cauciones en pesos son los tickers PESOS1 ... PESOS120 (número = días).
- MarketData/Current da el cierre del último día que operó cada plazo (no es en vivo).
  MarketData/Intraday da un punto por minuto con "price" = TNA en % y "volume" en pesos.
- El plazo que opera es el del próximo día hábil (ej. jueves antes de un fin de semana
  largo: PESOS6), según Configuration/Holidays.
- La operación se llama "COLOCAR-CAUCION" sin tilde (la doc dice con tilde). Por eso
  los valores se toman de Configuration/* comparando sin acentos.

No usamos el SDK `ppi-client` porque fija msgpack==1.0.2, que no instala en
Python 3.12+. Solo hace falta REST, así que va con la biblioteca estándar.
"""
import json
import logging
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from datetime import date, timedelta

from .strategy import Quote

log = logging.getLogger("caucionbot")

URL_PROD = "https://clientapi.portfoliopersonal.com/api/1.0/"
URL_SANDBOX = "https://clientapisbx.portfoliopersonal.com/api/1.0/"
TIPO = "CAUCIONES"
LIQUIDACION = "INMEDIATA"
PREFIJO = "PESOS"  # PESOS1 = caución en pesos a 1 día, PESOS3 = a 3 días, etc.


class PPIError(Exception):
    pass


class PPIClient:
    def __init__(self, authorized_client, client_key, api_key, api_secret, sandbox=True, timeout=15):
        self.base = URL_SANDBOX if sandbox else URL_PROD
        self.headers = {"AuthorizedClient": authorized_client, "ClientKey": client_key,
                        "Content-Type": "application/json", "Accept": "application/json"}
        self.api_key, self.api_secret, self.timeout = api_key, api_secret, timeout
        self._token = self._refresh = None
        self._exp = 0.0

    def _raw(self, method, path, params=None, body=None, extra=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, headers={**self.headers, **(extra or {})},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                txt = r.read().decode()
        except urllib.error.HTTPError as e:
            raise PPIError(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}") from e
        return json.loads(txt) if txt else None

    def _guardar_token(self, d):
        d = d[0] if isinstance(d, list) else d
        self._token, self._refresh = d["accessToken"], d.get("refreshToken")
        self._exp = time.time() + min(float(d.get("expires") or 900), 3600)

    def login(self):
        self._guardar_token(self._raw("POST", "Account/LoginApi",
                                      extra={"ApiKey": self.api_key, "ApiSecret": self.api_secret}))

    def _auth(self):
        if self._token and time.time() < self._exp - 60:
            return
        if self._refresh:
            try:
                self._guardar_token(self._raw("POST", "Account/RefreshToken",
                                              body={"refreshToken": self._refresh},
                                              extra={"Authorization": f"Bearer {self._token}"}))
                return
            except PPIError:
                pass
        self.login()

    def request(self, method, path, params=None, body=None):
        self._auth()
        try:
            return self._raw(method, path, params, body, {"Authorization": f"Bearer {self._token}"})
        except PPIError as e:
            if "-> 401" not in str(e):
                raise
            self._token = None  # token revocado: reloguear una vez
            self._auth()
            return self._raw(method, path, params, body, {"Authorization": f"Bearer {self._token}"})

    def get(self, path, **params):
        return self.request("GET", path, params=params)

    def post(self, path, body):
        return self.request("POST", path, body=body)


def _norm(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().upper().strip()


def parse_tickers_override(txt):
    """'TICKER1:1,TICKER3:3' -> {'TICKER1': 1, 'TICKER3': 3}"""
    out = {}
    for par in filter(None, (p.strip() for p in (txt or "").split(","))):
        t, d = par.rsplit(":", 1)
        out[t.strip()] = int(d)
    return out


def plazo_proximo_habil(hoy, feriados):
    """Días corridos hasta el próximo día hábil: es el plazo que se opera al cierre."""
    d = hoy + timedelta(days=1)
    while d.weekday() >= 5 or d in feriados:
        d += timedelta(days=1)
    return (d - hoy).days


class PPIBroker:
    nombre = "PPI"

    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.api = client or PPIClient(cfg.ppi_authorized_client, cfg.ppi_client_key,
                                       cfg.ppi_key, cfg.ppi_secret, cfg.ppi_sandbox)
        self.cuenta = cfg.ppi_cuenta or str(self.api.get("Account/Accounts")[0]["accountNumber"])
        self._override = parse_tickers_override(cfg.caucion_tickers)
        self._config = {}
        self._plazo = None  # (fecha, días)

    # --- Configuración ---
    def valor(self, recurso, deseado):
        """Devuelve el valor exacto que usa PPI (ej. 'COLOCAR-CAUCION'), comparando sin acentos."""
        if recurso not in self._config:
            try:
                self._config[recurso] = self.api.get(f"Configuration/{recurso}") or []
            except (PPIError, urllib.error.URLError) as e:
                log.warning("No pude leer Configuration/%s: %s", recurso, e)
                self._config[recurso] = []
        for v in self._config[recurso]:
            if _norm(v) == _norm(deseado):
                return v
        return deseado

    # --- Calendario ---
    def es_feriado(self):
        try:
            return bool(self.api.get("Configuration/IsLocalHoliday"))
        except (PPIError, urllib.error.URLError) as e:
            log.warning("No pude consultar feriados en PPI: %s", e)
            return False

    def feriados(self, desde, hasta):
        res = self.api.get("Configuration/Holidays", start=desde.isoformat(), end=hasta.isoformat(),
                           isUSA="false") or []
        return {date.fromisoformat(str(h["date"])[:10]) for h in res if not h.get("isUSA")}

    def plazo_objetivo(self, hoy=None):
        hoy = hoy or date.today()
        if not self._plazo or self._plazo[0] != hoy:
            try:
                fer = self.feriados(hoy, hoy + timedelta(days=15))
            except (PPIError, urllib.error.URLError, KeyError, ValueError) as e:
                log.warning("No pude leer feriados (%s); uso solo fines de semana", e)
                fer = set()
            self._plazo = (hoy, plazo_proximo_habil(hoy, fer))
        return self._plazo[1]

    # --- Datos de mercado ---
    def instrumentos(self):
        """Ticker -> días, para mostrar en probe/doctor."""
        if self._override:
            return dict(self._override)
        return {f"{PREFIJO}{d}": d for d in range(1, self.cfg.max_dias + 1)}

    def ticker_para(self, dias):
        for t, d in self._override.items():
            if d == dias:
                return t
        return f"{PREFIJO}{dias}"

    def cotizacion(self, ticker, dias):
        """Tasa en vivo desde el intradiario (un punto por minuto con TNA y volumen en pesos).

        MarketData/Current NO sirve para cauciones: devuelve el cierre del último día
        que operó ese plazo (verificado en producción)."""
        serie = []
        for p in self.api.get("MarketData/Intraday", ticker=ticker, type=TIPO, settlement=LIQUIDACION) or []:
            tna, f = float(p.get("price") or 0), str(p.get("date") or "")
            if tna > 0 and len(f) >= 16:
                serie.append((f[:10], f[11:16], tna, float(p.get("volume") or 0)))
        if not serie:
            return None
        serie.sort()
        fecha, hora, tna, vol = serie[-1]
        return Quote(dias, tna, vol, ticker, fecha, hora,
                     [(h, t, v) for f, h, t, v in serie if f == fecha], {"ultimo": serie[-1]})

    def cotizaciones(self, hoy=None):
        """Solo el plazo al próximo hábil (1 día, 3 los viernes, más con feriados):
        es el que opera y se dispara al cierre."""
        dias = self.plazo_objetivo(hoy)
        ticker = self.ticker_para(dias)
        try:
            q = self.cotizacion(ticker, dias)
        except PPIError as e:
            log.warning("Cotización %s: %s", ticker, e)
            return []
        return [q] if q else []

    def pesos_disponibles(self):
        total = 0.0
        for b in self.api.get("Account/AvailableBalance", accountNumber=self.cuenta) or []:
            nombre = str(b.get("name", "")).lower()
            simbolo = str(b.get("symbol", b.get("simbol", ""))).strip().upper()
            liq = str(b.get("settlement", "")).upper()
            es_pesos = "peso" in nombre or simbolo in ("$", "ARS")
            if es_pesos and liq in ("", LIQUIDACION):
                total += float(b.get("amount") or 0)
        return total

    # --- Ejecución ---
    def _orden(self, monto, tasa, ticker):
        return {"accountNumber": self.cuenta, "quantity": int(monto), "price": tasa, "ticker": ticker,
                "instrumentType": TIPO,
                "quantityType": self.valor("QuantityTypes", "DINERO"),
                "operationType": self.valor("OperationTypes", "PRECIO-LIMITE"),
                "operationTerm": self.valor("OperationTerms", "POR-EL-DIA"),
                "operationMaxDate": None,
                "operation": self.valor("Operations", "COLOCAR-CAUCION"),
                "settlement": LIQUIDACION, "activationPrice": None}

    def colocar_caucion(self, monto, tasa, plazo_dias, ticker, external_id):
        orden = self._orden(monto, tasa, ticker)
        budget = self.api.post("Order/Budget", orden) or {}
        log.info("Presupuesto PPI: %s", budget)
        # Control de cordura antes de confirmar: el presupuesto tiene que coincidir con lo pedido
        monto_b = float(budget.get("amount") or 0)
        if monto_b and abs(monto_b - monto) / monto > 0.05:
            raise PPIError(f"El presupuesto ({monto_b:,.0f}) no coincide con el monto ({monto:,.0f})")
        disc = budget.get("disclaimers") or []
        for d in disc:
            log.info("Acepto disclaimer %s: %s", d.get("code"), d.get("description"))
        orden["disclaimers"] = [{**d, "accepted": True} for d in disc]
        orden["externalID"] = external_id
        return self.api.post("Order/Confirm", orden)

    def estado_orden(self, orden):
        d = self.api.get("Order/Detail", accountNumber=self.cuenta, orderID=orden.get("id"),
                         externalID=orden.get("externalID") or "")
        return (d or {}).get("status", "?")



def resumen_probe(broker):
    lines = [f"Cuenta: {broker.cuenta}", f"Feriado hoy: {broker.es_feriado()}",
             f"Plazo al próximo hábil: {broker.plazo_objetivo()} días"]
    mapa = broker.instrumentos()
    mapa[broker.ticker_para(broker.plazo_objetivo())] = broker.plazo_objetivo()
    for ticker, dias in sorted(mapa.items(), key=lambda x: x[1]):
        try:
            q = broker.cotizacion(ticker, dias)
            lines.append(f"  {ticker:>10} {dias:>3}d " +
                         (f"TNA {q.tna:6.2f}% a las {q.hora} del {q.fecha}" if q else "sin operaciones hoy"))
        except PPIError as e:
            lines.append(f"  {ticker:>10} {dias:>3}d error: {e}")
    lines.append(f"Pesos disponibles: {broker.pesos_disponibles():,.2f}")
    return "\n".join(lines)

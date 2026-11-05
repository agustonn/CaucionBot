"""Broker PPI (Portfolio Personal Inversiones) vía su API REST oficial.

La API pública de PPI soporta la operación "COLOCAR-CAUCIÓN" con el tipo de
instrumento "CAUCIONES", así que el bot puede operar punta a punta:
feriados, cotización, saldo, orden y seguimiento.
Docs: https://itatppi.github.io/ppi-official-api-docs/

No usamos el SDK `ppi-client` porque fija msgpack==1.0.2, que no instala en
Python 3.12+. Solo hace falta REST, así que va con la biblioteca estándar.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from .strategy import Quote, plazo_dias

log = logging.getLogger("caucionbot")

URL_PROD = "https://clientapi.portfoliopersonal.com/api/1.0/"
URL_SANDBOX = "https://clientapisbx.portfoliopersonal.com/api/1.0/"
TIPO = "CAUCIONES"
LIQUIDACION = "INMEDIATA"


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


def _es_pesos(moneda):
    m = str(moneda or "").lower()
    return not m or "peso" in m or m in ("$", "ars")


def parse_tickers_override(txt):
    """'TICKER1:1,TICKER3:3' -> {'TICKER1': 1, 'TICKER3': 3}"""
    out = {}
    for par in filter(None, (p.strip() for p in (txt or "").split(","))):
        t, d = par.rsplit(":", 1)
        out[t.strip()] = int(d)
    return out


class PPIBroker:
    nombre = "PPI"

    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.api = client or PPIClient(cfg.ppi_authorized_client, cfg.ppi_client_key,
                                       cfg.ppi_key, cfg.ppi_secret, cfg.ppi_sandbox)
        self.cuenta = cfg.ppi_cuenta or str(self.api.get("Account/Accounts")[0]["accountNumber"])
        self._override = parse_tickers_override(cfg.caucion_tickers)
        self._instrumentos = None

    # --- Datos ---
    def es_feriado(self):
        try:
            return bool(self.api.get("Configuration/IsLocalHoliday"))
        except (PPIError, urllib.error.URLError) as e:
            log.warning("No pude consultar feriados en PPI: %s", e)
            return False

    def buscar_instrumentos(self):
        return self.api.get("MarketData/SearchInstrument", ticker="", name="", market="BYMA", type=TIPO) or []

    def instrumentos(self):
        """Tickers de caución en pesos -> plazo en días. Se cachea (el bot corre un día por proceso)."""
        if self._instrumentos is None:
            if self._override:
                self._instrumentos = dict(self._override)
            else:
                self._instrumentos = {}
                for ins in self.buscar_instrumentos():
                    if not _es_pesos(ins.get("currency")):
                        continue
                    dias = plazo_dias({"descripcion": ins.get("description", ""),
                                       "simbolo": ins.get("ticker", "")})
                    if dias:
                        self._instrumentos[ins["ticker"]] = dias
        return self._instrumentos

    def cotizaciones(self):
        out = []
        for ticker, dias in sorted(self.instrumentos().items(), key=lambda x: x[1]):
            if dias > self.cfg.max_dias:
                continue
            md = self.api.get("MarketData/Current", ticker=ticker, type=TIPO, settlement=LIQUIDACION) or {}
            tna = float(md.get("price") or 0)
            if tna > 0:
                out.append(Quote(dias, tna, float(md.get("volume") or 0), ticker,
                                 str(md.get("date") or "")[:10], md))
        return out

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
                "instrumentType": TIPO, "quantityType": "DINERO", "operationType": "PRECIO-LIMITE",
                "operationTerm": "POR-EL-DÍA", "operationMaxDate": None,
                "operation": "COLOCAR-CAUCIÓN", "settlement": LIQUIDACION, "activationPrice": None}

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
    lines = [f"Cuenta: {broker.cuenta}", f"Feriado hoy: {broker.es_feriado()}"]
    ins = broker.buscar_instrumentos()
    lines.append(f"Instrumentos CAUCIONES en BYMA: {len(ins)}")
    for i in ins[:40]:
        lines.append(f"  {i.get('ticker')!s:>16} | {i.get('currency')} | {i.get('description')}")
    lines.append(f"Interpretados (ticker -> días): {broker.instrumentos()}")
    for q in broker.cotizaciones():
        lines.append(f"  {q.simbolo:>16} {q.plazo_dias:>3}d TNA {q.tna:6.2f}% volumen {q.monto_operado:,.0f} {q.fecha}")
    lines.append(f"Pesos disponibles: {broker.pesos_disponibles():,.2f}")
    if not broker.instrumentos():
        lines.append("No pude deducir los plazos. Configurá CAUCION_TICKERS=TICKER:dias,...")
    return "\n".join(lines)

"""Cliente mínimo de la API v2 de InvertirOnline (solo stdlib)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.invertironline.com/token"
API = "https://api.invertironline.com/api/v2"


class IOLError(Exception):
    pass


class IOLClient:
    def __init__(self, user, password, timeout=15):
        self.user, self.password, self.timeout = user, password, timeout
        self._access = self._refresh = None
        self._exp = 0.0

    def _post_token(self, form):
        req = urllib.request.Request(
            TOKEN_URL, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.load(r)
        self._access, self._refresh = d["access_token"], d.get("refresh_token")
        self._exp = time.time() + float(d.get("expires_in", 900))

    def _token(self):
        if self._access and time.time() < self._exp - 60:
            return self._access
        if self._refresh:
            try:
                self._post_token({"refresh_token": self._refresh, "grant_type": "refresh_token"})
                return self._access
            except urllib.error.HTTPError:
                pass  # refresh vencido: volvemos a usuario y contraseña
        self._post_token({"username": self.user, "password": self.password, "grant_type": "password"})
        return self._access

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                txt = r.read().decode()
        except urllib.error.HTTPError as e:
            raise IOLError(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}") from e
        return json.loads(txt) if txt else None

    # --- Lecturas ---
    def cauciones(self, panel="todos"):
        return self.request("GET", f"/Cotizaciones/cauciones/{panel}/argentina")

    def paneles_cauciones(self):
        return self.request("GET", "/argentina/Titulos/Cotizacion/Paneles/cauciones")

    def estado_cuenta(self):
        return self.request("GET", "/estadocuenta")

    def pesos_disponibles(self):
        """Pesos con liquidación inmediata en la cuenta de inversión Argentina."""
        total = 0.0
        for c in (self.estado_cuenta() or {}).get("cuentas", []):
            if "peso" not in str(c.get("moneda", "")).lower():
                continue
            inmediato = [s for s in c.get("saldos", []) if s.get("liquidacion") == "inmediato"]
            total += float((inmediato[0] if inmediato else c).get("disponible") or 0)
        return total

    # --- Ejecución ---
    def colocar_caucion(self, *a, **kw):
        """La API pública v2 de IOL no documenta un endpoint para operar cauciones
        (sí Comprar/Vender, FCI y cheques). Con IOL el bot funciona en modo alerta."""
        raise NotImplementedError("IOL no publica un endpoint de cauciones en la API v2")


class IOLBroker:
    """IOL como fuente de tasas y saldo. No ejecuta: el bot cae a modo alerta."""
    nombre = "IOL"

    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or IOLClient(cfg.iol_user, cfg.iol_pass)

    def es_feriado(self):
        return False  # se detecta por fecha vieja en las cotizaciones

    def cotizaciones(self, hoy=None):
        from .strategy import parse_cauciones
        return parse_cauciones(self.client.cauciones(self.cfg.panel))

    def pesos_disponibles(self):
        return self.client.pesos_disponibles()

    def colocar_caucion(self, *a, **kw):
        return self.client.colocar_caucion()

    def estado_orden(self, orden):
        return "?"

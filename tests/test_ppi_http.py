"""Cliente REST de PPI y doctor contra un servidor HTTP falso local."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

import caucionbot.ppi as ppi

SECRETOS = {"PPI_AUTHORIZED_CLIENT": "authcli-SECRETO-1", "PPI_CLIENT_KEY": "clikey-SECRETO-2",
            "PPI_KEY": "apikey-SECRETO-3", "PPI_SECRET": "apisecret-SECRETO-4"}
HOY = "2026-11-04"


class Handler(BaseHTTPRequestHandler):
    log = []
    tokens_validos = {"tok-1"}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _common(self):
        h = self.headers
        ok = (h.get("AuthorizedClient") == SECRETOS["PPI_AUTHORIZED_CLIENT"]
              and h.get("ClientKey") == SECRETOS["PPI_CLIENT_KEY"])
        u = urlparse(self.path)
        path = u.path.split("/api/1.0/", 1)[-1]
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        n = int(h.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        Handler.log.append((self.command, path, q, body))
        return ok, path, q, body

    def _authed(self):
        return self.headers.get("Authorization", "").removeprefix("Bearer ") in Handler.tokens_validos

    def do_GET(self):
        ok, path, q, _ = self._common()
        if not ok or not self._authed():
            return self._send(401, {"message": "unauthorized"})
        data = {
            "Account/Accounts": [{"accountNumber": "4567890"}],
            "Configuration/IsLocalHoliday": False,
            "MarketData/SearchInstrument": [
                {"ticker": "PESOS1D", "description": "Caución 1 día", "currency": "Pesos", "type": "CAUCIONES"},
                {"ticker": "PESOS7D", "description": "Caución 7 días", "currency": "Pesos", "type": "CAUCIONES"}],
            "MarketData/Current": {"date": f"{HOY}T16:40:00", "price": 41.5, "volume": 1.2e9},
            "MarketData/Book": {"date": f"{HOY}T16:40:00", "bids": [{"position": 1, "price": 41.4, "quantity": 5e6}],
                                "offers": [{"position": 1, "price": 41.6, "quantity": 3e6}]},
            "Account/AvailableBalance": [{"name": "Pesos", "symbol": "$", "amount": 350000, "settlement": "INMEDIATA"}],
            "Order/ActiveOrders": [],
        }.get(path)
        if data is None:
            return self._send(404, {"message": path})
        self._send(200, data)

    def do_POST(self):
        ok, path, q, body = self._common()
        if not ok:
            return self._send(401, {"message": "bad client"})
        if path == "Account/LoginApi":
            if (self.headers.get("ApiKey"), self.headers.get("ApiSecret")) != (SECRETOS["PPI_KEY"], SECRETOS["PPI_SECRET"]):
                return self._send(401, {"message": "bad key"})
            return self._send(200, {"accessToken": "tok-1", "refreshToken": "ref-1", "expires": 900})
        if not self._authed():
            return self._send(401, {"message": "unauthorized"})
        if path == "Order/Budget":
            return self._send(200, {"amount": body["quantity"], "disclaimers": [{"code": "C1", "description": "x"}]})
        if path == "Order/Confirm":
            return self._send(200, {"id": 9, "status": "PENDIENTE"})
        self._send(404, {"message": path})


class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/api/1.0/"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Handler.log = []
        Handler.tokens_validos = {"tok-1"}

    def cliente(self):
        c = ppi.PPIClient(*(SECRETOS[k] for k in ("PPI_AUTHORIZED_CLIENT", "PPI_CLIENT_KEY", "PPI_KEY", "PPI_SECRET")))
        c.base = self.url
        return c

    def test_login_y_parametros(self):
        c = self.cliente()
        self.assertEqual(c.get("MarketData/Current", ticker="PESOS1D", type="CAUCIONES", settlement="INMEDIATA")["price"], 41.5)
        (m1, p1, _, _), (m2, p2, q2, _) = Handler.log
        self.assertEqual((m1, p1), ("POST", "Account/LoginApi"))
        self.assertEqual(q2, {"ticker": "PESOS1D", "type": "CAUCIONES", "settlement": "INMEDIATA"})

    def test_relogin_si_el_token_se_revoca(self):
        c = self.cliente()
        c.get("Account/Accounts")
        Handler.tokens_validos = {"tok-1"}  # el server sigue aceptando tok-1 tras relogin
        c._token = "revocado"
        self.assertEqual(c.get("Account/Accounts")[0]["accountNumber"], "4567890")

    def test_doctor_no_filtra_secretos_y_no_confirma(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {**SECRETOS, "PPI_SANDBOX": "true", "MONTO_MIN": "100000",
                                             "PPI_CUENTA": "", "CAUCION_TICKERS": ""}), \
                mock.patch.object(ppi, "URL_SANDBOX", self.url), \
                mock.patch("caucionbot.config.load_dotenv"):
            from caucionbot.doctor import doctor
            out = os.path.join(d, "doctor.txt")
            with mock.patch("builtins.print"):
                doctor(con_budget=True, salida=out)
            with open(out, encoding="utf-8") as f:
                txt = f.read()
        for s in SECRETOS.values():
            self.assertNotIn(s, txt)
        self.assertNotIn("4567890", txt)          # cuenta enmascarada
        self.assertIn("****890", txt)
        self.assertNotIn("[FALLA]", txt)
        self.assertIn("{'PESOS1D': 1, 'PESOS7D': 7}", txt)
        paths = [p for _, p, _, _ in Handler.log]
        self.assertIn("Order/Budget", paths)
        self.assertNotIn("Order/Confirm", paths)


if __name__ == "__main__":
    unittest.main()

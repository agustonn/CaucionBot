import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from caucionbot.main import TZ, run
from caucionbot.config import Config
from caucionbot.store import Store
from caucionbot.strategy import (Quote, decidir, elegir, es_disparada, monto_a_colocar,
                                 parse_cauciones, tasa_base)

HOY = "2026-11-04"  # miércoles


def cfg(**kw):
    c = Config.from_env()
    c.__dict__.update(dict(
        salto_pp=4, salto_pct=15, confirmaciones=2, min_tna_neta=0, costos_pp=2,
        slippage_pp=0.5, min_monto_operado=0, solo_disparada=True, reserva=0,
        monto_max=1e12, monto_min=10_000, dry_run=True, modo="alerta", db_path=":memory:",
        telegram_token="", telegram_chat="", cada_seg=300, cada_seg_ventana=30))
    c.__dict__.update(kw)
    return c


def row(desc, tna, monto=1e9, fecha=HOY, moneda="peso_Argentino"):
    return {"simbolo": "PESOS", "descripcionTitulo": desc, "ultimoPrecio": tna,
            "montoOperado": monto, "fechaHora": f"{fecha}T16:30:00", "moneda": moneda, "plazo": "t0"}


class Parseo(unittest.TestCase):
    def test_parsea_y_elige_plazo_mas_corto_liquido(self):
        data = {"titulos": [row("Caución 7 días", 36), row("Caución 1 día", 35, monto=10),
                            row("Caución 2 días", 37), row("Caución 1 día", 40, moneda="dolar_Estadounidense")]}
        qs = parse_cauciones(data)
        self.assertEqual(sorted(q.plazo_dias for q in qs), [1, 2, 7])
        self.assertEqual(elegir(qs, 1000).plazo_dias, 2)
        self.assertEqual(elegir(qs, 0).plazo_dias, 1)

    def test_no_confunde_t0_con_dias(self):
        self.assertEqual(parse_cauciones({"titulos": [{"ultimoPrecio": 30, "plazo": "t1"}]}), [])

    def test_formato_simbolo(self):
        self.assertEqual(parse_cauciones([{"simbolo": "PESOS 3D", "ultimoPrecio": 30}])[0].plazo_dias, 3)


class Estrategia(unittest.TestCase):
    def test_base(self):
        self.assertEqual(tasa_base([30] * 6, [20]), 30)
        self.assertEqual(tasa_base([30] * 2, [20, 22, 24]), 22)
        self.assertIsNone(tasa_base([], []))

    def test_umbral_toma_el_mayor(self):
        self.assertFalse(es_disparada(33.9, 30, 4, 10))   # umbral 34
        self.assertTrue(es_disparada(34.5, 30, 4, 15))    # umbral 34.5
        self.assertFalse(es_disparada(34.4, 30, 4, 15))

    def test_monto(self):
        self.assertEqual(monto_a_colocar(1_234_567, 200_000, 1e12, 10_000), 1_034_000)
        self.assertEqual(monto_a_colocar(5_000, 0, 1e12, 10_000), 0)
        self.assertEqual(monto_a_colocar(9e6, 0, 1e6, 10_000), 1_000_000)

    def test_requiere_confirmacion(self):
        q = Quote(1, 45, 1e9)
        self.assertFalse(decidir(q, [30, 45], 30, 1e6, cfg(), False).colocar)
        d = decidir(q, [45, 45], 30, 1e6, cfg(), False)
        self.assertTrue(d.colocar)
        self.assertEqual((d.tasa_limite, d.monto, d.plazo_dias), (44.5, 1_000_000, 1))

    def test_piso_neto_y_limite(self):
        q = Quote(1, 31, 1e9)
        self.assertFalse(decidir(q, [31, 31], 30, 1e6, cfg(), True).colocar)
        self.assertTrue(decidir(q, [31, 31], 30, 1e6, cfg(solo_disparada=False), True).colocar)
        self.assertFalse(decidir(q, [31, 31], 30, 1e6, cfg(solo_disparada=False, min_tna_neta=30), True).colocar)


class FakeBroker:
    nombre = "FAKE"

    def __init__(self, tasa_fn, reloj):
        self.tasa_fn, self.reloj, self.ordenes = tasa_fn, reloj, []

    def es_feriado(self):
        return False

    def cotizaciones(self, hoy=None):
        return parse_cauciones({"titulos": [row("Caución 1 día", self.tasa_fn(self.reloj.t))]})

    def pesos_disponibles(self):
        return 2_500_000

    def colocar_caucion(self, monto, tasa, plazo, ticker, ext_id):
        self.ordenes.append((monto, tasa, plazo))
        return {"id": 1}

    def estado_orden(self, orden):
        return "EJECUTADA"


class Reloj:
    def __init__(self, inicio):
        self.t = inicio

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def simular(tasa_fn, **kw):
    c = cfg(**kw)
    store = Store(":memory:")
    reloj = Reloj(datetime(2026, 11, 4, 10, 0, tzinfo=TZ))
    broker = FakeBroker(tasa_fn, reloj)
    run(c, broker, store, now=reloj.now, sleep=reloj.sleep)
    return store, broker, reloj


class Loop(unittest.TestCase):
    def test_coloca_en_la_disparada(self):
        spike = lambda t: 50 if t.hour == 16 and t.minute >= 40 else 30
        store, broker, reloj = simular(spike, dry_run=False, modo="auto")
        o = store.orden_de(reloj.t.date())
        self.assertEqual(o[5], "auto")
        self.assertEqual(broker.ordenes, [(2_500_000, 49.5, 1)])
        self.assertEqual((reloj.t.hour, reloj.t.minute), (16, 41))  # 2da muestra >= 16:40, +60 s de seguimiento

    def test_sin_disparada_no_coloca(self):
        store, broker, reloj = simular(lambda t: 30)
        self.assertIsNone(store.orden_de(reloj.t.date()))
        self.assertGreaterEqual(reloj.t.time(), cfg().limite)

    def test_hora_limite_coloca_si_no_es_solo_disparada(self):
        store, broker, reloj = simular(lambda t: 30, solo_disparada=False)
        o = store.orden_de(reloj.t.date())
        self.assertEqual(o[5], "dry_run")
        self.assertLessEqual(reloj.t.time(), cfg().limite)


class FakePPIApi:
    """Respuestas con la forma que vimos en producción (doctor, nov-2026)."""

    def __init__(self, budget_amount=None, feriados=(), fallar_config=False):
        self.posts, self.gets, self.budget_amount = [], [], budget_amount
        self.feriados, self.fallar_config = list(feriados), fallar_config

    def get(self, path, **p):
        from caucionbot.ppi import PPIError
        self.gets.append((path, p))
        if path.startswith("Configuration/") and self.fallar_config and path != "Configuration/IsLocalHoliday":
            raise PPIError(f"GET {path} -> 500: caído")
        if path == "MarketData/Current":
            t = p["ticker"]
            if not t.startswith("PESOS"):
                raise PPIError('GET MarketData/Current -> 400: "Instrument not found"')
            return {"date": f"{HOY}T16:40:00.473-03:00", "price": 26.1 if t == "PESOS1" else 25.0,
                    "volume": 0, "openingPrice": 0, "max": 0, "min": 0, "previousClose": 18.9}
        return {
            "Account/Accounts": [{"accountNumber": "123"}],
            "Configuration/IsLocalHoliday": False,
            "Configuration/Holidays": [{"date": f"{f}T00:00:00", "description": "feriado", "isUSA": False}
                                       for f in self.feriados],
            "Configuration/Operations": ["COMPRA", "VENTA", "SUSCRIPCION-FCI", "COLOCAR-CAUCION"],
            "Configuration/OperationTerms": ["POR-EL-DIA", "HASTA-SU-EJECUCION"],
            "Configuration/OperationTypes": ["PRECIO-DE-MERCADO", "PRECIO-LIMITE"],
            "Configuration/QuantityTypes": ["DINERO", "PAPELES"],
            "Account/AvailableBalance": [
                {"name": "Pesos", "symbol": "ARS", "amount": 700_000, "settlement": "INMEDIATA"},
                {"name": "Pesos", "symbol": "ARS", "amount": 50_000, "settlement": "A-24HS"},
                {"name": "Dolar Divisa | CCL", "symbol": "USD", "amount": 999, "settlement": "INMEDIATA"}],
        }[path]

    def post(self, path, body):
        self.posts.append((path, dict(body)))
        if path == "Order/Budget":
            return {"amount": self.budget_amount or body["quantity"],
                    "disclaimers": [{"code": "D1", "description": "riesgo", "mandatory": True, "accepted": False}]}
        return {"id": 77, "status": "PENDIENTE", "externalID": body["externalID"]}


class PPI(unittest.TestCase):
    def broker(self, api, **kw):
        from caucionbot.ppi import PPIBroker
        return PPIBroker(cfg(ppi_cuenta="", caucion_tickers="", max_dias=4, **kw), client=api)

    def test_plazo_al_proximo_habil(self):
        from caucionbot.ppi import plazo_proximo_habil
        mie, vie = date(2026, 11, 4), date(2026, 11, 6)
        self.assertEqual(plazo_proximo_habil(mie, set()), 1)
        self.assertEqual(plazo_proximo_habil(vie, set()), 3)
        self.assertEqual(plazo_proximo_habil(vie, {date(2026, 11, 9)}), 4)   # lunes feriado
        self.assertEqual(plazo_proximo_habil(mie, {date(2026, 11, 5)}), 2)   # jueves feriado

    def test_datos(self):
        api = FakePPIApi()
        b = self.broker(api)
        self.assertEqual(b.cuenta, "123")
        self.assertEqual(b.instrumentos(), {"PESOS1": 1, "PESOS2": 2, "PESOS3": 3, "PESOS4": 4})
        self.assertEqual(b.plazo_objetivo(date(2026, 11, 4)), 1)
        qs = b.cotizaciones(date(2026, 11, 4))
        self.assertEqual([(q.simbolo, q.tna, q.fecha, q.monto_operado) for q in qs], [("PESOS1", 26.1, HOY, 0.0)])
        self.assertEqual(elegir(qs, 0).simbolo, "PESOS1")  # volumen 0 no lo descarta con MIN_MONTO_OPERADO=0
        self.assertEqual(b.pesos_disponibles(), 700_000)

    def test_viernes_con_feriado_usa_pesos4(self):
        api = FakePPIApi(feriados=["2026-11-09"])
        b = self.broker(api)
        self.assertEqual(b.plazo_objetivo(date(2026, 11, 6)), 4)
        self.assertEqual([q.simbolo for q in b.cotizaciones(date(2026, 11, 6))], ["PESOS4"])

    def test_feriados_caidos_usa_fines_de_semana(self):
        b = self.broker(FakePPIApi(fallar_config=True))
        self.assertEqual(b.plazo_objetivo(date(2026, 11, 6)), 3)

    def test_orden_usa_valores_de_ppi_sin_tilde(self):
        api = FakePPIApi()
        r = self.broker(api).colocar_caucion(600_000, 25.6, 1, "PESOS1", "caucionbot-20261104")
        self.assertEqual(r["id"], 77)
        (p1, budget), (p2, confirm) = api.posts
        self.assertEqual((p1, p2), ("Order/Budget", "Order/Confirm"))
        self.assertEqual(confirm["operation"], "COLOCAR-CAUCION")
        self.assertEqual(confirm["operationTerm"], "POR-EL-DIA")
        self.assertEqual(confirm["instrumentType"], "CAUCIONES")
        self.assertEqual((confirm["ticker"], confirm["quantity"], confirm["price"]), ("PESOS1", 600_000, 25.6))
        self.assertEqual(confirm["disclaimers"][0]["accepted"], True)
        self.assertEqual(confirm["externalID"], "caucionbot-20261104")

    def test_valor_respeta_acentos_de_ppi(self):
        api = FakePPIApi()
        b = self.broker(api)
        b._config["Operations"] = ["COLOCAR-CAUCIÓN"]  # si algún día vuelve con tilde
        self.assertEqual(b.valor("Operations", "COLOCAR-CAUCION"), "COLOCAR-CAUCIÓN")

    def test_no_confirma_si_el_presupuesto_no_coincide(self):
        from caucionbot.ppi import PPIError
        api = FakePPIApi(budget_amount=6_000_000)
        with self.assertRaises(PPIError):
            self.broker(api).colocar_caucion(600_000, 25.6, 1, "PESOS1", "x")
        self.assertEqual([p for p, _ in api.posts], ["Order/Budget"])

    def test_override_de_tickers(self):
        from caucionbot.ppi import PPIBroker
        b = PPIBroker(cfg(ppi_cuenta="9", caucion_tickers="ABC:1, XYZ:3", max_dias=4), client=FakePPIApi())
        self.assertEqual(b.instrumentos(), {"ABC": 1, "XYZ": 3})


if __name__ == "__main__":
    unittest.main()

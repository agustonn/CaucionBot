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
        telegram_token="", telegram_chat="", cada_seg=300, cada_seg_ventana=30, tramos=1,
        salto_tramo_pp=10))
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


class Tramos(unittest.TestCase):
    def test_secuencia_de_tres_tramos(self):
        c = cfg(tramos=3)
        q = lambda tna: Quote(6, tna, 1e9, "PESOS6")
        d1 = decidir(q(70), [50.1, 70], 40, 3_000_000, c, False, [])
        self.assertEqual((d1.colocar, d1.tramo, d1.monto, d1.tasa_limite), (True, 1, 1_000_000, 69.5))
        # el 2do solo si subió SALTO_TRAMO_PP (10) sobre el 1ro
        self.assertFalse(decidir(q(75), [70, 75], 40, 2_000_000, c, False, [69.5]).colocar)
        d2 = decidir(q(80), [70, 80], 40, 2_000_000, c, False, [69.5])
        self.assertEqual((d2.colocar, d2.tramo, d2.monto), (True, 2, 1_000_000))
        # el último espera la hora límite aunque siga subiendo
        self.assertFalse(decidir(q(178), [110, 178], 40, 1_000_000, c, False, [69.5, 79.5]).colocar)
        d3 = decidir(q(162), [178, 162], 40, 1_000_000, c, True, [69.5, 79.5])
        self.assertEqual((d3.colocar, d3.tramo, d3.monto), (True, 3, 1_000_000))
        self.assertFalse(decidir(q(190), [190], 40, 1e6, c, True, [69.5, 79.5, 161.5]).colocar)

    def test_hora_limite_coloca_todo_lo_que_queda(self):
        d = decidir(Quote(1, 60, 0), [60, 60], 40, 2_000_000, cfg(tramos=3), True, [55])
        self.assertEqual((d.colocar, d.tramo, d.monto), (True, 2, 2_000_000))

    def test_tramo_chico_respeta_monto_minimo(self):
        d = decidir(Quote(1, 60, 0), [60, 60], 40, 150_000, cfg(tramos=3, monto_min=100_000), False, [])
        self.assertEqual(d.monto, 100_000)


class FakeBroker:
    """Broker con intradiario: un punto por minuto desde las 11:00 hasta la hora del reloj."""
    nombre = "FAKE"

    def __init__(self, tasa_fn, reloj, saldo=2_500_000, intradiario=True, fallar_orden=False):
        self.tasa_fn, self.reloj, self.ordenes, self.saldo = tasa_fn, reloj, [], saldo
        self.intradiario, self.fallar_orden = intradiario, fallar_orden

    def es_feriado(self):
        return False

    def cotizaciones(self, hoy=None):
        t = self.reloj.t
        if not self.intradiario:
            return [Quote(1, self.tasa_fn(t), 0, "PESOS1", HOY)]
        serie, m = [], t.replace(hour=11, minute=0, second=0)
        while m <= t and m.hour < 17:
            serie.append((m.strftime("%H:%M"), self.tasa_fn(m), 1e9))
            m += timedelta(minutes=1)
        if not serie:
            return []
        return [Quote(1, serie[-1][1], serie[-1][2], "PESOS1", HOY, serie[-1][0], serie)]

    def pesos_disponibles(self):
        return self.saldo

    def colocar_caucion(self, monto, tasa, plazo, ticker, ext_id):
        if self.fallar_orden:
            raise RuntimeError("saldo insuficiente")
        self.ordenes.append((monto, tasa, ext_id))
        self.saldo -= monto
        return {"id": len(self.ordenes), "externalID": ext_id}

    def estado_orden(self, orden):
        return "EJECUTADA"


class Reloj:
    def __init__(self, inicio):
        self.t = inicio

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def simular(tasa_fn, broker_kw=None, **kw):
    c = cfg(**kw)
    store = Store(":memory:")
    reloj = Reloj(datetime(2026, 11, 4, 10, 0, tzinfo=TZ))
    broker = FakeBroker(tasa_fn, reloj, **(broker_kw or {}))
    run(c, broker, store, now=reloj.now, sleep=reloj.sleep)
    return store, broker, reloj


# Disparada real de PESOS6 del 5/11/2026 (intradiario de PPI), con 40% de base el resto del día
DISPARADA_REAL = {"16:50": 50.1, "16:51": 70, "16:52": 60, "16:53": 68, "16:54": 80, "16:55": 110,
                  "16:56": 178, "16:57": 162, "16:58": 175, "16:59": 190}


def dia_real(t):
    return DISPARADA_REAL.get(t.strftime("%H:%M"), 40.0)


class Loop(unittest.TestCase):
    def test_coloca_en_la_disparada(self):
        spike = lambda t: 50 if (t.hour, t.minute) >= (16, 40) else 30
        store, broker, reloj = simular(spike, dry_run=False, modo="auto")
        self.assertEqual(broker.ordenes, [(2_500_000, 49.5, "caucionbot-20261104-1")])
        tramos = store.tramos_de(reloj.t.date())
        self.assertEqual([(r[0], r[3]) for r in tramos], [(1, "auto")])

    def test_sin_disparada_no_coloca(self):
        store, broker, reloj = simular(lambda t: 30)
        self.assertEqual(store.tramos_de(reloj.t.date()), [])
        self.assertEqual(broker.ordenes, [])

    def test_hora_limite_coloca_si_no_es_solo_disparada(self):
        store, broker, reloj = simular(lambda t: 30, solo_disparada=False)
        tramos = store.tramos_de(reloj.t.date())
        self.assertEqual([r[3] for r in tramos], ["dry_run"])

    def test_dia_real_en_tres_tramos(self):
        store, broker, reloj = simular(dia_real, dry_run=False, modo="auto", tramos=3)
        tasas = [o[1] for o in broker.ordenes]
        montos = [o[0] for o in broker.ordenes]
        self.assertEqual(tasas, [69.5, 79.5, 174.5])   # 16:51, 16:54 y 16:58 (hora límite)
        self.assertEqual(sum(montos), 2_500_000)
        self.assertEqual([o[2] for o in broker.ordenes],
                         ["caucionbot-20261104-1", "caucionbot-20261104-2", "caucionbot-20261104-3"])
        promedio = sum(m * t for m, t, _ in broker.ordenes) / sum(montos)
        self.assertGreater(promedio, 100)  # vs 69.5% con una sola orden

    def test_dia_real_una_sola_orden(self):
        store, broker, reloj = simular(dia_real, dry_run=False, modo="auto", tramos=1)
        self.assertEqual([o[1] for o in broker.ordenes], [69.5])

    def test_error_frena_los_tramos_del_dia(self):
        store, broker, reloj = simular(dia_real, broker_kw={"fallar_orden": True},
                                       dry_run=False, modo="auto", tramos=3)
        self.assertEqual(store.errores_de(reloj.t.date()), 1)
        self.assertEqual(broker.ordenes, [])

    def test_sin_intradiario_usa_muestras_propias(self):
        spike = lambda t: 50 if (t.hour, t.minute) >= (16, 40) else 30
        store, broker, reloj = simular(spike, broker_kw={"intradiario": False}, dry_run=False, modo="auto")
        self.assertEqual([o[1] for o in broker.ordenes], [49.5])

    def test_guarda_intradiario_al_cierre(self):
        store, broker, reloj = simular(dia_real)
        n = store.db.execute("SELECT count(*), max(tna) FROM intradia").fetchone()
        self.assertEqual(n, (360, 190.0))  # 11:00 a 16:59


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
        if path == "MarketData/Intraday":
            if p["ticker"] not in ("PESOS1", "PESOS4", "PESOS6"):
                return []  # plazo que no operó hoy
            return [{"date": f"{HOY}T16:{m:02d}:00-03:00", "price": 20.0 + m, "volume": 1e9} for m in range(50, 60)]
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
        self.assertEqual([(q.simbolo, q.tna, q.fecha, q.hora, q.monto_operado) for q in qs],
                         [("PESOS1", 79.0, HOY, "16:59", 1e9)])
        self.assertEqual(len(qs[0].serie), 10)
        self.assertFalse(any(path == "MarketData/Current" for path, _ in api.gets))  # Current no es en vivo
        self.assertEqual(b.pesos_disponibles(), 700_000)

    def test_jueves_antes_de_fin_de_semana_largo_usa_pesos6(self):
        # Caso real del 5/11/2026: PPI marcó feriados el 6, 9 y 10/11
        api = FakePPIApi(feriados=["2026-11-06", "2026-11-09", "2026-11-10"])
        b = self.broker(api)  # MAX_DIAS=4 no debe impedirlo
        self.assertEqual([q.simbolo for q in b.cotizaciones(date(2026, 11, 5))], ["PESOS6"])

    def test_plazo_sin_operaciones_no_devuelve_cotizacion(self):
        api = FakePPIApi()
        b = self.broker(api)
        b._plazo = (date(2026, 11, 4), 2)
        self.assertEqual(b.cotizaciones(date(2026, 11, 4)), [])

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

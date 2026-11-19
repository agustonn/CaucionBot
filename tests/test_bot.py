import unittest
from datetime import date, datetime, time, timedelta

from caucionbot.config import Config
from caucionbot.main import TZ, run
from caucionbot.store import Store
from caucionbot.strategy import (Quote, decidir, elegir, evaluar, mediana_ponderada, monto_a_colocar,
                                 parse_cauciones, ponderada)

HOY = "2026-11-04"  # miércoles


def cfg(**kw):
    c = Config.from_env()
    c.__dict__.update(dict(
        factor_disparada=1.5, factor_volumen=1.0, minutos_tasa=3, confirmaciones=2,
        min_tna_neta=0, costos_pp=2.5, slippage_pp=0.5, min_monto_operado=0, solo_disparada=False,
        reserva=0, monto_max=1e12, monto_min=100_000, dry_run=True, modo="alerta", db_path=":memory:",
        telegram_token="", telegram_chat="", cada_seg=300, cada_seg_ventana=15,
        tramo_pct=20, salto_tramo_pct=15, tramos_max=10, ventana=time(16, 15), limite=time(16, 58, 30),
        inicio=time(11, 5), dias_historia=10))
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


# Disparada real de PESOS6 del 5/11/2026 (intradiario de PPI, con volúmenes en $).
# El resto del día lo suponemos en 40% con $1.000M por minuto.
DISPARADA_REAL = {"16:50": (50.1, 2774060947.8), "16:51": (70, 339906490), "16:52": (60, 41586404580),
                  "16:53": (68, 340000000000), "16:54": (80, 378572160), "16:55": (110, 6636033030),
                  "16:56": (178, 1806615094), "16:57": (162, 226567530), "16:58": (175, 210126000),
                  "16:59": (190, 57318440)}


def dia_real(t):
    return DISPARADA_REAL.get(t.strftime("%H:%M"), (40.0, 1e9))


def serie_hasta(hhmm, fn=dia_real):
    out, m = [], datetime(2026, 11, 4, 11, 0)
    while m.strftime("%H:%M") <= hhmm:
        tna, vol = fn(m)
        out.append((m.strftime("%H:%M"), tna, vol))
        m += timedelta(minutes=1)
    return out


class Disparada(unittest.TestCase):
    def test_ponderadas(self):
        self.assertAlmostEqual(ponderada([("a", 40, 1), ("b", 100, 3)]), 85)
        self.assertEqual(ponderada([("a", 40, 0), ("b", 100, 0)]), 70)  # sin volumen: promedio simple
        self.assertEqual(mediana_ponderada([("a", 10, 1), ("b", 20, 1), ("c", 90, 5)]), 90)

    def test_operacion_suelta_no_dispara(self):
        # un minuto a 200% con $1M (nada) en un día de 40% con $1.000M por minuto
        fn = lambda t: (200, 1e6) if t.strftime("%H:%M") == "16:40" else (40, 1e9)
        e = evaluar(serie_hasta("16:40", fn), cfg())
        self.assertFalse(e.disparada)
        self.assertLess(e.tna, 60)

    def test_salto_sin_volumen_no_dispara(self):
        fn = lambda t: (80, 1e8) if t.strftime("%H:%M") >= "16:40" else (40, 1e9)
        self.assertFalse(evaluar(serie_hasta("16:45", fn), cfg()).disparada)
        self.assertTrue(evaluar(serie_hasta("16:45", fn), cfg(factor_volumen=0)).disparada)

    def test_salto_moderado_no_es_disparada(self):
        # el 4/11: de 18,9% a 26,1% (1,38x) no llega a 1,5x
        fn = lambda t: (26.1, 2e9) if t.strftime("%H:%M") >= "16:40" else (18.9, 1e9)
        e = evaluar(serie_hasta("16:50", fn), cfg())
        self.assertFalse(e.disparada)
        self.assertAlmostEqual(e.umbral, 28.35)

    def test_dia_real_minuto_a_minuto(self):
        c = cfg()
        disp = {h: evaluar(serie_hasta(h), c).disparada for h in DISPARADA_REAL}
        self.assertFalse(disp["16:52"])        # 59,5% ponderado: todavía no llega a 60%
        self.assertFalse(disp["16:53"])        # primer minuto que cumple, falta sostenerlo
        self.assertTrue(disp["16:54"])         # sostenida 2 minutos
        self.assertTrue(disp["16:56"])
        self.assertFalse(disp["16:58"])        # volumen bajo en los últimos 3 minutos

    def test_sin_datos_previos_usa_la_historia(self):
        serie = [("16:40", 80, 1e9), ("16:41", 80, 1e9)]
        self.assertIsNone(evaluar(serie, cfg()).normal)
        e = evaluar(serie, cfg(), normal_historica=40)
        self.assertTrue(e.disparada)


class Decisiones(unittest.TestCase):
    def est(self, hhmm):
        return evaluar(serie_hasta(hhmm), cfg())

    def test_ordenes_sucesivas_mientras_sube(self):
        c = cfg()
        d1 = decidir(self.est("16:54"), 6, 1_000_000, c, False)
        self.assertEqual((d1.colocar, d1.tramo, d1.monto), (True, 1, 200_000))
        # 16:55 sigue disparada pero no superó un 15% a la anterior
        self.assertFalse(decidir(self.est("16:55"), 6, 800_000, c, False, [d1.tasa_limite], [200_000]).colocar)
        d2 = decidir(self.est("16:56"), 6, 800_000, c, False, [d1.tasa_limite], [200_000])
        self.assertEqual((d2.colocar, d2.tramo, d2.monto), (True, 2, 200_000))
        self.assertGreater(d2.tasa_limite, d1.tasa_limite * 1.15)

    def test_hora_limite_coloca_todo_lo_que_queda_aunque_no_haya_disparada(self):
        fn = lambda t: (19.8, 1e9)
        e = evaluar(serie_hasta("16:58", fn), cfg())
        d = decidir(e, 1, 1_000_000, cfg(), True)
        self.assertEqual((d.colocar, d.monto, d.tasa_limite), (True, 1_000_000, 19.3))

    def test_solo_disparada_no_coloca_al_cierre(self):
        e = evaluar(serie_hasta("16:58", lambda t: (19.8, 1e9)), cfg())
        self.assertFalse(decidir(e, 1, 1_000_000, cfg(solo_disparada=True), True).colocar)

    def test_no_deja_un_resto_que_no_se_puede_colocar(self):
        d = decidir(self.est("16:54"), 6, 250_000, cfg(), False)  # 20% de 250k < mínimo -> 100k, resto 150k
        self.assertEqual(d.monto, 100_000)
        d = decidir(self.est("16:54"), 6, 180_000, cfg(), False)  # 100k dejaría 80k sueltos -> todo
        self.assertEqual(d.monto, 180_000)

    def test_monto_max_es_el_capital_del_dia(self):
        d = decidir(self.est("16:54"), 6, 5_000_000, cfg(monto_max=1_000_000), False)
        self.assertEqual(d.monto, 200_000)
        d = decidir(self.est("16:58"), 6, 4_800_000, cfg(monto_max=1_000_000), True, [66.6], [200_000])
        self.assertEqual(d.monto, 800_000)

    def test_piso_neto(self):
        e = evaluar(serie_hasta("16:58", lambda t: (19.8, 1e9)), cfg())
        self.assertFalse(decidir(e, 1, 1e6, cfg(min_tna_neta=18), True).colocar)

    def test_tope_de_ordenes(self):
        self.assertFalse(decidir(self.est("16:56"), 6, 1e6, cfg(tramos_max=2), False, [60, 70], [2e5, 2e5]).colocar)

    def test_monto(self):
        self.assertEqual(monto_a_colocar(1_234_567, 200_000, 1e12, 10_000), 1_034_000)
        self.assertEqual(monto_a_colocar(5_000, 0, 1e12, 10_000), 0)


class Consola(unittest.TestCase):
    def test_respuestas(self):
        from unittest import mock
        from caucionbot.confirmar import preguntar
        with mock.patch("builtins.print"):
            self.assertTrue(preguntar("x", 5, entrada=lambda: "s\n"))
            self.assertTrue(preguntar("x", 5, entrada=lambda: "Sí"))
            self.assertFalse(preguntar("x", 5, entrada=lambda: "n"))
            self.assertFalse(preguntar("x", 5, entrada=lambda: ""))
            self.assertIsNone(preguntar("x", 5, entrada=lambda: None))


class FakeBroker:
    """Broker con intradiario: un punto por minuto desde las 11:00 hasta la hora del reloj."""
    nombre = "FAKE"

    def __init__(self, fn, reloj, saldo=1_000_000, intradiario=True, fallar_orden=False):
        self.fn, self.reloj, self.ordenes, self.saldo = fn, reloj, [], saldo
        self.intradiario, self.fallar_orden = intradiario, fallar_orden

    def es_feriado(self):
        return False

    def cotizaciones(self, hoy=None):
        t = self.reloj.t
        if not self.intradiario:
            return [Quote(1, self.fn(t)[0], 0, "PESOS1", HOY)]
        serie = [p for p in serie_hasta(min(t.strftime("%H:%M"), "16:59"), self.fn)] if t.hour >= 11 else []
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


def simular(fn, broker_kw=None, preguntar=None, **kw):
    c = cfg(**kw)
    store = Store(":memory:")
    reloj = Reloj(datetime(2026, 11, 4, 10, 0, tzinfo=TZ))
    broker = FakeBroker(fn, reloj, **(broker_kw or {}))
    run(c, broker, store, now=reloj.now, sleep=reloj.sleep, preguntar=preguntar)
    return store, broker, reloj


class Loop(unittest.TestCase):
    def test_dia_real_con_un_millon(self):
        store, broker, reloj = simular(dia_real, dry_run=False, modo="auto")
        montos = [o[0] for o in broker.ordenes]
        tasas = [o[1] for o in broker.ordenes]
        self.assertEqual(montos, [200_000, 200_000, 600_000])
        self.assertAlmostEqual(tasas[0], 66.64, places=1)   # 16:54, se sostiene la disparada
        self.assertGreater(tasas[1], 120)                   # 16:56, superó en 15% a la anterior
        self.assertEqual(tasas[2], 174.5)                   # 16:58, hora límite: todo lo que queda
        self.assertEqual([o[2] for o in broker.ordenes],
                         ["caucionbot-20261104-1", "caucionbot-20261104-2", "caucionbot-20261104-3"])
        promedio = sum(m * t for m, t, _ in broker.ordenes) / sum(montos)
        self.assertGreater(promedio, 140)

    def test_dia_normal_coloca_todo_a_ultima_hora(self):
        store, broker, reloj = simular(lambda t: (19.8, 1e9), dry_run=False, modo="auto")
        self.assertEqual(broker.ordenes, [(1_000_000, 19.3, "caucionbot-20261104-1")])

    def test_solo_disparada_dia_normal_no_coloca(self):
        store, broker, reloj = simular(lambda t: (19.8, 1e9), dry_run=False, modo="auto", solo_disparada=True)
        self.assertEqual(broker.ordenes, [])

    def test_simulacion_descuenta_lo_colocado(self):
        store, broker, reloj = simular(dia_real)  # dry_run
        tramos = store.tramos_de(reloj.t.date(), simulados=True)
        self.assertEqual([r[1] for r in tramos], [200_000, 200_000, 600_000])
        self.assertEqual(broker.ordenes, [])

    def test_error_frena_las_ordenes_del_dia(self):
        store, broker, reloj = simular(dia_real, broker_kw={"fallar_orden": True}, dry_run=False, modo="auto")
        self.assertEqual(store.errores_de(reloj.t.date()), 1)
        self.assertEqual(broker.ordenes, [])

    def test_sin_intradiario_usa_muestras_propias(self):
        fn = lambda t: (60, 0) if (t.hour, t.minute) >= (16, 40) else (30, 0)
        store, broker, reloj = simular(fn, broker_kw={"intradiario": False}, dry_run=False, modo="auto")
        self.assertEqual(broker.ordenes[0][:2], (200_000, 59.5))

    def test_simulacion_previa_no_bloquea_la_real(self):
        from caucionbot.strategy import Decision
        store = Store(":memory:")
        store.guardar_tramo(datetime(2026, 11, 4, 16, 54, tzinfo=TZ), Decision(True, "sim", 66.6, 1, 200_000, 1),
                            "dry_run", "simulada")
        reloj = Reloj(datetime(2026, 11, 4, 10, 0, tzinfo=TZ))
        broker = FakeBroker(dia_real, reloj)
        run(cfg(dry_run=False, modo="auto"), broker, store, now=reloj.now, sleep=reloj.sleep)
        self.assertEqual(len(broker.ordenes), 3)

    def test_confirmar_si_envia(self):
        preguntas = []
        si = lambda msg, seg: preguntas.append(msg) or True
        store, broker, reloj = simular(dia_real, preguntar=si, dry_run=False, modo="confirmar")
        self.assertEqual(len(broker.ordenes), 3)
        self.assertEqual(len(preguntas), 3)

    def test_confirmar_no_corta_el_dia(self):
        store, broker, reloj = simular(dia_real, preguntar=lambda m, s: False, dry_run=False, modo="confirmar")
        self.assertEqual(broker.ordenes, [])
        self.assertEqual(store.errores_de(reloj.t.date()), 1)

    def test_confirmar_sin_respuesta_vuelve_a_preguntar(self):
        respuestas = iter([None, None, True])
        store, broker, reloj = simular(dia_real, preguntar=lambda m, s: next(respuestas, True),
                                       dry_run=False, modo="confirmar")
        self.assertGreaterEqual(len(broker.ordenes), 1)

    def test_confirmar_respeta_dry_run(self):
        store, broker, reloj = simular(dia_real, preguntar=lambda m, s: self.fail("no debe preguntar"),
                                       dry_run=True, modo="confirmar")
        self.assertEqual(broker.ordenes, [])

    def test_orden_manual(self):
        from caucionbot.main import orden_manual
        reloj = Reloj(datetime(2026, 11, 4, 16, 20, tzinfo=TZ))
        broker = FakeBroker(lambda t: (18.9, 1e9), reloj, saldo=110_000)
        o = orden_manual(cfg(), broker, None, lambda m, s: True, reloj.sleep, reloj.now)
        self.assertEqual(o["id"], 1)
        self.assertEqual(broker.ordenes[0][:2], (100_000, 18.4))

    def test_orden_manual_valida_saldo_y_confirmacion(self):
        from caucionbot.main import orden_manual
        reloj = Reloj(datetime(2026, 11, 4, 16, 20, tzinfo=TZ))
        broker = FakeBroker(lambda t: (18.9, 1e9), reloj, saldo=90_000)
        self.assertIsNone(orden_manual(cfg(), broker, None, lambda m, s: True, reloj.sleep, reloj.now))
        broker.saldo = 110_000
        self.assertIsNone(orden_manual(cfg(), broker, None, lambda m, s: None, reloj.sleep, reloj.now))
        self.assertEqual(broker.ordenes, [])

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

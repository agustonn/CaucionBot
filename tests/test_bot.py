import unittest
from datetime import datetime, timedelta
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


class FakeIOL:
    def __init__(self, tasas):
        self.tasas, self.consultas, self.ordenes = tasas, 0, []

    def cauciones(self, panel):
        self.consultas += 1
        return {"titulos": [row("Caución 1 día", self.tasa)]}

    def pesos_disponibles(self):
        return 2_500_000

    def colocar_caucion(self, *a):
        self.ordenes.append(a)
        return {"ok": True}


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
    iol = FakeIOL(None)
    orig = iol.cauciones

    def cauciones(panel):
        iol.tasa = tasa_fn(reloj.t)
        return orig(panel)
    iol.cauciones = cauciones
    run(c, iol, store, now=reloj.now, sleep=reloj.sleep)
    return store, iol, reloj


class Loop(unittest.TestCase):
    def test_coloca_en_la_disparada(self):
        spike = lambda t: 50 if t.hour == 16 and t.minute >= 40 else 30
        store, iol, reloj = simular(spike, dry_run=False, modo="iol")
        o = store.orden_de(reloj.t.date())
        self.assertEqual(o[5], "iol")
        self.assertEqual(iol.ordenes, [(2_500_000, 49.5, 1)])
        self.assertEqual((reloj.t.hour, reloj.t.minute), (16, 40))  # 2da muestra >= 16:40

    def test_sin_disparada_no_coloca(self):
        store, iol, reloj = simular(lambda t: 30)
        self.assertIsNone(store.orden_de(reloj.t.date()))
        self.assertGreaterEqual(reloj.t.time(), cfg().limite)

    def test_hora_limite_coloca_si_no_es_solo_disparada(self):
        store, iol, reloj = simular(lambda t: 30, solo_disparada=False)
        o = store.orden_de(reloj.t.date())
        self.assertEqual(o[5], "dry_run")
        self.assertLessEqual(reloj.t.time(), cfg().limite)


if __name__ == "__main__":
    unittest.main()

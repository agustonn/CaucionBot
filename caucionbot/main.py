"""CLI: python -m caucionbot.main {probe|check|run}"""
import argparse
import json
import logging
import time
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .iol import IOLClient, IOLError
from .notify import avisar
from .store import Store
from .strategy import decidir, elegir, parse_cauciones, tasa_base

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
log = logging.getLogger("caucionbot")


def ahora():
    return datetime.now(TZ)


def cotizacion(cfg, client, hoy):
    quotes = parse_cauciones(client.cauciones(cfg.panel))
    # Descarta precios viejos (feriados, antes de la primera operación del día)
    quotes = [q for q in quotes if not q.fecha or q.fecha == hoy.isoformat()]
    return elegir(quotes, cfg.min_monto_operado)


def base_de(cfg, store, hoy):
    return tasa_base(store.tnas_hoy(hoy, hasta=cfg.ventana),
                     store.medianas_previas(hoy, cfg.dias_historia, cfg.ventana))


def ejecutar(cfg, client, store, t, d):
    msg = (f"Caución colocadora {d.plazo_dias}d por ${d.monto:,.0f} "
           f"a TNA mínima {d.tasa_limite:.2f}%. Motivo: {d.motivo}")
    if cfg.dry_run:
        avisar(cfg, "[SIMULACIÓN] " + msg)
        store.guardar_orden(t, d, "dry_run", "simulada")
        return
    if cfg.modo == "iol":
        try:
            res = client.colocar_caucion(d.monto, d.tasa_limite, d.plazo_dias)
            avisar(cfg, "Orden enviada a IOL. " + msg)
            store.guardar_orden(t, d, "iol", json.dumps(res))
            return
        except NotImplementedError as e:
            log.error("%s Paso a modo alerta.", e)
        except IOLError as e:
            avisar(cfg, f"Falló la orden en IOL ({e}). Hacelo a mano: {msg}")
            store.guardar_orden(t, d, "iol", f"error: {e}")
            return
    avisar(cfg, "COLOCÁ AHORA en IOL. " + msg)
    store.guardar_orden(t, d, "alerta", "avisado")


def paso_ventana(cfg, client, store, t, q, llego_limite):
    """Evalúa la ventana. Devuelve True si el día terminó."""
    hoy = t.date()
    base = base_de(cfg, store, hoy)
    ultimas = store.tnas_hoy(hoy, desde=cfg.ventana)
    # Primero decidimos con saldo "infinito" para no consultar la cuenta cada 30 s
    d = decidir(q, ultimas, base, float("inf"), cfg, llego_limite)
    if d.colocar:
        d = decidir(q, ultimas, base, client.pesos_disponibles(), cfg, llego_limite)
    log.info("%s | %s", t.strftime("%H:%M:%S"), d.motivo)
    if d.colocar:
        ejecutar(cfg, client, store, t, d)
        return True
    return llego_limite


def run(cfg, client, store, now=ahora, sleep=time.sleep):
    hoy = now().date()
    if hoy.weekday() >= 5:
        log.info("Fin de semana, nada que hacer.")
        return
    if store.orden_de(hoy):
        log.info("Ya hubo orden hoy, salgo.")
        return
    while True:
        t = now()
        if t.time() < cfg.inicio:
            sleep(min(cfg.cada_seg, (datetime.combine(hoy, cfg.inicio, TZ) - t).total_seconds() + 1))
            continue
        if t.time() > cfg.limite:
            log.info("Pasó la hora límite.")
            return
        en_ventana = t.time() >= cfg.ventana
        paso = cfg.cada_seg_ventana if en_ventana else cfg.cada_seg
        try:
            q = cotizacion(cfg, client, hoy)
            if q:
                store.guardar_muestra(t, q)
                log.debug("%s %sd %.2f%% monto %.0f", t.strftime("%H:%M:%S"), q.plazo_dias, q.tna, q.monto_operado)
            if en_ventana:
                llego_limite = (t + timedelta(seconds=paso)).time() > cfg.limite
                if paso_ventana(cfg, client, store, t, q, llego_limite):
                    return
        except (IOLError, urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
            log.warning("Error consultando IOL: %s", e)
        sleep(paso)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="caucionbot")
    ap.add_argument("cmd", choices=["probe", "check", "run"],
                    help="probe: vuelca JSON crudo de IOL | check: una evaluación | run: el día completo")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if not cfg.iol_user or not cfg.iol_pass:
        ap.error("Faltan IOL_USER / IOL_PASS (ver .env.example)")
    client = IOLClient(cfg.iol_user, cfg.iol_pass)

    if a.cmd == "probe":
        print("== Paneles de cauciones ==")
        print(json.dumps(client.paneles_cauciones(), indent=2, ensure_ascii=False))
        raw = client.cauciones(cfg.panel)
        print(f"== Cotizaciones/cauciones/{cfg.panel} ==")
        print(json.dumps(raw, indent=2, ensure_ascii=False)[:5000])
        print("== Interpretado ==")
        for q in parse_cauciones(raw):
            print(f"  {q.simbolo:>12} {q.plazo_dias:>3}d  TNA {q.tna:6.2f}%  monto {q.monto_operado:,.0f}  {q.fecha}")
        print(f"== Pesos disponibles: {client.pesos_disponibles():,.2f}")
        return

    store = Store(cfg.db_path)
    if a.cmd == "check":
        t = now = ahora()
        q = cotizacion(cfg, client, now.date())
        base = base_de(cfg, store, now.date())
        ultimas = store.tnas_hoy(now.date(), desde=cfg.ventana) + ([q.tna] if q else [])
        d = decidir(q, ultimas, base, client.pesos_disponibles(), cfg, llego_limite=False)
        print(f"{t:%H:%M} cotización: {q}\nbase: {base}\ndecisión: {d}")
        return

    run(cfg, client, store)


if __name__ == "__main__":
    main()

"""CLI: python -m caucionbot.main {probe|check|run}"""
import argparse
import json
import logging
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .iol import IOLError
from .notify import avisar
from .ppi import PPIError
from .store import Store
from .strategy import decidir, elegir, tasa_base

try:
    TZ = ZoneInfo("America/Argentina/Buenos_Aires")
except ZoneInfoNotFoundError:
    # Windows no trae base de husos horarios. Argentina es UTC-3 fijo (sin horario de verano).
    TZ = timezone(timedelta(hours=-3), "ART")
log = logging.getLogger("caucionbot")


def ahora():
    return datetime.now(TZ)


def crear_broker(cfg):
    if cfg.broker == "ppi":
        if not all((cfg.ppi_authorized_client, cfg.ppi_client_key, cfg.ppi_key, cfg.ppi_secret)):
            raise SystemExit("Faltan PPI_AUTHORIZED_CLIENT / PPI_CLIENT_KEY / PPI_KEY / PPI_SECRET (ver .env.example)")
        from .ppi import PPIBroker
        return PPIBroker(cfg)
    if cfg.broker == "iol":
        if not cfg.iol_user or not cfg.iol_pass:
            raise SystemExit("Faltan IOL_USER / IOL_PASS (ver .env.example)")
        from .iol import IOLBroker
        return IOLBroker(cfg)
    raise SystemExit(f"BROKER desconocido: {cfg.broker}")


def cotizacion(cfg, broker, hoy):
    quotes = broker.cotizaciones(hoy)
    # Descarta precios viejos (feriados, antes de la primera operación del día)
    quotes = [q for q in quotes if not q.fecha or q.fecha == hoy.isoformat()]
    return elegir(quotes, cfg.min_monto_operado)


def _hm(t):
    return t.strftime("%H:%M")


def base_de(cfg, store, hoy, q=None):
    """Mediana de hoy antes de la ventana: del intradiario del broker si lo hay, si no de las muestras."""
    if q is not None and q.serie:
        hoy_antes = [t for h, t, _ in q.serie if h < _hm(cfg.ventana)]
    else:
        hoy_antes = store.tnas_hoy(hoy, hasta=cfg.ventana)
    return tasa_base(hoy_antes, store.medianas_previas(hoy, cfg.dias_historia, cfg.ventana))


def ultimas_de(cfg, store, hoy, q):
    """TNAs de la ventana para confirmar: un punto por minuto si el broker da intradiario."""
    if q is not None and q.serie:
        return [t for h, t, _ in q.serie if h >= _hm(cfg.ventana)]
    return store.tnas_hoy(hoy, desde=cfg.ventana)


def ejecutar(cfg, broker, store, t, d, q):
    """Envía (o simula, o avisa) un tramo. Devuelve False si hubo error."""
    msg = (f"Caución colocadora {d.plazo_dias}d ({q.simbolo}) por ${d.monto:,.0f} "
           f"a TNA mínima {d.tasa_limite:.2f}% en {broker.nombre}. {d.motivo}")
    if cfg.dry_run:
        avisar(cfg, "[SIMULACIÓN] " + msg)
        store.guardar_tramo(t, d, "dry_run", "simulada")
        return True
    if cfg.modo == "auto":
        ext_id = f"caucionbot-{t:%Y%m%d}-{d.tramo}"
        try:
            orden = broker.colocar_caucion(d.monto, d.tasa_limite, d.plazo_dias, q.simbolo, ext_id)
            store.guardar_tramo(t, d, "auto", orden)
            avisar(cfg, "Orden enviada. " + msg)
            return True
        except NotImplementedError as e:
            log.warning("%s. Paso a modo alerta.", e)
        except Exception as e:  # nunca reintentar a ciegas una orden con plata
            store.guardar_tramo(t, d, "auto", f"error: {e}")
            avisar(cfg, f"Falló la orden ({e}). No envío más tramos hoy. Revisá: {msg}")
            return False
    avisar(cfg, f"COLOCÁ AHORA en {broker.nombre}. " + msg)
    store.guardar_tramo(t, d, "alerta", "avisado")
    return True


def paso_ventana(cfg, broker, store, t, q, llego_limite):
    """Evalúa la ventana. Devuelve True si el día terminó."""
    hoy = t.date()
    base = base_de(cfg, store, hoy, q)
    ultimas = ultimas_de(cfg, store, hoy, q)
    colocadas = [r[2] for r in store.tramos_de(hoy)]
    # Primero decidimos con saldo "infinito" para no consultar la cuenta cada 20 s
    d = decidir(q, ultimas, base, float("inf"), cfg, llego_limite, colocadas)
    if d.colocar:
        d = decidir(q, ultimas, base, broker.pesos_disponibles(), cfg, llego_limite, colocadas)
    log.info("%s | %s", t.strftime("%H:%M:%S"), d.motivo)
    if d.colocar:
        if not ejecutar(cfg, broker, store, t, d, q):
            return True
        if d.tramo >= cfg.tramos:
            return True
    return llego_limite


def cierre(cfg, broker, store, hoy, q):
    """Fin del día: estado de las órdenes y guardar el intradiario para calibrar."""
    if q is not None and q.serie:
        store.guardar_intradia(q)
        log.info("Intradiario de %s guardado (%d puntos, máx %.2f%%)", q.simbolo, len(q.serie),
                 max(t for _, t, _ in q.serie))
    for tramo, monto, tasa, modo, resultado in store.tramos_de(hoy):
        if modo != "auto":
            continue
        try:
            estado = broker.estado_orden(json.loads(resultado))
        except Exception as e:
            estado = f"no pude consultar ({e})"
        avisar(cfg, f"Tramo {tramo}: ${monto:,.0f} a TNA mín {tasa:.2f}% -> estado {estado}")


ERRORES_RED = (IOLError, PPIError, urllib.error.URLError, TimeoutError, ConnectionError, KeyError, ValueError)


def run(cfg, broker, store, now=ahora, sleep=time.sleep):
    hoy = now().date()
    if hoy.weekday() >= 5 or broker.es_feriado():
        log.info("Hoy no hay mercado, nada que hacer.")
        return
    if store.errores_de(hoy) or len(store.tramos_de(hoy)) >= cfg.tramos:
        log.info("Hoy ya se colocaron todos los tramos o hubo un error, salgo.")
        return
    q = None
    while True:
        t = now()
        if t.time() < cfg.inicio:
            sleep(min(cfg.cada_seg, (datetime.combine(hoy, cfg.inicio, TZ) - t).total_seconds() + 1))
            continue
        if t.time() > cfg.limite:
            log.info("Pasó la hora límite.")
            break
        en_ventana = t.time() >= cfg.ventana
        paso = cfg.cada_seg_ventana if en_ventana else cfg.cada_seg
        try:
            q = cotizacion(cfg, broker, hoy) or q
            if q:
                store.guardar_muestra(t, q)
                log.debug("%s %s %sd %.2f%% (%s) vol %.0f", t.strftime("%H:%M:%S"), q.simbolo,
                          q.plazo_dias, q.tna, q.hora, q.monto_operado)
            if en_ventana:
                llego_limite = (t + timedelta(seconds=paso)).time() > cfg.limite
                if paso_ventana(cfg, broker, store, t, q, llego_limite):
                    break
        except ERRORES_RED as e:
            log.warning("Error consultando %s: %s", broker.nombre, e)
        sleep(paso)
    # Después del cierre (17:00) el intradiario ya está completo
    fin = datetime.combine(hoy, cfg.limite, TZ) + timedelta(minutes=4)
    espera = (fin - now()).total_seconds()
    if espera > 0:
        sleep(espera)
    try:
        q = cotizacion(cfg, broker, hoy) or q
    except ERRORES_RED as e:
        log.warning("Error en el cierre: %s", e)
    cierre(cfg, broker, store, hoy, q)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="caucionbot")
    ap.add_argument("cmd", choices=["doctor", "probe", "check", "run"],
                    help="doctor: diagnóstico de PPI | probe: lo que devuelve el broker | "
                         "check: una evaluación | run: el día completo")
    ap.add_argument("--budget", action="store_true", help="doctor: pedir también un presupuesto (no confirma)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "doctor":
        from .doctor import doctor
        doctor(con_budget=a.budget)
        return
    cfg = Config.from_env()
    broker = crear_broker(cfg)

    if a.cmd == "probe":
        if cfg.broker == "ppi":
            from .ppi import resumen_probe
            print(resumen_probe(broker))
        else:
            raw = broker.client.cauciones(cfg.panel)
            print(json.dumps(raw, indent=2, ensure_ascii=False)[:5000])
            for q in broker.cotizaciones():
                print(f"  {q.simbolo:>12} {q.plazo_dias:>3}d TNA {q.tna:6.2f}% monto {q.monto_operado:,.0f} {q.fecha}")
            print(f"Pesos disponibles: {broker.pesos_disponibles():,.2f}")
        return

    store = Store(cfg.db_path)
    if a.cmd == "check":
        t = ahora()
        q = cotizacion(cfg, broker, t.date())
        base = base_de(cfg, store, t.date(), q)
        ultimas = ultimas_de(cfg, store, t.date(), q) or ([q.tna] if q else [])
        colocadas = [r[2] for r in store.tramos_de(t.date())]
        d = decidir(q, ultimas, base, broker.pesos_disponibles(), cfg, False, colocadas)
        print(f"{t:%H:%M} cotización: {q}\nbase: {base}\ndecisión: {d}")
        return

    run(cfg, broker, store)


if __name__ == "__main__":
    main()

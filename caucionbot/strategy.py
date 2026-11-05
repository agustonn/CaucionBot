"""Lógica pura (sin red ni disco): parseo de cotizaciones y decisión de colocar."""
import re
from dataclasses import dataclass, field
from statistics import median


@dataclass
class Quote:
    plazo_dias: int
    tna: float            # TNA en %, ej. 38.5
    monto_operado: float
    simbolo: str = ""
    fecha: str = ""       # YYYY-MM-DD de la última operación
    hora: str = ""        # HH:MM de la última operación, si el broker la informa
    serie: list = field(default_factory=list, repr=False)  # [(HH:MM, tna, volumen)] del día
    raw: dict = field(default_factory=dict, repr=False)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def plazo_dias(row):
    """Días de la caución. IOL no documenta el campo, así que probamos varios.

    Ojo: en títulos 'plazo' suele ser la liquidación ('t0', 't1'), no los días.
    """
    for key in ("descripcionTitulo", "descripcion", "simbolo"):
        m = re.search(r"(\d+)\s*(d\b|d[ií]a)", str(row.get(key, "")), re.I)
        if m:
            return int(m.group(1))
    for key in ("plazoDias", "dias", "plazo"):
        v = str(row.get(key, "")).strip()
        if v and not re.fullmatch(r"t\d", v, re.I):
            m = re.search(r"\d+", v)
            if m:
                return int(m.group())
    return None


def parse_cauciones(data):
    """Convierte la respuesta de /Cotizaciones/cauciones/{panel}/argentina en Quotes."""
    rows = data.get("titulos", []) if isinstance(data, dict) else (data or [])
    out = []
    for r in rows:
        moneda = str(r.get("moneda", "")).lower()
        if moneda and "peso" not in moneda and moneda not in ("1", "ars"):
            continue  # descarta cauciones en dólares
        dias = plazo_dias(r)
        tna = _num(r.get("ultimoPrecio"))
        if dias is None or tna <= 0:
            continue
        out.append(Quote(dias, tna, _num(r.get("montoOperado")), str(r.get("simbolo", "")),
                         str(r.get("fechaHora") or "")[:10], r))
    return out


def elegir(quotes, min_monto_operado):
    """El plazo más corto con liquidez (a fin de día es el que se dispara)."""
    liquidas = [q for q in quotes if q.monto_operado >= min_monto_operado]
    return min(liquidas, key=lambda q: q.plazo_dias) if liquidas else None


def tasa_base(muestras_hoy, medianas_historicas, min_muestras=6):
    """Tasa 'normal' contra la cual medir la disparada.

    Preferimos la mediana de hoy antes de la ventana; si hay pocas muestras,
    la mediana de las medianas diarias de los días previos.
    """
    if len(muestras_hoy) >= min_muestras:
        return median(muestras_hoy)
    if medianas_historicas:
        return median(medianas_historicas)
    return None


def umbral(base, salto_pp, salto_pct):
    return base + max(salto_pp, base * salto_pct / 100)


def es_disparada(tna, base, salto_pp, salto_pct):
    return base is not None and tna >= umbral(base, salto_pp, salto_pct)


def monto_a_colocar(disponible, reserva, monto_max, monto_min):
    m = min(disponible - reserva, monto_max)
    m = int(m // 1000 * 1000)  # redondeo a miles hacia abajo
    return m if m >= monto_min else 0


@dataclass
class Decision:
    colocar: bool
    motivo: str
    tasa_limite: float = 0.0
    plazo_dias: int = 0
    monto: int = 0
    tramo: int = 0


def decidir(quote, ultimas_tna, base, disponible, cfg, llego_limite, colocadas=()):
    """Decide si colocar un tramo ahora.

    ultimas_tna: TNAs de la ventana, la última es la actual (para confirmar).
    colocadas:   TNAs límite de los tramos ya colocados hoy, en orden.

    Tramos (cfg.tramos = N):
      1      cuando se confirma la disparada;
      2..N-1 solo si la tasa subió al menos cfg.salto_tramo_pp sobre el tramo anterior;
      N      a la hora límite, si sigue disparada (las disparadas suelen
             acelerarse en los últimos minutos).
    Con N=1 es una sola orden con todo apenas se confirma.
    """
    tramo = len(colocadas) + 1
    if tramo > cfg.tramos:
        return Decision(False, f"ya se colocaron los {cfg.tramos} tramos")
    if quote is None:
        return Decision(False, "sin cotización de caución")
    neta = quote.tna - cfg.costos_pp
    if neta < cfg.min_tna_neta:
        return Decision(False, f"TNA neta {neta:.2f}% < piso {cfg.min_tna_neta:.2f}%")

    ultimo = tramo == cfg.tramos or llego_limite
    restantes = cfg.tramos - len(colocadas)
    libre = disponible - cfg.reserva
    parte = libre if ultimo else max(libre / restantes, cfg.monto_min)
    monto = monto_a_colocar(min(parte, libre) + cfg.reserva, cfg.reserva, cfg.monto_max, cfg.monto_min)
    limite_tasa = round(max(quote.tna - cfg.slippage_pp, cfg.min_tna_neta + cfg.costos_pp), 2)

    def si(motivo):
        motivo = f"tramo {tramo}/{cfg.tramos}: {motivo}"
        if monto <= 0:
            return Decision(False, f"{motivo}, pero no hay saldo suficiente ({disponible:,.0f})")
        return Decision(True, motivo, limite_tasa, quote.plazo_dias, monto, tramo)

    disparada = es_disparada(quote.tna, base, cfg.salto_pp, cfg.salto_pct)
    base_txt = f"{base:.2f}%" if base is not None else "s/d"
    if llego_limite:
        if disparada:
            return si(f"hora límite con disparada: {quote.tna:.2f}% vs base {base_txt}")
        if not cfg.solo_disparada:
            return si(f"hora límite sin disparada, coloco a {quote.tna:.2f}% (neta {neta:.2f}%)")
        return Decision(False, f"hora límite sin disparada: {quote.tna:.2f}% vs base {base_txt}")

    if not colocadas:
        n = cfg.confirmaciones
        confirmada = (base is not None and len(ultimas_tna) >= n
                      and all(es_disparada(t, base, cfg.salto_pp, cfg.salto_pct) for t in ultimas_tna[-n:]))
        if confirmada:
            return si(f"disparada: {quote.tna:.2f}% vs base {base:.2f}% "
                      f"(umbral {umbral(base, cfg.salto_pp, cfg.salto_pct):.2f}%)")
        return Decision(False, f"sin disparada: {quote.tna:.2f}% vs base {base_txt}")

    if tramo < cfg.tramos and disparada and quote.tna >= colocadas[-1] + cfg.slippage_pp + cfg.salto_tramo_pp:
        return si(f"siguió subiendo: {quote.tna:.2f}% vs {colocadas[-1]:.2f}% del tramo anterior")
    return Decision(False, f"esperando: {quote.tna:.2f}% (último tramo a {colocadas[-1]:.2f}%)")

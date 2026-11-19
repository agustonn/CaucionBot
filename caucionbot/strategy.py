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


# ---------------------------------------------------------------------------
# ¿Se disparó la tasa?
#
# Trabajamos con el intradiario: un punto por minuto (HH:MM, TNA, volumen en $).
#   - Tasa "de ahora": promedio de los últimos MINUTOS_TASA minutos ponderado por
#     volumen, para que una operación suelta y chica no mueva el número.
#   - Tasa "normal" del día: mediana ponderada por volumen antes de la ventana.
#   - Se disparó si, durante CONFIRMACIONES minutos seguidos:
#       1. tasa de ahora >= FACTOR_DISPARADA x tasa normal       (salto grande)
#       2. volumen de esos minutos >= FACTOR_VOLUMEN x lo típico  (hay plata detrás)
# ---------------------------------------------------------------------------

def ponderada(puntos):
    """Tasa promedio ponderada por volumen de [(hora, tna, vol)]. Sin volumen, promedio simple."""
    vol = sum(v for _, _, v in puntos)
    if vol > 0:
        return sum(t * v for _, t, v in puntos) / vol
    return sum(t for _, t, _ in puntos) / len(puntos) if puntos else None


def mediana_ponderada(puntos):
    """Tasa en la que se acumula la mitad del volumen."""
    if not puntos:
        return None
    if sum(v for _, _, v in puntos) <= 0:
        return median(t for _, t, _ in puntos)
    orden = sorted(puntos, key=lambda p: p[1])
    mitad, acum = sum(v for _, _, v in orden) / 2, 0.0
    for _, t, v in orden:
        acum += v
        if acum >= mitad:
            return t
    return orden[-1][1]


@dataclass
class Estado:
    tna: float                 # tasa de ahora (ponderada últimos minutos)
    ultima: float              # último punto
    normal: float = None       # tasa normal del día (o de días previos)
    umbral: float = None
    vol: float = 0.0           # volumen de los últimos minutos
    vol_tipico: float = 0.0    # volumen típico de esa misma cantidad de minutos
    disparada: bool = False    # sostenida CONFIRMACIONES minutos
    hora: str = ""

    def texto(self):
        if self.normal is None:
            return f"TNA {self.tna:.2f}% (sin tasa normal todavía)"
        vol = f", vol {self.vol / 1e6:,.0f}M vs típico {self.vol_tipico / 1e6:,.0f}M" if self.vol_tipico else ""
        return (f"TNA {self.tna:.2f}% vs normal {self.normal:.2f}% "
                f"(umbral {self.umbral:.2f}%{vol}){' DISPARADA' if self.disparada else ''}")


def evaluar(serie, cfg, normal_historica=None):
    """Calcula el Estado a partir del intradiario [(HH:MM, tna, vol)] ordenado."""
    if not serie:
        return None
    m, ventana = max(1, cfg.minutos_tasa), cfg.ventana.strftime("%H:%M")
    antes = [p for p in serie if p[0] < ventana]
    normal = mediana_ponderada(antes) if len(antes) >= 6 else normal_historica
    vols = sorted(v for _, _, v in antes)
    vol_tipico = (vols[len(vols) // 2] * m) if vols else 0.0

    def cumple(i):
        pts = serie[max(0, i - m + 1):i + 1]
        tna, vol = ponderada(pts), sum(v for _, _, v in pts)
        ok = normal is not None and tna >= normal * cfg.factor_disparada
        ok = ok and (vol_tipico <= 0 or vol >= vol_tipico * cfg.factor_volumen)
        return ok and serie[i][0] >= ventana, tna, vol

    ok, tna, vol = cumple(len(serie) - 1)
    n = max(1, cfg.confirmaciones)
    sostenida = len(serie) >= n and all(cumple(i)[0] for i in range(len(serie) - n, len(serie)))
    return Estado(tna, serie[-1][1], normal, normal * cfg.factor_disparada if normal else None,
                  vol, vol_tipico, sostenida, serie[-1][0])


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


def decidir(estado, plazo_dias, disponible, cfg, llego_limite, colocadas=(), montos=()):
    """Decide si colocar ahora.

    colocadas: TNA límite de cada orden ya colocada hoy durante la disparada, en orden.
    montos:    montos de esas órdenes (para calcular el capital del día).

    - Primera orden: cuando la disparada se sostiene CONFIRMACIONES minutos.
    - Siguientes: cada vez que la tasa supera en SALTO_TRAMO_PCT % a la orden anterior.
      Cada orden es TRAMO_PCT % del capital del día (saldo + lo ya colocado hoy).
    - Hora límite: coloca TODO lo que sobró, haya o no disparada (mejor rendir algo que 0),
      salvo SOLO_DISPARADA=true.
    """
    tramo = len(colocadas) + 1
    if estado is None:
        return Decision(False, "sin cotización de caución")
    if tramo > cfg.tramos_max:
        return Decision(False, f"ya se hicieron las {cfg.tramos_max} órdenes del día")
    neta = estado.ultima - cfg.costos_pp
    if neta < cfg.min_tna_neta:
        return Decision(False, f"TNA neta {neta:.2f}% < piso {cfg.min_tna_neta:.2f}%")

    libre = min(disponible - cfg.reserva, cfg.monto_max - sum(montos))

    def si(motivo, todo, tasa):
        motivo = f"orden {tramo}: {motivo}"
        if todo:
            parte = libre
        else:
            capital = min(disponible - cfg.reserva + sum(montos), cfg.monto_max)
            parte = max(capital * cfg.tramo_pct / 100, cfg.monto_min)
            if libre - parte < cfg.monto_min:  # que no quede un resto que no se pueda colocar
                parte = libre
        monto = monto_a_colocar(min(parte, libre) + cfg.reserva, cfg.reserva, float("inf"), cfg.monto_min)
        if monto <= 0:
            return Decision(False, f"{motivo}, pero no hay saldo suficiente ({disponible:,.0f})")
        limite = round(max(tasa - cfg.slippage_pp, cfg.min_tna_neta + cfg.costos_pp), 2)
        return Decision(True, motivo, limite, plazo_dias, monto, tramo)

    if llego_limite:
        if cfg.solo_disparada and not estado.disparada:
            return Decision(False, f"hora límite sin disparada: {estado.texto()}")
        # La última orden va a la tasa del último punto: es la que paga el mercado ahora
        return si(f"hora límite, coloco todo lo que queda. {estado.texto()}", True,
                  min(estado.ultima, estado.tna))

    if not estado.disparada:
        return Decision(False, estado.texto())
    if not colocadas:
        return si(f"se disparó. {estado.texto()}", False, estado.tna)
    siguiente = colocadas[-1] * (1 + cfg.salto_tramo_pct / 100)
    if estado.tna >= siguiente:
        return si(f"sigue subiendo: {estado.tna:.2f}% >= {siguiente:.2f}% "
                  f"(orden anterior a {colocadas[-1]:.2f}%). {estado.texto()}", False, estado.tna)
    return Decision(False, f"disparada, esperando que supere {siguiente:.2f}%. {estado.texto()}")

"""Historial de tasas y órdenes en SQLite (tasa base, tramos del día y datos para calibrar)."""
import json
import sqlite3
from statistics import median


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS muestras(
                ts TEXT, fecha TEXT, plazo INT, tna REAL, monto_operado REAL);
            CREATE TABLE IF NOT EXISTS tramos(
                id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, ts TEXT, tramo INT, monto REAL,
                tasa REAL, plazo INT, modo TEXT, motivo TEXT, resultado TEXT);
            CREATE TABLE IF NOT EXISTS intradia(
                fecha TEXT, ticker TEXT, hora TEXT, tna REAL, volumen REAL,
                PRIMARY KEY (fecha, ticker, hora));
        """)

    def guardar_muestra(self, ts, q):
        self.db.execute("INSERT INTO muestras VALUES (?,?,?,?,?)",
                        (ts.isoformat(), ts.date().isoformat(), q.plazo_dias, q.tna, q.monto_operado))
        self.db.commit()

    def tnas_hoy(self, fecha, desde=None, hasta=None):
        sql, args = "SELECT tna FROM muestras WHERE fecha=?", [fecha.isoformat()]
        if desde:
            sql, args = sql + " AND substr(ts,12,5)>=?", args + [desde.strftime("%H:%M")]
        if hasta:
            sql, args = sql + " AND substr(ts,12,5)<?", args + [hasta.strftime("%H:%M")]
        return [r[0] for r in self.db.execute(sql + " ORDER BY ts", args)]

    def medianas_previas(self, fecha, dias, hasta):
        """Mediana diaria de los últimos `dias` días hábiles, solo antes de la ventana.

        Usa el intradiario guardado (un punto por minuto) y, si no hay, las muestras propias."""
        por_dia = {}
        for f, t in self.db.execute(
                "SELECT fecha, tna FROM intradia WHERE fecha<? AND hora<?",
                (fecha.isoformat(), hasta.strftime("%H:%M"))):
            por_dia.setdefault(f, []).append(t)
        for f, t in self.db.execute(
                "SELECT fecha, tna FROM muestras WHERE fecha<? AND substr(ts,12,5)<?",
                (fecha.isoformat(), hasta.strftime("%H:%M"))):
            if f not in por_dia or len(por_dia[f]) < 6:
                por_dia.setdefault(f, []).append(t)
        return [median(v) for _, v in sorted(por_dia.items())[-dias:]]

    def guardar_intradia(self, q):
        self.db.executemany("INSERT OR REPLACE INTO intradia VALUES (?,?,?,?,?)",
                            [(q.fecha, q.simbolo, h, t, v) for h, t, v in q.serie])
        self.db.commit()

    def tramos_de(self, fecha):
        """Tramos del día (excluye los que fallaron)."""
        return self.db.execute(
            "SELECT tramo, monto, tasa, modo, resultado FROM tramos WHERE fecha=? "
            "AND resultado NOT LIKE 'error%' ORDER BY tramo", (fecha.isoformat(),)).fetchall()

    def errores_de(self, fecha):
        return self.db.execute("SELECT count(*) FROM tramos WHERE fecha=? AND resultado LIKE 'error%'",
                               (fecha.isoformat(),)).fetchone()[0]

    def guardar_tramo(self, ts, d, modo, resultado):
        if not isinstance(resultado, str):
            resultado = json.dumps(resultado, default=str)
        self.db.execute("INSERT INTO tramos(fecha, ts, tramo, monto, tasa, plazo, modo, motivo, resultado) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts.date().isoformat(), ts.isoformat(), d.tramo, d.monto, d.tasa_limite,
                         d.plazo_dias, modo, d.motivo, resultado))
        self.db.commit()

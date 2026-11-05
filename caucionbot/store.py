"""Historial de tasas y órdenes en SQLite (para la tasa base y para no colocar dos veces)."""
import sqlite3
from statistics import median


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS muestras(
                ts TEXT, fecha TEXT, plazo INT, tna REAL, monto_operado REAL);
            CREATE TABLE IF NOT EXISTS ordenes(
                fecha TEXT PRIMARY KEY, ts TEXT, monto REAL, tasa REAL,
                plazo INT, modo TEXT, motivo TEXT, resultado TEXT);
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
        """Mediana diaria de los últimos `dias` días hábiles, solo antes de la ventana."""
        rows = self.db.execute(
            "SELECT fecha, tna FROM muestras WHERE fecha<? AND substr(ts,12,5)<? ORDER BY fecha",
            (fecha.isoformat(), hasta.strftime("%H:%M"))).fetchall()
        por_dia = {}
        for f, t in rows:
            por_dia.setdefault(f, []).append(t)
        return [median(v) for _, v in sorted(por_dia.items())[-dias:]]

    def orden_de(self, fecha):
        return self.db.execute("SELECT * FROM ordenes WHERE fecha=?", (fecha.isoformat(),)).fetchone()

    def guardar_orden(self, ts, d, modo, resultado):
        self.db.execute("INSERT OR REPLACE INTO ordenes VALUES (?,?,?,?,?,?,?,?)",
                        (ts.date().isoformat(), ts.isoformat(), d.monto, d.tasa_limite,
                         d.plazo_dias, modo, d.motivo, resultado))
        self.db.commit()

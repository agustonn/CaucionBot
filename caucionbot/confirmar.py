"""Pregunta por consola con tiempo límite (para MODO=confirmar). Funciona en Windows y Linux."""
import sys
import time


def preguntar(texto, segundos, entrada=None):
    """Devuelve True si responden 's'/'si', False si responden otra cosa, None si no hay respuesta a tiempo."""
    print(f"\n>>> {texto}\n>>> ¿Enviar la orden? Escribí s y Enter ({segundos} s para responder): ",
          end="", flush=True)
    linea = entrada() if entrada else _leer_linea(segundos)
    if linea is None:
        print("\n>>> Sin respuesta, no envío nada.")
        return None
    return linea.strip().lower() in ("s", "si", "sí", "y", "yes")


def _leer_linea(segundos):
    fin = time.monotonic() + segundos
    try:
        import msvcrt  # Windows
    except ImportError:
        import select
        listo, _, _ = select.select([sys.stdin], [], [], segundos)
        return sys.stdin.readline() if listo else None
    buf = ""
    while time.monotonic() < fin:
        if msvcrt.kbhit():
            c = msvcrt.getwche()
            if c in ("\r", "\n"):
                print()
                return buf
            if c == "\b":
                buf = buf[:-1]
            else:
                buf += c
        else:
            time.sleep(0.05)
    return None

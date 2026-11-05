# CaucionBot

Bot que vigila la tasa de caución en pesos durante el día y, si al cierre se
"dispara" por una suba en la demanda de pesos, coloca tus pesos a 1 día
automáticamente. Opera punta a punta en **PPI (Portfolio Personal Inversiones)**
con su API oficial.

## Por qué funciona la idea

La caución a 1 día suele saltar en los últimos minutos de la rueda: bancos y
ALyCs que quedaron cortos de pesos salen a tomar a cualquier tasa para cerrar su
posición. Pasa más seguido en fin de mes (encajes), fechas de vencimientos de
impuestos, aguinaldo y licitaciones del Tesoro. El que tiene pesos líquidos en
ese momento cobra esa tasa por un día (o tres, si es viernes).

## Dónde se puede automatizar

| Broker | API | ¿Opera cauciones por API? | Veredicto |
|---|---|---|---|
| **PPI** | Oficial, documentada, con sandbox | **Sí**: operación `COLOCAR-CAUCIÓN`, tipo `CAUCIONES` | **Elegido** |
| Cocos Capital | No oficial (librería `pyCocos`, ingeniería inversa de la app) | Sí (`api/v2/orders/caucion`) | Funciona, pero sin soporte, con 2FA y riesgo de que la cambien o de violar términos |
| IOL | Oficial | No: solo Comprar/Vender, FCI y cheques | Solo sirve para alertas (`BROKER=iol`) |
| Interactive Brokers | Oficial | No opera BYMA | No aplica |
| C/C Galicia | Sin API para personas | No | Solo como origen de los fondos |

PPI además expone el calendario de feriados, el saldo disponible por plazo de
liquidación y el estado de cada orden, así que el bot no depende de nada manual
una vez que la plata está en la cuenta.

**Fondeo:** transferí desde Galicia a PPI el monto que quieras tener trabajando.
Como la caución a 1 día devuelve la plata al día siguiente, queda disponible para
volver a colocar sin tocar el banco.

## Cómo sacar las credenciales de PPI

1. Abrí cuenta en PPI y fondeala (la API pide haber fondeado al menos una vez).
2. En la web: **Gestiones → Gestión de servicio API**, activá el servicio y
   generá tu `ApiKey` y `ApiSecret`. Ahí también están el `AuthorizedClient` y
   el `ClientKey` de producción. Los de sandbox te llegan por mail.
3. Cargalos en `.env` (ver `.env.example`). Empezá con `PPI_SANDBOX=true`.

## Estrategia

Cada día hábil (consulta feriados a PPI), de 11:05 a 16:52:

1. Busca los instrumentos `CAUCIONES` en pesos de BYMA, deduce el plazo de cada
   uno y toma la tasa del **plazo más corto que haya operado hoy** (1 día, o 3
   si es viernes). Descarta precios con fecha vieja.
2. Guarda cada muestra en SQLite.
3. **Tasa base** = mediana de las muestras de hoy antes de las 16:15. Si hay
   pocas, la mediana de los últimos `DIAS_HISTORIA` días.
4. En la ventana mira cada 30 s. Hay **disparada** si
   `TNA >= base + max(SALTO_PP, base * SALTO_PCT / 100)` durante
   `CONFIRMACIONES` muestras seguidas y la TNA neta de costos supera
   `MIN_TNA_NETA`. Con base 30%: umbral = 30 + max(4, 4.5) = 34.5%.
5. Si se confirma, en PPI:
   - pide un **presupuesto** (`Order/Budget`) por
     `min(disponible - RESERVA, MONTO_MAX)` redondeado a miles, a tasa límite
     `TNA actual - SLIPPAGE_PP`;
   - verifica que el presupuesto coincida con el monto pedido (si no, no confirma);
   - acepta los disclaimers que devuelve PPI (quedan en el log) y **confirma**
     (`Order/Confirm`) con un `externalID` por día;
   - al minuto consulta el estado de la orden y te avisa por Telegram.
6. Una sola orden por día. Si una orden falla, **no reintenta**: te avisa para
   que lo revises (con plata real, reintentar a ciegas puede duplicar la orden).
7. Si llega a la hora límite sin disparada:
   - `SOLO_DISPARADA=true`: no hace nada.
   - `SOLO_DISPARADA=false`: coloca igual a la tasa del momento si supera el
     piso. Es lo que recomiendo: rolleando a 1 día todos los días, la plata
     siempre está disponible al cierre para la próxima disparada.

## Instalación

Solo usa la biblioteca estándar de Python (3.9 o más nuevo). No hace falta el
SDK `ppi-client`: fija `msgpack==1.0.2`, que no instala en Python 3.12+, y el bot
habla REST directo.

```bash
git clone https://github.com/agustonn/CaucionBot.git
```

```bash
cd CaucionBot && cp .env.example .env
```

Completá `.env` y probá:

```bash
python3 -m caucionbot.main probe
```

`probe` muestra la cuenta, los tickers de caución que devuelve PPI, cómo
interpreta el plazo de cada uno, las tasas actuales y tu saldo en pesos. Si no
logra deducir los plazos, mapealos a mano con
`CAUCION_TICKERS=TICKER:dias,...`.

```bash
python3 -m caucionbot.main check
```

`check` hace una evaluación puntual sin ejecutar nada.

### Correrlo todos los días

Necesita una máquina prendida durante la rueda (una VPS chica, una Raspberry o
tu PC). Con cron, de lunes a viernes:

```bash
CRON_TZ=America/Argentina/Buenos_Aires
0 11 * * 1-5 cd /ruta/CaucionBot && python3 -m caucionbot.main run >> bot.log 2>&1
```

GitHub Actions no sirve: sus crons se atrasan varios minutos y la disparada dura
pocos minutos.

### Telegram (opcional, recomendado)

1. Hablá con `@BotFather`, creá un bot y copiá el token a `TELEGRAM_TOKEN`.
2. Mandale un mensaje a tu bot y sacá tu chat id de
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para `TELEGRAM_CHAT_ID`.

## Windows (PowerShell)

PowerShell de Windows no acepta `&&`: los comandos van de a uno. En Windows
Python se llama `python` (o `py`), no `python3`. Trabajá en tu carpeta de
usuario, no en `C:\WINDOWS\system32`.

```powershell
cd $HOME
```

```powershell
git clone -b claude/financial-bot-auto-deposits-s0dl6k https://github.com/agustonn/CaucionBot.git
```

```powershell
cd CaucionBot
```

```powershell
Copy-Item .env.example .env
```

```powershell
notepad .env
```

```powershell
python -m caucionbot.main doctor --budget
```

## Implementación paso a paso

### 1. Diagnóstico contra el sandbox

Con las claves de sandbox en `.env` y `PPI_SANDBOX=true`:

```bash
python3 -m caucionbot.main doctor --budget
```

Prueba login, cuentas, feriados, instrumentos de caución, tasas, puntas, saldo
y órdenes activas, y pide un **presupuesto** de caución por `MONTO_MIN`. El
presupuesto no crea ninguna orden: el doctor nunca llama a `Order/Confirm`.
Deja todo en `doctor.txt`, con las claves reemplazadas por `<SECRETO>` y la
cuenta enmascarada. Revisalo y compartilo si hay que ajustar algo (formato de
tickers, unidad de la tasa, estados de orden).

### 2. Un día completo en sandbox

`PPI_SANDBOX=true`, `DRY_RUN=false`, `MODO=auto`, `SOLO_DISPARADA=false`
(así coloca sí o sí a la hora límite y ves el ciclo entero):

```bash
python3 -m caucionbot.main run -v
```

### 3. Producción en simulación (1 o 2 semanas)

Claves de producción, `PPI_SANDBOX=false`, `DRY_RUN=true`. Repetí el `doctor`
(sin `--budget` si preferís) y dejalo correr todos los días: junta historia de
tasas y registra qué habría hecho en la tabla `ordenes` de `caucionbot.sqlite`.
Ajustá `SALTO_PP`/`SALTO_PCT` según cuántas veces disparó.

### 4. Producción real

`DRY_RUN=false` con un `MONTO_MAX` chico, y después lo subís.

### Dejarlo corriendo en un servidor Linux (VPS o Raspberry)

```bash
sudo useradd -r -m caucionbot && sudo git clone https://github.com/agustonn/CaucionBot.git /opt/CaucionBot
```

```bash
sudo cp /opt/CaucionBot/.env.example /opt/CaucionBot/.env && sudo chown -R caucionbot /opt/CaucionBot && sudo chmod 600 /opt/CaucionBot/.env
```

Editá `/opt/CaucionBot/.env` y después:

```bash
sudo cp /opt/CaucionBot/deploy/caucionbot.service /opt/CaucionBot/deploy/caucionbot.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now caucionbot.timer
```

Logs del día:

```bash
journalctl -u caucionbot -f
```

En Windows: Programador de tareas, tarea diaria de lunes a viernes a las 10:58
que ejecute `python -m caucionbot.main run` con la carpeta del repo como
directorio de inicio (y la PC sin suspenderse hasta las 17:00).

## Lo que hay que verificar en el primer `probe`

- **Unidad de la tasa:** el bot asume que `price` de una caución es la TNA en %
  (ej. `38.5`). Si PPI la devuelve distinto, ajustalo antes de operar.
- **Unidad del volumen:** `MIN_MONTO_OPERADO` viene en 0. Mirá qué número da
  `volume` y poné un mínimo razonable para ignorar plazos sin liquidez.
- **Costos:** PPI cobra 2% + IVA anual en la caución colocadora en pesos según
  fuentes de terceros. Confirmalo en el tarifario y ajustá `COSTOS_PP`.
- **Mínimos:** PPI pide AR$100.000 por caución y cantidades enteras.

## Riesgos

La caución colocadora está garantizada por el mercado (BYMA), con riesgo de
contraparte muy bajo pero no nulo. El bot acepta automáticamente los disclaimers
que PPI devuelve en el presupuesto (los registra en el log). Esto no es
asesoramiento financiero.

## Tests

```bash
python3 -m unittest -v
```

Incluyen una simulación de un día completo con un broker falso, y el flujo de
PPI (presupuesto, control de monto y confirmación) contra una API falsa.

## Estructura

- `caucionbot/strategy.py`: parseo y decisión (lógica pura, testeada).
- `caucionbot/ppi.py`: cliente REST de PPI y broker que opera cauciones.
- `caucionbot/iol.py`: IOL como fuente de tasas, solo alertas.
- `caucionbot/store.py`: historial de tasas y órdenes en SQLite.
- `caucionbot/main.py`: loop del día y CLI.
- `caucionbot/notify.py`: avisos por Telegram.

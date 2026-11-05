# CaucionBot

Bot que vigila la tasa de caución en pesos durante el día y, si al cierre se
"dispara" por una suba en la demanda de pesos, coloca (o te avisa para colocar)
tus pesos a 1 día en IOL.

## Por qué funciona la idea

La caución a 1 día suele saltar en los últimos minutos de la rueda: bancos y
ALyCs que quedaron cortos de pesos salen a tomar a cualquier tasa para cerrar su
posición. Pasa más seguido en fin de mes (encajes), fechas de vencimientos de
impuestos, aguinaldo y licitaciones del Tesoro. El que tiene pesos líquidos en
ese momento cobra esa tasa por un día (o tres, si es viernes).

## Cómo encajan tus herramientas

| Herramienta | Rol | Por qué |
|---|---|---|
| **IOL** | Datos y ejecución | Su API v2 da cotizaciones de cauciones y el saldo de la cuenta. Es donde se coloca. |
| **C/C Galicia** | Fuente de fondos | No tiene API pública para personas: el bot no puede mover plata desde ahí. Transferí a IOL de antemano el monto que quieras tener "listo para disparadas". |
| **Interactive Brokers** | No aplica | IBKR no opera cauciones de BYMA. Sirve para el lado dólares, no para esto. |

Consecuencia práctica: **la plata tiene que estar en IOL antes de la disparada**.
Si la dejás como pesos disponibles sin hacer nada, pierde contra la inflación;
por eso existe `SOLO_DISPARADA=false` (ver abajo).

## Lo que hay que saber antes de usarlo

1. **La API pública de IOL no documenta un endpoint para operar cauciones.**
   Revisé los clientes open source de la API v2 y solo hay órdenes de
   Comprar/Vender títulos, FCI y cheques (CPD). Por eso el bot trae dos modos:
   - `MODO=alerta` (por defecto): detecta la disparada y te manda por Telegram
     el monto, el plazo y la tasa mínima exactos. Vos la colocás desde la app de
     IOL en 30 segundos.
   - `MODO=iol`: envía la orden sola. Para habilitarlo, pedile a IOL (soporte
     API) la ruta y el formato para cauciones colocadoras y completá
     `IOLClient.colocar_caucion` en `caucionbot/iol.py`. No uses endpoints
     internos de la web sin autorización: podés violar los términos de uso.
   Si en algún momento querés ejecución 100% automática sin depender de IOL,
   preguntá en ALyCs que den acceso a API de mercado (por ejemplo vía Primary)
   si esa API incluye cauciones; el bot está armado para cambiar el ejecutor.
2. **El formato de la respuesta de cauciones de IOL tampoco está documentado.**
   El parser es tolerante (busca los días en la descripción o el símbolo), pero
   corré `probe` el primer día y confirmá que interpreta bien los plazos.
3. **Costos.** En un plazo de 1 día, un arancel mal calculado se come la
   ganancia. Cargá `COSTOS_PP` con el tarifario real de IOL.
4. **Riesgo.** La caución colocadora está garantizada por el mercado (BYMA),
   con riesgo de contraparte muy bajo, pero no es cero. Esto no es
   asesoramiento financiero.

## Estrategia

Cada día hábil, de 11:05 a 16:52 (configurable):

1. Toma la cotización de cauciones en pesos y elige el **plazo más corto con
   liquidez** (1 día, o 3 días si es viernes; así no hace falta calendario de
   feriados). Descarta precios con fecha vieja, así que un feriado no dispara nada.
2. Guarda cada muestra en SQLite.
3. **Tasa base** = mediana de las muestras de hoy antes de la ventana (16:15).
   Si hay pocas, usa la mediana de los últimos `DIAS_HISTORIA` días.
4. En la ventana mira cada 30 s. Hay **disparada** si
   `TNA >= base + max(SALTO_PP, base * SALTO_PCT / 100)`
   durante `CONFIRMACIONES` muestras seguidas, y la TNA neta de costos supera
   `MIN_TNA_NETA`. Ejemplo con base 30%: umbral = 30 + max(4, 4.5) = 34.5%.
5. Si se confirma: calcula `monto = min(disponible - RESERVA, MONTO_MAX)`
   redondeado a miles, y coloca con tasa mínima `TNA actual - SLIPPAGE_PP`.
6. Una sola orden por día. Si llega a la hora límite sin disparada:
   - `SOLO_DISPARADA=true`: no hace nada.
   - `SOLO_DISPARADA=false`: coloca igual a la tasa del momento si supera el
     piso. Esto es lo que recomiendo: si rolleás caución a 1 día todos los días
     al cierre, la plata vuelve a estar disponible al día siguiente y siempre
     estás posicionado para la próxima disparada.

## Instalación

Solo usa la biblioteca estándar de Python (3.9+).

```bash
git clone https://github.com/agustonn/CaucionBot.git
```

```bash
cd CaucionBot && cp .env.example .env
```

Editá `.env` con tu usuario de IOL. Después:

```bash
python3 -m caucionbot.main probe
```

```bash
python3 -m caucionbot.main check
```

`probe` muestra la respuesta cruda de IOL y cómo la interpreta el bot. `check`
hace una evaluación puntual sin ejecutar nada.

### Correrlo todos los días

Necesita una máquina prendida durante la rueda (una VPS chica, una Raspberry o
tu PC). Con cron, en hora de Buenos Aires, de lunes a viernes:

```bash
CRON_TZ=America/Argentina/Buenos_Aires
0 11 * * 1-5 cd /ruta/CaucionBot && python3 -m caucionbot.main run >> bot.log 2>&1
```

GitHub Actions no sirve para esto: sus crons se atrasan varios minutos y la
disparada dura pocos minutos.

### Telegram (opcional, muy recomendado en modo alerta)

1. Hablá con `@BotFather`, creá un bot y copiá el token a `TELEGRAM_TOKEN`.
2. Mandale un mensaje a tu bot y sacá tu chat id de
   `https://api.telegram.org/bot<TOKEN>/getUpdates` en `TELEGRAM_CHAT_ID`.

## Plan de puesta en marcha

1. Semana 1 y 2: `DRY_RUN=true`. El bot junta historia y registra qué habría
   hecho (tabla `ordenes` en `caucionbot.sqlite`). Ajustá `SALTO_PP`/`SALTO_PCT`
   mirando cuántas veces disparó.
2. Después: `DRY_RUN=false` con `MODO=alerta` y un `MONTO_MAX` chico.
3. Cuando IOL confirme el endpoint: `MODO=iol`.

## Tests

```bash
python3 -m unittest -v
```

Incluyen una simulación de un día completo con una IOL falsa (disparada a las
16:40, día sin disparada y colocación a la hora límite).

## Estructura

- `caucionbot/strategy.py`: parseo y decisión (lógica pura, testeada).
- `caucionbot/iol.py`: cliente de la API v2 de IOL (token, cauciones, saldo).
- `caucionbot/store.py`: historial de tasas y órdenes en SQLite.
- `caucionbot/main.py`: loop del día y CLI.
- `caucionbot/notify.py`: avisos por Telegram.

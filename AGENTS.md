# AGENTS.md

## Propósito y alcance

Este repositorio contiene un bot autónomo de trading de acciones y ETFs de EE. UU.
contra Alpaca. La estrategia implementada es una reversión a la media con filtros
de SMA, RSI y Bandas de Bollinger; las entradas pasan por un gestor de riesgo y se
envían como órdenes bracket con stop-loss y take-profit.

Estas instrucciones aplican a todo el repositorio, incluido `tests/`. El proyecto
no tiene `pyproject.toml`, `Makefile`, Dockerfile ni configuración de linter o
formatter. La fuente de verdad para el comportamiento es el código Python y sus
pruebas; `README.md` y `.env.example` documentan el uso y deben mantenerse
alineados con ellos.

Antes de modificar el código, comprueba el estado del árbol con:

```bash
git status --short --branch
```

El archivo `.env` local, los logs, la máquina virtual y las cachés son artefactos
locales. Nunca los incluyas en commits ni expongas sus credenciales.

## Estructura del proyecto

```text
.
├── main.py                    # Entrada, compuerta de mercado, ciclo y scheduler
├── config.py                  # .env, dataclasses y validación de configuración
├── modes.py                   # Perfiles de riesgo y resolución de aliases
├── data_feed.py               # Barras históricas/latest de Alpaca
├── strategy.py                # Indicadores y señales BUY/SELL/HOLD
├── news_feed.py               # Noticias de Alpaca y RSS oficial
├── macro_data.py              # Series macro opcionales de FRED
├── fundamental_agent.py       # Contexto y JSON estricto del LLM
├── fundamental_overlay.py     # Worker y veto conservador de BUY
├── fundamental_types.py       # Excepciones compartidas del agente
├── risk_manager.py            # Límites, estado diario y reconciliación de resultados
├── executor.py                # Órdenes bracket, cierres y consulta de órdenes
├── portfolio.py               # Cuenta, posiciones, P&L y reloj de mercado
├── logger.py                  # Loguru: consola y archivos rotativos
├── requirements.txt           # Dependencias de runtime y pruebas
├── .env.example               # Plantilla de configuración sin secretos
├── mode.txt                   # Ejemplo de archivo de hot reload de modo
├── start.sh                   # Wrapper Linux para arrancar el bot
├── ecosystem.config.js        # Configuración PM2 para el VPS
├── deploy.sh                  # Instalación/reinicio del proceso en el VPS
├── .github/workflows/deploy.yml # Deploy por push a main o manual
└── tests/                     # Pruebas unitarias sin llamadas reales a Alpaca
```

El código es un paquete plano: los módulos se importan por nombre (`from config
import ...`), no hay paquete Python ni capas adicionales. Conserva esa estructura
salvo que una migración explícita justifique crear un paquete.

## Arquitectura y flujo de una decisión

`main.main()` crea una sola instancia de cada componente y programa el ciclo con
`schedule`. El ciclo inicial se ejecuta inmediatamente; después se repite cada
`CHECK_INTERVAL_MINUTES` (5 por defecto). El flujo es:

1. `PortfolioTracker.get_market_status()` consulta el reloj de Alpaca. Si no está
   disponible, `main.py` usa una comprobación local de días laborables y horario
   ET. No se opera en los primeros 15 minutos después de las 09:30 ET ni en los
   últimos 15 minutos antes del cierre. La ruta basada en `next_close` respeta
   festivos y cierres anticipados; no se usan horas extendidas.
2. Se obtiene un snapshot de cuenta y posiciones. El universo se normaliza a
   mayúsculas, elimina duplicados y, si existe `ASSETS_FILE`, se vuelve a leer en
   cada ciclo. Las posiciones abiertas siempre se añaden al universo para que
   puedan salir aunque se hayan retirado del archivo de activos.
3. `RiskManager` sincroniza posiciones existentes, reconcilia ventas llenadas
   previamente en Alpaca y actualiza el límite de pérdida diaria usando
   `last_equity` como referencia. Si no hay `last_equity` válido, usa el valor
   actual como baseline inicial.
4. `DataFeed.get_historical_bars()` devuelve un `DataFrame` con MultiIndex
   `(symbol, timestamp)`. Se calculan indicadores por símbolo. Con
   `USE_CLOSED_BARS_ONLY=true`, se evalúa la penúltima fila cuando hay al menos
   dos barras; así se descarta la barra todavía formándose.
5. `MeanReversionStrategy.evaluate()` devuelve `SignalResult` con `Signal.BUY`,
   `Signal.SELL` o `Signal.HOLD`, motivo e indicadores para logging.
6. Si está habilitado, `FundamentalService` actualiza contexto en segundo plano
   y el ciclo solo consulta el último estado validado. En `shadow` no cambia
   decisiones; en `overlay` únicamente puede vetar una BUY nueva por vista
   negativa de alta confianza o riesgo de evento alto. Nunca interviene en SELL,
   stops, take-profits ni en la validación del gestor de riesgo.
7. Las entradas BUY solo pasan si no hay posición, el gestor permite nuevas
   entradas, no existe una orden abierta para el símbolo y el pedido supera todas
   las validaciones de riesgo. La cantidad se calcula con el peor precio estimado
   de entrada, no solo con el cierre de la barra.
8. `OrderExecutor` envía la entrada con bracket. Una entrada aceptada se registra
   en `RiskManager` para poder clasificar posteriormente su salida. Las salidas
   manuales cancelan primero órdenes bracket abiertas y luego solicitan el cierre
   de la posición.

Las excepciones de la configuración terminan el proceso con código 1. El bucle
principal registra excepciones no controladas, espera cinco segundos y continúa;
esto no elimina los fallos de inicialización de componentes ni sustituye la
observabilidad de una API caída.

## Configuración

`config.py` ejecuta `load_dotenv()` al importarse. `load_config()` construye y
valida un `AppConfig`; las credenciales son obligatorias al crear `AlpacaConfig`.
Los números mal formados producen `ValueError` y los booleanos aceptan
`true/1/yes` y `false/0/no`; cualquier otro texto booleano vuelve al valor por
defecto.

Variables obligatorias:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

Variables de conexión y seguridad:

- `PAPER_MODE=true` por defecto. `false` habilita órdenes reales y emite un
  `RuntimeWarning`; exige revisar las claves y hacer pruebas extensas en paper.
- `ALPACA_DATA_FEED=iex` por defecto; también se resuelven `sip` y `otc`. IEX es
  el feed indicado para cuentas gratuitas.
- `ALPACA_BASE_URL` y `ALPACA_DATA_URL` se cargan en `AlpacaConfig`, pero los
  clientes actuales se construyen con las claves y `paper=config.paper_mode` y
  no consumen esos dos campos. No asumas que cambiar esas variables cambia el
  endpoint sin revisar `portfolio.py`, `executor.py` y `data_feed.py`.
- `LOG_LEVEL=DEBUG` por defecto.

Parámetros de estrategia:

| Variable | Defecto | Uso |
|---|---:|---|
| `ASSETS` | `SPY,QQQ,GLD,IWM` | Universo estático, normalizado y deduplicado |
| `ASSETS_FILE` | vacío | Archivo de símbolos re-leído por ciclo; reemplaza `ASSETS` |
| `SMA_SHORT` / `SMA_LONG` | `20` / `50` | Medias móviles de corto y largo plazo |
| `RSI_PERIOD` | `14` | Periodo RSI con suavizado de Wilder |
| `RSI_OVERSOLD` / `RSI_OVERBOUGHT` | `40` / `70` | Umbrales de entrada/salida |
| `BB_PERIOD` / `BB_STD_DEV` | `20` / `1.8` | Ventana y desviaciones de Bollinger |
| `BUY_SIGNAL_MODE` | `score` | `score` multifactorial o `strict` original |
| `BUY_MIN_SCORE` | `5` | Mínimo de la puntuación sobre 8 |
| `RSI_NEAR_OVERSOLD_MARGIN` | `10` | Banda de evidencia RSI débil |
| `REQUIRE_UPTREND` | `true` | Exige precio sobre SMA larga en modo score |
| `BAR_TIMEFRAME` | `15Min` | `15Min`, `5Min`, `1Hour`, `1Day`, etc. |
| `HISTORY_DAYS` | `10` | Días calendario para calentamiento de indicadores |
| `CHECK_INTERVAL_MINUTES` | `5` | Periodicidad del ciclo |
| `USE_CLOSED_BARS_ONLY` | `true` | Ignora la barra en formación |
| `USE_LIMIT_ENTRY` | `true` | Entrada limit con buffer o entrada market |
| `ENTRY_LIMIT_BUFFER_PCT` | `0.25` | Margen porcentual sobre el precio de señal para BUY limit |

Parámetros del agente fundamental opcional:

| Variable | Defecto | Uso |
|---|---:|---|
| `FUNDAMENTAL_ENABLED` | `false` | Habilita el worker de noticias/macro |
| `FUNDAMENTAL_MODE` | `shadow` | `shadow` registra; `overlay` solo puede vetar BUY nuevas |
| `FUNDAMENTAL_POLL_INTERVAL_MINUTES` | `15` | Ingesta durante 07:30–18:00 ET |
| `FUNDAMENTAL_MIN_INFERENCE_GAP_MINUTES` | `30` | Separación mínima entre inferencias |
| `FUNDAMENTAL_STATE_TTL_MINUTES` | `120` | Edad máxima del estado usado por overlay |
| `FUNDAMENTAL_FAIL_OPEN` | `true` | Ante fallo/stale, continuar técnico-only |
| `FUNDAMENTAL_VETO_CONFIDENCE` | `0.75` | Umbral de confianza para postura negativa |
| `FUNDAMENTAL_STATE_FILE` | `logs/fundamental_state.json` | Estado cacheado validado |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | DeepSeek / vacío / `deepseek-v4-flash` | Endpoint compatible con OpenAI y credenciales |
| `LLM_TIMEOUT_SECONDS` | `20` | Timeout estricto del proveedor |
| `FUNDAMENTAL_RSS_URLS` | BLS/Fed/BEA | URLs RSS/Atom oficiales separadas por comas |
| `FRED_API_KEY` / `FRED_SERIES` | vacío / series macro base | Datos macro estructurados opcionales |

El worker agrupa noticias nuevas y cambios macro, limita la frecuencia de
inferencia y usa estado persistente. Los proveedores y el LLM son datos no
confiables; sus textos están delimitados y nunca reciben herramientas de
órdenes. Los fallos, timeouts, 429, XML/JSON inválido y circuitos abiertos
conservan el último estado válido hasta su TTL; después se aplica `fail-open`
como `technical_only`. La implementación del agente no puede saltarse
`RiskManager` ni cambiar cantidad, SL o TP.

Límites de riesgo:

| Variable | Defecto | Efecto |
|---|---:|---|
| `MAX_POSITION_SIZE_PCT` | `7.5` | Máximo por posición |
| `MAX_TOTAL_EXPOSURE_PCT` | `30.0` | Exposición agregada máxima |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | `2.0` / `3.5` | Pierna SL/TP de cada entrada |
| `MAX_DAILY_LOSS_PCT` | `3.0` | Bloquea nuevas entradas durante el día |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Pérdidas antes del cooldown |
| `CONSECUTIVE_LOSS_COOLDOWN_MINUTES` | `120` | Duración de la pausa de nuevas entradas |

`AppConfig.validate()` exige límites positivos, stop menor que take-profit, SMA
corta menor que SMA larga, un universo estático no vacío salvo que exista
`ASSETS_FILE`, `BUY_SIGNAL_MODE` válido, score entre 1 y 8, intervalo y warm-up
positivos, y buffer de entrada no negativo.

### Perfiles de modo

`BOT_MODE=custom` deja todos los valores individuales del entorno. Los otros seis
perfiles viven en `modes.py` y sobrescriben in-place los límites y umbrales que
administran:

`preservacion`, `conservador`, `balanceado`, `crecimiento`, `agresivo` y
`especulativo`. También funcionan los aliases ingleses `preservation`,
`conservative`, `balanced`, `growth`, `aggressive` y `speculative`; la resolución
ignora mayúsculas, espacios y acentos.

Cuando se elige un perfil, los valores de `.env` administrados por el perfil solo
se usan para construir inicialmente la configuración y se reemplazan después;
se registra un warning por cada valor explícito distinto. `custom` es el modo
correcto para ajustar cada parámetro manualmente.

Si `BOT_MODE_FILE` apunta a un archivo, `main.py` toma la primera línea no vacía
y no comentada en cada ciclo. Un modo válido diferente se aplica in-place sobre
los mismos objetos `config.risk` y `config.strategy`; esto es necesario porque
`RiskManager`, `MeanReversionStrategy` y `OrderExecutor` conservan referencias a
ellos. Un archivo ilegible, vacío o con modo inválido conserva el modo actual.
Los brackets ya enviados no cambian sus SL/TP; el nuevo perfil afecta decisiones
nuevas y umbrales de señales posteriores.

`mode.txt` está versionado como ejemplo y contiene `balanceado`, pero no tiene
efecto si `BOT_MODE_FILE` no se configura en `.env`. `ASSETS_FILE` usa el mismo
patrón de hot reload; acepta comas o una línea por símbolo y elimina comentarios
que comienzan con `#`.

## Estrategia

`strategy.py` no usa una librería TA externa. `compute_indicators()` copia el
DataFrame y añade:

- SMA corta y larga.
- RSI con suavizado de Wilder.
- Bandas inferior, media y superior de Bollinger.

En modo `score`, la puntuación BUY es:

- Bollinger: 3 puntos bajo la banda inferior, 1 punto bajo la media, 0 en otro
  caso.
- RSI: 3 puntos bajo `RSI_OVERSOLD`, 1 punto bajo `RSI_OVERSOLD +
  RSI_NEAR_OVERSOLD_MARGIN`, 0 en otro caso.
- Tendencia: 2 puntos si el precio está por encima de SMA larga.

Se necesitan al menos `BUY_MIN_SCORE`, al menos dos familias con puntos y, si
`REQUIRE_UPTREND`, evidencia de tendencia alcista. En modo `strict` deben
cumplirse simultáneamente precio bajo banda inferior, RSI bajo oversold y precio
por encima de SMA larga. Una posición abierta nunca genera BUY.

Para una posición abierta, la implementación actual genera SELL si RSI está por
encima de overbought o si el precio está por encima de la banda media. El stop y
take-profit no se evalúan aquí: los ejecuta Alpaca como piernas bracket. Si faltan
indicadores de calentamiento, la señal es HOLD.

## Riesgo y estado persistente

`RiskManager.validate_order()` bloquea con `RiskBlock` y una regla identificable
cuando falla cualquiera de estas comprobaciones, en este orden: tamaño máximo por
posición, exposición total, pérdida diaria, cooldown por pérdidas consecutivas y
buying power. No saltes el gestor para ejecutar una señal.

La cantidad se calcula en acciones enteras y puede ser cero si una sola acción
supera el límite de la posición. `get_bracket_prices()` devuelve precios
redondeados a cuatro decimales y revierte correctamente SL/TP para SELL, aunque
la estrategia actual abre posiciones largas.

El estado por defecto se guarda en `logs/risk_state.json` e incluye:

- contador de pérdidas consecutivas y momento de fin del cooldown;
- baseline diario, fecha de comprobación y bandera de halt;
- hasta 500 IDs de órdenes de salida procesadas para evitar doble conteo.

Las salidas llenadas se descubren mediante las últimas órdenes cerradas de Alpaca,
incluidas piernas bracket anidadas. Solo ventas llenadas con precio, cantidad,
timestamp e ID válidos se consideran; el resultado se clasifica usando el precio
de entrada rastreado. En pruebas o instancias aisladas, inyecta siempre un
`state_file` temporal para no escribir en `logs/`.

## Ejecución de órdenes

`OrderExecutor.place_bracket_order()` crea una orden `DAY` de clase `bracket` con
`StopLossRequest` y `TakeProfitRequest` adjuntos. Con `USE_LIMIT_ENTRY=true` usa
una limit alrededor del precio de señal; con `false` usa market, pero mantiene
las piernas protectoras. Las entradas rechazadas se registran y devuelven `None`.

Antes de enviar SL/TP, los precios se ajustan al tick de Alpaca: centavos para
precios de al menos un dólar y cuatro decimales para precios inferiores. El
guardia `has_open_order_for_symbol()` revisa tanto la orden padre como sus legs y,
si no puede consultar las órdenes abiertas, asume que existe una para evitar
apilar otra entrada. `close_position()` cancela primero las órdenes abiertas del
símbolo porque los brackets pueden reservar acciones.

Las operaciones de feed y muchas operaciones de ejecución usan reintentos con
backoff exponencial de hasta tres intentos. Los métodos que capturan una
excepción y devuelven `False`/`None` deben conservar ese contrato al modificarse;
no conviertas silenciosamente un rechazo en una orden duplicada.

## Logging y observabilidad

`setup_logger()` se invoca después de cargar y validar la configuración. Loguru
escribe en stderr con colores y en `logs/trading_bot_YYYY-MM-DD.log`, con rotación
de 10 MB, retención de 7 días y compresión gzip. El estado de riesgo usa también
`logs/`. Usa `get_logger()` en módulos nuevos y mensajes que incluyan símbolo,
acción, precio/cantidad y motivo de bloqueo cuando corresponda.

No uses `print()` en el flujo normal. La única salida directa existente es el
error de configuración antes de que el logger esté configurado.

## Pruebas y verificación

Instalación local recomendada:

```bash
python -m venv venv
source venv/bin/activate              # Linux/macOS
# Windows: venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pytest -q
```

La suite actual tiene 91 pruebas y no necesita credenciales ni llamadas de red:

- `tests/test_config.py`: normalización, defaults y validación de modos.
- `tests/test_modes.py`: integridad, aliases y mutación in-place de perfiles.
- `tests/test_data_feed.py`: parsing de timeframes Alpaca.
- `tests/test_strategy.py`: score, strict, BUY bloqueado y SELL.
- `tests/test_risk_manager.py`: límites, pérdida diaria, cooldown, persistencia,
  reconciliación idempotente y matemática de brackets.
- `tests/test_executor.py`: parsing de fills, legs, cancelación, guardia de
  duplicados y requests limit/market.
- `tests/test_portfolio.py`: snapshots con campos opcionales y variantes de P&L.
- `tests/test_main.py`: reloj de mercado, fallback, universo, hot reload y la
  regla de permitir salidas aunque estén bloqueadas las entradas.
- `tests/test_news_feed.py`, `tests/test_macro_data.py`: parseo, deduplicación,
  paginación y ausencia de proveedores.
- `tests/test_fundamental_agent.py`, `tests/test_fundamental_overlay.py`:
  esquema estricto, shadow/overlay, stale, fallback, persistencia y lock de
  inferencia.

Los tests construyen `SimpleNamespace`, clientes fake o instancias con
`object.__new__`; sigue ese patrón para evitar crear `TradingClient` real. Para
cambios de riesgo, prueba tanto el estado en memoria como la recreación desde
JSON. Para cambios de órdenes, verifica el request generado y las piernas antes
de considerar una prueba de integración.

No hay job de tests en el workflow de GitHub Actions: `.github/workflows/deploy.yml`
solo despliega. Por eso, ejecuta `python -m pytest -q` localmente antes de hacer
push. No existe una orden de lint/format oficial; conserva el estilo de los
módulos existentes (type hints, `from __future__ import annotations`, dataclasses,
`snake_case`, clases PascalCase y docstrings en APIs públicas).

## Arranque y despliegue

Para desarrollo o paper trading, copia `.env.example` a `.env`, completa las
claves de paper y ejecuta:

```bash
python main.py
```

El bot no arranca sin las dos claves requeridas. Comprueba `PAPER_MODE`, el feed,
el modo activo y el universo antes de iniciar; nunca cambies a live como parte de
una prueba automática.

El despliegue esperado es Linux en `/home/ubuntu/trading_bot`:

- `start.sh` entra al directorio, activa `venv` y ejecuta `python main.py`.
- `ecosystem.config.js` registra el proceso PM2 `trading-bot`, reinicio
  automático, delay de 5 segundos y logs PM2 bajo `logs/`.
- `deploy.sh` crea `venv` si falta, instala `requirements.txt`, crea `logs`, da
  permisos a `start.sh`, elimina el proceso PM2 existente, lo inicia de nuevo y
  ejecuta `pm2 save`.
- El workflow se dispara en push a `main` o manualmente. Usa rsync con `--delete`
  y excluye `venv/`, `logs/`, `.env`, `__pycache__/` y `.git/`; después ejecuta
  `deploy.sh` por SSH.

El workflow requiere los secretos `VPS_SSH_PRIVATE_KEY`, `VPS_HOST`, `VPS_USER` y
opcionalmente `VPS_PORT` (22 por defecto). No modifica el `.env` del servidor,
por lo que ese archivo debe existir y gestionarse fuera del repositorio. Ten
presente que `rsync --delete` elimina del destino archivos versionados que ya no
estén en el árbol enviado; confirma el alcance antes de alterar archivos de
despliegue.

## Reglas para cambios

1. Mantén `PAPER_MODE=true` en ejemplos y pruebas. Nunca pongas claves reales en
   código, fixtures, logs ni mensajes.
2. Toda nueva ruta de decisión de trading debe pasar por `RiskManager`, registrar
   su motivo y tener pruebas de permitido y bloqueado.
3. Si cambias `StrategyConfig`, `RiskConfig`, perfiles o defaults, actualiza
   `config.py`, `modes.py`, `.env.example`, `README.md` y las pruebas pertinentes.
4. Si cambias el contrato de Alpaca, cubre variantes del SDK con objetos fake y
   revisa los nombres de campos (`unrealized_plpc`, `filled_avg_price`, `legs`,
   estados y enums).
5. No reemplaces `config.risk` ni `config.strategy` durante un hot reload: los
   componentes ya construidos deben seguir viendo la misma instancia.
6. Conserva la seguridad de salida: una posición retirada del universo debe
   seguir analizándose hasta cerrarse; una orden bracket abierta no debe duplicarse
   ni dejar acciones reservadas antes de un cierre manual.
7. Acompaña cambios de comportamiento con pruebas unitarias y documentación.
   El historial usa mensajes enfocados, habitualmente con estilo
   `feat(scope): ...`, `fix(scope): ...` o `refactor(scope): ...`.
8. Antes de entregar, ejecuta la suite completa, revisa `git diff` y confirma que
   el único artefacto nuevo o modificado es el esperado.
9. Todo agente de IA que realice un cambio o modificación en el proyecto debe
   actualizar también este `AGENTS.md` en la misma entrega. Debe añadir una
   entrada en el registro indicando la fecha, un resumen del cambio, los archivos
   afectados y las pruebas o verificaciones ejecutadas. Esta obligación aplica a
   cambios de código, configuración, documentación, pruebas y despliegue.

## Registro de cambios realizados por agentes

Cada modificación hecha por un agente de IA debe añadir una entrada con este
formato:

```text
### YYYY-MM-DD — Nombre del agente
- Cambio: descripción breve del cambio realizado.
- Archivos: lista de archivos modificados.
- Verificación: pruebas o comprobaciones ejecutadas y su resultado.
```

### 2026-08-24 — Codex

- Cambio: se añadió la regla que obliga a los agentes de IA a registrar sus
  modificaciones en este archivo.
- Archivos: `AGENTS.md`.
- Verificación: revisión del diff del archivo.

### 2026-08-25 — Codex

- Cambio: se integró un agente fundamental opcional de noticias macro y de
  activos, con fuentes Alpaca/RSS/FRED, cliente LLM compatible con OpenAI,
  worker cacheado, modo shadow y overlay que solo puede vetar nuevas BUY.
  También se documentaron la configuración, el fallback técnico y la política
  de seguridad del componente.
- Archivos: `config.py`, `.env.example`, `requirements.txt`, `main.py`,
  `news_feed.py`, `macro_data.py`, `fundamental_types.py`,
  `fundamental_agent.py`, `fundamental_overlay.py`, `README.md`, `AGENTS.md`,
  `tests/test_news_feed.py`, `tests/test_macro_data.py`,
  `tests/test_fundamental_agent.py`, `tests/test_fundamental_overlay.py`.
- Verificación: `python -m compileall -q config.py fundamental_types.py
  news_feed.py macro_data.py fundamental_agent.py fundamental_overlay.py
  main.py tests`; `python -m pytest -q` — 91 passed, 1 warning.

### 2026-08-26 — Codex

- Cambio: se acotó el contexto y la salida del agente fundamental, se desactivó
  por defecto el razonamiento de DeepSeek, se añadió telemetría segura de
  latencia/tokens y se limitaron los reintentos a fallos transitorios o
  respuestas vacías. El circuito de inferencia ahora evita nuevos intentos
  durante 30 minutos tras un fallo y se documentó el diagnóstico correcto desde
  el directorio de despliegue de la VPS.
- Archivos: `config.py`, `.env.example`, `fundamental_agent.py`,
  `fundamental_overlay.py`, `README.md`, `tests/test_config.py`,
  `tests/test_fundamental_agent.py`, `tests/test_fundamental_overlay.py`,
  `AGENTS.md`.
- Verificación: `python -m pytest -q` — 97 passed, 1 warning; `python -m
  compileall -q config.py fundamental_agent.py fundamental_overlay.py tests`;
  `git diff --check` sin errores.

### 2026-08-26 — Codex

- Cambio: se normalizaron los espacios internos, saltos de línea y espacios no
  separables de titulares y resúmenes RSS después de eliminar HTML y decodificar
  entidades, antes de aplicar los límites del contexto del LLM.
- Archivos: `news_feed.py`, `tests/test_news_feed.py`, `README.md`, `AGENTS.md`.
- Verificación: `python -m pytest -q tests/test_news_feed.py` — 4 passed, 1
  warning.

## Discrepancias conocidas de la documentación

Al actualizar documentación, verifica el código para no perpetuar estas
diferencias observadas:

- El código y `.env.example` usan `BB_STD_DEV=1.8` por defecto y el perfil
  `conservador`; algunas frases antiguas del README mencionan 2.0.
- El logger actual genera nombres diarios `trading_bot_YYYY-MM-DD.log`, no un
  único `logs/trading_bot.log`.
- `data_feed.py` implementa barras históricas y las más recientes mediante
  polling; no hay websocket ni stream de datos en tiempo real.
- `ALPACA_BASE_URL` y `ALPACA_DATA_URL` están modeladas en configuración, pero no
  son argumentos usados por los clientes Alpaca actuales.

La documentación futura debe describir el comportamiento efectivo o, si se cambia
el comportamiento, incluir el cambio de código y sus pruebas en la misma entrega.

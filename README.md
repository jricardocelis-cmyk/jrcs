# goalalert

Agente que vigila partidos de fútbol en vivo (ligas locales e internacionales) y te avisa por Telegram, ntfy o consola cuando una apuesta en **mercados de goles** tiene valor esperado positivo según un modelo de goles restantes. Mercados: más/menos de N.5 goles, ambos anotan y próximo gol.

No usa un LLM para estimar probabilidades: un modelo de Poisson es más rápido, reproducible y auditable. Lo que entrega cada alerta es una probabilidad, una cuota y un valor esperado, no una certeza. Ningún filtro garantiza ganar; lo que sí puedes hacer es medir con tus propios datos si el modelo tiene ventaja (ver *Calibración*).

## Arranque rápido (sin API key)

```bash
python -m goalalert simulate --matches 10 --seed 7
python -m pytest -q
```

`simulate` corre una jornada ficticia de punta a punta (alertas, confirmación previa, deduplicación, liquidación y reporte). Sirve para ver la tubería funcionando. Su ROI no es evidencia de nada: el simulador inyecta ruido en las cuotas y el agente lo explota por construcción.

## Datos reales

El proveedor incluido es [API-Football](https://www.api-football.com) (marcador, eventos, estadísticas y cuotas en vivo en un solo servicio). El plan gratuito (100 requests/día) no alcanza para sondeo continuo; con las ligas populares y unos 10 partidos en vivo a la vez el consumo ronda las 400 consultas por hora (2 por ciclo más las estadísticas de cada partido cada 2 minutos), unas 5 000 en una jornada cargada de 12 horas, así que para uso continuo necesitas un plan de pago. El agente lee el contador `x-ratelimit-requests-remaining` y deja de pedir estadísticas extra cuando quedan menos de 50. Sin partidos en vivo consulta una sola vez cada `idle_poll_seconds` (120 por defecto) y no pide cuotas; con partidos, cada `poll_seconds`. La edad de las cuotas se mide contra el reloj del servidor de la API (cabecera `Date`), no contra el de tu PC.

```bash
cp config.example.toml config.toml
export APIFOOTBALL_KEY=...
python -m goalalert markets -c config.toml   # ligas en vivo (ids), nombres de mercado y valores crudos de tu cuenta
python -m goalalert schedule -c config.toml   # partidos de hoy y próximos días de tus ligas (hora de Lima); puede no estar en el plan gratuito
python -m goalalert coverage -c config.toml   # verifica tus ligas contra tu cuenta: nombre real, estadísticas, eventos y cuotas
python -m goalalert snapshot -c config.toml  # un ciclo real sin alertas: qué calcula el modelo y por qué se descarta cada apuesta
python -m goalalert run -c config.toml
```

Con una cuenta real se confirmó la conexión, la lectura de partidos en vivo y los nombres de mercado. La lectura de las cuotas y el comportamiento del modelo sobre partidos reales se comprueban con `snapshot`, que imprime por cada partido en vivo el marcador, los tiros, los goles esperados y, para cada apuesta, la probabilidad del modelo, la del mercado y el motivo exacto por el que se descarta (o «ALERTA»). Es la herramienta para entender por qué no llegan alertas, y usa la misma lógica de filtros que el agente en vivo. «Match Goals» aparece en la lista de mercados pero no está mapeado a propósito: hasta confirmar con la muestra de valores crudos de `markets` que es un Over/Under comparable, mezclarlo podría generar señales falsas. El próximo gol se lee de «Which team will score the Nth goal?», y solo cuando N es los goles actuales más uno. Ejecuta `markets` primero: si los nombres de mercado de tu cuenta difieren, corrígelos en `[provider.bet_names]`. Una discrepancia se manifiesta como cero alertas, no como alertas erróneas, porque el agente exige mercados completos (Over y Under, Sí y No, local/visita/ninguno) antes de evaluar.

Por defecto vigila las competiciones que más ofrecen las casas de apuestas (`leagues = ["popular"]`): Champions, Europa League, Conference, las grandes ligas europeas, Libertadores, Sudamericana, Brasileirão, Liga Argentina, Liga MX, MLS, selecciones (Mundial, Eurocopa, Copa América, Nations League, Eliminatorias, amistosos) y copas nacionales. La Liga 1 de Perú es una más. Puedes agregar ids (`["popular", 999]`), dejar solo los tuyos o usar `[]` para vigilar todo lo que esté en vivo; la lista está en `goalalert/leagues.py`. Esos ids vienen de la documentación de la API y no se contrastaron al escribirlos, por eso `coverage` los verifica contra tu cuenta y marca con ⚠ cualquiera cuyo nombre real no coincida.

## Tu casa peruana

Te Apuesto, Apuesta Total, Inkabet, Doradobet, Betano, Betsson y similares no publican una API de cuotas, y automatizar la lectura de sus sitios va contra sus términos de uso y suele terminar en cuenta cerrada, además de romperse cada vez que cambian la página. Por eso el agente no las lee. El mercado de referencia (el de API-Football) sirve para detectar la oportunidad y validar el modelo, y la decisión final se toma contra el precio de tu casa.

Cada alerta incluye una línea «EN TU CASA» (con `my_bookmaker = "Te Apuesto"` dice «EN TE APUESTO») con la **cuota mínima** a la que sigue habiendo valor: `(1 + min_ev) / probabilidad combinada`. Si tu casa paga esa cuota o más en el mismo mercado y con el mismo marcador, la señal sigue en pie; si paga menos, se descarta. Esto importa porque el valor esperado de la alerta está calculado a la cuota de referencia, y una casa con más margen en vivo puede borrarlo por completo. También hay que apostar el stake sugerido sobre la cuota que realmente te den, no sobre la de referencia. Si tu casa no ofrece algún mercado en vivo (por ejemplo, próximo gol), quítalo de `markets` en `[value]` para no recibir alertas que no puedes jugar.

## Alertas al celular

- **Telegram**: crea un bot con @BotFather, exporta `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, y pon `telegram = true`.
- **ntfy**: instala la app ntfy, suscríbete a un tema difícil de adivinar, exporta `NTFY_TOPIC` y pon `ntfy = true`.
- Verifica con `python -m goalalert test-notify -c config.toml`.

El agente necesita estar encendido durante los partidos: una laptop, un VPS pequeño o una Raspberry con `systemd` o `tmux`. Un contenedor efímero en la nube no sirve para esto.

## Cómo decide

Con `prior = "market"` (el valor por defecto) el modelo no parte de un promedio de liga, sino de los goles restantes que implican las cuotas en vivo: del Over/Under más equilibrado se despeja el total esperado y del mercado de próximo gol el reparto entre local y visita. Así conoce a los equipos: en un Flamengo–Estudiantes real el mercado daba 62% al próximo gol de Flamengo y el prior de liga solo 51%, y esa diferencia no era una oportunidad sino ignorancia del modelo. Desde esa base solo se mueve por la presión observada en tiros y xG, de modo que una alerta significa «el partido se juega distinto a como lo cotiza el mercado». Dos consecuencias: las alertas serán poco frecuentes, y solo salen en partidos con estadísticas. El mercado también ve el xG, así que esta ventaja existe únicamente si lo incorpora con retraso; `report` dirá con datos propios si es el caso. Con `prior = "league"` se vuelve al modelo anterior, que sí ajusta por marcador y tarjetas y sirve para comparar. El resto de este apartado describe ese modelo base.

Para cada partido en juego entre los minutos 20 y 82, el modelo estima los goles restantes de cada equipo como `base × fracción de tiempo restante × estado del marcador × tarjetas rojas × presión`. La presión compara el xG observado (o uno aproximado con tiros y tiros al arco) con el esperado hasta ese minuto, encogido hacia 1 con `pressure_k0`, de modo que poca evidencia mueve poco la estimación. Con esas tasas, las probabilidades de cada mercado salen de distribuciones de Poisson: la suma de los goles restantes de ambos equipos para Over/Under, el producto de que cada equipo anote al menos una vez para ambos anotan, y la razón entre tasas para el próximo gol.

Cada probabilidad del modelo se mezcla con la implícita del mercado (sin margen, `market_weight = 0.4`). Es un encogimiento deliberado: el mercado en vivo es eficiente en promedio, y alertar solo con el modelo puro multiplicaría las señales falsas. Una apuesta se alerta si cumple todo lo siguiente: probabilidad combinada ≥ 55%, valor esperado ≥ 6%, cuota entre 1.40 y 4.00, mercado completo y no suspendido, cuota con menos de 60 s, y una diferencia modelo-mercado de menos de 15 puntos. Esta última guarda es la más importante: una brecha grande casi siempre indica una cuota vieja, un gol que aún no se refleja o un modelo roto, no una oportunidad.

Antes de enviar, el agente vuelve a consultar marcador, tarjetas y cuota de ese partido. Si cayó un gol entre las dos lecturas, la cuota se suspendió o bajó más de 3%, no alerta. Si la cuota cambió poco, la alerta sale con la cuota vigente y el valor esperado recalculado. Además hay enfriamiento por pick, tope de señales por partido y por ciclo, y un stake sugerido con Kelly fraccional (10%) topado en 1.5% del bankroll.

Las ligas sin cobertura de estadísticas (frecuente en torneos locales menores) no generan alertas mientras `require_stats = true`, porque sin tiros ni xG el modelo solo tendría su prior y cualquier diferencia con el mercado sería ruido. Puedes desactivarlo, sabiendo que pierdes esa protección.

## Qué esperar de las alertas

En el primer `snapshot` real con el prior de mercado (6 partidos en vivo, 5 con cuotas, unas 40 apuestas evaluadas) ninguna tuvo valor esperado positivo: la mejor quedó en −1.5% y la mayoría entre −5% y −10%. No es una falla. El mercado de referencia cobra un margen de ~5-6% por apuesta, y el modelo parte de ese mismo mercado, así que para llegar a +6% tendría que estar ~12% mejor informado que él, lo que solo ocurre cuando la presión observada (tiros, xG) contradice el precio. Por eso las alertas serán raras, y que haya semanas con pocas o ninguna es normal.

Los datos tienen dos límites que conviene conocer. Las cuotas de la API llegan con ~40 s de retraso respecto al partido (sin relación con el reloj de tu PC), por lo que el agente descarta un partido cuando las cuotas son de otro marcador que el del partido (cayó un gol en ese intervalo) y reconfirma marcador y cuota justo antes de alertar. Y solo los partidos con estadísticas pueden alertar: en el primer `snapshot` real, la Libertadores traía tiros y xG y las ligas menores (Bolivia, Caribe, Venezuela, USL) no traían ninguno. Las competiciones populares suelen declarar estadísticas y `coverage` muestra cuáles; que lleguen partido a partido en vivo se comprueba con `snapshot` mientras se juega.

## Calibración

Cada alerta queda en SQLite (`goalalert.db`) y se liquida sola cuando termina el partido con el marcador a 90'. El próximo gol se anula si ambos equipos anotaron después de la señal, porque el orden no se puede inferir del marcador final.

```bash
python -m goalalert report -c config.toml
```

El reporte muestra, por mercado, la tasa de acierto contra la probabilidad media alertada, el puntaje de Brier y el ROI a stake plano. Un modelo calibrado tiene acierto ≈ probabilidad media; un ROI positivo sostenido es la única prueba de ventaja real. Con menos de unas 200-300 señales liquidadas el ruido domina: un 89% de acierto en 9 señales no significa nada. Opera con stake simbólico o sin dinero hasta tener esa muestra, y recalibra `[model]` (priors por liga, `score_mult`, factores de rojas) con lo que muestre el reporte.

## Supuestos sin validar

Los valores por defecto (reparto 45/55% de goles entre tiempos, multiplicadores por marcador y rojas, `xg_per_sot = 0.24`, priors de 1.45 y 1.15 goles) son supuestos razonables tomados de órdenes de magnitud conocidos, no ajustes sobre datos. Se verificó que el modelo reproduce tasas base sensatas al inicio de un partido (Over 2.5 ≈ 48%, ambos anotan ≈ 52%), pero su comportamiento en vivo en cada liga es justo lo que hay que medir con `report`.

## Estructura

```
goalalert/model.py       Poisson de goles restantes y probabilidades por mercado
goalalert/value.py       cuota vs probabilidad: EV, Kelly, guardas de cordura
goalalert/agent.py       ciclo en vivo: leer, evaluar, confirmar, alertar, liquidar
goalalert/providers/     api-football (real) y sim (simulador)
goalalert/store.py       SQLite: deduplicación, liquidación, reporte
goalalert/notifiers.py   consola, Telegram, ntfy
```

Sin dependencias externas (solo biblioteca estándar, Python ≥ 3.11). Para añadir otra fuente de datos basta con implementar la interfaz de `providers/__init__.py`.

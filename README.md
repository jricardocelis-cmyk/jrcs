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

El proveedor incluido es [API-Football](https://www.api-football.com) (marcador, eventos, estadísticas y cuotas en vivo en un solo servicio). El plan gratuito (100 requests/día) no alcanza para sondeo continuo; con un plan de pago, `poll_seconds = 30` y unas pocas ligas el consumo es manejable. El agente lee el contador `x-ratelimit-requests-remaining` y deja de pedir estadísticas extra cuando quedan menos de 50.

```bash
cp config.example.toml config.toml
export APIFOOTBALL_KEY=...
python -m goalalert markets -c config.toml   # ligas en vivo (ids) y nombres de mercado de tu cuenta
python -m goalalert run -c config.toml
```

Los parsers de API-Football siguen la forma documentada de la API pero **no se contrastaron con respuestas reales** al escribirlos (no hubo acceso a su documentación desde el entorno de desarrollo). Ejecuta `markets` primero: si los nombres de mercado de tu cuenta difieren, corrígelos en `[provider.bet_names]`. Una discrepancia se manifiesta como cero alertas, no como alertas erróneas, porque el agente exige mercados completos (Over y Under, Sí y No, local/visita/ninguno) antes de evaluar.

Para fijar la liga local, pon su id en `leagues` (Liga 1 de Perú, Libertadores, etc.; los ids salen de `markets`). Con `leagues = []` vigila todo lo que esté en vivo.

## Alertas al celular

- **Telegram**: crea un bot con @BotFather, exporta `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, y pon `telegram = true`.
- **ntfy**: instala la app ntfy, suscríbete a un tema difícil de adivinar, exporta `NTFY_TOPIC` y pon `ntfy = true`.
- Verifica con `python -m goalalert test-notify -c config.toml`.

El agente necesita estar encendido durante los partidos: una laptop, un VPS pequeño o una Raspberry con `systemd` o `tmux`. Un contenedor efímero en la nube no sirve para esto.

## Cómo decide

Para cada partido en juego entre los minutos 8 y 82, el modelo estima los goles restantes de cada equipo como `base × fracción de tiempo restante × estado del marcador × tarjetas rojas × presión`. La presión compara el xG observado (o uno aproximado con tiros y tiros al arco) con el esperado hasta ese minuto, encogido hacia 1 con `pressure_k0`, de modo que poca evidencia mueve poco la estimación. Con esas tasas, las probabilidades de cada mercado salen de distribuciones de Poisson: la suma de los goles restantes de ambos equipos para Over/Under, el producto de que cada equipo anote al menos una vez para ambos anotan, y la razón entre tasas para el próximo gol.

Cada probabilidad del modelo se mezcla con la implícita del mercado (sin margen, `market_weight = 0.4`). Es un encogimiento deliberado: el mercado en vivo es eficiente en promedio, y alertar solo con el modelo puro multiplicaría las señales falsas. Una apuesta se alerta si cumple todo lo siguiente: probabilidad combinada ≥ 60%, valor esperado ≥ 6%, cuota entre 1.40 y 4.00, mercado completo y no suspendido, cuota con menos de 60 s, y una diferencia modelo-mercado de menos de 15 puntos. Esta última guarda es la más importante: una brecha grande casi siempre indica una cuota vieja, un gol que aún no se refleja o un modelo roto, no una oportunidad.

Antes de enviar, el agente vuelve a consultar marcador, tarjetas y cuota de ese partido. Si cayó un gol entre las dos lecturas, la cuota se suspendió o bajó más de 3%, no alerta. Si la cuota cambió poco, la alerta sale con la cuota vigente y el valor esperado recalculado. Además hay enfriamiento por pick, tope de señales por partido y por ciclo, y un stake sugerido con Kelly fraccional (10%) topado en 1.5% del bankroll.

Las ligas sin cobertura de estadísticas (frecuente en torneos locales menores) no generan alertas mientras `require_stats = true`, porque sin tiros ni xG el modelo solo tendría su prior y cualquier diferencia con el mercado sería ruido. Puedes desactivarlo, sabiendo que pierdes esa protección.

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

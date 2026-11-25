# Spike 001 — un orquestador agéntico frente al runner por etapas

**Pregunta:** ¿aporta algo poner un agente por encima del pipeline, con las etapas como
herramientas (`run_stage`, `read_checkpoint`, `finish`), en lugar del runner determinista?

**Respuesta corta:** no. El agente se recupera bien de un fallo transitorio, pero no sabe cuándo
terminar, cuesta ~12× en tokens y no añade ninguna garantía que el runner no tenga ya. La
estructura por etapas se queda; los agentes entran dentro de las etapas, donde falta criterio
(`--editorial`, el investigador de `gather`, la elección de voz), no encima.

## Montaje

Ambos caminos parten del mismo `articles.json`, sembrado con 8 páginas reales de RTVE del
2026-11-25 (`seed-raw.json`) resumidas por el modelo real. La inferencia va por Aixy, ruta
`aixy/alibaba-routing` (sustituta de `aixy/podcast-agent`, que aún devuelve 404 con la key local).

| Camino     | Qué es                                                                     |
|------------|----------------------------------------------------------------------------|
| `runner`   | `EpisodeRun` determinista: classify → select → script, con checkpoints      |
| `agent`    | un agente con `run_stage` / `read_checkpoint` / `finish`, mismas etapas     |

Al camino del agente se le inyecta **un fallo transitorio** en la primera llamada a `script`
(HTTP 503 simulado) para ver si se recupera. El runner corre limpio.

```bash
.venv/bin/python main.py seed      # siembra articles.json con llamadas reales
.venv/bin/python main.py runner
.venv/bin/python main.py agent     # deja agent-transcript.json con toda la traza
```

## Resultados (2026-11-25)

| Métrica                                | runner          | agente                    |
|----------------------------------------|-----------------|---------------------------|
| Llamadas de etapa                      | 9               | 9                         |
| Tokens de etapa (prompt+completion)    | 11 749          | 11 749                    |
| Llamadas del orquestador               | —               | 16 (agota `max_steps`)    |
| Tokens del orquestador                 | —               | **111 723**               |
| Guion producido                        | sí              | sí (en el reintento)      |
| Recupera del 503 inyectado             | n/a             | **sí**                    |
| Llama a `finish` con un informe        | n/a             | **no**                    |
| Resultado                              | `EpisodeRunReport` | `AgentError: No result after 16 steps` |

Traza real del agente (`agent-transcript.json`):

```
run_stage(gather) → run_stage(classify) → run_stage(select)
run_stage(script)   ← 503 inyectado
run_stage(script)   ← reintento, guion escrito
read_checkpoint(script.json) ×11   ← bucle; nunca llama a finish
```

### Lo que salió bien

- **Recuperación del fallo transitorio.** El agente leyó el error, entendió que era reintentable
  y volvió a llamar a `script`. El checkpoint hizo el reintento barato: no repitió classify.
- **El checkpoint como contrato funciona.** Cada `run_stage` dejó su fichero y el reintento
  reutilizó lo ya hecho, igual que en el runner.

### Lo que salió mal

- **No termina.** Leyó `script.json` once veces seguidas y agotó los 16 pasos sin llamar a
  `finish`. El runner no tiene este problema porque no decide cuándo terminar: termina.
- **Coste.** 111 723 tokens de prompt para hacer lo mismo que el runner con 9 483. El bucle
  relee 6 000 caracteres de checkpoint en cada vuelta: el contexto crece y cada paso cuesta más.
  Con `qwen-flash` es asumible; con el modelo de la ruta de producción, no.
- **Sin informe.** Al no llamar a `finish` no hay resultado estructurado: ni etapas ejecutadas,
  ni de qué se recuperó, ni aviso de que el guion existe. El runner devuelve un
  `EpisodeRunReport` validado, siempre.

### Hallazgo colateral: JSON largo del modelo de ruta

El primer intento de sembrar las 8 noticias en **una sola** llamada devolvió JSON inválido dos
veces (`Expecting ',' delimiter`: el modelo cerró un array y abrió el objeto siguiente sin coma).
Pedir un ítem por llamada funcionó a la primera. Es exactamente la forma en que el pipeline real
clasifica (una llamada por noticia, validada con Pydantic), así que el diseño ya estaba en el lado
bueno; el spike lo confirma con un caso real.

## Conclusión

- **Se mantiene el runner determinista.** Da control de coste, checkpoints legibles, reanudación y
  un informe por ejecución; el orquestador no mejora nada de eso y añade un modo de fallo nuevo
  (no terminar).
- **Los agentes se quedan donde hay juicio, no donde hay secuencia:** el investigador de `gather`
  (lee y elige qué es noticia), `--editorial` en `select` y la elección de voz. Todos con salida
  validada y con el camino determinista como respaldo.
- **Si algún día se quiere un orquestador**, tendría que aportar algo que el runner no tiene:
  p. ej. decidir *qué* fuentes mirar o re-ejecutar una etapa por calidad. Con las etapas como
  caja negra y un `finish` que el modelo puede ignorar, no compensa.

## Notas de medición

- Los tokens son los que reporta el gateway de Aixy (`usage` por llamada). Las llamadas de etapa
  del camino agéntico dieron exactamente los mismos tokens que el runner: mismas entradas,
  mismo modelo, y el gateway sirvió respuestas idénticas desde caché (de ahí también los 0,1 s del
  guion). La comparación de coste válida es la del **orquestador**, cuyo prompt crece con cada
  vuelta del bucle.
- `voice` y `publish` están desactivadas en el spike: la primera gasta créditos de ElevenLabs.
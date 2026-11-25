# Plan: agente de podcast diario con Aixy

Objetivo: cada ejecución del script levanta un agente que obtiene noticias de fuentes de confianza, las clasifica por categoría y sentimiento, escribe un guion pensado para ElevenLabs, genera el audio y lo publica. La inferencia y las herramientas externas pasan por el gateway de Aixy.

## Decisiones

| Tema | Decisión |
| --- | --- |
| Modelo | Ruta virtual de Aixy `aixy/podcast-agent`. El código no usa ningún modelo concreto. |
| Modelos de la ruta | `alibaba/deepseek-v4-pro-0813` (guion) y `alibaba/deepseek-v4.1-flash` (clasificación, JSON). |
| Búsqueda y fetch web | Exa conectado en Aixy. El código no nombra proveedor: pregunta a la discovery de Aixy qué conector puede leer la web (`AIXY_WEB_PROVIDER` solo fija uno). TinyFish sigue válido sin tocar código. |
| Voz | ElevenLabs vía MCP de Aixy. Los nombres de acción se resuelven por discovery, con los conocidos como preferencia; el conector lo fija `AIXY_TTS_PROVIDER` (por defecto `elevenlabs`). |
| Subida | Google Drive: diferido hasta que termine su configuración en Aixy. |
| Infra | Local por ahora. Quien decida la infra final es Oscar. |
| Repo | Público (`oscarcpozas/ai-daily-podcast`). Se documenta cómo montarlo con Aixy como conector. |

## Pasos del agente

1. **Fuentes.** `sources.toml`: URL, patrón de enlace y categoría esperada por fuente.
2. **Obtención.** Un agente por fuente. El agente descubre con `aixy_search_tools` las acciones web disponibles y lee la portada y los artículos con `aixy_execute_tool`. El código acota la ejecución a los proveedores que ofrecen la capacidad `web_search` y rechaza la automatización de navegador (de pago).
3. **Clasificación.** Por noticia, `aixy/podcast-agent` devuelve JSON validado con Pydantic: `category`, `sentiment`, `relevance` y motivo.
4. **Selección.** Determinista: deduplica el mismo suceso entre fuentes, equilibra categorías y elige una buena noticia.
5. **Guion.** `aixy/podcast-agent` redacta el guion con tags de audio de ElevenLabs (`[excited]`, `[pause 1s]`…), en segmentos por debajo del límite por petición.
6. **Voz.** Elige voz (o usa `ELEVENLABS_VOICE_ID`) y genera el audio por trozos, con checkpoint de los jobs ya pagados en `audio/jobs.json`.
7. **Publicación.** Une los trozos con la sintonía, masteriza el MP3 y escribe las notas del episodio. La subida a Drive se enchufa aquí.

Cada paso deja un fichero en `out/<fecha>/` para poder reanudar sin repetir llamadas caras.

## Estado de las integraciones (2026-11-25)

- **MCP de Aixy:** funcional. La discovery responde y devuelve acciones con esquema.
- **Conectores en Aixy:** ElevenLabs y TinyFish conectados. **Exa no aparece todavía** entre las conexiones del catálogo, así que hoy el pipeline lee la web con TinyFish; en cuanto Exa esté conectado, el mismo código lo usará sin cambios.
- **Ruta `aixy/podcast-agent`:** sigue devolviendo 404 con la key que hay en el entorno de la máquina (pertenece a otro proyecto). Hay que crearla en el proyecto del podcast y usar su key.
- **¿Orquestador agéntico por encima de las etapas?** Probado en `spikes/001-orchestrator-over-stages` con noticias reales: se recupera de un 503, pero gasta ~12× tokens y no llama a `finish` (agota los pasos releyendo un checkpoint). Se queda el runner determinista; los agentes viven dentro de las etapas.
- **Google Drive:** pendiente de configurar en Aixy.

## Pendiente / bloqueado

1. Conectar Exa en Aixy (o dejar TinyFish: el código acepta cualquiera).
2. Crear la ruta `aixy/podcast-agent` en el proyecto del podcast con los dos modelos DeepSeek.
3. Poner en `.env` la key del proyecto del podcast y el token MCP personal (`AIXY_API_KEY`, `AIXY_MCP_TOKEN`). Las claves antiguas (ElevenLabs, Groq, OpenRouter, Latitude) ya no se usan.
4. Configurar Google Drive en Aixy y enchufar la subida en `publish`.
5. Decidir la infra de ejecución (Oscar).

## Preguntas abiertas

- Carpeta de Drive y formato de nombre de fichero.
- Número de noticias y de categorías por episodio.
- Idioma del guion (hoy: español).
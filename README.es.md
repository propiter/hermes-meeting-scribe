# meeting-scribe

> Plugin de Hermes Agent que graba reuniones de voz de Discord, las transcribe **en tu máquina** y las convierte en notas, decisiones y tareas.

[English](README.md) · [Diseño](docs/DESIGN.md) · [Changelog](CHANGELOG.md)

`meeting-scribe` entra a un canal de voz de Discord y graba **a cada participante en una pista
separada**, así cada línea de la transcripción queda atribuida a la persona correcta. Transcribe en
local con [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Después envía el texto de la
transcripción **al LLM que ya configuraste en Hermes**, que extrae resumen, decisiones, preguntas
abiertas y tareas. Los resultados llegan a archivos Markdown, a un hilo de Discord con botones de
aprobación, al tablero Kanban de Hermes, a Linear y a tu bóveda de Obsidian.

El audio nunca sale de tu máquina. Solo se envía texto de la transcripción, y solo a tu propio
proveedor de LLM.

## Funcionalidades

- **Grabación por hablante.** Cada participante tiene su propia pista Ogg/Opus, alineada a una línea
  de tiempo común, así que no hay que adivinar quién habló (no hace falta diarización).
- **Transcripción local.** faster-whisper corre en un subproceso de baja prioridad (el gateway nunca
  se bloquea), en CPU o CUDA, con filtros contra alucinaciones.
- **Notas generadas por tu LLM de Hermes:**
  - TL;DR, resumen y temas
  - decisiones y preguntas abiertas
  - tareas con responsable, cita textual y marca de tiempo
  - fecha límite, pero solo cuando alguien la dijo en voz alta
- **Detección automática del proyecto.** El LLM elige entre tus proyectos de Hermes, tableros Kanban
  y proyectos de Linear, y recuerda qué canal corresponde a qué proyecto.
- **Destinos de entrega.** La carpeta de la reunión (siempre), un hilo de Discord con botones de
  aprobación, Kanban de Hermes, Linear y Obsidian.
- **Duradero e idempotente.** Cada reunión es una máquina de estados reanudable, guardada en SQLite:
  - al reiniciar, retoma el trabajo pendiente
  - reprocesar nunca duplica una tarea, un issue ni un mensaje
- **Entrada y salida automáticas.** El bot entra cuando se reúne gente en un canal de voz y deja de
  grabar cuando el canal se vacía.
- **Herramientas para el agente.** `meeting_search` y `meeting_get` te permiten preguntarle a Hermes
  cosas como *"¿qué decidimos sobre la migración de SMTP?"*.
- **Un archivo por reunión.** `recording.mka` contiene una mezcla reproducible más una pista por
  hablante.
- **Idiomas.** Los mensajes del bot están en inglés o español (`ui_language`). Las notas pueden
  escribirse en cualquier idioma.

## Cómo funciona

```
 Canal de voz de Discord
        │  RTP/Opus (descifrado, incluido DAVE, por el adaptador de Discord de Hermes)
        ▼
 ┌──────────────┐   tracks/<usuario>.ogg (una por hablante, alineadas al t0 de la reunión)
 │   capture    │──────────────────────────────────────────────┐
 └──────────────┘                                              ▼
                                        ┌──────────────────────────────────────┐
 Cola de trabajos SQLite (reanudable) ▶ │ transcribe  subproceso faster-whisper │
                                        └──────────────────┬───────────────────┘
                                                           ▼ transcript.jsonl / .md
                                        ┌──────────────────────────────────────┐
                                        │ analyze     LLM de Hermes (ctx.llm)   │
                                        │             map-reduce, JSON schema   │
                                        └──────────────────┬───────────────────┘
                                                           ▼ notes.json / notes.md / tasks.json
                                        ┌──────────────────────────────────────┐
                                        │ deliver     archivos · Discord ·      │
                                        │             Kanban · Linear · Obsidian│
                                        └──────────────────┬───────────────────┘
                                                           ▼
                                        ┌──────────────────────────────────────┐
                                        │ archive     recording.mka (mezcla +   │
                                        │             una pista por hablante)   │
                                        └──────────────────────────────────────┘
```

Una reunión pasa por estos estados: `recording → captured → transcribing → transcribed → analyzing →
analyzed → delivering → done`. Si una etapa falla, se reintenta sola con espera creciente. Cuando se
agotan los reintentos, `reprocess` la retoma desde esa etapa.

## Requisitos

| | |
|---|---|
| Hermes Agent | **>= 0.21**, con el gateway de Discord configurado (`hermes gateway`) |
| Python | el que use tu Hermes (3.11 – 3.14) |
| ffmpeg + ffprobe | compilado **con libopus**. Se busca en `PATH`, luego en `~/.hermes/tools/ffmpeg-*/bin` y luego en `audio_ffmpeg_path` |
| faster-whisper | `>=1.1,<2`. Hermes lo instala cuando das tu consentimiento de dependencias al instalar |
| Disco | ~20 MB por hora y hablante a 48 kbps, más el modelo de whisper (de 75 MB con `tiny` a 3 GB con `large-v3`) |
| SO | Linux, macOS |

### Modelo de whisper: velocidad vs precisión

Estas cifras salen de una ejecución real de extremo a extremo: una reunión de 114 segundos en español
con 3 hablantes (343 s de voz sumando las pistas). Corrió **solo en CPU** (AMD Ryzen 9 270, 14 hilos,
int8), con el idioma fijado en `es`. *Coincidencia* es el porcentaje de palabras que coinciden con el
guion que leyeron los hablantes.

| Modelo | Tiempo de transcripción | ≈ por minuto de reunión | Coincidencia | Uso sugerido |
|---|---|---|---|---|
| `tiny` | 7,5 s | 4 s | 94,7 % | borradores rápidos |
| `base` | 11,5 s | 6 s | 97,7 % | |
| `small` | 20,5 s | 11 s | 98,9 % | opción rápida en CPU |
| `medium` (por defecto) | 49,5 s | 26 s | 99,2 % | mejor equilibrio en CPU |
| `large-v3` | no medido | más que `medium` | | usar con GPU CUDA |

El tiempo crece con la **cantidad de voz en las pistas de los hablantes**. Quien no habla casi no
suma. Con una GPU CUDA (`transcribe_device: cuda`, `transcribe_compute_type: float16`), `large-v3`
se vuelve práctico. Fija `transcribe_language` si puedes, porque la detección automática puede
equivocarse de idioma en fragmentos cortos.

## Instalación

```bash
hermes plugins install propiter/hermes-meeting-scribe --enable
hermes meeting-scribe setup      # idioma, modelo, canal de notas, owners, auto-join, Kanban/Linear, Obsidian
hermes meeting-scribe doctor     # revisa ffmpeg/libopus, whisper, almacenamiento, LLM, Kanban, Linear, Discord
hermes gateway restart           # carga el plugin en el gateway en ejecución
```

Hermes pide tu consentimiento antes de instalar la dependencia de Python (`faster-whisper`). Si
instalas desde una shell no interactiva y el plugin queda desactivado, ejecuta
`hermes plugins enable meeting-scribe`, que instala la dependencia y activa el plugin.

`setup` también puede correr sin preguntas, por ejemplo:

```bash
hermes meeting-scribe setup --non-interactive --language es --model small --kanban-mode approve
```

### Bot de Discord

El plugin reutiliza el bot que ya ejecuta el adaptador de Discord de Hermes. No necesita un token
propio.

- **Intents.** Hermes ya activa `voice_states` (no es privilegiado). Hermes en sí necesita
  **Message Content** (privilegiado). El plugin no necesita nada más.
- **Permisos** en los canales que vayas a grabar:
  - obligatorios: **Ver canal, Conectar, Enviar mensajes, Crear hilos públicos**
  - opcional: **Gestionar apodos**, para el prefijo `[REC] ` en el apodo
  - *Hablar* no hace falta
- **Qué revisa `doctor`:** la prueba de compatibilidad de captura (`discord_compat`), las
  dependencias de voz (PyNaCl, davey, libopus), los intents y los permisos.

## Uso

### Comandos slash

El comando principal es **`/meeting`**. Sus alias **`/meet`** y **`/rec`** se cambian con
`commands_aliases`. El plugin no puede usar `/start` ni `/stop`, porque Hermes los reserva como
comandos propios.

| Subcomando | Qué hace |
|---|---|
| `start [#canal-de-voz]` (lo que se ejecuta si no pasas argumentos) | Entra a tu canal de voz, o al indicado, y empieza a grabar |
| `stop` | Detiene la grabación y empieza el procesamiento |
| `status` | Muestra el estado de grabación y procesamiento, y la cola |
| `list [n]` | Lista las reuniones recientes |
| `show <id>` | Muestra las notas de una reunión |
| `search <texto>` | Busca texto en todas las transcripciones |
| `reprocess <id> [from=transcribe\|analyze\|deliver]` | Vuelve a procesar una reunión desde una etapa |
| `link @usuario <email-o-nombre-en-linear>` | Vincula un usuario de Discord con uno de Linear |
| `project <id> <proyecto>` | Asigna o corrige el proyecto de una reunión, y enseña el mapa canal → proyecto |
| `config` | Muestra la configuración efectiva |
| `help` | Muestra la ayuda |

**Entrada y salida automáticas.** Con `autojoin_enabled` activo, el bot entra a un canal de voz
cuando `autojoin_min_humans` personas llevan `autojoin_grace_seconds` en él. Deja de grabar en
cualquiera de estos casos:

- no ha habido ningún humano en el canal durante `autoleave_grace_seconds`
- la grabación llega a `limits_max_duration_minutes`
- alguien ejecuta `/meeting stop`

Después de una parada manual, o por límite de duración, el bot no vuelve a entrar a ese canal hasta
que se haya vaciado.

### CLI

```text
hermes meeting-scribe setup [--non-interactive] [--language CODIGO] [--model NOMBRE] [--notes-channel ID]
                            [--owners ID,ID] [--autojoin | --no-autojoin]
                            [--retention multitrack|mixed|none] [--kanban-mode approve|auto|off]
                            [--linear-mode approve|auto|off] [--linear-team CLAVE] [--obsidian-vault RUTA]
hermes meeting-scribe doctor [--json]
hermes meeting-scribe status [--json]
hermes meeting-scribe list [-n 20]
hermes meeting-scribe show <id>
hermes meeting-scribe reprocess <id> [--from transcribe|analyze|deliver] [--now]
hermes meeting-scribe export <id> [--format md|json] [--out ARCHIVO]
hermes meeting-scribe config get [CLAVE]
hermes meeting-scribe config set CLAVE VALOR
```

Por defecto, `reprocess` deja el trabajo en la cola para el worker del gateway. Con `--now` se
procesa en el propio proceso del CLI. Puedes acortar el id de una reunión a cualquier prefijo que no
sea ambiguo.

### Preguntarle al agente

El toolset `meeting_scribe` ofrece dos herramientas:

- `meeting_search(query, limit)`
- `meeting_get(meeting_id, part=notes|transcript|tasks|meta)`

El skill incluido, `meeting-scribe`, le enseña al agente a responder preguntas con ellas.

## Configuración

Los ajustes viven en el `config.yaml` del perfil, bajo `plugins.entries.meeting-scribe.settings`.
Puedes cambiarlos de tres maneras:

- el formulario de ajustes del plugin en Hermes Desktop
- `hermes meeting-scribe config set CLAVE VALOR`
- `hermes meeting-scribe setup`

Los ajustes se vuelven a leer en cada operación, así que los cambios no requieren reiniciar (salvo
`commands_aliases`). Un valor inválido vuelve a su valor por defecto, y `doctor` lo muestra como
advertencia.

| Clave | Tipo | Por defecto | Descripción |
|---|---|---|---|
| `commands_aliases` | list | `[meet, rec]` | Nombres extra de comandos slash que llevan a /meeting (se leen al arrancar el gateway). |
| `autojoin_enabled` | bool | `true` | Entrar automáticamente a un canal de voz cuando se reúne gente. |
| `autojoin_min_humans` | int | `2` | Humanos necesarios en un canal de voz para entrar solo. |
| `autojoin_grace_seconds` | int | `20` | Segundos que el canal debe seguir con gente antes de entrar. |
| `autojoin_channels` | list | `[]` | Ids/nombres de canales de voz permitidos para auto-join (vacío = todos). |
| `autojoin_ignore_channels` | list | `[]` | Ids/nombres de canales de voz donde nunca se entra solo. |
| `autoleave_grace_seconds` | int | `60` | Segundos sin humanos antes de detener la grabación. |
| `limits_max_duration_minutes` | int | `240` | Límite máximo de una grabación. |
| `audio_retention` | str | `multitrack` | Audio que se conserva tras procesar: `multitrack` / `mixed` / `none`. |
| `audio_bitrate_kbps` | int | `48` | Bitrate Opus por pista de hablante. |
| `audio_ffmpeg_path` | str | `""` | Binario de ffmpeg que se usa si no se encuentra ninguno en `PATH` ni en `~/.hermes/tools`. |
| `transcribe_model` | str | `medium` | Modelo de faster-whisper (tiny/base/small/medium/large-v3...). |
| `transcribe_device` | str | `auto` | Dispositivo de inferencia: `auto` / `cpu` / `cuda`. |
| `transcribe_compute_type` | str | `auto` | Tipo de cómputo de CTranslate2: `auto` / `int8` / `int8_float16` / `float16` / `float32`. |
| `transcribe_cpu_threads` | int | `0` | Hilos de CPU para whisper (0 = núcleos menos 2). |
| `transcribe_language` | str | `auto` | Código del idioma hablado (`auto` = detectar; fíjalo si puedes). |
| `transcribe_beam_size` | int | `5` | Tamaño del beam al decodificar. |
| `analysis_language` | str | `auto` | Idioma de las notas (`auto` = idioma de la transcripción). |
| `analysis_chunk_chars` | int | `12000` | Tamaño de fragmento para el análisis map-reduce. |
| `projects_min_confidence` | float | `0.6` | Confianza mínima para asignar un proyecto automáticamente. |
| `delivery_discord_enabled` | bool | `true` | Publicar las notas en Discord. |
| `delivery_discord_channel` | str | `""` | Id del canal de notas (vacío = chat de texto del canal de voz y, si no, el canal home de Hermes). |
| `delivery_discord_thread` | bool | `true` | Publicar las notas en un hilo cuando sea posible. |
| `owners` | list | `[]` | Ids de Discord cuyas tareas pueden ir a Kanban (vacío = primera entrada de `DISCORD_ALLOWED_USERS`). |
| `kanban_mode` | str | `approve` | Envío a Kanban de las tareas de los owners: `approve` / `auto` / `off`. |
| `kanban_board` | str | `""` | Slug del tablero Kanban (vacío = tablero por defecto). |
| `linear_mode` | str | `approve` | Creación de issues en Linear: `approve` / `auto` / `off`. |
| `linear_default_team` | str | `""` | Clave/id del equipo de Linear si no se resuelve un proyecto. |
| `obsidian_vault_path` | str | `""` | Ruta de la bóveda de Obsidian (vacío = desactivado). |
| `obsidian_folder` | str | `Meetings` | Carpeta dentro de la bóveda para las notas. |
| `ui_language` | str | `en` | Idioma de los mensajes del bot: `en` / `es`. |
| `consent_announce` | bool | `true` | Anunciar la grabación en el chat del canal. |
| `consent_nickname_prefix` | str | `"[REC] "` | Prefijo del apodo mientras graba (vacío = desactivado). |

**Usar otro modelo para el análisis.** El análisis de reuniones corre como la tarea auxiliar de
Hermes `meeting_scribe`. Para enviarlo a un modelo distinto del que usas en el chat, configura
`auxiliary.meeting_scribe.*` en `config.yaml`, o elige un modelo para "Meeting Scribe" en los ajustes
de modelos auxiliares de Desktop.

## Integraciones

### Kanban de Hermes

Solo las tareas cuyo responsable es uno de los **owners** se convierten en tareas de Kanban. Los
owners son los ids de `owners`, o la primera entrada de `DISCORD_ALLOWED_USERS` si `owners` está
vacío. Cada tarea se crea en **triage**, y su cuerpo incluye la carpeta de la reunión, la cita y la
marca de tiempo.

**Modos:**

- `kanban_mode: approve` (por defecto): apruebas las tareas con ✅ o con *Aprobar todas → Kanban*
  debajo de las notas en Discord.
- `kanban_mode: auto`: las tareas se crean en cuanto termina de procesarse la reunión.

**A dónde va cada tarea:**

- Si la reunión se resuelve a un **proyecto de Hermes**, la tarea va al tablero de ese proyecto y
  lleva su `project_id`.
- Si se resuelve a un **tablero Kanban**, se usa ese tablero directamente.
- Si no, va a `kanban_board`, o al tablero por defecto si está vacío.

**Idempotente.** Cada tarea lleva una clave de idempotencia, `mtg:<reunión>:<item>`. Si un nuevo
análisis redacta una tarea de otra forma, el plugin la empareja con su id anterior, así que no
aparece ningún duplicado.

### Linear

Linear se activa cuando tienes cualquiera de estas dos cosas:

- `LINEAR_API_KEY` en el `.env` del perfil (usa la API GraphQL)
- un servidor MCP de Hermes llamado `linear`, autorizado para este plugin:

  ```yaml
  plugins:
    entries:
      meeting-scribe:
        mcp_allowlist: [linear]
  ```

Cada issue se crea en el proyecto o equipo de Linear resuelto, y si no hay ninguno, en
`linear_default_team`. El responsable sale de los vínculos hechos con `/meeting link`, o de comparar
los nombres visibles de Discord con los usuarios de Linear. La descripción incluye la cita y una
referencia a la reunión.

Los modos funcionan igual que en Kanban: en modo `approve` se usa el botón 🟣. Si Linear no está
conectado, el plugin lo omite en silencio y `doctor` lo indica.

### Obsidian

Configura `obsidian_vault_path` y cada `notes.md` se copiará a
`<bóveda>/<obsidian_folder>/<carpeta-de-la-reunión>.md`. Las notas llevan frontmatter YAML con
título, fecha, participantes, proyecto y etiquetas.

## Estructura de almacenamiento

```
<HERMES_HOME>/plugin-data/meeting-scribe/
  index.sqlite                 reuniones, hablantes, intervenciones (FTS5), trabajos, entregas,
                               tareas, vínculos, mapa canal→proyecto
  meetings/YYYY/MM/<YYYY-MM-DD_HHMM>_<canal>_<id>/
    meta.json                  metadatos de la reunión
    transcript.jsonl           una intervención por línea (t0, t1, hablante, texto, palabras, confianza)
    transcript.md              transcripción legible
    notes.json / notes.md      notas estructuradas y en Markdown (frontmatter compatible con Obsidian)
    tasks.json                 tareas y su estado de entrega
    recording.mka              retención multitrack: pista 0 = mezcla, luego una por hablante
    recording.ogg              retención mixed
```

El almacenamiento es independiente para cada perfil de Hermes. `reprocess --from transcribe` vuelve
a extraer las pistas de cada hablante de `recording.mka`.

## Privacidad y consentimiento

> **Grabar a personas sin que lo sepan puede ser ilegal donde vives tú o donde viven ellas.** Muchas
> jurisdicciones exigen el consentimiento de **todos** los participantes (leyes de consentimiento de
> todas las partes; RGPD en la UE). **Tú** eres responsable de avisar a los participantes de que se
> les está grabando, y de obtener su consentimiento antes de grabar.

El plugin te ayuda a hacerlo:

- `consent_announce` publica un aviso en el chat de texto del canal de voz al empezar a grabar.
- `consent_nickname_prefix` muestra `[REC] ` en el apodo del bot mientras graba.
- El audio se queda en tu disco. El **texto** de la transcripción se envía al proveedor de LLM que
  configuraste en Hermes, así que elige uno cuya política de datos encaje con tus reuniones.
- Para borrar una reunión, elimina su carpeta. `audio_retention: none` borra el audio cuando termina
  el procesamiento.

## Solución de problemas

| Síntoma | Solución |
|---|---|
| `/meeting start` dice que la captura no es compatible | El adaptador de Discord de Hermes cambió detalles internos de los que depende el plugin. `doctor` muestra las comprobaciones que fallan en `discord_compat`. Actualiza el plugin, o usa una versión de Hermes compatible. |
| "Ya estoy conectado a un canal de voz en este servidor" | Discord permite **una sola conexión de voz por servidor para cada bot**. Primero ejecuta `/voice leave` (chat de voz de Hermes). |
| `/meeting start` falla desde el CLI o la TUI | La captura en vivo solo funciona desde Discord, porque necesita la conexión del gateway con Discord. |
| Falta la primera palabra de un hablante nuevo | Limitación conocida de Discord/DAVE: el audio de un hablante nuevo se descarta hasta que Discord asocia su flujo, lo que tarda unos 100 ms. |
| Idioma equivocado o palabras mal transcritas | Fija `transcribe_language`, y prueba un `transcribe_model` más grande. |
| Una reunión quedó atascada o falló | `hermes meeting-scribe status` muestra la etapa y el error. Luego ejecuta `hermes meeting-scribe reprocess <id> --from <etapa>`. |
| Se encuentra ffmpeg pero sin libopus | Instala una versión de ffmpeg que incluya libopus. `doctor` muestra qué binario eligió. |

## Limitaciones

- La captura en vivo funciona solo en Discord. El procesamiento, la búsqueda y las herramientas del
  agente funcionan en cualquier superficie de Hermes.
- Cada servidor de Discord puede tener una grabación a la vez. Varios servidores pueden grabar al
  mismo tiempo.
- Se pueden perder los primeros ~100 ms de un hablante nuevo (ver Solución de problemas).
- La calidad de la transcripción depende del modelo de whisper y del micrófono de cada hablante.
- En CPU, transcribir con `medium` lleva unos 25–30 s por minuto de reunión con tres hablantes
  activos, en una CPU moderna de 16 hilos. Tenlo en cuenta en reuniones largas.
- Esta versión todavía no se ha probado en una llamada de Discord en vivo. La ruta de captura está
  cubierta por tests unitarios y de integración contra el adaptador real de Hermes (ver el
  [CHANGELOG](CHANGELOG.md)).

## Desarrollo

```bash
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # tests unitarios (no necesitan Hermes)
.venv/bin/python -m pytest -q -m slow         # faster-whisper real (descarga el modelo tiny)
scripts/test-integration.sh -q                # contra una copia real de Hermes (HERMES_SRC, HERMES_PYTHON)
hermes plugins validate .                     # manifiesto, prueba de capacidades, análisis de seguridad
.venv/bin/python scripts/gen_manifest.py      # regenera plugin.yaml tras cambiar config.py
```

El código sigue una arquitectura hexagonal: `domain/` no importa librerías de terceros. El
desarrollo se hace escribiendo primero los tests. Lee [docs/DESIGN.md](docs/DESIGN.md) antes de
cambiar comportamiento, y [CONTRIBUTING.md](CONTRIBUTING.md) antes de abrir un pull request.

## Créditos

El diseño está inspirado en [Parley](https://github.com/SakethKanchi/parley) de Saketh Kanchi
(licencia ISC), que también graba Discord por hablante y alimenta Whisper local y un resumen con LLM.
No se copió código: `meeting-scribe` es un plugin de Hermes escrito desde cero.

## Licencia

[MIT](LICENSE) © Pedro Rodriguez (propiter)

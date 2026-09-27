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
- **Un proyecto por tarea.** Cada tarea tiene su propio proyecto. El LLM elige entre tus proyectos de
  Hermes, tableros Kanban, proyectos de Linear y los propios canales y categorías del servidor de
  Discord, y la coincidencia aproximada absorbe los errores de transcripción. Una corrección con 📁
  se recuerda.
- **Las tareas donde vive el trabajo.** Cada tarea se publica en un hilo del canal de su proyecto,
  con sus botones justo debajo. El chat de la reunión recibe el resumen y un índice compacto de
  tareas. Cada responsable recibe un DM con sus tareas, y **📋 Mis tareas** abre un panel privado.
- **Destinos de entrega.** La carpeta de la reunión (siempre), Discord, Kanban de Hermes, Linear y
  Obsidian.
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
hermes meeting-scribe google connect|status|sync|disconnect   # ver "Google Meet"
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
| `delivery_project_threads` | bool | `true` | Publicar cada tarea en un hilo del canal de su proyecto. |
| `delivery_dm_assignees` | bool | `true` | Enviar por DM a cada responsable sus tareas con botones. |
| `delivery_discord_transcript` | bool | `true` | Adjuntar la transcripción completa (`transcript-<fecha>-<slug>.md`) a las notas en Discord. |
| `google_meet_enabled` | bool | `false` | Importar transcripciones de Google Meet (requiere `hermes meeting-scribe google connect`). |
| `google_meet_poll_minutes` | int | `5` | Minutos entre sondeos de Google Meet (2–1440). |
| `google_meet_discord_channel` | str | `""` | Id del canal de texto de Discord para las notas de Meet (vacío = `delivery_discord_channel` y, si no, home). |
| `project_channels` | list | `[]` | Mapa explícito proyecto → canal, entradas como `Nombre del proyecto=id_canal`. |
| `project_match_min_score` | float | `0.8` | Puntuación difusa mínima para enviar una tarea a un canal por nombre. |
| `channel_name_ignore_prefixes` | list | `[]` | Palabras decorativas iniciales que se ignoran en los nombres de canal. |
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

## Tareas en Discord

Cuando termina de procesarse una reunión, el plugin publica:

- **En el chat de la reunión:** el resumen (TL;DR, decisiones, preguntas abiertas) y después un
  **índice de tareas**: cuántas hay por proyecto, con un enlace al hilo que las contiene, cuántas
  por persona, y un único botón **📋 Mis tareas**.
- **En el canal de cada proyecto:** un hilo de la reunión con **un mensaje por tarea** y los botones
  de esa tarea justo debajo: ✅ Kanban · 🟣 Linear · ❌ Descartar · 📁 Mover. Cuando se resuelve una
  tarea, su mensaje muestra el resultado (``✅ Kanban `t_42` ``, `🟣 Linear ENG-7`, `❌ Descartada`) y
  pierde sus botones. Las demás tareas no cambian.
- **A cada responsable:** un DM con sus tareas y los mismos botones (`delivery_dm_assignees`, activo
  por defecto). Si alguien tiene los DMs cerrados, queda anotado en el índice y nada más falla.

**📋 Mis tareas** abre un panel *efímero* que solo ve quien hizo clic. Muestra sus tareas, cada una
con sus botones, 4 por página. Los owners tienen además un botón 👥 para ver todas las tareas.

**Quién puede pulsar qué.** Los botones de un mensaje público los ve todo el mundo, así que cada clic
se comprueba contra la tarea:

- El **responsable** de la tarea y los **owners** pueden actuar sobre ella. Cualquier otra persona
  recibe en privado "Esta tarea pertenece a @X" y no pasa nada.
- Una tarea **sin responsable** solo la pueden gestionar los owners.
- **✅ Kanban** es el tablero personal de los owners, así que solo aparece en tareas asignadas a un
  owner. Las tareas de otras personas van a Linear.

**Cómo encuentra una tarea su canal.** Gana la primera regla que coincida:

1. Una entrada explícita de `project_channels`, por ejemplo `["Website=123456789012345678"]`.
2. Un mapeo aprendido de una corrección con 📁.
3. La mejor coincidencia aproximada entre los canales de texto y las categorías del servidor. Una
   categoría se resuelve a su primer canal donde el bot puede publicar.
4. Si no, el chat de la reunión.

Los nombres de canal se comparan después de quitar la decoración: emojis, símbolos, separadores de
dibujo como `┃` o `・` y corchetes como `『』` o `【】`. Así, `『🚀』website`, `🟢┃website` y `【Website】`
se leen como `website`. La comparación tolera errores de transcripción: *Nebulla* encuentra
`#nebula`. Las palabras cortas o comunes nunca coinciden por sí solas; una coincidencia de una sola
palabra necesita al menos 4 letras.

Si tu servidor antepone *palabras* decorativas a los nombres de canal, ponlas en
`channel_name_ignore_prefixes`. Por ejemplo, `["team", "proj"]` hace que `team-website` y
`proj-website` se lean como `website`.

Cuando la coincidencia es débil, o dos canales puntúan casi igual, la tarea se publica igualmente en
el canal más probable, marcada con **⚠️ proyecto no seguro — confirma con 📁**. Pulsar 📁 mueve la
tarea: se vuelve a publicar en el hilo del canal correcto y se borra el mensaje anterior. Cuando un
owner mueve una tarea, la corrección además se recuerda para próximas reuniones; si la mueve su
responsable, solo afecta a esa tarea. Si el bot no tiene Ver canal, Enviar mensajes o Crear
hilos públicos en el canal elegido, la tarea se queda en el chat de la reunión y el índice explica
por qué.

Reprocesar una reunión edita estos mensajes en su sitio en vez de publicar mensajes nuevos.

## Google Meet

meeting-scribe también puede importar **transcripciones que Google Meet ya generó** y pasarlas por
el mismo análisis y entrega que las reuniones de Discord (resumen, decisiones, tareas por proyecto,
Kanban/Linear, hilos de Discord). No se descarga audio y ningún bot entra a la llamada.

### Requisitos

- Una edición de Google Workspace con transcripción en Meet (por ejemplo Business Standard/Plus,
  Enterprise, Education Plus, Workspace Individual). El administrador debe permitir transcripciones.
- La transcripción debe estar **activada** en la reunión (Actividades → Transcripciones) o activarse
  automáticamente desde el evento de Calendar.
- Solo se importan las reuniones **que organiza la cuenta conectada**: la API de Meet lista los
  registros de conferencia filtrados por organizador. Las reuniones a las que solo asististe
  (organizadas por un compañero u otra organización) no son visibles; quien las organiza debe
  conectar su propia cuenta.
- Google borra las entradas de la transcripción **30 días** después de terminar la reunión;
  importa antes.

### Crea tu propia app OAuth (una vez)

Cada instalación usa **su propio** cliente OAuth de Google Cloud; el plugin no trae ninguno.

1. Abre <https://console.cloud.google.com/> y crea (o elige) un proyecto.
2. **APIs y servicios → Biblioteca**: habilita **Google Meet REST API**.
3. **Google Auth Platform → Marca / Público** (pantalla de consentimiento): tipo de usuario
   **Interno** (solo Workspace; no requiere revisión de Google). Completa nombre y correo de soporte.
4. **Acceso a datos**: añade el scope `https://www.googleapis.com/auth/meetings.space.readonly`.
5. **Clientes → Crear cliente**: tipo de aplicación **App de escritorio**. Descarga el JSON.

### Conectar y usar

```text
hermes meeting-scribe google connect --client-secret ~/Descargas/client_secret_XXXX.json [--no-browser]
hermes meeting-scribe config set google_meet_enabled true
hermes meeting-scribe config set google_meet_discord_channel <id del canal de texto>   # opcional
# reinicia el gateway para que arranque su sondeo
hermes meeting-scribe google status [--json]
hermes meeting-scribe google sync [--since 2026-09-01T00:00:00Z | --days N] [--dry-run] [--json]
hermes meeting-scribe google disconnect
```

- `connect` copia el JSON del cliente a `<HERMES_HOME>/plugin-data/meeting-scribe/google/client.json`
  y guarda el token en `token.json` al lado (ambos con permisos 0600). Abre el navegador y escucha
  una sola vez en `http://127.0.0.1:<puerto libre>`. En una máquina remota/SSH usa `--no-browser`:
  abre la URL impresa en cualquier equipo, acepta y pega la URL completa a la que te redirigió (la
  página puede no cargar; es normal) o solo el código.
- El gateway sondea cada `google_meet_poll_minutes` (5 por defecto). Solo un proceso sondea (un
  lease en el SQLite del plugin). Solo se importan automáticamente las conferencias que **terminan
  después de conectar**; usa `google sync --days N` para importar historia de forma explícita
  (máximo 30 días).
- Una conferencia se importa una sola vez (única por el nombre de su registro en Meet), aunque haya
  reinicios o dos procesos. Si la transcripción aún se está generando (`ENDED`) se reintenta en el
  siguiente sondeo.
- Las notas van a `google_meet_discord_channel`; si está vacío, a `delivery_discord_channel`; si no,
  al canal home del gateway. Sin ninguno, la reunión se procesa igual (CLI, herramientas del agente,
  archivos, Kanban) y la entrega en Discord se omite.
- Los participantes de Meet no son usuarios de Discord: las tareas muestran su nombre, sin menciones
  ni DMs.
- `invalid_grant` (acceso revocado o caducado) aparece como "desconectado" en `google status` y
  `doctor`; vuelve a ejecutar `connect`.

**Qué sale de tu máquina:** el plugin solo llama a `meet.googleapis.com` (scope de solo lectura
`meetings.space.readonly`: registros de conferencia, participantes, transcripciones y entradas) y a
`oauth2.googleapis.com` (tokens, revocación). Sin acceso a Drive ni al perfil del usuario. Después,
el texto de la transcripción sigue el camino normal: tu proveedor LLM de Hermes y Discord (ver el
adjunto de transcripción abajo).

## Transcripción completa en Discord

Con `delivery_discord_transcript` (**activado** por defecto) cada reunión — de Discord o de Google
Meet — recibe su transcripción completa adjunta como `transcript-<fecha>-<slug>.md`
(`[mm:ss] Nombre: texto`) justo después del resumen en el chat de la reunión. Solo la etapa de
entrega la adjunta, una vez: los reintentos no la repiten y los botones nunca adjuntan nada; un
reprocesado que cambia las líneas de la transcripción la reemplaza (un título nuevo, no). Las
reuniones cuyo resumen se publicó sin el adjunto (entregadas por una versión anterior o con el ajuste
desactivado) no lo reciben después. Los archivos de más de 8 MB se dividen en partes numeradas. Sin el
permiso **Adjuntar archivos** se publica un aviso de una línea y la entrega continúa. Desactívalo con
`hermes meeting-scribe config set delivery_discord_transcript false`.

## Integraciones

### Kanban de Hermes

Solo las tareas cuyo responsable es uno de los **owners** se convierten en tareas de Kanban. Los
owners son los ids de `owners`, o la primera entrada de `DISCORD_ALLOWED_USERS` si `owners` está
vacío. Cada tarea se crea en **triage**, y su cuerpo incluye la carpeta de la reunión, la cita y la
marca de tiempo.

**Modos:**

- `kanban_mode: approve` (por defecto): apruebas cada tarea con su botón ✅ en Discord.
- `kanban_mode: auto`: las tareas se crean en cuanto termina de procesarse la reunión.

**A dónde va cada tarea:**

- Si la tarea (o, si no, la reunión) se resuelve a un **proyecto de Hermes**, la tarea va al tablero de ese proyecto y
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
- La importación de Google Meet (opcional) lee transcripciones con tu propio cliente OAuth y el scope
  de solo lectura `meetings.space.readonly`; ver [Google Meet](#google-meet).
- La transcripción completa se adjunta a las notas en Discord por defecto
  (`delivery_discord_transcript`); quien pueda leer el canal de notas puede leerla.
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

- La importación de Google Meet requiere la transcripción propia de Meet (Workspace) y solo ve las
  reuniones que la cuenta conectada organizó o a las que asistió; aún no se ha probado contra la API
  real de Google.
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

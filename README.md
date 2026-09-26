# Programador

Motor MCP de triaje y extracción documental sobre el buzón **iCloud**, pensado
para operar varias entidades a la vez (Rambaid Ibérica, ECME-FOREGO, FOREGO
INTERNATIONAL, S@ND) desde un único correo personal.

No es un cliente de correo ni un clon de Mailopoly. Es la capa que falta entre
un buzón IMAP y un asistente: enruta cada correo a la entidad que le
corresponde, le pone etiquetas de tipo documental (factura, BL, aduana,
contrato…) y guarda de forma estructurada lo que se extrae de él.

## La apuesta arquitectónica

El reparto de trabajo es deliberado y es lo que hace este proyecto viable:

- **El servidor hace lo determinista y barato.** IMAP, SQLite, reglas de
  enrutado, búsqueda de texto completo. Sin IA, sin llamadas a ninguna API, sin
  coste por token. Una decisión de enrutado siempre se puede explicar
  (`to_addr=rambaid@…`), lo que importa cuando lo mal clasificado es una factura
  de aduanas.
- **Claude hace lo semántico.** Leer el PDF de una proforma y sacar importe,
  vencimiento e incoterm. El modelo ya está al otro lado de la tubería MCP: no
  hace falta pagar inferencia aparte ni mantener prompts en el repositorio.
  Lo que extrae lo devuelve por `registrar_extraccion` y queda persistido.

Consecuencia práctica: el coste recurrente de este sistema es **cero**, y la
calidad de clasificación semántica es la del modelo que uses, no la de un
clasificador propio que habría que entrenar y mantener.

## Requisitos

- Python 3.11 o superior.
- Una **contraseña específica de app** de Apple (`account.apple.com` →
  Inicio de sesión y seguridad → Contraseñas específicas de app). La contraseña
  normal del Apple ID **no funciona** con IMAP.
- Una máquina con salida a `imap.mail.me.com:993` y `smtp.mail.me.com:587`.

## Instalación

```bash
git clone https://github.com/lestersv-85/Programador.git
cd Programador
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

## Configuración

```bash
cp .env.example .env                       # credenciales y rutas
cp config/entities.example.yaml config/entities.yaml
```

Edita `.env` con tu correo y la contraseña específica de app. Edita
`config/entities.yaml` y sustituye los `CAMBIAME-…` por tus alias y los dominios
reales de tus contrapartes.

Los dos ficheros están en `.gitignore`: ni las credenciales ni tu mapa de
proveedores y clientes salen del ordenador.

**El criterio de enrutado más fiable es el alias de destino.** Si ya usas (o
puedes empezar a usar) una dirección distinta por empresa, esas cuatro reglas de
`to_addr` resuelven la mayor parte del enrutado solas y sin ambigüedad. Las
reglas por dominio de remitente son el segundo mejor criterio; las de asunto,
el último recurso.

## Comprobar que todo funciona antes de sincronizar

```bash
set -a && . ./.env && set +a
./.venv/bin/programador-doctor
```

Recorre la cadena entera en orden —entorno, configuración, base de datos, puerto
IMAP, login IMAP, carpetas, puerto SMTP, login SMTP— y **no envía ningún correo**.
Cuando algo falla dice qué hacer en vez de escupir una excepción de `imaplib`:
distingue un puerto bloqueado por la red de una contraseña de app caducada, y si
`PROGRAMADOR_FOLDERS` nombra una carpeta que no existe, te lista las reales.

Con `--sin-red` comprueba solo lo local, sin tocar iCloud.

## Primera sincronización

```bash
set -a && . ./.env && set +a
./.venv/bin/programador-sync
```

La primera pasada trae `PROGRAMADOR_INITIAL_DAYS` días de histórico (180 por
defecto). Las siguientes son incrementales: solo piden los UID posteriores al
último visto, así que tardan segundos. Cada pasada refresca además los flags de
los mensajes recientes ya indexados —sin volver a descargar cuerpos—, así que si
marcas algo como leído en el iPhone, el índice se entera.

Para sincronizar cada 15 minutos en macOS, hay un ejemplo de `launchd` en
`scripts/com.lester.programador.sync.plist`.

## Registrar el servidor en Claude Code

```bash
claude mcp add programador -- /ruta/absoluta/al/repo/.venv/bin/programador-mcp
```

El servidor lee su configuración del entorno, así que las variables de `.env`
deben estar disponibles para el proceso (o pásalas con `--env` al registrarlo).

## Herramientas MCP

| Herramienta | Para qué |
|---|---|
| `estado` | Configuración (sin secretos), volumen indexado, estado de sincronización |
| `diagnostico` | La misma cadena de comprobaciones que `programador-doctor`, desde la conversación |
| `sincronizar` | Trae correo nuevo del buzón |
| `triaje` | Qué ha entrado en N días, por entidad y tipo documental |
| `listar` | Filtra por entidad, tipo documental, antigüedad, no leídos, adjuntos |
| `buscar` | Texto completo sobre asunto, cuerpo, remitente y nombres de adjuntos |
| `leer` | Un correo completo |
| `descargar_adjuntos` | Guarda los adjuntos en disco para poder leerlos |
| `registrar_extraccion` | Persiste los campos estructurados extraídos de un correo |
| `listar_extracciones` / `marcar_extraccion` | Consulta y revisión de lo extraído |
| `carpetas` / `mover` / `marcar` | Actúa sobre el buzón real de iCloud |
| `enviar` | Envía por SMTP. **Exige `confirmar=True`**: sin ese flag solo previsualiza |

Flujo típico en conversación:

> «Sincroniza y dame el triaje de la semana» → «Léeme las tres de Rambaid con
> adjunto» → «Extrae importe, vencimiento e incoterm de esa proforma y
> regístralo» → «¿Qué facturas de FOREGO tengo pendientes de confirmar?»

## Seguridad

- La contraseña específica de app solo llega por variable de entorno. Nunca se
  escribe en el repositorio ni la devuelve ninguna herramienta MCP
  (`estado` la enmascara como `<set>`).
- `enviar` no manda nada sin `confirmar=True` explícito. Una llamada accidental
  devuelve una previsualización, no un correo enviado.
- Los nombres de carpeta se codifican en modified UTF-7 y se escapan antes de
  entrar en un comando IMAP.
- El texto libre de búsqueda se tokeniza antes de llegar a FTS5, así que un
  `"` o un `*` en la consulta no cambian lo que se busca ni revientan el índice.
- Todo el correo indexado vive en un SQLite local. No sale de tu máquina.

## Que el resumen matutino lea iCloud directo: el lector vive en Supabase

Las sesiones de Claude en la nube —donde corre el resumen matutino— **no pueden
hablar IMAP**. Se comprobó con un control, no con una suposición: el mismo
`ClientHello` por el mismo túnel del proxy recibe un `ServerHello` real de
`www.icloud.com:443` y cero bytes seguidos de un reset de `imap.mail.me.com:993`.
El relé de egreso solo transporta HTTPS. No es una casilla de política: es la
arquitectura.

Lo que sí puede hablar IMAP es una **Edge Function de Supabase**: el 26 sep 2026
una sonda desplegada en el proyecto abrió TLS crudo contra
`imap.mail.me.com:993` y leyó el saludo del servidor en 328 ms. Así que el
lector se movió allí y el Mac dejó de ser necesario:

```
Supabase (pg_cron, cada 10 min)                         Nube (08:00 La Habana)
programador-sync (Edge Function) ──IMAP──> iCloud        Resumen matutino
        └── publica con programador_publicar ──> tablas <── lee con su conector
                                       (programador_mensajes,
                                        programador_sync_log)
```

- `supabase/functions/programador-sync/index.ts`: cliente IMAP mínimo sobre
  `Deno.connectTls` (LOGIN, SELECT, UID SEARCH, UID FETCH con `BODY.PEEK`,
  BODYSTRUCTURE para elegir la parte de texto y listar adjuntos), decodificación
  RFC 2047 / quoted-printable / base64 / HTML→texto, enrutado por entidad con
  las reglas de `public.programador_reglas` y pistas documentales ES/EN.
  Sincroniza `INBOX` y `Sent Messages`, incremental por `(UIDVALIDITY, UID)`
  (estado en `public.programador_sync_state`), reconcilia flags y borra de las
  tablas lo que desapareció del buzón, y deja una fila en `programador_sync_log`
  en cada pasada, con el error si lo hubo.
- `supabase/migrations/20260926150000_programador_sync_en_supabase.sql`:
  extensiones `pg_cron` y `pg_net`, tablas de estado y reglas, la función
  `programador_secreto` y el `cron.schedule` que llama a la Edge Function cada
  10 minutos.

### Modelo de seguridad

- La **contraseña de app de Apple** y el **token del cron** viven en el Vault
  de Supabase. La Edge Function los lee con `programador_secreto(nombre)`,
  una función `SECURITY DEFINER` que solo puede ejecutar `service_role`.
  No hay ningún secreto en el código ni en variables de entorno propias.
- La Edge Function tiene `verify_jwt` apagado porque `pg_net` no manda JWT;
  a cambio exige la cabecera `x-programador-token` igual al secreto del Vault.
  Sin ella responde 401.
- Escribe con la clave de servicio que Supabase inyecta en la función, a
  través de la misma RPC `programador_publicar` que ya estaba probada en vivo.
  RLS sigue activo en todas las tablas sin políticas para `anon`.
- El brief lee con el conector de Supabase (solo `select`), que nunca sale de
  Anthropic.

### Lo que queda en tus manos (una vez)

La contraseña de app de Apple ya está en el Vault (`icloud_app_password`,
26 sep 2026) y el cron publica cada 10 minutos: a la fecha, 24/24 mensajes de
los últimos 3 días con cuerpo legible y enrutados. Queda **un solo clic**:

1. Rutinas → Resumen matutino → Conectores → añadir **Supabase**. Las rutinas
   creadas por API no admiten conectores; el prompt ya está cargado
   (`docs/prompt-resumen-matutino.md`) y, sin el conector, sigue usando
   Mailopoly de forma provisional.

Cuando el brief lleve unos días leyendo de Supabase, revoca en
`account.apple.com` la contraseña de app que usa Mailopoly: es la única copia de
tu correo fuera de iCloud que sigue viva.

Cómo comprobar que sigue vivo sin abrir nada: la última fila de
`programador_sync_log` con `error is null` tiene `synced_at` de hace menos de
10 minutos. Si `error` trae «LOGIN en iCloud», Apple revocó la contraseña y hay
que crear otra (`select vault.update_secret(...)`).

### El camino del Mac (opcional, ya no hace falta)

`programador-sync` en el Mac sigue existiendo y sigue publicando con la clave
publicable si `PROGRAMADOR_SUPABASE_KEY` está en `.env`
(`scripts/com.lester.programador.sync.plist` para launchd). Es útil si algún día
quieres el índice local con FTS5 y las 15 herramientas MCP, pero el brief ya no
depende de él.

## Qué NO hace todavía

Honestidad sobre los límites de esta v1:

- **Una sola cuenta.** El esquema ya guarda `account` en cada mensaje, pero el
  ciclo de sincronización recorre un único buzón.
- **No lee el contenido de los PDF.** Guarda los adjuntos y su metadato; la
  lectura la hace Claude sobre el fichero descargado.
- **No hay IDLE ni push.** La actualización es por sondeo (`programador-sync`).
- **Mover un correo lo saca del índice.** Su UID de origen deja de existir, así
  que la fila se retira para que nadie actúe sobre una posición que ya no es
  cierta. Si la carpeta de destino está en `PROGRAMADOR_FOLDERS`, vuelve a
  indexarse en la siguiente sincronización; si no, deja de estar buscable. La
  herramienta lo avisa en su respuesta.
- **Nunca se ha conectado a iCloud.** Esto importa y conviene decirlo sin
  adornos. El código se escribió en un entorno sin salida a los puertos 993 y
  587. Lo que sí está probado es el protocolo: `tests/fake_imap.py` es un
  servidor IMAP4rev1 que habla por un socket de verdad, y los tests de
  integración corren el cliente auténtico sobre `imaplib` auténtico contra él
  —literales `{n}` en `UID FETCH`, formato de `LIST` y `STATUS`, el eco del
  comodín en `UID SEARCH`, `MOVE` y su alternativa `COPY+EXPUNGE`, carpetas con
  acentos. Ese servidor ya destapó un fallo real que los dobles de Python no
  podían ver: `LIST` decodificaba los nombres a ASCII y destruía cualquier
  carpeta con tilde. Aun así, iCloud tendrá sus propias rarezas: ejecuta
  `programador-doctor` antes que nada.

## Desarrollo

```bash
./.venv/bin/python -m pytest -q     # 113 tests, ~9 segundos
```

La suite tiene dos capas. Los tests unitarios usan dobles de Python y cubren el
enrutado, el parseo MIME, el índice y la lógica de sincronización. Los de
integración (`test_integration_imap.py`, `test_doctor.py`) levantan el servidor
IMAP falso y ejercitan el cliente real sobre el protocolo real. Cuando toques
`imap_client.py`, los que importan son los segundos.

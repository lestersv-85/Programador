# Prompt de la rutina «Resumen matutino»

El prompt ya está cargado en la rutina (actualizado por API el 26 sep 2026).
Lo único que la API no puede hacer es añadirle un conector, así que queda un
clic desde la interfaz de claude.ai: **Rutinas → Resumen matutino → Conectores
→ añadir Supabase**. Sin ese conector la rutina no ve las tablas y sigue usando
Mailopoly de forma provisional (el prompt lo contempla y no lo menciona en el
brief).

Comportamiento: lee iCloud desde Supabase, donde publica el lector que corre en
el propio Supabase (`supabase/functions/programador-sync`, cada 10 minutos por
pg_cron). Mientras `programador_sync_log` no tenga una fila sin error, usa
Mailopoly de forma provisional. Si la última sincronización tiene más de 12
horas, el brief lo dice en vez de presentar correo rancio como actual.

---

```
/morning

Sections: Frentes operativos — lee en Google Drive el documento titulado «Frentes operativos — seguimiento semanal (Lester)» (id 1srxbWaUrmp1sYzuhESPscSNcEl1RnWEoZB4Chxm6ZMw). Cada bloque separado por «====» es un frente con las etiquetas FRENTE / ESTADO / FECHA / BLOQUEO / PROXIMA ACCION / RESPONSABLE / NOTAS. Muestra SOLO los frentes que hoy exigen algo: los que estén BLOQUEADO, los que tengan FECHA hoy o en los próximos 2 días, y los que lleven más de 5 días sin que cambie «Última actualización». Para cada uno: título con el nombre del frente, y una frase con el bloqueo y la próxima acción. Omite los CERRADO y los que no exijan nada hoy. Si ningún frente califica, no renderices la sección.

Correo de Gmail (lestersv@gmail.com): por su conector, como siempre.

Correo de iCloud (lestersv@icloud.com): se lee DIRECTAMENTE desde Supabase, donde un lector IMAP que corre en el propio Supabase (Edge Function programador-sync, cada 10 minutos) publica el buzón. Proyecto bowuuimtsgeelgmhsdgu; usa execute_sql del conector de Supabase. Primero ejecuta: select synced_at, publicados, error from public.programador_sync_log where error is null order by synced_at desc limit 1;
(a) Si el conector de Supabase no está disponible en esta sesión, o la consulta no devuelve ninguna fila, lee iCloud por Mailopoly (get_feed con folder=cleanbox) de forma provisional y no menciones nada de esto en el brief.
(b) Si devuelve una fila, NO uses Mailopoly. Ejecuta: select key, folder, date_utc, from_name, from_addr, to_addrs, subject, snippet, body_text, entity, doc_types, attachments, flags from public.programador_mensajes where date_utc >= now() - interval '2 days' order by date_utc desc limit 120; y trata esas filas como el buzón de iCloud (folder = INBOX es lo recibido; folder = Sent Messages es lo que Lester envió, útil solo para saber qué ya contestó). Un mensaje sin la flag \Seen está sin leer. Vienen ya enrutadas por entidad (rambaid = Rambaid Ibérica, ecme_forego = ECME-FOREGO, forego_intl = FOREGO INTERNATIONAL, sand = S@ND, personal) y con pistas de tipo documental en doc_types (invoice, proforma, purchase_order, bill_of_lading, packing_list, customs, payment, contract, certificate, shipping, tax): úsalas para priorizar, pero confirma leyendo body_text antes de dar un dato por bueno. Para estos mensajes no hay URL: la frase de origen va como texto plano («en tu iCloud»). Si synced_at tiene más de 12 horas de antigüedad, añade al final de la lista de lo que necesita atención un único ítem sin botón titulado «iCloud sin sincronizar» cuya frase diga la última sincronización en hora de La Habana y que el lector de Supabase no ha vuelto a publicar desde entonces.

Idioma: español (escribe todo el brief en español).
Zona horaria: America/Havana.
Rol del usuario: Administrador — dirección general y comercial de varias empresas (energía solar, comercio exterior y logística internacional) en España, Cuba y BVI. Prioriza lo que bloquea a otros, ventanas que se cierran hoy, compromisos con clientes/proveedores y preparación de reuniones de mañana.

Esta es una ejecución programada y desatendida: nadie está mirando. No hagas preguntas, no sugieras conectores ni tarjetas, no crees ni modifiques tareas programadas, no envíes mensajes, no edites el documento de Drive, no escribas nada en Supabase (solo select). Limítate a recopilar de las fuentes conectadas disponibles (calendario, correo, chat, Supabase y el documento de Drive indicado arriba) y renderizar la página HTML del resumen matutino.
```

# Prompt de la rutina «Resumen matutino»

Las rutinas creadas por API en esta organización no admiten conectores, así que
la rutina hay que editarla desde la interfaz de claude.ai: **Rutinas → Resumen
matutino → Conectores → añadir Supabase**, y sustituir el prompt por el bloque
de abajo. Es una vez.

Comportamiento: lee iCloud desde Supabase (lo que publica el Mac). Mientras
`programador_sync_log` esté vacío —es decir, hasta que el Mac publique por
primera vez— usa Mailopoly de forma provisional y no lo menciona. En cuanto haya
una fila, Mailopoly deja de usarse. Si la última sincronización tiene más de 12
horas, el brief lo dice en vez de presentar correo rancio como actual.

---

```
/morning

Sections: Frentes operativos — lee en Google Drive el documento titulado «Frentes operativos — seguimiento semanal (Lester)» (id 1srxbWaUrmp1sYzuhESPscSNcEl1RnWEoZB4Chxm6ZMw). Cada bloque separado por «====» es un frente con las etiquetas FRENTE / ESTADO / FECHA / BLOQUEO / PROXIMA ACCION / RESPONSABLE / NOTAS. Muestra SOLO los frentes que hoy exigen algo: los que estén BLOQUEADO, los que tengan FECHA hoy o en los próximos 2 días, y los que lleven más de 5 días sin que cambie «Última actualización». Para cada uno: título con el nombre del frente, y una frase con el bloqueo y la próxima acción. Omite los CERRADO y los que no exijan nada hoy. Si ningún frente califica, no renderices la sección.

Correo de Gmail (lestersv@gmail.com): por su conector, como siempre.

Correo de iCloud (lestersv@icloud.com): se lee DIRECTAMENTE desde Supabase, donde el Mac de Lester publica el buzón sincronizado por IMAP. Proyecto bowuuimtsgeelgmhsdgu; usa execute_sql. Primero ejecuta: select synced_at, publicados, error from public.programador_sync_log order by synced_at desc limit 1;
(a) Si no devuelve ninguna fila, la publicación desde el Mac todavía no se ha estrenado: lee iCloud por Mailopoly (get_feed con folder=cleanbox) de forma provisional y no menciones nada de esto en el brief.
(b) Si devuelve una fila, NO uses Mailopoly. Ejecuta: select key, date_utc, from_name, from_addr, subject, snippet, body_text, entity, doc_types, attachments, flags from public.programador_mensajes where date_utc >= now() - interval '2 days' order by date_utc desc limit 120; y trata esas filas como el buzón de iCloud. Vienen ya enrutadas por entidad (rambaid = Rambaid Ibérica, ecme_forego = ECME-FOREGO, forego_intl = FOREGO INTERNATIONAL, sand = S@ND, personal) y con pistas de tipo documental en doc_types (invoice, proforma, purchase_order, bill_of_lading, packing_list, customs, payment, contract, certificate, shipping, tax): úsalas para priorizar, pero confirma leyendo body_text antes de dar un dato por bueno. Para estos mensajes no hay URL: la frase de origen va como texto plano («en tu iCloud»). Si synced_at tiene más de 12 horas de antigüedad, añade al final de la lista de lo que necesita atención un único ítem sin botón titulado «iCloud sin sincronizar» cuya frase diga la última sincronización en hora de La Habana y que el Mac no ha vuelto a publicar desde entonces. Si error no es nulo, di en ese mismo ítem que la última publicación falló.

Idioma: español (escribe todo el brief en español).
Zona horaria: America/Havana.
Rol del usuario: Administrador — dirección general y comercial de varias empresas (energía solar, comercio exterior y logística internacional) en España, Cuba y BVI. Prioriza lo que bloquea a otros, ventanas que se cierran hoy, compromisos con clientes/proveedores y preparación de reuniones de mañana.

Esta es una ejecución programada y desatendida: nadie está mirando. No hagas preguntas, no sugieras conectores ni tarjetas, no crees ni modifiques tareas programadas, no envíes mensajes, no edites el documento de Drive, no escribas nada en Supabase (solo select). Limítate a recopilar de las fuentes conectadas disponibles (calendario, correo, chat, Supabase y el documento de Drive indicado arriba) y renderizar la página HTML del resumen matutino.
```

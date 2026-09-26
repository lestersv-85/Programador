-- Reglas de enrutado observadas en el primer buzon real (26 sep 2026).
-- Antes de esto todo caia en `personal` porque las reglas semilla solo
-- miraban asunto y dominios de la administracion.
--
-- Nota: la Edge Function ahora tambien busca `body_contains` en los
-- nombres de los adjuntos, asi que "PREFACTURA FOREGO ....pdf" enruta.

insert into public.programador_reglas (entity, priority, from_domain, from_addr, to_addr, subject_contains, body_contains)
values
  ('rambaid',     90, array['omesl.es','rambaidiberica.com'], array[]::text[],
                      array['lester@rambaidiberica.com','info@rambaidiberica.com'], array[]::text[], array[]::text[]),
  ('rambaid',     40, array[]::text[], array[]::text[], array[]::text[], array[]::text[], array['rambaid']),
  ('forego_intl', 40, array[]::text[], array[]::text[], array[]::text[], array[]::text[], array['forego']),
  ('ecme_forego', 40, array[]::text[], array[]::text[], array[]::text[], array[]::text[], array['ecme-forego','aei ecme']),
  ('sand',        40, array[]::text[], array[]::text[], array[]::text[], array[]::text[], array['s@nd srl','sand srl']);

-- El lector IMAP de iCloud pasa a vivir en Supabase (Edge Function
-- programador-sync) disparado por pg_cron + pg_net cada 10 minutos.
-- Aplicada en el proyecto bowuuimtsgeelgmhsdgu el 26 sep 2026.

create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;
grant usage on schema cron to postgres;
grant all privileges on all tables in schema cron to postgres;

-- Estado incremental por carpeta (UIDVALIDITY + ultimo UID visto).
create table if not exists public.programador_sync_state (
  account text not null,
  folder text not null,
  uidvalidity bigint not null,
  last_uid bigint not null default 0,
  updated_at timestamptz not null default now(),
  primary key (account, folder)
);
alter table public.programador_sync_state enable row level security;
revoke all on public.programador_sync_state from anon, authenticated;

-- Reglas de enrutado por entidad (mismas semanticas que config/entities.yaml).
create table if not exists public.programador_reglas (
  id bigserial primary key,
  entity text not null,
  priority integer not null default 50,
  from_domain text[] not null default '{}',
  from_addr text[] not null default '{}',
  to_addr text[] not null default '{}',
  subject_contains text[] not null default '{}',
  body_contains text[] not null default '{}',
  enabled boolean not null default true,
  nota text
);
alter table public.programador_reglas enable row level security;
revoke all on public.programador_reglas from anon, authenticated;

insert into public.programador_reglas (entity, priority, from_domain, subject_contains, nota)
select * from (values
  ('rambaid', 80, array['agenciatributaria.gob.es','correo.aeat.es','seg-social.es','registradores.org'], array[]::text[], 'Administracion espanola: siempre Rambaid'),
  ('rambaid', 60, array[]::text[], array['rambaid'], 'Mencion en asunto'),
  ('forego_intl', 60, array[]::text[], array['forego international','forego intl'], 'Mencion en asunto'),
  ('ecme_forego', 60, array[]::text[], array['ecme'], 'Mencion en asunto'),
  ('sand', 60, array[]::text[], array['s@nd','sand srl'], 'Mencion en asunto')
) as v(entity, priority, from_domain, subject_contains, nota)
where not exists (select 1 from public.programador_reglas);

-- Lector de secretos de Vault para la Edge Function (solo service_role).
create or replace function public.programador_secreto(nombre text)
returns text
language sql
security definer
set search_path = ''
as $$
  select decrypted_secret from vault.decrypted_secrets where name = nombre limit 1
$$;
revoke all on function public.programador_secreto(text) from public, anon, authenticated;
grant execute on function public.programador_secreto(text) to service_role;

-- Token que autentica al cron ante la funcion; se genera una vez.
do $$
begin
  if not exists (select 1 from vault.secrets where name = 'programador_cron_token') then
    perform vault.create_secret(encode(extensions.gen_random_bytes(24), 'hex'), 'programador_cron_token', 'Cabecera x-programador-token de la Edge Function programador-sync');
  end if;
  if not exists (select 1 from vault.secrets where name = 'icloud_email') then
    perform vault.create_secret('lestersv@icloud.com', 'icloud_email', 'Cuenta iCloud que sincroniza programador-sync');
  end if;
end $$;

-- La contrasena de app de Apple NO va en este fichero. Se guarda una vez con:
--   select vault.create_secret('<contrasena-de-app>', 'icloud_app_password', 'App password de Apple para programador-sync');

-- Disparo cada 10 minutos.
select cron.schedule(
  'programador-sync',
  '*/10 * * * *',
  $cron$
  select net.http_post(
    url := 'https://bowuuimtsgeelgmhsdgu.supabase.co/functions/v1/programador-sync',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-programador-token', (select decrypted_secret from vault.decrypted_secrets where name = 'programador_cron_token')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 150000
  )
  $cron$
);

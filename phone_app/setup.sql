-- Moo phone app: run this once in your Supabase project (SQL Editor > New query > paste > Run).
-- It creates three private tables and a handful of functions. Nothing is readable without
-- Moo's secret pairing code, which never leaves your PC and phone.

create extension if not exists pgcrypto with schema extensions;

create table if not exists public.moo_keys (
  id int primary key default 1 check (id = 1),
  token_hash text not null,
  created timestamptz not null default now()
);

create table if not exists public.moo_messages (
  id bigserial primary key,
  created timestamptz not null default now(),
  sender text not null check (sender in ('phone', 'pc')),
  kind text not null default 'text',
  body text,
  audio text,          -- base64 voice or image
  audio_mime text,
  meta jsonb not null default '{}'::jsonb
);
create index if not exists moo_messages_sender_id on public.moo_messages (sender, id);

create table if not exists public.moo_state (
  id int primary key default 1 check (id = 1),
  data jsonb not null default '{}'::jsonb,
  updated timestamptz not null default now()
);

-- Locked down: no direct access at all, only through the functions below.
alter table public.moo_keys enable row level security;
alter table public.moo_messages enable row level security;
alter table public.moo_state enable row level security;
revoke all on public.moo_keys, public.moo_messages, public.moo_state from anon, authenticated;

create or replace function public.moo_ok(p_token text) returns boolean
language sql stable security definer set search_path = public, extensions as $$
  select exists (select 1 from moo_keys
                 where token_hash = encode(extensions.digest(coalesce(p_token, ''), 'sha256'), 'hex'));
$$;

-- First connection from the PC sets the pairing code. To re-pair later: delete from moo_keys;
create or replace function public.moo_register(p_token text) returns boolean
language plpgsql security definer set search_path = public, extensions as $$
begin
  if length(coalesce(p_token, '')) < 32 then return false; end if;
  if exists (select 1 from moo_keys) then return moo_ok(p_token); end if;
  insert into moo_keys (id, token_hash) values (1, encode(extensions.digest(p_token, 'sha256'), 'hex'));
  return true;
end $$;

create or replace function public.moo_post(p_token text, p_sender text, p_kind text, p_body text,
                                           p_audio text default null, p_mime text default null,
                                           p_meta jsonb default '{}'::jsonb) returns bigint
language plpgsql security definer set search_path = public, extensions as $$
declare new_id bigint;
begin
  if not moo_ok(p_token) then raise exception 'not paired'; end if;
  insert into moo_messages (sender, kind, body, audio, audio_mime, meta)
  values (p_sender, coalesce(p_kind, 'text'), p_body, p_audio, p_mime, coalesce(p_meta, '{}'::jsonb))
  returning id into new_id;
  -- tidy up: voice and pictures kept a day, messages a fortnight
  update moo_messages set audio = null where audio is not null and created < now() - interval '1 day';
  delete from moo_messages where created < now() - interval '14 days';
  return new_id;
end $$;

create or replace function public.moo_poll(p_token text, p_sender text, p_after bigint)
returns setof public.moo_messages
language plpgsql security definer set search_path = public, extensions as $$
begin
  if not moo_ok(p_token) then raise exception 'not paired'; end if;
  return query select * from moo_messages where sender = p_sender and id > coalesce(p_after, 0)
               order by id limit 20;
end $$;

create or replace function public.moo_history(p_token text, p_limit int default 40)
returns table (id bigint, created timestamptz, sender text, kind text, body text, meta jsonb)
language plpgsql security definer set search_path = public, extensions as $$
begin
  if not moo_ok(p_token) then raise exception 'not paired'; end if;
  return query select * from (select m.id, m.created, m.sender, m.kind, m.body, m.meta from moo_messages m
                              where m.kind in ('text', 'draft', 'alert')
                              order by m.id desc limit least(p_limit, 100)) t order by t.id;
end $$;

create or replace function public.moo_last_id(p_token text) returns bigint
language plpgsql security definer set search_path = public, extensions as $$
begin
  if not moo_ok(p_token) then raise exception 'not paired'; end if;
  return coalesce((select max(id) from moo_messages), 0);
end $$;

create or replace function public.moo_state_set(p_token text, p_data jsonb) returns void
language plpgsql security definer set search_path = public, extensions as $$
begin
  if not moo_ok(p_token) then raise exception 'not paired'; end if;
  insert into moo_state (id, data, updated) values (1, p_data, now())
  on conflict (id) do update set data = excluded.data, updated = now();
end $$;

create or replace function public.moo_state_get(p_token text) returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
begin
  if not moo_ok(p_token) then raise exception 'not paired'; end if;
  return (select jsonb_build_object('data', data, 'updated', updated) from moo_state where id = 1);
end $$;

revoke all on function public.moo_ok(text) from public, anon, authenticated;
grant execute on function public.moo_register(text), public.moo_post(text, text, text, text, text, text, jsonb),
  public.moo_poll(text, text, bigint), public.moo_history(text, int), public.moo_last_id(text),
  public.moo_state_set(text, jsonb), public.moo_state_get(text) to anon;

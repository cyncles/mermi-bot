-- Supabase > SQL Editor > New query'ye yapıştır ve RUN'a bas

create table if not exists public.members (
  id             text primary key,
  name           text        not null default '',
  avatar         text        not null default '',
  joined         timestamptz,
  messages       integer     not null default 0,
  voice_seconds  integer     not null default 0,
  night_messages integer     not null default 0,
  last_seen      timestamptz,
  updated_at     timestamptz not null default now()
);

-- Güvenlik: herkes OKUYABİLİR (site için), YAZMAYI sadece bot yapabilir (secret key ile)
alter table public.members enable row level security;

drop policy if exists "herkes okuyabilir" on public.members;
create policy "herkes okuyabilir" on public.members
  for select using (true);

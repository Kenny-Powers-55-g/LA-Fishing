-- OceanCast accounts: run this once in your Supabase project (SQL Editor > New query > Run).
-- One row per person holds their saved spots, their own pins, catch log and settings.
-- Row Level Security means a signed-in person can only ever see or change their own row.

create table if not exists public.oc_user_data (
  user_id    uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  data       jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.oc_user_data enable row level security;

drop policy if exists "read own data"   on public.oc_user_data;
drop policy if exists "add own data"    on public.oc_user_data;
drop policy if exists "change own data" on public.oc_user_data;
drop policy if exists "delete own data" on public.oc_user_data;

create policy "read own data"   on public.oc_user_data for select to authenticated using ((select auth.uid()) = user_id);
create policy "add own data"    on public.oc_user_data for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "change own data" on public.oc_user_data for update to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "delete own data" on public.oc_user_data for delete to authenticated using ((select auth.uid()) = user_id);

revoke all on public.oc_user_data from anon;
grant select, insert, update, delete on public.oc_user_data to authenticated;

-- "Delete my account" in the app: removes the person's sign-in and (by cascade) their data.
create or replace function public.oc_delete_me()
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  delete from auth.users where id = auth.uid();
end;
$$;

revoke all on function public.oc_delete_me() from public, anon;
grant execute on function public.oc_delete_me() to authenticated;

-- =====================================================================================
-- Profiles (usernames) and friends. Safe to run again: it only adds what is missing.
-- =====================================================================================

-- One profile per person: a unique username and what they share with friends.
create table if not exists public.oc_profiles (
  user_id       uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  username      text not null,
  share_spots   boolean not null default false,
  share_catches boolean not null default false,
  created_at    timestamptz not null default now(),
  constraint oc_username_format check (username ~ '^[a-z0-9_]{3,20}$')
);
create unique index if not exists oc_profiles_username on public.oc_profiles (username);

alter table public.oc_profiles enable row level security;
drop policy if exists "signed-in people see usernames" on public.oc_profiles;
drop policy if exists "add own profile"                on public.oc_profiles;
drop policy if exists "change own profile"             on public.oc_profiles;
-- Usernames are how people find each other, so any signed-in person can look one up.
-- A profile holds nothing else private: no email, no spots, no catches.
create policy "signed-in people see usernames" on public.oc_profiles for select to authenticated using (true);
create policy "add own profile"    on public.oc_profiles for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "change own profile" on public.oc_profiles for update to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
revoke all on public.oc_profiles from anon;
grant select, insert, update on public.oc_profiles to authenticated;

-- A username + password sign-up passes the chosen username along; make its profile right away.
create or replace function public.oc_new_user_profile()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  u text := lower(new.raw_user_meta_data ->> 'username');
begin
  if u is not null and u ~ '^[a-z0-9_]{3,20}$' then
    insert into public.oc_profiles (user_id, username) values (new.id, u) on conflict do nothing;
  end if;
  return new;
end;
$$;
drop trigger if exists oc_new_user_profile on auth.users;
create trigger oc_new_user_profile after insert on auth.users
  for each row execute function public.oc_new_user_profile();

-- "Is this username free?" for the sign-up form (works before signing in).
create or replace function public.oc_username_free(name text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select not exists (select 1 from public.oc_profiles where username = lower(name));
$$;
revoke all on function public.oc_username_free(text) from public;
grant execute on function public.oc_username_free(text) to anon, authenticated;

-- Friend requests. A request is 'pending' until the other person accepts it.
-- Either person can delete the row: that declines, cancels or unfriends.
create table if not exists public.oc_friends (
  requester  uuid not null references auth.users(id) on delete cascade,
  addressee  uuid not null references auth.users(id) on delete cascade,
  status     text not null default 'pending' check (status in ('pending', 'accepted')),
  created_at timestamptz not null default now(),
  primary key (requester, addressee),
  check (requester <> addressee)
);
-- Only one request per pair, whichever way round it was sent
create unique index if not exists oc_friends_pair
  on public.oc_friends (least(requester, addressee), greatest(requester, addressee));

alter table public.oc_friends enable row level security;
drop policy if exists "see own friendships"   on public.oc_friends;
drop policy if exists "send friend requests"  on public.oc_friends;
drop policy if exists "accept friend requests" on public.oc_friends;
drop policy if exists "end friendships"       on public.oc_friends;
create policy "see own friendships" on public.oc_friends for select to authenticated
  using ((select auth.uid()) in (requester, addressee));
create policy "send friend requests" on public.oc_friends for insert to authenticated
  with check ((select auth.uid()) = requester and status = 'pending');
create policy "accept friend requests" on public.oc_friends for update to authenticated
  using ((select auth.uid()) = addressee) with check ((select auth.uid()) = addressee);
create policy "end friendships" on public.oc_friends for delete to authenticated
  using ((select auth.uid()) in (requester, addressee));
revoke all on public.oc_friends from anon;
grant select, insert, update, delete on public.oc_friends to authenticated;
-- The only change allowed after sending is the status (accepting)
revoke update on public.oc_friends from authenticated;
grant update (status) on public.oc_friends to authenticated;

-- Text that should hold JSON, or null if it doesn't (saved data is stored as text).
create or replace function public.oc_try_jsonb(t text)
returns jsonb
language plpgsql
immutable
set search_path = ''
as $$
begin
  return t::jsonb;
exception when others then
  return null;
end;
$$;

-- A saved list, or an empty list if it's missing or not a list
create or replace function public.oc_json_list(t text)
returns jsonb
language sql
immutable
set search_path = ''
as $$
  select case when jsonb_typeof(public.oc_try_jsonb(t)) = 'array' then public.oc_try_jsonb(t) else '[]'::jsonb end;
$$;

-- What your accepted friends chose to share: their pinned spots and/or their catch log.
-- Runs with extra rights so it can read their saved data, but only ever returns the two
-- shared lists, only for accepted friends, and never catch notes.
create or replace function public.oc_friend_data()
returns table (user_id uuid, username text, spots jsonb, catches jsonb)
language sql
stable
security definer
set search_path = ''
as $$
  select p.user_id, p.username,
         case when p.share_spots then public.oc_json_list(d.data ->> 'la-fish-myspots') end,
         case when p.share_catches then (
           select coalesce(jsonb_agg(c - 'notes'), '[]'::jsonb)
           from jsonb_array_elements(public.oc_json_list(d.data ->> 'la-fish-catches-v1')) as c
           where jsonb_typeof(c) = 'object'
         ) end
  from public.oc_friends f
  join public.oc_profiles p
    on p.user_id = case when f.requester = (select auth.uid()) then f.addressee else f.requester end
  left join public.oc_user_data d on d.user_id = p.user_id
  where f.status = 'accepted' and (select auth.uid()) in (f.requester, f.addressee);
$$;
revoke all on function public.oc_friend_data() from public, anon;
grant execute on function public.oc_friend_data() to authenticated;

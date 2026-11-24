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

-- Per-friend sharing: what one person (owner) shares with one friend (viewer).
-- No row means "use my default" (share_spots / share_catches on the owner's profile).
create table if not exists public.oc_shares (
  owner   uuid not null references auth.users(id) on delete cascade,
  viewer  uuid not null references auth.users(id) on delete cascade,
  spots   boolean not null default false,
  catches boolean not null default false,
  primary key (owner, viewer),
  check (owner <> viewer)
);
alter table public.oc_shares enable row level security;
drop policy if exists "manage own sharing" on public.oc_shares;
-- Only the owner sees or changes these rows; friends only get the result through oc_friend_data()
create policy "manage own sharing" on public.oc_shares for all to authenticated
  using ((select auth.uid()) = owner) with check ((select auth.uid()) = owner);
revoke all on public.oc_shares from anon;
grant select, insert, update, delete on public.oc_shares to authenticated;

-- What your accepted friends chose to share with you: their pinned spots and/or their catch log.
-- Runs with extra rights so it can read their saved data, but only ever returns the two
-- shared lists, only for accepted friends, and only a fixed set of catch fields (never notes).
create or replace function public.oc_friend_data()
returns table (user_id uuid, username text, spots jsonb, catches jsonb)
language sql
stable
security definer
set search_path = ''
as $$
  select p.user_id, p.username,
         case when coalesce(s.spots, p.share_spots) then public.oc_json_list(d.data ->> 'la-fish-myspots') end,
         case when coalesce(s.catches, p.share_catches) then (
           -- Only these fields are ever shared. Notes, amounts, size, weight, kept/released counts, bait,
           -- photos and saved readings stay private, including any field added to the log later.
           select coalesce(jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
             'id', c -> 'id', 'sp', c -> 'sp', 'spot', c -> 'spot', 'date', c -> 'date',
             'depth', c -> 'depth', 'result', c -> 'result', 'scoreAtLog', c -> 'scoreAtLog'))), '[]'::jsonb)
           from jsonb_array_elements(public.oc_json_list(d.data ->> 'la-fish-catches-v1')) as c
           where jsonb_typeof(c) = 'object'
         ) end
  from public.oc_friends f
  join public.oc_profiles p
    on p.user_id = case when f.requester = (select auth.uid()) then f.addressee else f.requester end
  left join public.oc_user_data d on d.user_id = p.user_id
  left join public.oc_shares s on s.owner = p.user_id and s.viewer = (select auth.uid())
  where f.status = 'accepted' and (select auth.uid()) in (f.requester, f.addressee);
$$;
revoke all on function public.oc_friend_data() from public, anon;
grant execute on function public.oc_friend_data() to authenticated;

-- =====================================================================================
-- Abuse limits. Safe to run again.
-- =====================================================================================

-- Each person's saved data (spots, catch log, settings) is capped at about 1 MB: room for roughly 650
-- logged trips with their readings, but too little to use the free database as file storage (photos
-- never leave the phone). NOT VALID: existing rows aren't rechecked until they next change.
alter table public.oc_user_data drop constraint if exists oc_user_data_size;
alter table public.oc_user_data add constraint oc_user_data_size check (octet_length(data::text) <= 1000000) not valid;

-- At most 20 unanswered friend requests out at once, so nobody can spam every username.
create or replace function public.oc_my_pending_requests()
returns integer
language sql
stable
security definer
set search_path = ''
as $$
  select count(*)::int from public.oc_friends where requester = (select auth.uid()) and status = 'pending';
$$;
revoke all on function public.oc_my_pending_requests() from public, anon;
grant execute on function public.oc_my_pending_requests() to authenticated;

drop policy if exists "send friend requests" on public.oc_friends;
create policy "send friend requests" on public.oc_friends for insert to authenticated
  with check ((select auth.uid()) = requester and status = 'pending' and public.oc_my_pending_requests() < 20);

-- =====================================================================================
-- Anonymous community catch reports. Safe to run again.
-- Anyone can send one (no account needed) to help tune the scores. A report holds only:
-- the fish, one of seven big areas, the day, the result, the model's score and the 0-1
-- grade of each reading. Never a spot, pin, coordinates, time of day, sizes, counts, bait,
-- notes, photos, username or account id, so a report can't lead anyone to a spot or a person.
-- Nobody can read the reports themselves: the only way out is oc_community_stats(), which
-- returns totals and averages, and nothing at all from fewer than 5 reports.
-- =====================================================================================

create table if not exists public.oc_community_reports (
  id          bigint generated always as identity primary key,
  reported_on date not null default current_date,      -- the day only, never the time
  sp          text not null check (sp ~ '^[a-z]{2,20}$'),
  region      text not null check (region in ('sw', 'acadiana', 'terr', 'nola', 'delta', 'north', 'offshore')),
  result      text not null check (result in ('caught', 'slow', 'none')),
  score       smallint check (score between 0 and 100),
  factors     jsonb not null check (jsonb_typeof(factors) = 'object' and octet_length(factors::text) <= 1000)
);
alter table public.oc_community_reports enable row level security;
-- No policies on purpose: nobody reads or writes this table directly, only through the two functions below.
revoke all on public.oc_community_reports from public, anon, authenticated;

-- Daily send limits. A report is counted against a one-way fingerprint of the sender's network
-- address mixed with a random secret that is thrown away the next day. The fingerprint is never
-- stored with the report, and yesterday's counts and secret are deleted, so the limits can't be
-- used to work out who sent what.
create table if not exists public.oc_report_salt (
  day  date primary key,
  salt text not null
);
create table if not exists public.oc_report_limits (
  day    date not null,
  bucket text not null,
  n      integer not null default 0,
  primary key (day, bucket)
);
alter table public.oc_report_salt enable row level security;
alter table public.oc_report_limits enable row level security;
revoke all on public.oc_report_salt from public, anon, authenticated;
revoke all on public.oc_report_limits from public, anon, authenticated;

-- Send one anonymous report. The app calls this without the person's sign-in, even when they're signed in.
-- Readings are rounded to one decimal so they can't be matched back to one spot's exact numbers.
create or replace function public.oc_report_catch(sp text, region text, result text, score integer, factors jsonb)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  per_sender constant integer := 12;     -- reports a day from one network address
  per_day    constant integer := 1500;   -- reports a day from everyone together (the backstop)
  per_unknown constant integer := 300;   -- reports a day, all together, whose sender address isn't known
  hdr  json := nullif(current_setting('request.headers', true), '')::json;
  ip   text;
  net  text;
  s    text;
  b    text;
  cnt  integer;
  f    jsonb := '{}'::jsonb;
  k    text;
  v    jsonb;
begin
  if sp is null or sp !~ '^[a-z]{2,20}$' then raise exception 'Unknown fish'; end if;
  if region is null or region not in ('sw', 'acadiana', 'terr', 'nola', 'delta', 'north', 'offshore') then raise exception 'Unknown area'; end if;
  if result is null or result not in ('caught', 'slow', 'none') then raise exception 'Unknown result'; end if;
  if score is not null and (score < 0 or score > 100) then raise exception 'Score out of range'; end if;
  if factors is null or jsonb_typeof(factors) <> 'object' then raise exception 'No readings'; end if;
  for k, v in select e.key, e.value from jsonb_each(factors) as e loop
    if k !~ '^[a-z]{2,10}$' or jsonb_typeof(v) <> 'number' or v::text::numeric < 0 or v::text::numeric > 1 then
      raise exception 'Bad reading';
    end if;
    f := f || jsonb_build_object(k, round(v::text::numeric, 1));
  end loop;
  if (select count(*) from jsonb_object_keys(f)) not between 1 and 30 then raise exception 'Bad readings'; end if;

  -- Forget yesterday's limits and secret, then count this report against today's
  delete from public.oc_report_limits where day < current_date;
  delete from public.oc_report_salt where day < current_date;
  insert into public.oc_report_salt (day, salt) values (current_date, gen_random_uuid()::text) on conflict (day) do nothing;
  select rs.salt into s from public.oc_report_salt rs where rs.day = current_date;
  -- Only Cloudflare's own header: it is set at Supabase's edge and replaces anything the sender puts there.
  -- x-forwarded-for is not used, because a sender can write whatever they like into it.
  ip := nullif(trim(coalesce(hdr ->> 'cf-connecting-ip', '')), '');
  -- A phone on IPv6 can use a fresh address for every request, but they all share their network's
  -- first 64 bits, so an IPv6 sender is counted by that /64 network. IPv4 is counted by address.
  begin
    net := case when ip is null then null when family(ip::inet) = 6 then network(set_masklen(ip::inet, 64))::text else host(ip::inet) end;
  exception when others then
    net := null;                          -- not an address at all: count it with the unknowns
  end;
  b := case when net is null then 'unknown' else encode(sha256(convert_to(s || '|' || net, 'UTF8')), 'hex') end;
  insert into public.oc_report_limits as l (day, bucket, n) values (current_date, b, 1)
    on conflict (day, bucket) do update set n = l.n + 1
    returning l.n into cnt;
  if cnt > (case when b = 'unknown' then per_unknown else per_sender end) then
    raise exception 'Daily limit reached. Thanks for all the reports; try again tomorrow.';
  end if;
  if (select sum(l.n) from public.oc_report_limits l where l.day = current_date) > per_day then
    raise exception 'Daily limit reached for everyone. Try again tomorrow.';
  end if;

  insert into public.oc_community_reports (sp, region, result, score, factors)
  values (sp, region, result, score, f);
end;
$$;
revoke all on function public.oc_report_catch(text, text, text, integer, jsonb) from public, anon, authenticated;
grant execute on function public.oc_report_catch(text, text, text, integer, jsonb) to anon, authenticated;

-- Totals and averages for the hourly job that tunes the scores (and for anyone curious).
-- Per fish, for all areas together ("all") and for each area: how many trips caught fish or not,
-- the average grade of each reading on good and slow trips, and how often each score band caught fish.
-- A group with fewer than 5 reports is left out entirely; an average from fewer than 3 is left blank.
create or replace function public.oc_community_stats_now()
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  with r as (
    select c.sp, c.region, c.result = 'caught' as good, c.score, c.factors
    from public.oc_community_reports c
    where c.reported_on > current_date - 730
  ),
  cells as (
    select r.sp, coalesce(r.region, 'all') as region,
           count(*) filter (where r.good) as caught, count(*) filter (where not r.good) as slow,
           count(*) filter (where r.score >= 75) as ex_n, count(*) filter (where r.score >= 75 and r.good) as ex_c,
           count(*) filter (where r.score >= 55 and r.score < 75) as gd_n, count(*) filter (where r.score >= 55 and r.score < 75 and r.good) as gd_c,
           count(*) filter (where r.score >= 35 and r.score < 55) as fr_n, count(*) filter (where r.score >= 35 and r.score < 55 and r.good) as fr_c,
           count(*) filter (where r.score < 35) as po_n, count(*) filter (where r.score < 35 and r.good) as po_c
    from r
    group by grouping sets ((r.sp, r.region), (r.sp))
    having count(*) >= 5
  ),
  facs as (
    select r.sp, coalesce(r.region, 'all') as region, e.key,
           avg(e.value::numeric) filter (where r.good) as gm, count(*) filter (where r.good) as gn,
           avg(e.value::numeric) filter (where not r.good) as sm, count(*) filter (where not r.good) as sn
    from r cross join lateral jsonb_each_text(r.factors) as e(key, value)
    group by grouping sets ((r.sp, r.region, e.key), (r.sp, e.key))
  ),
  band(k, n, c) as (
    select 'ex', count(*) filter (where score >= 75), count(*) filter (where score >= 75 and good) from r
    union all select 'gd', count(*) filter (where score >= 55 and score < 75), count(*) filter (where score >= 55 and score < 75 and good) from r
    union all select 'fr', count(*) filter (where score >= 35 and score < 55), count(*) filter (where score >= 35 and score < 55 and good) from r
    union all select 'po', count(*) filter (where score < 35), count(*) filter (where score < 35 and good) from r
  ),
  cellj as (
    select c.sp, c.region, jsonb_build_object(
      'caught', c.caught, 'slow', c.slow,
      'b', jsonb_strip_nulls(jsonb_build_object(
        'ex', case when c.ex_n >= 5 then jsonb_build_array(c.ex_n, c.ex_c) end,
        'gd', case when c.gd_n >= 5 then jsonb_build_array(c.gd_n, c.gd_c) end,
        'fr', case when c.fr_n >= 5 then jsonb_build_array(c.fr_n, c.fr_c) end,
        'po', case when c.po_n >= 5 then jsonb_build_array(c.po_n, c.po_c) end)),
      'f', coalesce((
        select jsonb_object_agg(x.key, jsonb_build_array(
          case when x.gn >= 3 then round(x.gm, 3) end, x.gn,
          case when x.sn >= 3 then round(x.sm, 3) end, x.sn))
        from facs x where x.sp = c.sp and x.region = c.region), '{}'::jsonb)) as j
    from cells c
  )
  select jsonb_build_object(
    'total', (select count(*) from r),
    'regions', coalesce((select jsonb_object_agg(q.region, q.n) from (
        select r.region, count(*) as n from r group by r.region having count(*) >= 5) q), '{}'::jsonb),
    'bands', coalesce((select jsonb_object_agg(band.k, jsonb_build_array(band.n, band.c)) from band where band.n >= 5), '{}'::jsonb),
    'species', coalesce((select jsonb_object_agg(q.sp, q.regions) from (
        select cellj.sp, jsonb_object_agg(cellj.region, cellj.j) as regions from cellj group by cellj.sp) q), '{}'::jsonb));
$$;
-- Internal: only oc_community_stats() below calls it, at most once an hour.
revoke all on function public.oc_community_stats_now() from public, anon, authenticated;

-- The public totals are worked out at most once an hour and kept here, so nobody can make the database
-- redo the sums over every report on each request.
create table if not exists public.oc_community_snapshot (
  id   integer primary key check (id = 1),
  made timestamptz not null,
  data jsonb not null
);
alter table public.oc_community_snapshot enable row level security;
revoke all on public.oc_community_snapshot from public, anon, authenticated;

create or replace function public.oc_community_stats()
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  snap record;
  fresh jsonb;
begin
  select cs.made, cs.data into snap from public.oc_community_snapshot cs where cs.id = 1;
  if found and snap.made > now() - interval '1 hour' then
    return snap.data;
  end if;
  -- One caller does the sums; anyone arriving at the same moment waits and then gets its result
  perform pg_advisory_xact_lock(hashtext('oc_community_stats'));
  select cs.made, cs.data into snap from public.oc_community_snapshot cs where cs.id = 1;
  if found and snap.made > now() - interval '1 hour' then
    return snap.data;
  end if;
  fresh := public.oc_community_stats_now() || jsonb_build_object('as_of', now());
  insert into public.oc_community_snapshot (id, made, data) values (1, now(), fresh)
    on conflict (id) do update set made = excluded.made, data = excluded.data;
  return fresh;
end;
$$;
revoke all on function public.oc_community_stats() from public, anon, authenticated;
grant execute on function public.oc_community_stats() to anon, authenticated;

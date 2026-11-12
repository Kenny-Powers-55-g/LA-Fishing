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

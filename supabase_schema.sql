-- FYERS VWAP Trader cloud market-data schema.
-- Run this once in the Supabase SQL Editor.

create table if not exists public.instruments (
  symbol text primary key,
  underlying text not null,
  expiry text,
  strike numeric,
  option_type text check (option_type in ('CE','PE') or option_type is null),
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.market_candles_1m (
  symbol text not null references public.instruments(symbol),
  candle_start timestamptz not null,
  underlying text not null,
  expiry text,
  strike numeric,
  option_type text check (option_type in ('CE','PE') or option_type is null),
  open numeric,
  high numeric,
  low numeric,
  close numeric,
  ltp numeric,
  volume bigint not null default 0,
  oi bigint,
  oi_change bigint,
  prev_oi bigint,
  oi_snapshot_at timestamptz,
  source text not null default 'fyers_websocket',
  updated_at timestamptz not null default now(),
  primary key (symbol, candle_start)
);

create index if not exists market_candles_1m_time_idx
  on public.market_candles_1m (candle_start desc);

create index if not exists market_candles_1m_underlying_time_idx
  on public.market_candles_1m (underlying, candle_start desc);

create index if not exists market_candles_1m_option_time_idx
  on public.market_candles_1m (option_type, candle_start desc);

alter table public.instruments enable row level security;
alter table public.market_candles_1m enable row level security;

-- The Streamlit server uses the server-side Supabase secret key, which bypasses
-- RLS. Do NOT put that secret key in browser/client code.
-- Optional read-only policies for future authenticated users can be added later.

-- Keep updated_at correct on upsert/update.
create or replace function public.set_market_data_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists instruments_updated_at on public.instruments;
create trigger instruments_updated_at
before update on public.instruments
for each row execute function public.set_market_data_updated_at();

drop trigger if exists market_candles_updated_at on public.market_candles_1m;
create trigger market_candles_updated_at
before update on public.market_candles_1m
for each row execute function public.set_market_data_updated_at();

# stk - personal NSE/BSE stock screener

A stock tool for your terminal, built for Termux (works anywhere with Python 3.9+).

- **Phase 1:** `stk quote` - latest price for any NSE/BSE stock.
- **Phase 2:** a local database of daily prices for the Nifty 500, with `update`, `history`, `info`, `find` and a watchlist.
- **Phase 3:** indicators (RSI, moving averages, MACD, 52-week, volume) and `stk screen`: built-in, custom and saved screens.

Prices come from Yahoo Finance and are end-of-day (stored) or delayed by a few minutes (`quote`). This is for information, not for fast trading decisions.

## Install on Termux

```sh
pkg install git
git clone https://github.com/<your-username>/<your-repo>.git stk
cd stk
sh setup.sh
stk doctor
```

## First-time setup (Phase 2)

```sh
stk init       # creates the database and downloads the Nifty 500 list
stk update     # first run downloads 2 years of prices (a few minutes, use Wi-Fi)
stk status     # check what is stored
```

If `stk init` can't reach NSE, download the Nifty 500 constituents CSV from niftyindices.com in your browser and run `stk init --file ~/storage/downloads/ind_nifty500list.csv`. Any CSV with a `Symbol` column works, so you can load your own list too.

## Daily use

```sh
stk update                  # after market close (3:45 PM IST): fetches only the new days
stk quote RELIANCE          # live-ish price card
stk quote TCS INFY HDFCBANK # compact table
stk history TCS -d 20       # last 20 sessions from the database (works offline)
stk history TCS --ohlc      # include open, high, low
stk info RELIANCE           # 52-week range, 1D/1W/1M/6M/1Y change, volume vs average
stk find tata               # search symbols and company names
stk watch add TCS INFY      # build a watchlist
stk watch                   # live prices for the whole watchlist
stk watch remove INFY
```

### Screens (Phase 3)

```sh
stk screen                       # list all screens
stk screen oversold              # run a built-in screen
stk screen --rsi-below 30 --above-sma 200          # your own filters
stk screen --where "rsi14<35" --where "close>sma200" --where "vol_ratio>=1.5"
stk screen oversold --industry bank --names        # narrow a screen, show company names
stk screen save mydip --rsi-below 30 --above-sma 200   # save your filters
stk screen mydip                 # run it later
stk screen show mydip            # see its definition
stk screen delete mydip
stk screen fields                # everything you can filter on
stk screen breakout --limit 100 --csv ~/breakouts.csv  # more rows, and export every match
```

Built-in screens: `oversold`, `overbought`, `golden-cross`, `death-cross`, `breakout`, `near-high`, `near-low`, `volume-surge`, `macd-bullish`, `macd-bearish`, `trend-up`, `gainers`, `losers`.

Filters you can use with `--where`: a field, a comparison (`< <= > >= == !=`) and a number or another field, for example `chg_1m<-5`, `close>sma50*1.02`, `industry~bank` (`~` means "contains"). Run `stk screen fields` for the full list. Stocks with an empty value for a field never match a condition on it (a stock with under 200 days of history has no 200-day average, so it is skipped by `--above-sma 200`).

Results are sorted by average traded value (most liquid first) unless the screen or `--sort` says otherwise. `stk info RELIANCE` now also shows RSI, position against the 50/200-day averages, crosses, MACD and volume.

How the numbers are defined: RSI is the 14-day Wilder RSI; moving averages are simple averages of closes; MACD is 12/26/9; a golden cross is the 50-day average moving above the 200-day (death cross is the reverse); the 52-week range uses the last 252 sessions; volume ratio compares the latest session with the average of the 20 before it; `breakout` means the close is above the highest high of the previous 251 sessions. Indicators are recalculated automatically by `stk update` and, if needed, by `stk screen`.

Screens flag candidates from end-of-day data. They are a starting point for your own research, not advice.

### Home screen and colors (Phase 4A)

Run `stk` on its own for the home screen: logo, market status, Nifty/Sensex, data freshness,
watchlist summary and a numbered menu (1 Watchlist, 2 Screens, 3 Find/Info, 4 History,
5 Alerts, 6 Update, 7 Status). You can also type any normal command there, e.g. `quote TCS`.
`stk --no-splash` prints plain help instead. Output is colored (green up / red down, bold for
moves of 3% or more; volume spikes in yellow or bold; RSI red above 70, green below 30).
Use `--no-color` or set `NO_COLOR=1` for plain text.

### Alerts and Telegram (Phase 4B)

```
stk alert add TCS above 2200        price at or above (also: below)
stk alert add TCS rsi-below 30      RSI(14) at or below (also: rsi-above)
stk alert add TCS move 5            today's move of +/-5% or more
stk alert add TCS volume 2          volume 2x its 20-day average
stk alert                           list alerts (watching / fired)
stk alert check                     test them now (--loop 120 repeats every 2 min)
stk alert reset 3   /  remove 3     re-arm or delete an alert
```

An alert fires once, then switches itself off; `reset` re-arms it. Price, move and volume alerts
use a live quote; RSI alerts use the stored daily data.

**Telegram:** `stk telegram setup` walks you through creating a bot with @BotFather and finds your
chat id. `stk telegram test` sends a test message. The token is kept in `telegram.json` in the stk
data folder (not in the repo). With Telegram set up, fired alerts are sent to your phone.

**Daily run:** `stk daily` updates prices, recalculates indicators, checks alerts and sends a
summary (top gainers/losers, oversold/overbought counts, volume spikes, your watchlist).
`stk schedule` prints the steps to run it automatically with cron in Termux.

### Fundamentals (Phase 5)

```
stk fundamentals probe            check the data source works from your phone (run this first)
stk fundamentals update           download for your watchlist
stk fundamentals update TCS INFY  download for named stocks
stk fundamentals update --all     every stock (about 7 minutes; fine to run once a week)
stk fundamentals                  what is stored and how old it is
stk info TCS                      now includes market cap, P/E, P/B, ROE, margins, debt, growth
stk screen value                  P/E under 20, P/B under 3, ROE over 12 (also: quality, growth, dividend)
stk screen --where "pe<15" --where "roe>18" --rsi-below 40
```

Fields for your own screens: `market_cap_cr pe pb eps div_yield roe roa profit_margin op_margin
debt_equity rev_growth earn_growth` (see `stk screen fields`). Stocks with no figure for a field
never match a screen on it, and the screen tells you how many stocks have fundamentals.
Data comes from Yahoo and can be patchy or a little old for Indian stocks, so treat it as a
first filter, not a verdict. `stk daily` refreshes your watchlist's fundamentals weekly.

### Live prices

`stk watch` is now a live view on a terminal: prices refresh every 5 seconds (change it with
`--every 3`, minimum 3) until you press Ctrl+C; `stk watch --once` prints once.
`stk quote TCS --watch` does the same for any symbols. The refresh bypasses the 45-second cache,
fetches the stocks side by side, and backs off by itself if the data source pushes back.
Yahoo's free feed can itself run a few minutes behind the exchange.

### More options

`stk quote RELIANCE --bse`, `stk quote 500325` (BSE code), `stk quote ^NSEI`, `stk quote RELIANCE --watch 30` (refresh every 30s), `stk update TCS` (one stock), `stk update --full` (re-download everything), `stk update --years 5`, `stk update --retry-missing` (ask again about stocks Yahoo had no data for), `stk probe` (diagnose Yahoo 429 errors).

## How the data is handled

- **First run:** 2 years of daily candles per stock. Each stock is saved as soon as it downloads, so Ctrl+C or a dropped connection loses nothing. Run `stk update` again to continue.
- **Daily runs:** only the days since the last stored date (with a 7-day overlap to pick up corrections). Stocks already current are skipped with no network call.
- **Splits and bonuses:** if one happened recently, that stock's history is re-downloaded in full so old prices are re-adjusted.
- **Only finished days are stored.** Before 3:45 PM IST on a weekday, today's still-forming candle is ignored.
- **Failures** (network problems, rate limits) are logged and shown in `stk status`; the rest carry on.
- **Stocks Yahoo doesn't know** (placeholders, renamed or very new listings) are marked on the first update and skipped for 30 days, so they don't show up as failures every time.
- Stored data is NSE (`.NS`). BSE-only stocks aren't covered yet.
- Exchange holidays aren't modelled. After a holiday `stk update` may re-check stocks and find nothing new, which is harmless.
- Data lives in `~/.local/share/stk/stk.db` (override with `STK_HOME`).

## Update the app

```sh
cd stk && git pull
```

## Tests

```sh
python -m unittest discover -s tests -v
```

## Roadmap

1. `quote` (done)
2. Database, Nifty 500 list, daily updater, history/info/find/watchlist (done)
3. Indicators and `stk screen` with built-in, custom and saved screens (done)
4. Home screen, colors, alerts, Telegram and a daily run (done)
5. Fundamentals (P/E, ROE), live prices, new logo (done; fundamentals source needs a check on your phone)
6. Streamlit web dashboard (next) sharing the same core

## Disclaimer

Personal tool for information only. Not investment advice. Data can be delayed, incomplete or wrong.

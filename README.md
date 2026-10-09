# stk - personal NSE/BSE stock screener

Phase 1: the `quote` command. Look up the latest price for any NSE or BSE
stock from your terminal (built for Termux, works anywhere with Python 3.9+).

Prices come from Yahoo Finance and are typically delayed by a few minutes.
This is for checking prices, not for fast trading decisions.

## Install on Termux

```sh
pkg install git
git clone https://github.com/<your-username>/stk.git
cd stk
sh setup.sh
```

Then:

```sh
stk doctor                 # checks setup and internet
stk quote RELIANCE         # one stock, detailed card
stk quote TCS INFY HDFCBANK   # several stocks, compact table
stk quote RELIANCE --bse   # look up on BSE first
stk quote 500325           # BSE scrip code
stk quote ^NSEI ^BSESN     # Nifty 50, Sensex
stk quote RELIANCE --watch 30   # refresh every 30 seconds (Ctrl+C to stop)
```

## How it behaves

- A bare name like `RELIANCE` tries NSE first, then BSE.
- During market hours (Mon-Fri, 9:15-15:30 IST) you get the latest available price. Otherwise you get the last close, clearly labelled.
- Lookups are cached for 45 seconds, so repeating a command doesn't hammer the data source.
- If you're offline, it shows the last saved price and says so.
- Exchange holidays aren't modelled yet, so on a holiday it may say "market open" while the price isn't moving.

## Update later

```sh
cd stk && git pull
```

## Run without installing the command

```sh
python -m screener quote RELIANCE
```

## Tests

```sh
python -m unittest discover -s tests -v
```

## Roadmap

1. `quote` (this release)
2. SQLite storage + daily updater for the Nifty 500
3. Indicators (RSI, SMA 50/200, 52-week high/low) and the screen engine
4. Telegram alerts and scheduled runs
5. Streamlit web dashboard sharing the same core

## Disclaimer

Personal tool for information only. Not investment advice. Data can be
delayed, incomplete or wrong.

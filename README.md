# EVE Market Scout

A desktop market-analysis tool for [EVE Online](https://www.eveonline.com/). It
pulls public market data from the EVE Swagger Interface (ESI), combines it with
a local multi-year price-history archive, and surfaces trading, restocking and
manufacturing opportunities across the main trade hubs and player-owned
structures.

Written in Python with a Tkinter interface. Runs locally on your own machine —
there is no server, no account, and no telemetry of any kind.

**Status:** version 3.70, actively developed, single-author hobby project. Some
modules are finished and in daily use; others are explicitly work-in-progress and
marked as such below. Numbers it shows you are estimates derived from delayed
public data. See [Use at your own risk](#use-at-your-own-risk).

---

## What it does

**Finished and in regular use**

- **Deals** — scans a hub for buy-low/sell-high candidates across several risk
  categories, gated by real traded volume from the history archive rather than
  by order-book optimism alone.
- **Cross-Hub** — arbitrage between two hubs, including shipping-aware margins.
- **Demand / Restock** — "how much of this should I haul to fill demand," driven
  by observed velocity at the destination.
- **Watchlist** — per-item tracking with nested category tabs and alerts.
- **History** — price/volume charts from the local archive.
- **NPC Orders** — tracks items bought from players and sold to NPC buy orders,
  with a cash-flow sales summary and a jump/standings-aware max-buy calculator.
- **Tracking** — FIFO inventory and realized-profit tracking fed by your wallet.
- **Contracts** — searches item and blueprint-copy contracts. ESI has no
  server-side contract search, so contract lists and their immutable contents are
  cached locally and searched on your machine.
- **Reprocess** — paste a hangar full of junk, get sell-vs-reprocess per item at
  your actual Scrapmetal skill and tax rate.

**Work in progress — expect rough edges**

- **Stock Market** — longer-horizon holdings, P&L and risk panels.
- **Industry** — tier-1 manufacturing profitability: job cost from ESI adjusted
  prices and system cost indices, blueprint-copy pricing from cached contract
  offers, per-character skill and standings effects.
- **Player structures** — structures work as hubs (metadata, order books, and a
  locally observed daily-history layer, since ESI publishes no history for them),
  but the feature is under-used relative to what it could do.

## Requirements

- Windows, macOS or Linux with Python 3.10 or newer and Tkinter available
  (Tkinter ships with python.org builds; on Debian/Ubuntu install `python3-tk`)
- An EVE Online account, for the features that read your own character data
- `pip install -r requirements.txt` — `aiohttp` and `requests` are required;
  `matplotlib` is optional and only affects charts

## Running it

```
git clone https://github.com/spirit1flyer/Eve-market-scout.git
cd Eve-market-scout
pip install -r requirements.txt
python main.py
```

First launch downloads the EVE Static Data Export (item and market-group tables,
a few tens of MB from Fuzzwork) and offers to build the price-history archive.

**The history archive is large.** It is assembled from daily public market
snapshots published by [EVE Ref](https://everef.net/) and grows to roughly
**8–9 GB** for the full three-year rolling window. The app is usable before it
finishes — the scanner's trailing 60-day window imports first and blocks, while
deeper backfill runs in the background and yields to scans. Budget disk space and
bandwidth accordingly; if either is tight, a partial archive still works, it just
weakens the volume-based safety checks.

## Logging in

Two independent, optional EVE SSO logins, both **read-only** — no scope the app
requests can place, modify or cancel an order, move an item, or spend ISK:

- a **trading** login (wallet, transactions, journal, your market orders, skills
  and NPC standings for fee math, structure market access)
- a separate **industry character roster** (industry skills, standings, owned
  blueprints)

Everything public — hub order books, price history, contracts, adjusted prices —
works with no login at all. Refresh tokens are stored unencrypted in a local
file; the exact scopes and the reasoning are in
[ESI_DATA_USAGE.md](ESI_DATA_USAGE.md), which is also readable in-app from the
**Data use** dialog.

## Where your data lives

Nothing is written into the repository. User data, caches and credentials go to
one shared per-user directory:

- Windows: `%APPDATA%\EVEMarketScout\`
- macOS: `~/Library/Application Support/EVEMarketScout/`
- Linux: `~/.local/share/EVEMarketScout/`

That holds the history archive, the static-data databases, your watchlists,
holdings, tracked trades and custom stations, and `esi_auth.json`. Deleting
`esi_auth.json` forces a re-login; deleting the archive means rebuilding it.

## Use at your own risk

This application is provided as-is, with no warranty of any kind. ESI data can be
delayed, incomplete, unavailable or simply wrong, and the calculations layered on
top of it can contain errors. Every figure the app shows is an estimate.
**Verify anything that matters in-game before you commit ISK to it.** Market
losses are yours.

## Contributing and support

A personal project shared in the hope it is useful, not a maintained product.
Bug reports and observations are welcome via GitHub issues; there is no promise of
a timely fix, and feature requests may simply not fit what the tool is for. If
you fork it and improve it, that is entirely within the license.

Contact: `spirit1flyer@yahoo.com`, or `spirit1flyer` on Discord.

## Background

[history.md](history.md) is the origin story — how this went from three files and
no coding ability to the current codebase over the course of 2026.

## License and attribution

Released under the MIT License — see [LICENSE](LICENSE). Use it, fork it, sell
it, embed it; just keep the copyright notice.

EVE Online® and Fenris Creations™ and all related logos and other elements are
trademarks of Fenris Creations (formerly CCP hf., rebranded 2026-05-06).
© 2026 Fenris Creations. All rights reserved. All artwork, screenshots,
characters, vehicles, storylines, world facts and other recognizable features of
the intellectual property relating to these trademarks are likewise the
intellectual property of Fenris Creations. This application is an independent
third-party tool. It is not developed, published, endorsed, sponsored or approved
by Fenris Creations, and Fenris Creations is in no way responsible for it or for
anything that happens as a result of using it.

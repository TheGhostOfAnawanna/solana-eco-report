# Solana Ecosystem Auto-Updating Report & Dashboard

Live snapshot of the Solana ecosystem: chain TVL rank, top protocols by per-chain TVL, SOL market data.

- **Data sources:** DeFiLlama public API + CoinGecko free tier (no keys, no paid services)
- **Auto-update:** GitHub Actions refreshes `data/` every 6 hours
- **Dashboard:** static `index.html` renders from `data/snapshot.json`
- **Report:** human-readable `data/report.md` regenerated each run

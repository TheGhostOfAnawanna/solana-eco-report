# Solana Ecosystem Auto-Updating Report & Dashboard

Live snapshot of the Solana ecosystem: chain TVL rank, top protocols by per-chain TVL, SOL market data.

- **Data sources:** DeFiLlama public API + CoinGecko free tier (no keys, no paid services)
- **Auto-update:** dashboard queries DeFiLlama/CoinGecko/Solana public RPC LIVE on every page load (client-side, no backend)
- **Dashboard:** `index.html` renders live in-browser (TVL tables, SOL market data, epoch progress, real TPS from recent performance samples)
- **Report:** human-readable `data/report.md` regenerated each run

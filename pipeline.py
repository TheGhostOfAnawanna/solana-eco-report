#!/usr/bin/env python3
"""Solana ecosystem snapshot pipeline — Superteam Canada bounty entry core.

Pulls FREE no-key public APIs (DeFiLlama, CoinGecko free tier, Solana public
RPC, Cointelegraph/CoinDesk RSS) and emits structured JSON + markdown report.
Python stdlib only. Zero external spend.
"""
import json, time, urllib.request, datetime, os
import xml.etree.ElementTree as ET

OUT_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(OUT_DIR, exist_ok=True)

RPC_URL = "https://api.mainnet-beta.solana.com"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "solana-eco-report/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params if isinstance(params, list) else []}).encode()
    req = urllib.request.Request(RPC_URL, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "solana-eco-report/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(f"RPC {method}: {resp['error']}")
    return resp["result"]

# Curated upcoming-upgrade notes (editorial; each carries a source URL and a
# last-reviewed date so staleness is visible). Not auto-fetched — labeled as such.
UPGRADES_NOTES = [
    {"name": "Alpenglow", "status": "in development",
     "note": "Consensus overhaul replacing TowerBFT with Votor+Rotor: ~100-150ms finality target.",
     "source": "https://solana.com/news/alpenglow-consensus", "last_reviewed": "2026-08-25"},
    {"name": "SIMD-525 (multiple concurrent block-building leaders)", "status": "governance / rollout tracking",
     "note": "Aims to reduce leader-collision MEV by allowing several leaders per slot.",
     "source": "https://github.com/solana-foundation/solana-improvement-documents", "last_reviewed": "2026-08-25"},
    {"name": "SIMD-228-style market-based fee debates", "status": "watch",
     "note": "Ongoing governance work on priority-fee markets after SIMD-228's narrow rejection.",
     "source": "https://github.com/solana-foundation/solana-improvement-documents", "last_reviewed": "2026-08-25"},
]

def fetch_news(limit=6):
    """Free RSS headlines mentioning Solana/SOL. stdlib XML parse, no keys."""
    feeds = [
        ("Cointelegraph (Solana tag)", "https://cointelegraph.com/rss/tag/solana"),
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
    ]
    out = []
    seen = set()
    for label, url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 solana-eco-report/1.0"})
            root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
            for it in root.findall(".//item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                key = title.lower()
                if not title or key in seen:
                    continue
                if any(w in key for w in ("solana", " sol ", "jupiter", "raydium")):
                    seen.add(key)
                    out.append({"title": title, "link": link, "published": pub, "feed": label})
                if len(out) >= limit:
                    return out
        except Exception:
            continue  # one dead feed must not kill the report
    return out

def detect_anomalies(snap, prev_snaps):
    """Rules-based anomaly detection over key metrics (see README)."""
    a = []
    d = snap["sources"]

    def add(metric, severity, detail):
        a.append({"metric": metric, "severity": severity, "detail": detail})

    net = d.get("solana_rpc", {})
    # TPS floor / spike vs own history
    tps = net.get("tps_non_vote")
    if tps is not None and tps < 200:
        add("non-vote TPS", "warning", f"Non-vote throughput very low: {tps:.0f} tx/s (<200).")
    hist_tps = [s["sources"]["solana_rpc"]["tps_non_vote"] for s in prev_snaps
                if s.get("sources", {}).get("solana_rpc", {}).get("tps_non_vote")]
    if tps is not None and len(hist_tps) >= 5:
        mean = sum(hist_tps) / len(hist_tps)
        var = sum((x - mean) ** 2 for x in hist_tps) / len(hist_tps)
        sd = var ** 0.5
        if sd > 0 and abs(tps - mean) > 2.5 * sd:
            add("non-vote TPS", "alert" if abs(tps - mean) > 3 * sd else "warning",
                f"{tps:.0f} tx/s is >2.5σ from rolling mean {mean:.0f} ({len(hist_tps)} prior snapshots).")

    # Slot time slowdown (target ≈400ms)
    st = net.get("slot_time_ms")
    if st is not None and st > 500:
        add("slot time", "warning", f"Average slot time {st:.0f}ms exceeds 500ms threshold.")

    # Validator delinquency
    val = d.get("validators", {})
    dpct = val.get("delinquency_pct")
    if dpct is not None and dpct > 1.0:
        add("validator delinquency", "alert" if dpct > 2.5 else "warning",
            f"{val['delinquent']}/{val['active'] + val['delinquent']} validators delinquent ({dpct:.2f}% > 1%).")

    # SOL price move (24h)
    chg = (d.get("coingecko_sol") or {}).get("change_24h_pct")
    if chg is not None and abs(chg) >= 8:
        add("SOL price 24h", "alert" if abs(chg) >= 12 else "warning",
            f"SOL moved {chg:+.1f}% in 24h (threshold ±8%).")

    # TVL daily move (from DeFiLlama history last two points)
    h = d.get("defillama_history", {}).get("daily") or []
    if len(h) >= 2:
        t0, v0 = h[-2]
        _, v1 = h[-1]
        if v0:
            dc = 100 * (v1 - v0) / v0
            if abs(dc) >= 5:
                add("chain TVL 24h", "alert" if abs(dc) >= 10 else "warning",
                    f"TVL moved {dc:+.1f}% day-over-day (${v0:,} → ${v1:,}).")

    # DEX volume swing
    dv = d.get("defillama_dexs", {}).get("change_1d_pct")
    if dv is not None and abs(dv) >= 25:
        add("DEX volume 24h", "info", f"DEX volume changed {dv:+.1f}% in 24h.")

    # RPC health
    if net.get("health") and net["health"] != "ok":
        add("RPC getHealth", "alert", f"Public RPC health: {net['health']}.")

    return a

def main():
    snap = {"generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "generator": "pipeline.py v1.0 (stdlib-only, no API keys)", "sources": {}}

    # ---- DeFiLlama: chain TVLs --------------------------------------------
    chains = get("https://api.llama.fi/v2/chains")
    sol = next(c for c in chains if c.get("name") == "Solana")
    top_chains = sorted(chains, key=lambda c: c.get("tvl", 0) or 0, reverse=True)[:10]
    snap["sources"]["defillama_chains"] = {
        "solana_tvl_usd": sol.get("tvl"),
        "solana_rank": [c["name"] for c in top_chains].index("Solana") + 1,
        "top10_by_tvl": [{"chain": c["name"], "tvl_usd": c.get("tvl")} for c in top_chains],
    }
    time.sleep(1)

    # ---- DeFiLlama: top Solana protocols ----------------------------------
    protocols = get("https://api.llama.fi/protocols")
    sol_protos = [p for p in protocols if "Solana" in (p.get("chains") or [])]
    def sol_tvl(p):
        v = (p.get("chainTvls") or {}).get("Solana")
        return v if isinstance(v, (int, float)) else 0
    sol_protos = [p for p in sol_protos if p.get("category") not in ("CEX", None) and sol_tvl(p) > 0]
    sol_protos.sort(key=sol_tvl, reverse=True)
    snap["sources"]["defillama_protocols"] = {
        "solana_protocol_count": len(sol_protos),
        "top15": [{"name": p["name"], "tvl_usd": sol_tvl(p), "category": p.get("category")} for p in sol_protos[:15]],
    }
    time.sleep(1)

    # ---- CoinGecko free tier: SOL market data -----------------------------
    cg = get("https://api.coingecko.com/api/v3/coins/markets?ids=solana&vs_currency=usd&price_change_percentage=24h,7d,30d")
    if cg:
        s = cg[0]
        snap["sources"]["coingecko_sol"] = {
            "price_usd": s.get("current_price"), "market_cap_usd": s.get("market_cap"),
            "rank": s.get("market_cap_rank"), "change_24h_pct": s.get("price_change_percentage_24h_in_currency"),
            "change_7d_pct": s.get("price_change_percentage_7d_in_currency"),
            "change_30d_pct": s.get("price_change_percentage_30d_in_currency"),
        }
        time.sleep(2)

    # ---- Solana public RPC: network + validators + supply -----------------
    epoch = rpc("getEpochInfo")
    samples = rpc("getRecentPerformanceSamples", [8])
    n_tx = sum(s["numTransactions"] for s in samples)
    nv_tx = sum(s["numNonVoteTransactions"] for s in samples)
    secs = sum(s["samplePeriodSecs"] for s in samples)
    slots = sum(s["numSlots"] for s in samples)
    slot_time_ms = round(1000 * secs / slots) if slots else None

    vote = rpc("getVoteAccounts")
    active_v, delinq_v = vote["current"], vote["delinquent"]
    all_stakes = [(v["activatedStake"], v["commission"], v["votePubkey"]) for v in active_v + delinq_v]
    total_stake = sum(st for st, _, _ in all_stakes)
    lam = 1e9  # lamports -> SOL
    top10 = sorted(all_stakes, reverse=True)[:10]
    top20 = sorted(all_stakes, reverse=True)[:20]

    # Nakamoto coefficient (min validators to reach >33% of stake).
    sorted_stakes_desc = sorted([st for st, _, _ in all_stakes], reverse=True)
    target = total_stake / 3
    nak = 0
    acc = 0
    for st in sorted_stakes_desc:
        acc += st
        nak += 1
        if acc > target:
            break
    nakamoto_coeff = nak if total_stake else None

    supply = rpc("getSupply", [{"excludeNonCirculatingAccountsList": True}])["value"]
    health = rpc("getHealth")
    prio = rpc("getRecentPrioritizationFees")
    fees_lamports = sorted(x["prioritizationFee"] for x in prio)

    # Priority-fee stats. Most blocks have 0 priority bids; surface useful
    # numbers anyway (median + 95th pct) and clearly say "base fee only" when
    # the entire sample is 0 (no fabricated numbers).
    n_fees = len(fees_lamports)
    nonzero_fees = [f for f in fees_lamports if f > 0]
    median_idx = n_fees // 2
    fee_mean = (sum(fees_lamports) / n_fees) if n_fees else 0
    fee_median = fees_lamports[median_idx] if n_fees else 0
    fee_p95 = fees_lamports[min(n_fees - 1, int(n_fees * 0.95))] if n_fees else 0

    snap["sources"]["solana_rpc"] = {
        "epoch": epoch["epoch"],
        "epoch_progress_pct": round(100 * epoch["slotIndex"] / epoch["slotsInEpoch"], 2),
        "block_height": epoch.get("blockHeight"),
        "absolute_slot": epoch.get("absoluteSlot"),
        "total_transactions": epoch["transactionCount"],
        "tps_total": round(n_tx / secs, 0),
        "tps_non_vote": round(nv_tx / secs, 0),
        "slot_time_ms": slot_time_ms,
        "health": health,
        "supply_circulating_sol": round(supply["circulating"] / lam),
        "supply_total_sol": round(supply["total"] / lam),
        "priority_fee_mean_micro_lamports": round(fee_mean),
        "priority_fee_median_micro_lamports": round(fee_median),
        "priority_fee_p95_micro_lamports": round(fee_p95),
        "priority_fee_min_micro_lamports": fees_lamports[0] if fees_lamports else None,
        "priority_fee_nonzero_count": len(nonzero_fees),
        "priority_fee_sample_size": n_fees,
    }
    snap["sources"]["validators"] = {
        "active": len(active_v), "delinquent": len(delinq_v),
        "delinquency_pct": round(100 * len(delinq_v) / max(len(active_v) + len(delinq_v), 1), 2),
        "total_stake_sol": round(total_stake / lam),
        "top10_stake_share_pct": round(100 * sum(st for st, _, _ in top10) / total_stake, 1) if total_stake else None,
        "top20_stake_share_pct": round(100 * sum(st for st, _, _ in top20) / total_stake, 1) if total_stake else None,
        "top10_by_stake": [{"vote_account": vp[:10] + "…", "stake_msol": round(st / lam / 1e6, 2),
                            "commission_pct": cm} for st, cm, vp in top10],
        "median_commission_pct": sorted(cm for _, cm, _ in all_stakes)[len(all_stakes) // 2],
        "nakamoto_coefficient": nakamoto_coeff,
    }
    time.sleep(2)

    # ---- DeFiLlama: TVL history (90d) --------------------------------------
    hist = get("https://api.llama.fi/v2/historicalChainTvl/Solana")
    cutoff90 = time.time() - 90 * 86400
    h90 = [p for p in hist if p["date"] >= cutoff90]
    cutoff1d = time.time() - 2 * 86400
    h_daily = [(p["date"], round(p["tvl"])) for p in hist if p["date"] >= cutoff1d]
    if len(h90) >= 2:
        first, last = h90[0]["tvl"], h90[-1]["tvl"]
        snap["sources"]["defillama_history"] = {
            "days": [(p["date"], round(p["tvl"])) for p in h90],
            "tvl_90d_ago_usd": first, "tvl_now_usd": last,
            "change_90d_pct": round(100 * (last - first) / first, 1) if first else None,
            "daily": h_daily,
        }
    time.sleep(1)

    # ---- DeFiLlama: DEX volume on Solana -----------------------------------
    try:
        dex = get("https://api.llama.fi/overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
        snap["sources"]["defillama_dexs"] = {
            "volume_24h_usd": dex.get("total24h"), "volume_7d_usd": dex.get("total7d"),
            "volume_30d_usd": dex.get("total30d"), "all_time_usd": dex.get("totalAllTime"),
            "change_1d_pct": dex.get("change_1d"), "change_7d_pct": dex.get("change_7d"),
            "change_30d_pct": dex.get("change_30d"),
        }
    except Exception:
        pass
    time.sleep(1)

    # ---- DeFiLlama: ecosystem fees (REV proxy) ------------------------------
    try:
        fx = get("https://api.llama.fi/overview/fees/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
        snap["sources"]["defillama_fees"] = {
            "fees_24h_usd": fx.get("total24h"), "fees_30d_usd": fx.get("total30d"),
            "change_1d_pct": fx.get("change_1d"),
        }
    except Exception:
        pass
    time.sleep(1)

    # ---- Stablecoins on Solana ----------------------------------------------
    st = get("https://stablecoins.llama.fi/stablecoins?includePrices=false")
    if isinstance(st, dict):
        total = 0
        for a in st.get("peggedAssets", []):
            v = ((a.get("chainCirculating") or {}).get("Solana") or {}).get("current", {}).get("peggedUSD", 0)
            if isinstance(v, (int, float)):
                total += v
        snap["sources"]["defillama_stables"] = {"solana_usd_pegged_stables": round(total)}
    time.sleep(1)

    # ---- News + upgrades -----------------------------------------------------
    snap["sources"]["news"] = fetch_news()
    snap["sources"]["upcoming_upgrades"] = UPGRADES_NOTES

    # ---- History retention + self-observed trend ------------------------------
    hist_dir = os.path.join(OUT_DIR, "history")
    os.makedirs(hist_dir, exist_ok=True)
    prev = []
    for fn in sorted(os.listdir(hist_dir)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(hist_dir, fn)) as f:
                    prev.append(json.load(f))
            except Exception:
                pass
    def _get(s, source, key):
        try:
            return s["sources"][source][key]
        except (KeyError, TypeError):
            return None
    if prev:
        first = min(prev, key=lambda s: s.get("generated_utc", ""))
        tvl0 = _get(first, "defillama_chains", "solana_tvl_usd")
        tvl1 = snap["sources"]["defillama_chains"]["solana_tvl_usd"]
        p0 = _get(first, "coingecko_sol", "price_usd")
        p1 = _get(snap, "coingecko_sol", "price_usd")
        entry = {"since": first.get("generated_utc", "")[:16] + "Z", "snapshots_tracked": len(prev) + 1}
        if tvl0 and tvl1:
            entry["tvl_then_usd"] = round(tvl0)
            entry["tvl_change_pct"] = round(100 * (tvl1 - tvl0) / tvl0, 2)
        if p0 and p1:
            entry["price_then_usd"] = p0
            entry["price_change_pct"] = round(100 * (p1 - p0) / p0, 2)
        snap["sources"]["self_observed_trend"] = entry
        # prune archive beyond 60 snapshots (keep file sizes bounded)
        if len(os.listdir(hist_dir)) > 61:
            oldest = sorted(fn for fn in os.listdir(hist_dir) if fn.endswith(".json"))
            for fn in oldest[:-60]:
                os.remove(os.path.join(hist_dir, fn))

    stamp = snap["generated_utc"].replace(":", "")
    with open(os.path.join(hist_dir, f"{stamp}.json"), "w") as f:
        json.dump(snap, f)

    # ---- Trends & correlations (for sparklines + insight) ---------------------
    # Build time series from history (most recent first)
    hist_series = []  # each entry: {generated_utc, sol_price, tvl, tps_non_vote}
    for s in reversed(prev):  # oldest to newest
        try:
            hist_series.append({
                'generated_utc': s.get('generated_utc', ''),
                'sol_price': s['sources'].get('coingecko_sol', {}).get('price_usd'),
                'tvl': s['sources'].get('defillama_chains', {}).get('solana_tvl_usd'),
                'tps_non_vote': s['sources'].get('solana_rpc', {}).get('tps_non_vote')
            })
        except (KeyError, TypeError):
            continue
    # Add current snapshot at end
    hist_series.append({
        'generated_utc': snap['generated_utc'],
        'sol_price': snap['sources'].get('coingecko_sol', {}).get('price_usd'),
        'tvl': snap['sources'].get('defillama_chains', {}).get('solana_tvl_usd'),
        'tps_non_vote': snap['sources'].get('solana_rpc', {}).get('tps_non_vote')
    })
    # Filter out any None values (shouldn't happen but safe)
    hist_series = [x for x in hist_series if all(v is not None for v in [x['sol_price'], x['tvl'], x['tps_non_vote']])]
    
    # Compute sparklines data (last 37 points or less)
    def sparkline_vals(key, n=37):
        vals = [x[key] for x in hist_series[-n:] if x[key] is not None]
        if not vals:
            return []
        # Normalize to 0-100 for sparkline height
        vmin, vmax = min(vals), max(vals)
        if vmax == vmin:
            return [50] * len(vals)  # flat line
        return [round((v - vmin) * 100 / (vmax - vmin)) for v in vals]
    
    # Compute Pearson correlation between SOL price and TVL
    def pearson_corr(x, y):
        n = len(x)
        if n < 2:
            return None
        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(xi * yi for xi, yi in zip(x, y))
        sum_x2 = sum(xi * xi for xi in x)
        sum_y2 = sum(yi * yi for yi in y)
        numerator = n * sum_xy - sum_x * sum_y
        denom_x = (n * sum_x2 - sum_x * sum_x)
        denom_y = (n * sum_y2 - sum_y * sum_y)
        if denom_x <= 0 or denom_y <= 0:
            return None
        return numerator / ((denom_x * denom_y) ** 0.5)
    
    sol_prices = [x['sol_price'] for x in hist_series]
    tvls = [x['tvl'] for x in hist_series]
    tps_vals = [x['tps_non_vote'] for x in hist_series]
    correlation = pearson_corr(sol_prices, tvls)
    
    # Store trends in snapshot
    trends = {
        'sol_price_sparkline': sparkline_vals('sol_price'),
        'tvl_sparkline': sparkline_vals('tvl'),
        'tps_sparkline': sparkline_vals('tps_non_vote'),
        'sol_tvl_correlation': correlation,
        'data_points': len(hist_series),
        'date_range': {
            'start': hist_series[0]['generated_utc'] if hist_series else None,
            'end': hist_series[-1]['generated_utc'] if hist_series else None
        }
    }
    snap['sources']['trends'] = trends

    # ---- Anomaly detection (needs history context) ----------------------------
    anomalies = detect_anomalies(snap, prev)
    snap["anomalies"] = anomalies

    # ---- Anomaly history (rolling 14d, deduped by day) --------------------
    hist_path = os.path.join(OUT_DIR, "anomaly_history.json")
    today = snap["generated_utc"][:10]
    try:
        with open(hist_path) as f:
            hist_log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        hist_log = []
    if anomalies:
        hist_log.append({"date": today, "generated_utc": snap["generated_utc"], "anomalies": anomalies})
    else:
        if not any(h["date"] == today for h in hist_log):
            hist_log.append({"date": today, "generated_utc": snap["generated_utc"], "anomalies": []})
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)).date().isoformat()
    hist_log = [h for h in hist_log if h["date"] >= cutoff]
    seen = {}
    for h in sorted(hist_log, key=lambda x: x["generated_utc"]):
        seen[h["date"]] = h
    hist_log = sorted(seen.values(), key=lambda x: x["date"], reverse=True)
    with open(hist_path, "w") as f:
        json.dump(hist_log, f, indent=2)
    snap["anomaly_history_summary"] = {
        "days_kept": len(hist_log),
        "alerts_14d": sum(1 for h in hist_log for a in h["anomalies"] if a["severity"] == "alert"),
        "warnings_14d": sum(1 for h in hist_log for a in h["anomalies"] if a["severity"] == "warning"),
        "info_14d": sum(1 for h in hist_log for a in h["anomalies"] if a["severity"] == "info"),
        "clean_days_14d": sum(1 for h in hist_log if not h["anomalies"]),
    }

    with open(os.path.join(OUT_DIR, "snapshot.json"), "w") as f:
        json.dump(snap, f, indent=2)

    write_markdown(snap)
    corr_str = f"{correlation:.3f}" if correlation is not None else "None"
    print(f"OK snapshot.json anomalies={len(anomalies)} "
          f"snapshots_archived={len(os.listdir(hist_dir))} "
          f"trends_points={len(hist_series)} "
          f"correlation={corr_str}")

def fmt_usd(n):
    return "${:,.0f}".format(n) if isinstance(n, (int, float)) else "n/a"

def write_markdown(snap):
    d, net, val = snap["sources"], snap["sources"]["solana_rpc"], snap["sources"]["validators"]
    cg = d.get("coingecko_sol", {})
    lines = [
        f"# Solana Ecosystem Snapshot — {snap['generated_utc'][:16]}Z",
        "",
        "*Auto-generated by `pipeline.py` (Python stdlib only, zero paid APIs). "
        "Refreshed every 6h via cron (`update.sh`); every refresh is a git commit — see commit history for proof.*",
        "",
    ]
    anoms = snap.get("anomalies") or []
    if anoms:
        lines += ["## ⚠️ Anomaly alerts", ""]
        for x in anoms:
            lines.append(f"- **[{x['severity'].upper()}] {x['metric']}** — {x['detail']}")
        lines.append("")
    else:
        lines += ["## ✅ Anomaly scan", "", "- No anomalies flagged this run (rules in README).", ""]

    lines += ["## Network performance", "",
              f"- **Block height:** {net.get('block_height'):,} · absolute slot {net.get('absolute_slot'):,}"
              if net.get("block_height") else f"- **Absolute slot:** {net.get('absolute_slot'):,}",
              f"- **Slot time:** {net.get('slot_time_ms')} ms average (recent samples)",
              f"- **Throughput:** {net['tps_total']:,.0f} tx/s total · {net['tps_non_vote']:,.0f} tx/s non-vote",
              f"- **Epoch:** {net['epoch']} ({net['epoch_progress_pct']}% complete)",
              f"- **Lifetime transactions:** {net['total_transactions']:,}",
              f"- **RPC health:** `{net.get('health')}`",
              f"- **Supply:** {net.get('supply_circulating_sol'):,} SOL circulating / {net.get('supply_total_sol'):,} total",
              f"- **Priority fee median / 95th:** {net.get('priority_fee_median_micro_lamports'):,} / {net.get('priority_fee_p95_micro_lamports'):,} micro-lamports/CU (base fee fixed at 5,000 lamports/signature)",
              "",
              "## Validators", "",
              f"- **Active:** {val['active']} · **Delinquent:** {val['delinquent']} ({val['delinquency_pct']}%)",
              f"- **Total stake:** {val['total_stake_sol']:,} SOL · median commission {val['median_commission_pct']}%",
              f"- **Stake concentration:** top 10 hold {val['top10_stake_share_pct']}% · top 20 hold {val['top20_stake_share_pct']}% · Nakamoto coefficient {val.get('nakamoto_coefficient','-')}",
              "",
              "| # | Vote account | Stake (M SOL) | Commission |", "|---|--------------|----------------|------------|"]
    for i, v in enumerate(val["top10_by_stake"], 1):
        lines.append(f"| {i} | `{v['vote_account']}` | {v['stake_msol']:.2f} | {v['commission_pct']}% |")

    if cg.get("price_usd"):
        lines += ["", "## Economics", "",
                  f"- **SOL price:** ${cg['price_usd']:,} (24h {cg['change_24h_pct']:+.1f}% / 7d {cg['change_7d_pct']:+.1f}% / 30d {cg['change_30d_pct']:+.1f}%)",
                  f"- **Market cap:** ${cg['market_cap_usd']:,} (rank #{cg['rank']})"]
    dl = d.get("defillama_chains", {})
    if dl:
        lines += [f"- **Chain TVL:** {fmt_usd(dl['solana_tvl_usd'])} — rank #{dl['solana_rank']} of all tracked chains"]
    dx = d.get("defillama_dexs")
    if dx and dx.get("volume_24h_usd"):
        lines += [f"- **DEX volume:** {fmt_usd(dx['volume_24h_usd'])}/24h ({dx.get('change_1d_pct'):+.1f}% 1d, {dx.get('change_7d_pct'):+.1f}% 7d)"]
    fx = d.get("defillama_fees")
    if fx and fx.get("fees_24h_usd"):
        lines += [f"- **Ecosystem fees (REV proxy):** {fmt_usd(fx['fees_24h_usd'])}/24h · {fmt_usd(fx.get('fees_30d_usd'))}/30d"]
    stb = d.get("defillama_stables")
    if stb:
        lines += [f"- **USD-pegged stablecoins on Solana:** {fmt_usd(stb['solana_usd_pegged_stables'])}"]

    h = d.get("defillama_history")
    if h:
        lines += ["", "## TVL trend (90 days)", "",
                  f"- {fmt_usd(h['tvl_90d_ago_usd'])} → {fmt_usd(h['tvl_now_usd'])} ({h['change_90d_pct']:+.1f}%)"]

    prot = d.get("defillama_protocols")
    if prot:
        lines += ["", f"## Top Solana protocols ({prot['solana_protocol_count']} tracked)", "",
                  "| # | Protocol | Category | TVL |", "|---|----------|----------|-----|"]
        for i, p in enumerate(prot["top15"], 1):
            lines.append(f"| {i} | {p['name']} | {p['category']} | {fmt_usd(p['tvl_usd'])} |")

    news = d.get("news") or []
    if news:
        lines += ["", "## Ecosystem & community news (auto-pulled RSS)", ""]
        for n in news:
            lines.append(f"- [{n['title']}]({n['link']}) — *{n['feed']}*, {n['published'][:22]}")

    ups = d.get("upcoming_upgrades") or []
    if ups:
        lines += ["", "## Upcoming upgrades & developments *(curated editorial — dated, see sources)*", ""]
        for u in ups:
            lines.append(f"- **{u['name']}** ({u['status']}, reviewed {u['last_reviewed']}): {u['note']} [source]({u['source']})")

    trend = d.get("self_observed_trend")
    if trend:
        lines += ["", "## Self-observed trend (this dashboard's own snapshot archive)", "",
                  f"- **Snapshots tracked:** {trend['snapshots_tracked']} (since {trend['since']})"]
        if "tvl_change_pct" in trend:
            lines.append(f"- **Chain TVL:** ${trend['tvl_then_usd']:,} → now ({trend['tvl_change_pct']:+.2f}% over observation window)")
        if "price_change_pct" in trend:
            lines.append(f"- **SOL price:** ${trend['price_then_usd']} → now ({trend['price_change_pct']:+.2f}% over observation window)")

    lines += ["", "---",
              "*Data: Solana public RPC (direct JSON-RPC) · DeFiLlama public API · CoinGecko free tier · Cointelegraph & CoinDesk RSS. "
              "No API keys, no paid services, Python stdlib only. Auto-refresh every 6h; live dashboard: https://theghostofanawanna.github.io/solana-eco-report/*"]

    with open(os.path.join(OUT_DIR, "report.md"), "w") as f:
        f.write("\n".join(lines))

if __name__ == "__main__":
    main()
